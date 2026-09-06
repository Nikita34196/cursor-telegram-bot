# Cursor Telegram Bot

Telegram-бот для **Cursor Cloud Agents** — как [cursor.com/agents](https://cursor.com/agents). Работает 24/7 в облаке, без вашего ПК.

Отправляйте задачи текстом, фото **и PDF**. PDF скачивается из Telegram, из него извлекается текст и скриншоты страниц — это передаётся Cloud Agent.

## Возможности

- Создание Cloud Agent по текстовой задаче
- Продолжение диалога с тем же агентом
- Стриминг прогресса в Telegram
- **PDF:** бот качает файл, разбирает его и передаёт содержимое в Cursor
- Фото (PNG/JPEG/GIF/WebP) как изображения промпта
- Ссылка на агента и на PR после завершения
- Репозиторий и ветка на чат: `/repo`, `/branch`
- Лимит задач в сутки на пользователя

## PDF → Cursor

Cloud Agents API принимает в промпте только изображения, не бинарный PDF. Поэтому бот:

1. Скачивает документ из Telegram, если он **до 20 МБ** (лимит Bot API).
2. Если PDF больше — скачивает по публичной ссылке (Google Drive / Dropbox / Яндекс Диск / прямой `.pdf`, до 80 МБ).
3. Извлекает текст (до 80 страниц).
4. Рендерит первые страницы в PNG и прикладывает их как `prompt.images`.
5. Кладёт текст PDF в промпт агента.

Так агент видит и текст, и внешний вид страниц (в том числе сканы без текстового слоя).

**Как отправить:**

```
1. PDF до 20 МБ — просто файлом в чат (можно с подписью-задачей).
2. PDF больше 20 МБ — ссылка:
   сделай конспект https://drive.google.com/file/d/…/view
3. Или сначала файл/ссылка, потом текст задачи.
```

Лимиты: до 3 PDF в очереди, пароль на файле не поддерживается. Google Drive: доступ «любой, у кого есть ссылка».

## Требования

1. **Telegram Bot Token** — [@BotFather](https://t.me/BotFather) → `/newbot`
2. **Cursor API Key** — [cursor.com/dashboard](https://cursor.com/dashboard) → API Keys
3. **Платный Cursor** с Cloud Agents и доступом к GitHub-репозиторию
4. **GitHub App Cursor** с доступом к репозиторию, с которым работает бот

## Быстрый деплой на Railway (рекомендуется)

1. Форкните или склонируйте этот репозиторий на GitHub.
2. Зайдите на [railway.app](https://railway.app) → **New Project** → **Deploy from GitHub repo**.
3. **Variables** (Settings → Variables):

| Переменная | Обязательно | Описание |
|------------|-------------|----------|
| `BOT_TOKEN` | да | Токен от BotFather |
| `CURSOR_API_KEY` | да | API-ключ Cursor |
| `DEFAULT_REPO_URL` | да* | `https://github.com/user/repo` |
| `DEFAULT_BRANCH` | нет | По умолчанию `main` |
| `ADMIN_ID` | нет | Ваш Telegram user id |
| `MAX_DAILY_RUNS` | нет | Лимит в день (по умолчанию 100) |
| `ALLOWED_USER_IDS` | нет | Пусто = все; иначе `123,456` |

\* Если не задан — каждый пользователь указывает `/repo` сам.

4. Deploy. Бот сразу начнёт polling.

### Telegram user id

Напишите [@userinfobot](https://t.me/userinfobot) — он пришлёт ваш id для `ADMIN_ID`.

## Другие хостинги

- **Render**: Web Service, Docker, те же env-переменные.
- **Fly.io**: `fly launch` + `fly secrets set BOT_TOKEN=... CURSOR_API_KEY=...`
- **VPS**: `docker build -t cursor-bot . && docker run -d --env-file .env cursor-bot`

## Использование

```
/start
/repo https://github.com/owner/repo
Добавь в README раздел про деплой на Railway
```

PDF:

```
(отправьте файл brief.pdf с подписью)
Собери конспект и список задач из этого PDF
```

или новая сессия:

```
/new Рефакторни bot.py и добавь type hints
```

| Команда | Действие |
|---------|----------|
| Текст | Продолжить текущего агента |
| PDF / фото | Скачать вложение и передать в Cursor |
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
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export $(grep -v '^#' .env | xargs)
python bot.py
```

Тесты разбора PDF:

```bash
python -m unittest tests.test_pdf_attachments
```

## Структура

```
cursor_client.py    — Cloud Agents API v1
storage.py          — SQLite (сессии, лимиты)
pdf_urls.py           — скачивание больших PDF по публичной ссылке
telegram_files.py     — скачивание вложений из Telegram
pdf_attachments.py  — разбор PDF → текст + скриншоты
bot.py              — Telegram handlers
```

## Лицензия

MIT
