"""
Telegram bot → Cursor Cloud Agents API.
Deploy on Railway/Render/Fly.io — always online, no local PC required.
"""

from __future__ import annotations

import base64
import logging
import os
import re
import threading
import time
from datetime import date
from io import BytesIO

import telebot
from telebot import types

from cursor_client import (
    CursorAPIError,
    CursorClient,
    agent_web_url,
    format_pr_links,
    terminal_statuses,
)
from pdf_attachments import (
    PdfAttachmentError,
    ParsedPdf,
    looks_like_pdf,
    merge_into_prompt,
    parse_pdf,
)
from pdf_urls import (
    TOO_BIG_FOR_TELEGRAM,
    PdfUrlError,
    download_pdf_from_url,
    extract_pdf_urls,
    strip_urls,
)
from storage import Storage
from telegram_files import (
    TELEGRAM_BOT_FILE_LIMIT,
    TelegramDownloadError,
    download_telegram_file,
)

# ─── Config ───────────────────────────────────────────────────────────────────

BOT_TOKEN = os.environ["BOT_TOKEN"]
CURSOR_API_KEY = os.environ["CURSOR_API_KEY"]
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
DEFAULT_REPO_URL = os.environ.get("DEFAULT_REPO_URL", "").strip()
DEFAULT_BRANCH = os.environ.get("DEFAULT_BRANCH", "main").strip() or "main"
MAX_DAILY_RUNS = int(os.environ.get("MAX_DAILY_RUNS", "100"))
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

# chat_id -> "new" | "continue" | "repo" | "branch"
_pending_prompt: dict[int, str] = {}

# chat_id -> список изображений для следующего промпта (Cursor API images[])
_pending_images: dict[int, list[dict[str, str]]] = {}

# chat_id -> разобранные PDF, которые уйдут в следующий промпт Cursor
_pending_pdfs: dict[int, list[ParsedPdf]] = {}

# agent_id -> уже отправленные в Telegram пути артефактов
_sent_artifacts: dict[str, set[str]] = {}
_artifacts_lock = threading.Lock()

MAX_IMAGES = 5
MAX_IMAGE_BYTES = 15 * 1024 * 1024
MAX_TEXT_FILE_BYTES = 100_000
MAX_ARTIFACT_BYTES = 49 * 1024 * 1024
IMAGE_MIMES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})
TEXT_MIMES = frozenset(
    {"text/plain", "text/markdown", "text/x-markdown", "application/json", "text/x-python"}
)
IMAGE_ONLY_PROMPT = "Распознай и опиши содержимое приложенного изображения."
MAX_PENDING_PDFS = 3

# Кнопки меню (Reply Keyboard)
BTN_NEW = "🆕 Новая задача"
BTN_CONTINUE = "💬 Продолжить"
BTN_STATUS = "📊 Статус"
BTN_LINK = "🔗 Ссылка"
BTN_REPO = "📦 Репозиторий"
BTN_BRANCH = "🌿 Ветка"
BTN_SETTINGS = "⚙️ Настройки"
BTN_CANCEL = "🛑 Отмена"
BTN_RESET = "🔄 Сброс"
BTN_HELP = "❓ Помощь"

MENU_BUTTONS = {
    BTN_NEW,
    BTN_CONTINUE,
    BTN_STATUS,
    BTN_LINK,
    BTN_REPO,
    BTN_BRANCH,
    BTN_SETTINGS,
    BTN_CANCEL,
    BTN_RESET,
    BTN_HELP,
}

COMMON_BRANCHES = ("main", "master", "develop", "dev")

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


def download_telegram_bytes(file_id: str, file_size: int | None = None) -> bytes:
    return download_telegram_file(
        bot,
        file_id,
        token=BOT_TOKEN,
        file_size=file_size,
    )


def reply_download_error(message: types.Message, exc: BaseException) -> None:
    if isinstance(exc, TelegramDownloadError):
        text = f"❌ {exc.user_message}"
    else:
        text = f"❌ Не удалось скачать файл: {str(exc)[:280]}"
    logging.getLogger(__name__).exception("Telegram file download failed")
    bot.reply_to(message, text, reply_markup=main_menu_keyboard())


def guess_image_mime(filename: str, fallback: str = "application/octet-stream") -> str:
    low = filename.lower()
    if low.endswith(".png"):
        return "image/png"
    if low.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    if low.endswith(".gif"):
        return "image/gif"
    if low.endswith(".webp"):
        return "image/webp"
    return fallback


def bytes_to_image_payload(data: bytes, mime_type: str) -> dict[str, str] | None:
    if len(data) > MAX_IMAGE_BYTES or mime_type not in IMAGE_MIMES:
        return None
    return {
        "data": base64.b64encode(data).decode("ascii"),
        "mimeType": mime_type,
    }


