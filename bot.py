"""
Telegram bot → Cursor Cloud Agents API.
Deploy on Railway/Render/Fly.io — always online, no local PC required.
"""

from __future__ import annotations

import os
import re
import threading
import time
from datetime import date

import telebot
from telebot import types

from cursor_client import (
    CursorAPIError,
    CursorClient,
    agent_web_url,
    format_git_links,
    terminal_statuses,
)
from storage import Storage

# ─── Config ───────────────────────────────────────────────────────────────────

BOT_TOKEN = os.environ["BOT_TOKEN"]
CURSOR_API_KEY = os.environ["CURSOR_API_KEY"]
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
DEFAULT_REPO_URL = os.environ.get("DEFAULT_REPO_URL", "").strip()
DEFAULT_BRANCH = os.environ.get("DEFAULT_BRANCH", "main").strip() or "main"
MAX_DAILY_RUNS = int(os.environ.get("MAX_DAILY_RUNS", "20"))
AUTO_CREATE_PR = os.environ.get("AUTO_CREATE_PR", "true").lower() in ("1", "true", "yes")
ALLOWED_USER_IDS = {
    int(x.strip())
    for x in os.environ.get("ALLOWED_USER_IDS", "").split(",")
    if x.strip().isdigit()
}
bot = telebot.TeleBot(BOT_TOKEN)
bot.remove_webhook()

cursor = CursorClient(CURSOR_API_KEY)
store = Storage()

# chat_id -> {run_id, status_msg_id, busy}
_active_runs: dict[int, dict] = {}
_run_lock = threading.Lock()

GITHUB_REPO_RE = re.compile(
    r"^https?://github\.com/[\w.-]+/[\w.-]+/?$", re.IGNORECASE
)


def is_allowed(user_id: int) -> bool:
    if not ALLOWED_USER_IDS:
        return True
    return user_id in ALLOWED_USER_IDS


def check_rate_limit(user_id: int) -> str | None:
    today = date.today().isoformat()
    count = store.get_daily_runs(user_id, today)
    if count >= MAX_DAILY_RUNS:
        return f"⛔ Дневной лимит ({MAX_DAILY_RUNS} задач). Попробуйте завтра."
    return None


def resolve_repo(chat_id: int) -> tuple[str | None, str]:
    url, branch = store.get_repo_prefs(chat_id)
    if url:
        return url, branch
    if DEFAULT_REPO_URL:
        return DEFAULT_REPO_URL, DEFAULT_BRANCH
    return None, DEFAULT_BRANCH


def send_chunked(chat_id: int, text: str, max_len: int = 4000) -> None:
    if not text:
        return
    for i in range(0, len(text), max_len):
        bot.send_message(chat_id, text[i : i + max_len])


def progress_keyboard(agent_id: str | None, agent_url: str | None = None) -> types.InlineKeyboardMarkup | None:
    if not agent_id:
        return None
    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton(
            "📋 Статус задачи",
            url=agent_web_url(agent_id, agent_url),
        )
    )
    return markup


def set_busy(chat_id: int, busy: bool) -> None:
    with _run_lock:
        if chat_id in _active_runs:
            _active_runs[chat_id]["busy"] = busy


def is_busy(chat_id: int) -> bool:
    with _run_lock:
        return bool(_active_runs.get(chat_id, {}).get("busy"))


# ─── Run worker (stream + poll fallback) ───────────────────────────────────────


