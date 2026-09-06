from __future__ import annotations

import unittest
from types import SimpleNamespace

from telegram_files import (
    TelegramDownloadError,
    download_telegram_file,
    format_download_error,
)


class FakeBot:
    def __init__(
        self,
        *,
        path: str | None = "documents/brief.pdf",
        payload: bytes = b"%PDF-1.4 demo",
        get_error: Exception | None = None,
        download_error: Exception | None = None,
    ) -> None:
        self.path = path
        self.payload = payload
        self.get_error = get_error
        self.download_error = download_error
        self.downloaded_path = None

    def get_file(self, file_id: str):
        if self.get_error:
            raise self.get_error
        return SimpleNamespace(file_path=self.path, file_id=file_id)

    def download_file(self, file_path: str) -> bytes:
        self.downloaded_path = file_path
        if self.download_error:
            raise self.download_error
        return self.payload


class FormatErrorTests(unittest.TestCase):
    def test_too_big(self) -> None:
        msg = format_download_error(RuntimeError("Bad Request: file is too big"))
        self.assertIn("20 МБ", msg)

    def test_redacts_token(self) -> None:
        token = "123456:secret-token"
        msg = format_download_error(RuntimeError(f"GET bot{token}/file failed"), token)
        self.assertNotIn("secret-token", msg)
        self.assertIn("***", msg)


class DownloadTests(unittest.TestCase):
    def test_uses_telebot_download(self) -> None:
        bot = FakeBot(payload=b"%PDF-1.4 ok")
        data = download_telegram_file(bot, "file-id", token="tok")
        self.assertEqual(data, b"%PDF-1.4 ok")
        self.assertEqual(bot.downloaded_path, "documents/brief.pdf")

    def test_rejects_oversize_before_api(self) -> None:
        bot = FakeBot()
        with self.assertRaises(TelegramDownloadError) as ctx:
            download_telegram_file(
                bot, "file-id", token="tok", file_size=21 * 1024 * 1024
            )
        self.assertIn("20 МБ", ctx.exception.user_message)

    def test_missing_path(self) -> None:
        bot = FakeBot(path=None)
        with self.assertRaises(TelegramDownloadError) as ctx:
            download_telegram_file(bot, "file-id", token="tok")
        self.assertIn("путь", ctx.exception.user_message.lower())

    def test_get_file_too_big(self) -> None:
        bot = FakeBot(get_error=RuntimeError("Bad Request: file is too big"))
        with self.assertRaises(TelegramDownloadError) as ctx:
            download_telegram_file(bot, "file-id", token="tok")
        self.assertIn("20 МБ", ctx.exception.user_message)

    def test_http_fallback_when_telebot_download_fails(self) -> None:
        bot = FakeBot(download_error=RuntimeError("boom"), path="documents/ТЗ проект.pdf")
        called = {}

        def fake_get(url, timeout, headers):
            called["url"] = url
            return SimpleNamespace(status_code=200, content=b"%PDF-1.4 fallback", text="")

        import telegram_files

        original = telegram_files.requests.get
        telegram_files.requests.get = fake_get  # type: ignore[method-assign]
        try:
            data = download_telegram_file(bot, "file-id", token="123:abc")
        finally:
            telegram_files.requests.get = original  # type: ignore[method-assign]

        self.assertEqual(data, b"%PDF-1.4 fallback")
        self.assertIn("/file/bot123:abc/", called["url"])
        self.assertIn("documents/", called["url"])
        self.assertNotIn(" ", called["url"])
        self.assertIn("%D0%A2%D0%97", called["url"])


if __name__ == "__main__":
    unittest.main()
