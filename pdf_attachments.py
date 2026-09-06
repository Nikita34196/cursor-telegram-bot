"""Parse Telegram PDF documents so they can be passed to Cursor Cloud Agents.

The Cloud Agents API accepts images on the prompt, not binary PDFs. This
module extracts text and renders page screenshots so the agent receives the
document contents.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field

import pymupdf

MAX_PDF_BYTES = 80 * 1024 * 1024  # after Telegram or public URL download
MAX_PDF_TEXT_CHARS = 80_000
MAX_PDF_PAGES_FOR_TEXT = 80
MAX_PROMPT_CHARS = 100_000
MAX_PAGE_IMAGES = 5
MAX_IMAGE_BYTES = 15 * 1024 * 1024
PDF_MIMES = frozenset({"application/pdf", "application/x-pdf"})
RENDER_ZOOMS = (1.6, 1.2, 0.9, 0.6)

PDF_ONLY_PROMPT = (
    "Прочитай приложенный PDF и выполни задачу по его содержимому."
)


class PdfAttachmentError(Exception):
    """User-facing PDF processing error."""


@dataclass
class ParsedPdf:
    filename: str
    page_count: int
    size_bytes: int
    text: str
    images: list[dict[str, str]] = field(default_factory=list)
    warning: str | None = None


def looks_like_pdf(filename: str | None, mime: str | None) -> bool:
    name = (filename or "").lower()
    mime_type = (mime or "").split(";", 1)[0].strip().lower()
    if mime_type in PDF_MIMES:
        return True
    return name.endswith(".pdf")


def is_pdf_bytes(data: bytes) -> bool:
    return data.lstrip().startswith(b"%PDF")


def _png_payload(png: bytes) -> dict[str, str] | None:
    if not png or len(png) > MAX_IMAGE_BYTES:
        return None
    return {
        "data": base64.b64encode(png).decode("ascii"),
        "mimeType": "image/png",
    }


def _render_page(page: pymupdf.Page) -> dict[str, str] | None:
    for zoom in RENDER_ZOOMS:
        pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
        payload = _png_payload(pix.tobytes("png"))
        if payload:
            return payload
    return None


def parse_pdf(
    data: bytes,
    filename: str,
    *,
    max_page_images: int = MAX_PAGE_IMAGES,
) -> ParsedPdf:
    if not data:
        raise PdfAttachmentError("PDF пустой.")
    if len(data) > MAX_PDF_BYTES:
        raise PdfAttachmentError(
            f"PDF больше {MAX_PDF_BYTES // (1024 * 1024)} МБ — такой файл бот не обрабатывает."
        )
    if not is_pdf_bytes(data):
        raise PdfAttachmentError("Это не PDF (нет сигнатуры %PDF).")

    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:  # noqa: BLE001 — surface any MuPDF failure
        raise PdfAttachmentError("Не удалось открыть PDF.") from exc

    try:
        if doc.is_encrypted:
            try:
                unlocked = bool(doc.authenticate(""))
            except Exception:
                unlocked = False
            if not unlocked:
                raise PdfAttachmentError("PDF защищён паролем.")

        page_count = doc.page_count
        if page_count <= 0:
            raise PdfAttachmentError("В PDF нет страниц.")

        text_parts: list[str] = []
        extracted = 0
        for index in range(min(page_count, MAX_PDF_PAGES_FOR_TEXT)):
            page_text = (doc.load_page(index).get_text("text") or "").strip()
            if not page_text:
                continue
            chunk = f"--- страница {index + 1} ---\n{page_text}"
            remaining = MAX_PDF_TEXT_CHARS - extracted
            if remaining <= 0:
                break
            if len(chunk) > remaining:
                chunk = chunk[:remaining] + "\n[текст обрезан]"
            text_parts.append(chunk)
            extracted += len(chunk)

        images: list[dict[str, str]] = []
        image_limit = max(0, min(max_page_images, MAX_PAGE_IMAGES))
        for index in range(min(page_count, image_limit)):
            payload = _render_page(doc.load_page(index))
            if payload:
                images.append(payload)

        warning = None
        if not text_parts and not images:
            warning = "Не удалось извлечь текст и изображения из PDF."
        elif not text_parts:
            warning = "Текстового слоя почти нет — переданы скриншоты страниц."

        return ParsedPdf(
            filename=filename or "document.pdf",
            page_count=page_count,
            size_bytes=len(data),
            text="\n\n".join(text_parts),
            images=images,
            warning=warning,
        )
    finally:
        doc.close()


def format_pdf_for_prompt(parsed: ParsedPdf) -> str:
    size_kb = max(1, parsed.size_bytes // 1024)
    header = (
        f"Пользователь приложил PDF-файл «{parsed.filename}» "
        f"({parsed.page_count} стр., {size_kb} КБ).\n"
        "Ниже извлечённый текст. Скриншоты страниц переданы как изображения промпта Cursor."
    )
    if parsed.warning:
        header += f"\nПримечание: {parsed.warning}"
    if parsed.text.strip():
        return f"{header}\n\n--- {parsed.filename} ---\n{parsed.text}"
    return header


def merge_into_prompt(
    prompt: str,
    pdfs: list[ParsedPdf],
    images: list[dict[str, str]] | None = None,
    *,
    max_images: int = MAX_PAGE_IMAGES,
) -> tuple[str, list[dict[str, str]]]:
    """Combine user text, queued photos, and parsed PDFs for Cursor."""
    out_images = list(images or [])[:max_images]
    slots = max(0, max_images - len(out_images))
    blocks = [format_pdf_for_prompt(pdf) for pdf in pdfs]
    for pdf in pdfs:
        take = pdf.images[:slots]
        slots -= len(take)
        out_images.extend(take)

    text = (prompt or "").strip()
    if not text and pdfs:
        text = PDF_ONLY_PROMPT
    if blocks:
        extra = "\n\n".join(blocks)
        text = f"{text}\n\n{extra}" if text else extra
    if len(text) > MAX_PROMPT_CHARS:
        text = text[:MAX_PROMPT_CHARS] + "\n\n[текст PDF обрезан]"
    return text, out_images