def watch_run(
    chat_id: int,
    agent_id: str,
    run_id: str,
    agent_url: str | None,
    status_msg_id: int,
) -> None:
    assistant_buf: list[str] = []
    last_edit = 0.0
    terminal = False

    def update_status(prefix: str, body: str = "") -> None:
        nonlocal last_edit
        now = time.time()
        if now - last_edit < 2.0 and not terminal:
            return
        last_edit = now
        preview = (prefix + "\n\n" + body).strip()
        if len(preview) > 3900:
            preview = preview[:3900] + "…"
        try:
            bot.edit_message_text(
                preview,
                chat_id,
                status_msg_id,
                reply_markup=progress_keyboard(agent_id, agent_url),
            )
        except Exception:
            pass

    def on_event(event: str, data: dict) -> None:
        nonlocal terminal
        if event == "assistant":
            assistant_buf.append(data.get("text", ""))
            text = "".join(assistant_buf)
            update_status("⏳ Агент работает…", text[-1500:] if text else "")
        elif event == "tool_call" and data.get("status") == "running":
            name = data.get("name", "tool")
            update_status(f"🔧 {name}…", "".join(assistant_buf)[-1200:])
        elif event == "result":
            terminal = True
            status = data.get("status", "")
            result_text = data.get("text") or "".join(assistant_buf)
            git_block = format_git_links(data.get("git"))
            lines = [f"✅ Задача завершена ({status})"]
            if result_text:
                lines.append("\n" + result_text[:3500])
            if git_block:
                lines.append("\n" + git_block)
            if agent_url:
                lines.append(f"\n🔗 {agent_url}")
            final = "\n".join(lines)
            update_status(final[:3900])
            if len(final) > 3900:
                send_chunked(chat_id, final)
        elif event == "error":
            terminal = True
            update_status(f"❌ Ошибка стрима: {data.get('message', data)}")

    try:
        cursor.stream_run(agent_id, run_id, on_event)
    except CursorAPIError as e:
        if e.status != 410:
            update_status(f"⚠️ Стрим недоступен ({e.status}), опрашиваю статус…")

    # Poll until terminal
    for _ in range(180):
        if terminal:
            break
        try:
            run = cursor.get_run(agent_id, run_id)
        except CursorAPIError:
            time.sleep(5)
            continue

        status = run.get("status", "")
        if status in terminal_statuses():
            result_text = run.get("result") or "".join(assistant_buf)
            git_block = format_git_links(run.get("git"))
            emoji = "✅" if status == "FINISHED" else "⚠️"
            msg = f"{emoji} {status}"
            if result_text:
                msg += f"\n\n{result_text[:3500]}"
            if git_block:
                msg += f"\n\n{git_block}"
            if agent_url:
                msg += f"\n\n🔗 {agent_url}"
            update_status(msg[:3900])
            if len(msg) > 3900:
                send_chunked(chat_id, msg)
            terminal = True
            break

        update_status(f"⏳ Статус: {status}", "".join(assistant_buf)[-1200:])
        time.sleep(5)

    with _run_lock:
        _active_runs.pop(chat_id, None)
    set_busy(chat_id, False)


def start_task(chat_id: int, user_id: int, prompt: str, force_new: bool = False) -> None:
    if not is_allowed(user_id):
        bot.send_message(chat_id, "⛔ Бот недоступен для вашего аккаунта.")
        return

    limit_msg = check_rate_limit(user_id)
    if limit_msg:
        bot.send_message(chat_id, limit_msg)
        return

    if is_busy(chat_id):
        bot.send_message(
            chat_id,
            "⏳ Уже выполняется задача. Дождитесь завершения или /cancel",
        )
        return

    repo_url, branch = resolve_repo(chat_id)
    session = store.get_session(chat_id)

    try:
        if force_new or not session or not session.get("agent_id"):
            if not repo_url:
                bot.send_message(
                    chat_id,
                    "📦 Укажите репозиторий:\n"
                    "/repo https://github.com/user/project\n"
                    "или задайте DEFAULT_REPO_URL на сервере.",
                )
                return

            agent_name = "Telegram: " + prompt[:70]
            resp = cursor.create_agent(
                prompt=prompt,
                repo_url=repo_url,
                starting_ref=branch,
                auto_create_pr=AUTO_CREATE_PR,
                name=agent_name,
            )
            agent = resp["agent"]
            run = resp["run"]
            agent_id = agent["id"]
            agent_url = agent.get("url")
            run_id = run["id"]
            store.set_session(chat_id, agent_id, agent_url, repo_url, branch)
            title = "🚀 Новый Cloud Agent"
        else:
            agent_id = session["agent_id"]
            agent_url = session.get("agent_url")
            resp = cursor.create_run(agent_id, prompt)
            run = resp["run"]
            run_id = run["id"]
            title = "💬 Продолжение диалога с агентом"

        store.increment_daily_runs(user_id, date.today().isoformat())

        status_msg = bot.send_message(
            chat_id,
            f"{title}\n⏳ Запуск…\n\n{prompt[:500]}",
            reply_markup=progress_keyboard(agent_id, agent_url),
        )

        with _run_lock:
            _active_runs[chat_id] = {
                "run_id": run_id,
                "agent_id": agent_id,
                "status_msg_id": status_msg.message_id,
                "busy": True,
            }

        thread = threading.Thread(
            target=watch_run,
            args=(chat_id, agent_id, run_id, agent_url, status_msg.message_id),
            daemon=True,
        )
        thread.start()

    except CursorAPIError as e:
        if e.status == 409:
            bot.send_message(
                chat_id,
                "⏳ Агент занят другой задачей. Подождите или /cancel, затем повторите.",
            )
        else:
            bot.send_message(chat_id, f"❌ Cursor API ({e.status}):\n{e.message[:800]}")