def add_pending_image(chat_id: int, image: dict[str, str]) -> int:
    imgs = _pending_images.setdefault(chat_id, [])
    if len(imgs) >= MAX_IMAGES:
        return len(imgs)
    imgs.append(image)
    return len(imgs)


def pop_pending_images(chat_id: int) -> list[dict[str, str]]:
    return _pending_images.pop(chat_id, [])


def clear_pending_images(chat_id: int) -> None:
    _pending_images.pop(chat_id, None)


def add_pending_pdf(chat_id: int, parsed: ParsedPdf) -> int:
    pdfs = _pending_pdfs.setdefault(chat_id, [])
    if len(pdfs) >= MAX_PENDING_PDFS:
        pdfs.pop(0)
    pdfs.append(parsed)
    return len(pdfs)


def pop_pending_pdfs(chat_id: int) -> list[ParsedPdf]:
    return _pending_pdfs.pop(chat_id, [])


def clear_pending_pdfs(chat_id: int) -> None:
    _pending_pdfs.pop(chat_id, None)


def consume_pending_attachments(
    chat_id: int,
    prompt: str,
    extra_images: list[dict[str, str]] | None = None,
    extra_pdfs: list[ParsedPdf] | None = None,
) -> tuple[str, list[dict[str, str]]]:
    images = pop_pending_images(chat_id)
    pdfs = pop_pending_pdfs(chat_id)
    if extra_images:
        images.extend(extra_images)
    if extra_pdfs:
        pdfs.extend(extra_pdfs)
    return merge_into_prompt(prompt, pdfs, images, max_images=MAX_IMAGES)


def read_text_attachment(data: bytes, filename: str) -> str | None:
    if len(data) > MAX_TEXT_FILE_BYTES:
        return f"[Файл {filename} слишком большой для включения в промпт]"
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def append_text_file_to_prompt(prompt: str, filename: str, content: str) -> str:
    block = f"\n\n--- {filename} ---\n{content}"
    if len(prompt) + len(block) > 8000:
        return prompt + block[:8000 - len(prompt)]
    return prompt + block


def send_agent_artifacts(chat_id: int, agent_id: str) -> None:
    try:
        resp = cursor.list_artifacts(agent_id)
    except CursorAPIError:
        return

    items = resp.get("items") or []
    if not items:
        return

    with _artifacts_lock:
        sent = _sent_artifacts.setdefault(agent_id, set())
        new_items = [it for it in items if it.get("path") and it["path"] not in sent]

    for item in new_items:
        path = item["path"]
        try:
            dl = cursor.get_artifact_download_url(agent_id, path)
            url = dl.get("url")
            if not url:
                continue
            data = cursor.download_url(url)
            filename = path.rsplit("/", 1)[-1]
            if len(data) > MAX_ARTIFACT_BYTES:
                bot.send_message(
                    chat_id,
                    f"📎 Файл слишком большой для Telegram: `{filename}`",
                    parse_mode="Markdown",
                )
                with _artifacts_lock:
                    sent.add(path)
                continue
            mime = guess_image_mime(filename)
            caption = f"📎 {filename}"
            if mime in IMAGE_MIMES:
                bot.send_photo(chat_id, BytesIO(data), caption=caption)
            else:
                bot.send_document(
                    chat_id,
                    BytesIO(data),
                    visible_file_name=filename,
                    caption=caption,
                )
            with _artifacts_lock:
                sent.add(path)
        except Exception:
            continue


def dispatch_task(
    chat_id: int,
    user_id: int,
    prompt: str,
    pending: str | None,
    images: list[dict[str, str]] | None,
) -> None:
    imgs = images or None
    if pending == "new":
        start_task(chat_id, user_id, prompt, force_new=True, images=imgs)
    elif pending == "continue":
        start_task(chat_id, user_id, prompt, force_new=False, images=imgs)
    else:
        start_task(chat_id, user_id, prompt, images=imgs)


