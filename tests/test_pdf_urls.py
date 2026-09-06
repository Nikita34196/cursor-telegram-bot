from __future__ import annotations

import unittest

from pdf_urls import (
    PdfUrlError,
    _onedrive_share_from_url,
    download_pdf_from_url,
    extract_pdf_urls,
    is_onedrive_url,
    looks_like_pdf_url,
    normalize_pdf_url,
    strip_urls,
)


class UrlDetectTests(unittest.TestCase):
    def test_direct_pdf(self) -> None:
        url = "https://example.com/docs/spec.pdf"
        self.assertTrue(looks_like_pdf_url(url))
        self.assertEqual(extract_pdf_urls(f"сделай конспект {url} пожалуйста"), [url])

    def test_google_drive(self) -> None:
        url = "https://drive.google.com/file/d/abc123XYZ/view?usp=sharing"
        self.assertTrue(looks_like_pdf_url(url))
        self.assertEqual(
            normalize_pdf_url(url),
            "https://drive.google.com/uc?export=download&id=abc123XYZ&confirm=t",
        )

    def test_dropbox(self) -> None:
        url = "https://www.dropbox.com/s/xx/file.pdf?dl=0"
        self.assertIn("dl=1", normalize_pdf_url(url))

    def test_strips_url_from_prompt(self) -> None:
        url = "https://example.com/a.pdf"
        leftover = strip_urls(f"сделай конспект {url}", [url])
        self.assertEqual(leftover, "сделай конспект")

    def test_ignores_unrelated_links(self) -> None:
        self.assertFalse(looks_like_pdf_url("https://github.com/foo/bar"))
        self.assertEqual(extract_pdf_urls("смотри https://github.com/foo/bar"), [])

    def test_onedrive_short_link(self) -> None:
        url = (
            "https://1drv.ms/b/c/0dc74361cfc7e918/"
            "IQBbHsc-fEemTq7piS7hwCXeAR9g2Li6KLxBdkzxG27RSZ0?e=4UruYh"
        )
        self.assertTrue(is_onedrive_url(url))
        self.assertTrue(looks_like_pdf_url(url))
        self.assertEqual(extract_pdf_urls(f"пришли первый вариант {url}"), [url])

    def test_onedrive_live_and_sharepoint(self) -> None:
        live = (
            "https://onedrive.live.com/?cid=0dc74361cfc7e918"
            "&resid=0DC74361CFC7E918!s3ec71e5b477c4ea6aee9892ee1c025de"
            "&ithint=file,pdf"
        )
        spo = "https://contoso-my.sharepoint.com/:b:/g/personal/user/abc123"
        self.assertTrue(looks_like_pdf_url(live))
        self.assertTrue(looks_like_pdf_url(spo))

    def test_onedrive_share_params(self) -> None:
        url = (
            "https://onedrive.live.com/?cid=abc123"
            "&resid=ABC123!sfile"
            "&redeem=aHR0cHM6Ly8xZHJ2Lm1zL2I"
            "&migratedtospo=true"
        )
        share = _onedrive_share_from_url(url)
        self.assertEqual(share.cid, "abc123")
        self.assertEqual(share.resid, "ABC123!sfile")
        self.assertEqual(share.redeem, "aHR0cHM6Ly8xZHJ2Lm1zL2I")


class FakeResp:
    def __init__(
        self,
        content: bytes,
        content_type: str,
        filename: str | None = None,
        url: str = "https://cdn.example.com/brief.pdf",
        json_data: dict | None = None,
    ) -> None:
        self.status_code = 200
        self.headers = {"Content-Type": content_type}
        if filename:
            self.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
        self.url = url
        self.content = content
        self._json = json_data
        self.ok = True

    def raise_for_status(self) -> None:
        return None

    def json(self):
        if self._json is not None:
            return self._json
        raise ValueError("no json")

    def iter_content(self, _size: int):
        yield self.content


class StubSession:
    def __init__(self, resp: FakeResp) -> None:
        self.headers: dict = {}
        self.cookies: list = []
        self._resp = resp

    def get(self, url, **_kwargs):
        return self._resp

    def post(self, url, **_kwargs):
        return self._resp