# ─── Commands ───────────────────────────────────────────────────────────────────


@bot.message_handler(commands=["start", "help"])
def cmd_start(message: types.Message) -> None:
    bot.reply_to(
        message,
        "👋 **Cursor Bot**\n\n"
        "Пишите задачу текстом — Cloud Agent выполнит её в репозитории "
        "(как на cursor.com/agents).\n\n"
        "**Команды:**\n"
        "/new — новая тема\n"
        "/repo /branch — репозиторий GitHub\n"
        "/link — ссылка на текущую задачу\n"
        "/status /cancel /reset /settings\n\n"
        "Обычное сообщение = продолжение диалога.\n"
        f"Лимит: {MAX_DAILY_RUNS} задач/день.",
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )


@bot.message_handler(commands=["new"])
def cmd_new(message: types.Message) -> None:
    prompt = message.text.replace("/new", "", 1).strip()
    if not prompt:
        bot.reply_to(message, "Укажите задачу: /new Добавь README с установкой")
        return
    start_task(message.chat.id, message.from_user.id, prompt, force_new=True)


@bot.message_handler(commands=["repo"])
def cmd_repo(message: types.Message) -> None:
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        url, branch = resolve_repo(message.chat.id)
        bot.reply_to(
            message,
            f"Текущий репозиторий:\n{url or '— не задан —'}\nВетка: `{branch}`",
            parse_mode="Markdown",
        )
        return
    url = parts[1].strip().rstrip("/")
    if not GITHUB_REPO_RE.match(url):
        bot.reply_to(
            message,
            "❌ Нужен URL вида https://github.com/owner/repo",
        )
        return
    _, branch = resolve_repo(message.chat.id)
    store.set_repo_prefs(message.chat.id, url, branch)
    bot.reply_to(message, f"✅ Репозиторий:\n{url}\nВетка: `{branch}`", parse_mode="Markdown")


@bot.message_handler(commands=["branch"])
def cmd_branch(message: types.Message) -> None:
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(message, "Пример: /branch main")
        return
    branch = parts[1].strip()
    url, _ = resolve_repo(message.chat.id)
    if not url:
        bot.reply_to(message, "Сначала /repo https://github.com/...")
        return
    store.set_repo_prefs(message.chat.id, url, branch)
    bot.reply_to(message, f"✅ Ветка: `{branch}`", parse_mode="Markdown")