def handle_incoming_image(
    message: types.Message,
    data: bytes,
    mime_type: str,
    caption: str = "",
) -> None:
    chat_id = message.chat.id
    user_id = message.from_user.id
    image = bytes_to_image_payload(data, mime_type)
    if not image:
        bot.reply_to(
            message,
            "❌ Изображение не поддерживается или больше 15 МБ.\n"
            "Форматы: PNG, JPEG, GIF, WebP.",
            reply_markup=main_menu_keyboard(),
        )
        return

    pending = _pending_prompt.get(chat_id)
    if pending in ("repo", "branch"):
        bot.reply_to(
            message,
            "Сначала завершите настройку репозитория/ветки текстом.",
            reply_markup=main_menu_keyboard(),
        )
        return

    caption = caption.strip()
    if caption:
        _pending_prompt.pop(chat_id, None)
        prompt, images = consume_pending_attachments(
            chat_id, caption, extra_images=[image]
        )
        dispatch_task(chat_id, user_id, prompt, pending, images or None)
        return

    count = add_pending_image(chat_id, image)
    if count >= MAX_IMAGES:
        bot.reply_to(
            message,
            f"📷 Добавлено {count} фото (лимит). Напишите задачу.",
            reply_markup=main_menu_keyboard(),
        )
    else:
        bot.reply_to(
            message,
            f"📷 Фото добавлено ({count}/{MAX_IMAGES}).\n"
            "Напишите задачу или отправьте ещё фото.",
            reply_markup=main_menu_keyboard(),
        )


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


def main_menu_keyboard() -> types.ReplyKeyboardMarkup:
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, is_persistent=True)
    markup.row(BTN_NEW, BTN_CONTINUE)
    markup.row(BTN_REPO, BTN_BRANCH)
    markup.row(BTN_STATUS, BTN_LINK)
    markup.row(BTN_SETTINGS, BTN_CANCEL)
    markup.row(BTN_RESET, BTN_HELP)
    return markup


def repo_inline_keyboard() -> types.InlineKeyboardMarkup | None:
    if not DEFAULT_REPO_URL:
        return None
    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton(
            "✅ Использовать по умолчанию",
            callback_data="repo:default",
        )
    )
    return markup


def branch_inline_keyboard(current: str) -> types.InlineKeyboardMarkup:
    markup = types.InlineKeyboardMarkup()
    options: list[str] = []
    for name in (DEFAULT_BRANCH, *COMMON_BRANCHES):
        if name and name not in options:
            options.append(name)
    row: list[types.InlineKeyboardButton] = []
    for name in options[:6]:
        label = f"✓ {name}" if name == current else name
        row.append(types.InlineKeyboardButton(label, callback_data=f"branch:{name}"))
        if len(row) == 2:
            markup.row(*row)
            row = []
    if row:
        markup.row(*row)
    return markup


def apply_repo_url(chat_id: int, url: str) -> str | None:
    url = url.strip().rstrip("/")
    if not GITHUB_REPO_RE.match(url):
        return "❌ Нужен URL вида https://github.com/owner/repo"
    _, branch = resolve_repo(chat_id)
    store.set_repo_prefs(chat_id, url, branch)
    return f"✅ Репозиторий:\n{url}\nВетка: `{branch}`"


def apply_branch(chat_id: int, branch: str) -> str | None:
    branch = branch.strip()
    if not branch:
        return "❌ Укажите имя ветки"
    url, _ = resolve_repo(chat_id)
    if not url:
        return "❌ Сначала задайте репозиторий (кнопка «📦 Репозиторий»)."
    store.set_repo_prefs(chat_id, url, branch)
    return f"✅ Ветка: `{branch}`"


def send_repo_prompt(chat_id: int, reply_to: types.Message | None = None) -> None:
    _pending_prompt[chat_id] = "repo"
    url, branch = resolve_repo(chat_id)
    text = (
        f"📦 **Репозиторий**\n\n"
        f"Текущий: {url or '— не задан —'}\n"
        f"Ветка: `{branch}`\n\n"
        "Отправьте URL `https://github.com/owner/repo`\n"
        "или нажмите кнопку ниже."
    )
    markup = repo_inline_keyboard()
    if reply_to:
        bot.reply_to(
            reply_to,
            text,
            parse_mode="Markdown",
            reply_markup=markup,
        )
    else:
        bot.send_message(
            chat_id,
            text,
            parse_mode="Markdown",
            reply_markup=markup,
        )


def send_branch_prompt(chat_id: int, reply_to: types.Message | None = None) -> None:
    _pending_prompt[chat_id] = "branch"
    url, branch = resolve_repo(chat_id)
    if not url:
        text = "❌ Сначала задайте репозиторий (кнопка «📦 Репозиторий»)."
        if reply_to:
            bot.reply_to(reply_to, text, reply_markup=main_menu_keyboard())
        else:
            bot.send_message(chat_id, text, reply_markup=main_menu_keyboard())
        return
    text = (
        f"🌿 **Ветка**\n\n"
        f"Репозиторий: {url}\n"
        f"Текущая ветка: `{branch}`\n\n"
        "Выберите ветку кнопкой или отправьте имя текстом."
    )
    markup = branch_inline_keyboard(branch)
    if reply_to:
        bot.reply_to(
            reply_to,
            text,
            parse_mode="Markdown",
            reply_markup=markup,
        )
    else:
        bot.send_message(
            chat_id,
            text,
            parse_mode="Markdown",
            reply_markup=markup,
        )