class RoutingSession:
    def __init__(self, routes: dict[str, FakeResp]) -> None:
        self.headers: dict = {}
        self.cookies: list = []
        self.routes = routes

    def _match(self, url: str) -> FakeResp:
        for key, resp in self.routes.items():
            if key in url:
                return resp
        raise AssertionError(f"unexpected url: {url}")

    def get(self, url, **_kwargs):
        return self._match(url)

    def post(self, url, **_kwargs):
        return self._match(url)


class DownloadUrlTests(unittest.TestCase):
    def test_downloads_pdf_bytes(self) -> None:
        import pdf_urls

        payload = b"%PDF-1.4 fake"
        original = pdf_urls.requests.Session
        pdf_urls.requests.Session = lambda: StubSession(  # type: ignore[assignment]
            FakeResp(payload, "application/pdf", "brief.pdf")
        )
        try:
            result = download_pdf_from_url("https://example.com/brief.pdf")
        finally:
            pdf_urls.requests.Session = original  # type: ignore[assignment]
        self.assertEqual(result.data, payload)
        self.assertEqual(result.filename, "brief.pdf")

    def test_rejects_html(self) -> None:
        import pdf_urls

        original = pdf_urls.requests.Session
        pdf_urls.requests.Session = lambda: StubSession(  # type: ignore[assignment]
            FakeResp(b"<html>login</html>", "text/html")
        )
        try:
            with self.assertRaises(PdfUrlError):
                download_pdf_from_url("https://example.com/brief.pdf")
        finally:
            pdf_urls.requests.Session = original  # type: ignore[assignment]

    def test_downloads_onedrive_via_redeem(self) -> None:
        import pdf_urls

        payload = b"%PDF-1.4 onedrive"
        share_url = "https://1drv.ms/b/c/abc/TOKEN?e=zz"
        landing = (
            "https://onedrive.live.com/?cid=abc&resid=ABC!s1"
            "&redeem=aHR0cHM6Ly8xZHJ2Lm1zL2I&migratedtospo=true"
        )
        routes = {
            "1drv.ms": FakeResp(b"<html>onedrive</html>", "text/html", url=landing),
            "api-badgerp.svc.ms": FakeResp(
                b'{"token":"t"}',
                "application/json",
                json_data={"token": "badger-token"},
            ),
            "microsoftpersonalcontent.com/_api": FakeResp(
                b"{}",
                "application/json",
                json_data={
                    "name": "ОГЭ Английский.pdf",
                    "@content.downloadUrl": "https://cdn.example.com/file.bin",
                },
            ),
            "cdn.example.com/file.bin": FakeResp(payload, "application/pdf", "english.pdf"),
        }
        original = pdf_urls.requests.Session
        pdf_urls.requests.Session = lambda: RoutingSession(routes)  # type: ignore[assignment]
        try:
            result = download_pdf_from_url(share_url)
        finally:
            pdf_urls.requests.Session = original  # type: ignore[assignment]
        self.assertEqual(result.data, payload)
        self.assertEqual(result.filename, "english.pdf")

    def test_onedrive_rejects_non_pdf(self) -> None:
        import pdf_urls

        share_url = "https://1drv.ms/b/c/abc/TOKEN?e=zz"
        landing = "https://onedrive.live.com/?redeem=abc&resid=ABC!s1"
        routes = {
            "1drv.ms": FakeResp(b"<html>onedrive</html>", "text/html", url=landing),
            "api-badgerp.svc.ms": FakeResp(
                b'{"token":"t"}',
                "application/json",
                json_data={"token": "badger-token"},
            ),
            "microsoftpersonalcontent.com/_api": FakeResp(
                b"{}",
                "application/json",
                json_data={"name": "notes.docx", "@content.downloadUrl": "https://cdn.example.com/notes"},
            ),
            "cdn.example.com/notes": FakeResp(b"PK zip", "application/octet-stream"),
        }
        original = pdf_urls.requests.Session
        pdf_urls.requests.Session = lambda: RoutingSession(routes)  # type: ignore[assignment]
        try:
            with self.assertRaises(PdfUrlError):
                download_pdf_from_url(share_url)
        finally:
            pdf_urls.requests.Session = original  # type: ignore[assignment]


if __name__ == "__main__":
    unittest.main()
