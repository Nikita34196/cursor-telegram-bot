"""Download attachments from Telegram Bot API."""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

import requests

logger = logging.getLogger(__name__)

TELEGRAM_BOT_FILE_LIMIT = 20 * 1024 * 1024
API_FILE_BASE = "https://api.telegram.org/file/bot{token}/{path}"


class TelegramDownloadError(Exception):
    """User-facing Telegram download failure."""

    def __init__(self, user_message: str) -> None:
        super().__init__(user_message)
        self.user_message = user_message


def format_download_error(exc: BaseException, token: str = "") -> str:
    text = str(exc)
    if token:
        text = text.replace(token, "***")
    lowered = text.lower()
    if "too big" in lowered or "file is too big" in lowered:
        return (
            "Файл больше 20 МБ — Telegram Bot API не отдаёт такие файлы боту. "
            "Сожмите PDF и отправьте снова."
        )
    if "timed out" in lowered or "timeout" in lowered:
        return "Таймаут при скачивании из Telegram. Попробуйте ещё раз."
    if "forbidden" in lowered or " 403" in lowered or lowered.endswith("403"):
        return "Telegram отклонил скачивание (403). Перешлите файл ещё раз."
    if "not found" in lowered or " 404" in lowered:
        return "Telegram не нашёл файл (404). Отправьте PDF заново, не пересылкой из другого чата."
    return f"Не удалось скачать файл: {text[:280]}"


def _file_path_from_info(info: Any) -> str | None:
    if info is None:
        return None
    path = getattr(info, "file_path", None)
    if path:
        return str(path)
    if isinstance(info, dict):
        path = info.get("file_path")
        return str(path) if path else None
    return None


def download_telegram_file(
    bot: Any,
    file_id: str,
    *,
    token: str,
    file_size: int | None = None,
) -> bytes:
    """Fetch file bytes via TeleBot, with an HTTP fallback that URL-encodes the path."""
    if file_size and file_size > TELEGRAM_BOT_FILE_LIMIT:
        raise TelegramDownloadError(
            "Файл больше 20 МБ — Telegram не даёт боту его скачать. "
            "Сожмите PDF и отправьте снова."
        )

    try:
        info = bot.get_file(file_id)
    except Exception as exc:
        raise TelegramDownloadError(format_download_error(exc, token)) from exc

    file_path = _file_path_from_info(info)
    if not file_path:
        raise TelegramDownloadError(
            "Telegram не вернул путь к файлу. Отправьте PDF ещё раз."
        )

    try:
        data = bot.download_file(file_path)
        if data:
            return data
    except Exception:
        logger.exception("bot.download_file failed for %s", file_path)

    encoded = quote(file_path, safe="/")
    url = API_FILE_BASE.format(token=token, path=encoded)
    try:
        resp = requests.get(
            url,
            timeout=120,
            headers={"User-Agent": "CursorTelegramBot/1.0"},
        )
    except Exception as exc:
        raise TelegramDownloadError(format_download_error(exc, token)) from exc

    if resp.status_code != 200:
        raise TelegramDownloadError(
            format_download_error(
                RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}"),
                token,
            )
        )
    if not resp.content:
        raise TelegramDownloadError("Telegram вернул пустой файл.")
    return resp.content