def help_text() -> str:
    return (
        "👋 **Cursor Bot**\n\n"
        "Используйте **кнопки внизу** или пишите задачу текстом — "
        "Cloud Agent выполнит её в репозитории (как на cursor.com/agents).\n\n"
        "**Кнопки:**\n"
        "🆕 Новая задача — новый агент\n"
        "💬 Продолжить — сообщение текущему агенту\n"
        "📊 Статус / 🔗 Ссылка / ⚙️ Настройки\n"
        "📦 Репозиторий / 🌿 Ветка\n"
        "🛑 Отмена / 🔄 Сброс\n\n"
        "📷 **Файлы:** фото (PNG/JPEG/GIF/WebP) и **PDF** — "
        "с подписью-задачей или несколько файлов, затем текст.\n"
        "📄 PDF из чата — до 20 МБ (лимит Telegram). Более крупные — "
        "пришлите публичную ссылку (Google Drive / Dropbox / Яндекс Диск / прямой .pdf).\n"
        "📎 Cursor может вернуть файлы из `artifacts/` после задачи.\n\n"
        f"Лимит: {MAX_DAILY_RUNS} задач/день."
    )


def send_settings(chat_id: int, reply_to: types.Message | None = None) -> None:
    url, branch = resolve_repo(chat_id)
    session = store.get_session(chat_id)
    lines = [
        f"📦 Repo: {url or 'не задан'}",
        f"🌿 Branch: {branch}",
        f"🔀 Auto-PR: {AUTO_CREATE_PR}",
    ]
    if session and session.get("agent_id"):
        lines.append(f"🤖 Agent: `{session['agent_id']}`")
        if session.get("agent_url"):
            lines.append(f"🔗 {session['agent_url']}")
    text = "\n".join(lines)
    if reply_to:
        bot.reply_to(reply_to, text, parse_mode="Markdown", reply_markup=main_menu_keyboard())
    else:
        bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=main_menu_keyboard())


def send_status(chat_id: int, reply_to: types.Message | None = None) -> None:
    with _run_lock:
        active = _active_runs.get(chat_id)
    session = store.get_session(chat_id)
    if active:
        text = (
            f"⏳ Выполняется run `{active['run_id']}`\n"
            f"Agent: `{active['agent_id']}`"
        )
        if reply_to:
            bot.reply_to(reply_to, text, parse_mode="Markdown", reply_markup=main_menu_keyboard())
        else:
            bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=main_menu_keyboard())
        return
    if session and session.get("agent_id"):
        try:
            agent = cursor.get_agent(session["agent_id"])
            text = (
                f"Агент: {agent.get('status')}\n"
                f"🔗 {agent.get('url', session.get('agent_url', '—'))}"
            )
        except CursorAPIError as e:
            text = f"❌ {e.message[:300]}"
    else:
        text = "Нет активных задач."
    if reply_to:
        bot.reply_to(reply_to, text, reply_markup=main_menu_keyboard())
    else:
        bot.send_message(chat_id, text, reply_markup=main_menu_keyboard())


def send_link(chat_id: int, reply_to: types.Message | None = None) -> None:
    session = store.get_session(chat_id)
    if session and session.get("agent_id"):
        text = agent_web_url(session["agent_id"], session.get("agent_url"))
    else:
        text = "Нет активной задачи. Нажмите «🆕 Новая задача» или отправьте текст."
    if reply_to:
        bot.reply_to(reply_to, text, reply_markup=main_menu_keyboard())
    else:
        bot.send_message(chat_id, text, reply_markup=main_menu_keyboard())


def send_cancel(chat_id: int, reply_to: types.Message | None = None) -> None:
    with _run_lock:
        active = _active_runs.get(chat_id)
    session = store.get_session(chat_id)
    if not active and not session:
        text = "Нечего отменять."
        if reply_to:
            bot.reply_to(reply_to, text, reply_markup=main_menu_keyboard())
        else:
            bot.send_message(chat_id, text, reply_markup=main_menu_keyboard())
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
        text = "Не найден активный run."
        if reply_to:
            bot.reply_to(reply_to, text, reply_markup=main_menu_keyboard())
        else:
            bot.send_message(chat_id, text, reply_markup=main_menu_keyboard())
        return
    try:
        cursor.cancel_run(agent_id, run_id)
        text = "🛑 Отменено."
    except CursorAPIError as e:
        text = f"❌ {e.status}: {e.message[:300]}"
    if reply_to:
        bot.reply_to(reply_to, text, reply_markup=main_menu_keyboard())
    else:
        bot.send_message(chat_id, text, reply_markup=main_menu_keyboard())