@bot.message_handler(commands=["settings"])
def cmd_settings(message: types.Message) -> None:
    url, branch = resolve_repo(message.chat.id)
    session = store.get_session(message.chat.id)
    lines = [
        f"📦 Repo: {url or 'не задан'}",
        f"🌿 Branch: {branch}",
        f"🔀 Auto-PR: {AUTO_CREATE_PR}",
    ]
    if session and session.get("agent_id"):
        lines.append(f"🤖 Agent: `{session['agent_id']}`")
        if session.get("agent_url"):
            lines.append(f"🔗 {session['agent_url']}")
    bot.reply_to(message, "\n".join(lines), parse_mode="Markdown")


@bot.message_handler(commands=["link"])
def cmd_link(message: types.Message) -> None:
    session = store.get_session(message.chat.id)
    if session and session.get("agent_id"):
        url = agent_web_url(session["agent_id"], session.get("agent_url"))
        bot.reply_to(message, url)
    else:
        bot.reply_to(message, "Нет активной задачи. Отправьте текст.")


@bot.message_handler(commands=["admin"])
def cmd_admin(message: types.Message) -> None:
    if ADMIN_ID and message.from_user.id != ADMIN_ID:
        bot.reply_to(message, "⛔ Нет доступа.")
        return
    try:
        me = cursor.me()
        bot.reply_to(
            message,
            f"✅ Бот работает\n"
            f"Cursor: {me.get('userEmail') or me.get('apiKeyName', 'OK')}\n"
            f"Активных run: {len(_active_runs)}",
        )
    except CursorAPIError as e:
        bot.reply_to(message, f"❌ Cursor API: {e.status} {e.message[:200]}")


@bot.message_handler(commands=["status"])
def cmd_status(message: types.Message) -> None:
    with _run_lock:
        active = _active_runs.get(message.chat.id)
    session = store.get_session(message.chat.id)
    if active:
        bot.reply_to(
            message,
            f"⏳ Выполняется run `{active['run_id']}`\n"
            f"Agent: `{active['agent_id']}`",
            parse_mode="Markdown",
        )
        return
    if session and session.get("agent_id"):
        try:
            agent = cursor.get_agent(session["agent_id"])
            bot.reply_to(
                message,
                f"Агент: {agent.get('status')}\n"
                f"🔗 {agent.get('url', session.get('agent_url', '—'))}",
            )
        except CursorAPIError as e:
            bot.reply_to(message, f"❌ {e.message[:300]}")
    else:
        bot.reply_to(message, "Нет активных задач.")


@bot.message_handler(commands=["cancel"])
def cmd_cancel(message: types.Message) -> None:
    with _run_lock:
        active = _active_runs.get(message.chat.id)
    session = store.get_session(message.chat.id)
    if not active and not session:
        bot.reply_to(message, "Нечего отменять.")
        return
    agent_id = (active or {}).get("agent_id") or session.get("agent_id")
    run_id = (active or {}).get("run_id")
    if not run_id and session:
        try:
            agent = cursor.get_agent(agent_id)
            run_id = agent.get("latestRunId")
        except CursorAPIError:
            pass
    if not run_id:
        bot.reply_to(message, "Не найден активный run.")
        return
    try:
        cursor.cancel_run(agent_id, run_id)
        bot.reply_to(message, "🛑 Отменено.")
    except CursorAPIError as e:
        bot.reply_to(message, f"❌ {e.status}: {e.message[:300]}")


@bot.message_handler(commands=["reset"])
def cmd_reset(message: types.Message) -> None:
    store.clear_session(message.chat.id)
    with _run_lock:
        _active_runs.pop(message.chat.id, None)
    bot.reply_to(message, "🔄 Сессия сброшена. Следующее сообщение создаст нового агента.")


@bot.message_handler(func=lambda m: m.text and not m.text.startswith("/"))
def handle_text(message: types.Message) -> None:
    if not message.text:
        return
    start_task(message.chat.id, message.from_user.id, message.text.strip())


if __name__ == "__main__":
    print("Cursor Telegram Bot starting…")
    print(f"DEFAULT_REPO: {bool(DEFAULT_REPO_URL)}")
    bot.infinity_polling(timeout=60, long_polling_timeout=60)
