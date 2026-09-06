from __future__ import annotations

import unittest

from pdf_urls import (
    PdfUrlError,
    download_pdf_from_url,
    extract_pdf_urls,
    looks_like_pdf_url,
    normalize_pdf_url,
    onedrive_share_id,
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

    def test_onedrive(self) -> None:
        url = "https://1drv.ms/b/c/0dc74361cfc7e918/IQBbHsc-fEemTq7piS7hwCXeAR9g2Li6KLxBdkzxG27RSZ0?e=4UruYh"
        self.assertTrue(looks_like_pdf_url(url))
        self.assertEqual(extract_pdf_urls(f"конспект {url}"), [url])
        share = onedrive_share_id(url)
        self.assertTrue(share.startswith("u!"))
        self.assertNotIn("=", share)
        self.assertNotIn("/", share)

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


class FakeResp:
    def __init__(self, content: bytes, content_type: str, filename: str | None = None) -> None:
        self.status_code = 200
        self.headers = {"Content-Type": content_type}
        if filename:
            self.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
        self.url = "https://cdn.example.com/brief.pdf"
        self.content = content

    def raise_for_status(self) -> None:
        return None

    def iter_content(self, _size: int):
        yield self.content


class StubSession:
    def __init__(self, resp: FakeResp) -> None:
        self.headers: dict = {}
        self.cookies: list = []
        self._resp = resp

    def get(self, url, **_kwargs):
        return self._resp


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


if __name__ == "__main__":
    unittest.main()
