from __future__ import annotations

import base64
import unittest

import pymupdf

from pdf_attachments import (
    PDF_ONLY_PROMPT,
    PdfAttachmentError,
    looks_like_pdf,
    merge_into_prompt,
    parse_pdf,
)


def make_pdf_bytes(pages: list[str], *, password: str | None = None) -> bytes:
    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 72), text, fontsize=14)
    save_kwargs: dict = {"garbage": 4, "deflate": True}
    if password:
        save_kwargs.update(
            {
                "encryption": pymupdf.PDF_ENCRYPT_AES_256,
                "user_pw": password,
                "owner_pw": password,
            }
        )
    data = doc.tobytes(**save_kwargs)
    doc.close()
    return data


class LooksLikePdfTests(unittest.TestCase):
    def test_filename(self) -> None:
        self.assertTrue(looks_like_pdf("spec.PDF", "application/octet-stream"))
        self.assertFalse(looks_like_pdf("notes.txt", "text/plain"))

    def test_mime(self) -> None:
        self.assertTrue(looks_like_pdf("file.bin", "application/pdf"))
        self.assertTrue(looks_like_pdf("file.bin", "application/pdf; charset=binary"))


class ParsePdfTests(unittest.TestCase):
    def test_extracts_text_and_page_images(self) -> None:
        data = make_pdf_bytes(["Hello from page one", "Second page content"])
        parsed = parse_pdf(data, "brief.pdf", max_page_images=2)
        self.assertEqual(parsed.filename, "brief.pdf")
        self.assertEqual(parsed.page_count, 2)
        self.assertIn("Hello from page one", parsed.text)
        self.assertIn("Second page content", parsed.text)
        self.assertEqual(len(parsed.images), 2)
        self.assertEqual(parsed.images[0]["mimeType"], "image/png")
        self.assertTrue(parsed.images[0]["data"])
        png = base64.b64decode(parsed.images[0]["data"])
        self.assertTrue(png.startswith(b"\x89PNG"))

    def test_rejects_non_pdf(self) -> None:
        with self.assertRaises(PdfAttachmentError):
            parse_pdf(b"not a pdf", "x.pdf")

    def test_rejects_password(self) -> None:
        data = make_pdf_bytes(["secret"], password="lock")
        with self.assertRaises(PdfAttachmentError) as ctx:
            parse_pdf(data, "locked.pdf")
        self.assertIn("парол", str(ctx.exception).lower())

    def test_limits_rendered_pages(self) -> None:
        data = make_pdf_bytes([f"page {i}" for i in range(6)])
        parsed = parse_pdf(data, "many.pdf", max_page_images=3)
        self.assertEqual(parsed.page_count, 6)
        self.assertEqual(len(parsed.images), 3)
        self.assertIn("page 0", parsed.text)
        self.assertIn("page 5", parsed.text)


class MergePromptTests(unittest.TestCase):
    def test_merges_pdf_into_prompt_and_fills_image_slots(self) -> None:
        data = make_pdf_bytes(["Spec section A", "Spec section B"])
        parsed = parse_pdf(data, "spec.pdf", max_page_images=5)
        existing = [{"data": "aaa", "mimeType": "image/jpeg"}]
        prompt, images = merge_into_prompt(
            "Сверстай лендинг по этому ТЗ",
            [parsed],
            existing,
            max_images=3,
        )
        self.assertIn("Сверстай лендинг по этому ТЗ", prompt)
        self.assertIn("spec.pdf", prompt)
        self.assertIn("Spec section A", prompt)
        self.assertEqual(len(images), 3)
        self.assertEqual(images[0]["mimeType"], "image/jpeg")
        self.assertEqual(images[1]["mimeType"], "image/png")

    def test_empty_prompt_uses_default(self) -> None:
        data = make_pdf_bytes(["Only the document"])
        parsed = parse_pdf(data, "doc.pdf")
        prompt, images = merge_into_prompt("", [parsed], [])
        self.assertTrue(prompt.startswith(PDF_ONLY_PROMPT))
        self.assertIn("Only the document", prompt)
        self.assertGreaterEqual(len(images), 1)


class CursorPromptTests(unittest.TestCase):
    def test_prompt_body_attaches_pdf_page_images(self) -> None:
        from cursor_client import CursorClient

        data = make_pdf_bytes(["Cursor should see this spec"])
        parsed = parse_pdf(data, "tz.pdf")
        prompt, images = merge_into_prompt("Сверстай по ТЗ", [parsed], [])
        body = CursorClient._prompt_body(prompt, images)
        self.assertIn("tz.pdf", body["text"])
        self.assertIn("Cursor should see this spec", body["text"])
        self.assertEqual(body["images"][0]["mimeType"], "image/png")
        self.assertTrue(body["images"][0]["data"])


if __name__ == "__main__":
    unittest.main()