def send_reset(chat_id: int, reply_to: types.Message | None = None) -> None:
    store.clear_session(chat_id)
    with _run_lock:
        _active_runs.pop(chat_id, None)
    _pending_prompt.pop(chat_id, None)
    clear_pending_images(chat_id)
    clear_pending_pdfs(chat_id)
    text = "🔄 Сессия сброшена. Следующая задача создаст нового агента."
    if reply_to:
        bot.reply_to(reply_to, text, reply_markup=main_menu_keyboard())
    else:
        bot.send_message(chat_id, text, reply_markup=main_menu_keyboard())


def register_bot_commands() -> None:
    commands = [
        telebot.types.BotCommand("start", "Меню и кнопки"),
        telebot.types.BotCommand("new", "Новая задача"),
        telebot.types.BotCommand("status", "Статус агента"),
        telebot.types.BotCommand("link", "Ссылка на задачу"),
        telebot.types.BotCommand("settings", "Настройки"),
        telebot.types.BotCommand("cancel", "Отменить задачу"),
        telebot.types.BotCommand("reset", "Сбросить сессию"),
        telebot.types.BotCommand("repo", "Репозиторий GitHub"),
        telebot.types.BotCommand("branch", "Ветка Git"),
    ]
    try:
        bot.set_my_commands(commands)
    except Exception:
        pass


def set_busy(chat_id: int, busy: bool) -> None:
    with _run_lock:
        if chat_id in _active_runs:
            _active_runs[chat_id]["busy"] = busy


def is_busy(chat_id: int) -> bool:
    with _run_lock:
        return bool(_active_runs.get(chat_id, {}).get("busy"))


def build_completion_text(status_label: str, result_text: str, git: dict | None) -> str:
    lines = [status_label]
    if result_text:
        lines.append("\n" + result_text[:3500])
    pr_block = format_pr_links(git)
    if pr_block:
        lines.append("\n" + pr_block)
    return "\n".join(lines)


# ─── Run worker (stream + poll fallback) ───────────────────────────────────────


