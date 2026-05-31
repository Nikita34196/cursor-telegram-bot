# Cursor Telegram Bot

## 👉 [ДАЛЬШЕ.md](./ДАЛЬШЕ.md) — что делать сейчас (без повторных инструкций)

Код бота **уже готов**. Откройте папку в Cursor — агент прочитает `ДАЛЬШЕ.md` и продолжит с вашего шага, а не «с нуля».

---

Telegram-бот для **Cursor Cloud Agents** — как [cursor.com/agents](https://cursor.com/agents). Работает **24/7 в облаке**, без вашего ПК.

Отдельный проект от [ocr-bot](https://github.com/Nikita34196/ocr-bot) (распознавание текста).

## Возможности

- Создание Cloud Agent по текстовой задаче
- Продолжение диалога с тем же агентом (как в веб-версии)
- Стриминг прогресса в сообщение Telegram
- Ссылка на агента в браузере и на PR после завершения
- Репозиторий и ветка на чат: `/repo`, `/branch`
- Лимит задач в сутки на пользователя

## Требования

1. **Telegram Bot Token** — [@BotFather](https://t.me/BotFather) → `/newbot`
2. **Cursor API Key** — [cursor.com/dashboard](https://cursor.com/dashboard) → API Keys
3. **Платный Cursor** с Cloud Agents и доступом к GitHub-репозиторию
4. **GitHub App Cursor** с доступом к репозиторию, с которым работает бот

## Быстрый деплой на Railway (рекомендуется)

1. Форкните или склонируйте этот репозиторий на GitHub.
2. Зайдите на [railway.app](https://railway.app) → **New Project** → **Deploy from GitHub repo**.
3. Выберите репозиторий `cursor-bot`.
4. **Variables** (Settings → Variables):

| Переменная | Обязательно | Описание |
|------------|-------------|----------|
| `BOT_TOKEN` | да | Токен от BotFather |
| `CURSOR_API_KEY` | да | API-ключ Cursor |
| `DEFAULT_REPO_URL` | да* | `https://github.com/user/repo` |
| `DEFAULT_BRANCH` | нет | По умолчанию `main` |
| `ADMIN_ID` | нет | Ваш Telegram user id |
| `MAX_DAILY_RUNS` | нет | Лимит в день (по умолчанию 20) |
| `ALLOWED_USER_IDS` | нет | Пусто = все; иначе `123,456` |

\* Если не задан — каждый пользователь указывает `/repo` сам.

5. Deploy. Бот сразу начнёт polling.

### Telegram user id

Напишите [@userinfobot](https://t.me/userinfobot) — он пришлёт ваш id для `ADMIN_ID`.

## Другие хостинги

- **Render**: Web Service, Docker, те же env-переменные.
- **Fly.io**: `fly launch` + `fly secrets set BOT_TOKEN=... CURSOR_API_KEY=...`
- **VPS**: `docker build -t cursor-bot . && docker run -d --env-file .env cursor-bot`

## Использование

```
/start
/repo https://github.com/Nikita34196/ocr-bot
Добавь в README раздел про деплой на Railway
```

или новая сессия:

```
/new Рефакторни bot.py и добавь type hints
```

| Команда | Действие |
|---------|----------|
| Текст | Продолжить текущего агента |
| `/new …` | Новый Cloud Agent |
| `/repo URL` | Репозиторий GitHub |
| `/branch main` | Ветка |
| `/link` | Открыть в Cursor |
| `/status` | Статус задачи |
| `/cancel` | Отменить run |
| `/reset` | Сбросить сессию чата |
| `/admin` | Диагностика (ADMIN_ID) |

## Безопасность (важно для публичного бота)

- **Один `CURSOR_API_KEY` на сервере** — все расходы идут с вашего аккаунта Cursor.
- Cloud Agent может выполнять команды в терминале и открывать PR — давайте доступ только к нужным репозиториям.
- Используйте `MAX_DAILY_RUNS` и при необходимости `ALLOWED_USER_IDS`.
- Не публикуйте ключи в коде — только в переменных окружения хостинга.

## Локальный запуск

```bash
cp .env.example .env
# отредактируйте .env
pip install -r requirements.txt
export $(grep -v '^#' .env | xargs)
python bot.py
```

## Структура

```
cursor_client.py  — Cloud Agents API v1
storage.py        — SQLite (сессии, лимиты)
bot.py            — Telegram handlers
```

## Лицензия

MIT