def _is_stream_gone_message(message: object) -> bool:
    """SSE/API may close the live stream while the Cloud Agent run keeps going."""
    text = str(message or "").lower()
    return any(
        needle in text
        for needle in (
            "no longer available",
            "stream_expired",
            "stream expired",
            "stream is gone",
        )
    )


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

    def update_status(
        prefix: str,
        body: str = "",
        *,
        finished: bool = False,
        force: bool = False,
    ) -> None:
        nonlocal last_edit
        now = time.time()
        if not force and now - last_edit < 2.0 and not terminal:
            return
        last_edit = now
        preview = (prefix + "\n\n" + body).strip()
        if len(preview) > 3900:
            preview = preview[:3900] + "…"
        markup = None if finished else progress_keyboard(agent_id, agent_url)
        try:
            bot.edit_message_text(
                preview,
                chat_id,
                status_msg_id,
                reply_markup=markup,
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
            final = build_completion_text(f"✅ Задача завершена ({status})", result_text, data.get("git"))
            update_status(final[:3900], finished=True, force=True)
            if len(final) > 3900:
                send_chunked(chat_id, final[3900:])
            send_agent_artifacts(chat_id, agent_id)
        elif event == "error":
            # Stream retention / disconnect is not a run failure — poll Get A Run.
            err_msg = data.get("message", data)
            preview = "".join(assistant_buf)[-1200:]
            if _is_stream_gone_message(err_msg):
                update_status(
                    "⚠️ Стрим закрыт, задача продолжается — опрашиваю статус…",
                    preview,
                    force=True,
                )
            else:
                update_status(
                    f"⚠️ Стрим прервался ({err_msg}), проверяю статус задачи…",
                    preview,
                    force=True,
                )

    try:
        cursor.stream_run(agent_id, run_id, on_event)
    except CursorAPIError as e:
        preview = "".join(assistant_buf)[-1200:]
        if e.status == 410 or _is_stream_gone_message(e.message):
            update_status(
                "⚠️ Стрим истёк, опрашиваю статус задачи…",
                preview,
                force=True,
            )
        else:
            update_status(
                f"⚠️ Стрим недоступен ({e.status}), опрашиваю статус…",
                preview,
                force=True,
            )

    # Poll until terminal (authoritative status after stream ends/expires)
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
            emoji = "✅" if status == "FINISHED" else "⚠️"
            msg = build_completion_text(f"{emoji} {status}", result_text, run.get("git"))
            update_status(msg[:3900], finished=True, force=True)
            if len(msg) > 3900:
                send_chunked(chat_id, msg[3900:])
            send_agent_artifacts(chat_id, agent_id)
            terminal = True
            break

        update_status(f"⏳ Статус: {status}", "".join(assistant_buf)[-1200:])
        time.sleep(5)

    with _run_lock:
        _active_runs.pop(chat_id, None)
    set_busy(chat_id, False)


def start_task(
    chat_id: int,
    user_id: int,
    prompt: str,
    force_new: bool = False,
    images: list[dict[str, str]] | None = None,
) -> None:
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
                    "📦 Укажите репозиторий кнопкой «📦 Репозиторий» "
                    "или задайте DEFAULT_REPO_URL на сервере.",
                    reply_markup=main_menu_keyboard(),
                )
                return

            agent_name = "Telegram: " + prompt[:70]
            resp = cursor.create_agent(
                prompt=prompt,
                repo_url=repo_url,
                starting_ref=branch,
                auto_create_pr=AUTO_CREATE_PR,
                name=agent_name,
                images=images,
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
            resp = cursor.create_run(agent_id, prompt, images=images)
            run = resp["run"]
            run_id = run["id"]
            title = "💬 Продолжение диалога с агентом"

        store.increment_daily_runs(user_id, date.today().isoformat())

        img_note = f"\n📷 {len(images)} влож. для Cursor" if images else ""
        status_msg = bot.send_message(
            chat_id,
            f"{title}\n⏳ Запуск…\n\n{prompt[:500]}{img_note}",
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
    _pending_prompt.pop(message.chat.id, None)
    clear_pending_images(message.chat.id)
    clear_pending_pdfs(message.chat.id)
    bot.reply_to(
        message,
        help_text(),
        parse_mode="Markdown",
        disable_web_page_preview=True,
        reply_markup=main_menu_keyboard(),
    )


@bot.message_handler(commands=["new"])
def cmd_new(message: types.Message) -> None:
    prompt = message.text.replace("/new", "", 1).strip()
    if not prompt:
        _pending_prompt[message.chat.id] = "new"
        bot.reply_to(
            message,
            "✏️ Опишите задачу одним сообщением:",
            reply_markup=main_menu_keyboard(),
        )
        return
    if ingest_pdf_from_urls(message, prompt, "new"):
        return
    prompt, images = consume_pending_attachments(message.chat.id, prompt)
    start_task(
        message.chat.id,
        message.from_user.id,
        prompt,
        force_new=True,
        images=images or None,
    )


@bot.message_handler(commands=["repo"])
def cmd_repo(message: types.Message) -> None:
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        send_repo_prompt(message.chat.id, message)
        return
    result = apply_repo_url(message.chat.id, parts[1])
    if result and not result.startswith("❌"):
        _pending_prompt.pop(message.chat.id, None)
    bot.reply_to(message, result, parse_mode="Markdown", reply_markup=main_menu_keyboard())


@bot.message_handler(commands=["branch"])
def cmd_branch(message: types.Message) -> None:
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        send_branch_prompt(message.chat.id, message)
        return
    result = apply_branch(message.chat.id, parts[1])
    if result and not result.startswith("❌"):
        _pending_prompt.pop(message.chat.id, None)
    bot.reply_to(message, result, parse_mode="Markdown", reply_markup=main_menu_keyboard())


@bot.message_handler(commands=["settings"])
def cmd_settings(message: types.Message) -> None:
    send_settings(message.chat.id, message)


@bot.message_handler(commands=["link"])
def cmd_link(message: types.Message) -> None:
    send_link(message.chat.id, message)


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
    send_status(message.chat.id, message)


@bot.message_handler(commands=["cancel"])
def cmd_cancel(message: types.Message) -> None:
    send_cancel(message.chat.id, message)


@bot.message_handler(commands=["reset"])
def cmd_reset(message: types.Message) -> None:
    send_reset(message.chat.id, message)


@bot.message_handler(func=lambda m: m.text in MENU_BUTTONS)
def handle_menu_button(message: types.Message) -> None:
    chat_id = message.chat.id
    text = message.text

    if text == BTN_NEW:
        _pending_prompt[chat_id] = "new"
        bot.reply_to(
            message,
            "✏️ Опишите **новую** задачу одним сообщением:",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard(),
        )
    elif text == BTN_CONTINUE:
        session = store.get_session(chat_id)
        if not session or not session.get("agent_id"):
            bot.reply_to(
                message,
                "Нет активного агента. Нажмите «🆕 Новая задача».",
                reply_markup=main_menu_keyboard(),
            )
            return
        _pending_prompt[chat_id] = "continue"
        bot.reply_to(
            message,
            "✏️ Напишите сообщение для текущего агента:",
            reply_markup=main_menu_keyboard(),
        )
    elif text == BTN_STATUS:
        send_status(chat_id, message)
    elif text == BTN_LINK:
        send_link(chat_id, message)
    elif text == BTN_REPO:
        send_repo_prompt(chat_id, message)
    elif text == BTN_BRANCH:
        send_branch_prompt(chat_id, message)
    elif text == BTN_SETTINGS:
        send_settings(chat_id, message)
    elif text == BTN_CANCEL:
        send_cancel(chat_id, message)
    elif text == BTN_RESET:
        send_reset(chat_id, message)
    elif text == BTN_HELP:
        bot.reply_to(
            message,
            help_text(),
            parse_mode="Markdown",
            disable_web_page_preview=True,
            reply_markup=main_menu_keyboard(),
        )


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith(("repo:", "branch:")))
def handle_inline_callback(call: types.CallbackQuery) -> None:
    chat_id = call.message.chat.id
    data = call.data or ""

    if data == "repo:default":
        if not DEFAULT_REPO_URL:
            bot.answer_callback_query(call.id, "Репозиторий по умолчанию не задан")
            return
        result = apply_repo_url(chat_id, DEFAULT_REPO_URL)
        _pending_prompt.pop(chat_id, None)
        bot.answer_callback_query(call.id, "Репозиторий обновлён")
        bot.send_message(chat_id, result, parse_mode="Markdown", reply_markup=main_menu_keyboard())
        return

    if data.startswith("branch:"):
        branch = data.split(":", 1)[1]
        result = apply_branch(chat_id, branch)
        if result and not result.startswith("❌"):
            _pending_prompt.pop(chat_id, None)
            bot.answer_callback_query(call.id, f"Ветка: {branch}")
        else:
            bot.answer_callback_query(call.id, "Ошибка")
        bot.send_message(chat_id, result, parse_mode="Markdown", reply_markup=main_menu_keyboard())


def handle_incoming_pdf(
    message: types.Message,
    data: bytes,
    filename: str,
    caption: str | None = None,
) -> None:
    chat_id = message.chat.id
    user_id = message.from_user.id
    pending = _pending_prompt.get(chat_id)
    if pending in ("repo", "branch"):
        bot.reply_to(
            message,
            "Сначала завершите настройку репозитория/ветки текстом.",
            reply_markup=main_menu_keyboard(),
        )
        return

    try:
        parsed = parse_pdf(data, filename, max_page_images=MAX_IMAGES)
    except PdfAttachmentError as e:
        bot.reply_to(message, f"❌ {e}", reply_markup=main_menu_keyboard())
        return

    caption_text = (caption if caption is not None else (message.caption or "")).strip()
    if caption_text:
        _pending_prompt.pop(chat_id, None)
        prompt, images = consume_pending_attachments(
            chat_id, caption_text, extra_pdfs=[parsed]
        )
        dispatch_task(chat_id, user_id, prompt, pending, images or None)
        return

    count = add_pending_pdf(chat_id, parsed)
    extra = f"\n{parsed.warning}" if parsed.warning else ""
    bot.reply_to(
        message,
        f"📄 PDF добавлен: `{filename}` ({parsed.page_count} стр.)\n"
        f"В очереди: {count}/{MAX_PENDING_PDFS}.{extra}\n"
        "Напишите задачу или отправьте ещё файлы — содержимое уйдёт в Cursor.",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard(),
    )


def ingest_pdf_from_urls(message: types.Message, text: str, pending: str | None) -> bool:
    """Download a PDF from a public URL. Returns True if the message was a PDF link."""
    urls = extract_pdf_urls(text)
    if not urls:
        return False
    chat_id = message.chat.id
    leftover = strip_urls(text, urls)
    if pending in ("new", "continue"):
        _pending_prompt[chat_id] = pending
    status = bot.reply_to(
        message,
        "⏬ Скачиваю PDF по ссылке…",
        reply_markup=main_menu_keyboard(),
    )
    try:
        downloaded = download_pdf_from_url(urls[0])
    except PdfUrlError as exc:
        bot.edit_message_text(
            f"❌ {exc}",
            chat_id,
            status.message_id,
            reply_markup=main_menu_keyboard(),
        )
        return True
    except Exception as exc:
        logging.getLogger(__name__).exception("URL PDF download failed")
        bot.edit_message_text(
            f"❌ Не удалось скачать PDF по ссылке: {str(exc)[:280]}",
            chat_id,
            status.message_id,
            reply_markup=main_menu_keyboard(),
        )
        return True
    try:
        bot.delete_message(chat_id, status.message_id)
    except Exception:
        pass
    extra = ""
    if len(urls) > 1:
        extra = " (взял первую ссылку)"
    if extra:
        bot.send_message(chat_id, f"📄 Скачал PDF{extra}.", reply_markup=main_menu_keyboard())
    handle_incoming_pdf(message, downloaded.data, downloaded.filename, caption=leftover)
    return True


@bot.message_handler(content_types=["photo"])
def handle_photo(message: types.Message) -> None:
    if not message.photo:
        return
    try:
        data = download_telegram_bytes(
            message.photo[-1].file_id,
            getattr(message.photo[-1], "file_size", None),
        )
    except Exception as exc:
        reply_download_error(message, exc)
        return
    handle_incoming_image(message, data, "image/jpeg", message.caption or "")


@bot.message_handler(content_types=["document"])
def handle_document(message: types.Message) -> None:
    doc = message.document
    if not doc:
        return

    chat_id = message.chat.id
    user_id = message.from_user.id
    filename = doc.file_name or "file"
    mime = doc.mime_type or guess_image_mime(filename)

    if _pending_prompt.get(chat_id) in ("repo", "branch"):
        bot.reply_to(
            message,
            "Сначала завершите настройку репозитория/ветки текстом.",
            reply_markup=main_menu_keyboard(),
        )
        return

    if doc.file_size and doc.file_size > TELEGRAM_BOT_FILE_LIMIT:
        bot.reply_to(
            message,
            f"❌ {TOO_BIG_FOR_TELEGRAM}",
            reply_markup=main_menu_keyboard(),
        )
        return

    try:
        data = download_telegram_bytes(doc.file_id, doc.file_size)
    except Exception as exc:
        reply_download_error(message, exc)
        return

    if looks_like_pdf(filename, mime) or data.lstrip().startswith(b"%PDF"):
        handle_incoming_pdf(message, data, filename if filename.lower().endswith(".pdf") else f"{filename}.pdf")
        return

    if mime in IMAGE_MIMES or guess_image_mime(filename) in IMAGE_MIMES:
        image_mime = mime if mime in IMAGE_MIMES else guess_image_mime(filename)
        handle_incoming_image(message, data, image_mime, message.caption or "")
        return

    if mime in TEXT_MIMES or filename.lower().endswith((".txt", ".md", ".json", ".py", ".csv")):
        content = read_text_attachment(data, filename)
        if content is None:
            bot.reply_to(
                message,
                "❌ Не удалось прочитать файл как UTF-8 текст.",
                reply_markup=main_menu_keyboard(),
            )
            return
        caption = (message.caption or "").strip() or f"Обработай приложенный файл {filename}"
        pending = _pending_prompt.pop(chat_id, None)
        prompt = append_text_file_to_prompt(caption, filename, content)
        prompt, images = consume_pending_attachments(chat_id, prompt)
        dispatch_task(chat_id, user_id, prompt, pending, images or None)
        return

    bot.reply_to(
        message,
        "❌ Формат не поддерживается.\n"
        "Отправьте PDF, изображение (PNG/JPEG/GIF/WebP) или текстовый файл "
        "(.txt, .md, .json, .py).",
        reply_markup=main_menu_keyboard(),
    )


@bot.message_handler(func=lambda m: m.text and not m.text.startswith("/"))
def handle_text(message: types.Message) -> None:
    if not message.text:
        return
    chat_id = message.chat.id
    user_id = message.from_user.id
    prompt = message.text.strip()
    pending = _pending_prompt.pop(chat_id, None)

    if pending == "repo":
        result = apply_repo_url(chat_id, prompt)
        if result and result.startswith("❌"):
            _pending_prompt[chat_id] = "repo"
        bot.reply_to(message, result, parse_mode="Markdown", reply_markup=main_menu_keyboard())
        return
    if pending == "branch":
        result = apply_branch(chat_id, prompt)
        if result and result.startswith("❌"):
            _pending_prompt[chat_id] = "branch"
        bot.reply_to(message, result, parse_mode="Markdown", reply_markup=main_menu_keyboard())
        return

    if ingest_pdf_from_urls(message, prompt, pending):
        return

    prompt, images = consume_pending_attachments(chat_id, prompt)
    if not prompt and images:
        prompt = IMAGE_ONLY_PROMPT
    if not prompt and not images:
        return

    if pending == "new":
        start_task(chat_id, user_id, prompt, force_new=True, images=images or None)
        return
    if pending == "continue":
        start_task(chat_id, user_id, prompt, force_new=False, images=images or None)
        return
    start_task(chat_id, user_id, prompt, images=images or None)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    print("Cursor Telegram Bot starting…")
    print(f"DEFAULT_REPO: {bool(DEFAULT_REPO_URL)}")
    register_bot_commands()
    bot.infinity_polling(timeout=60, long_polling_timeout=60)
