"""Fetch PDFs from public URLs when Telegram's 20 MB Bot API limit blocks chat files."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urlparse

import requests

from pdf_attachments import is_pdf_bytes

MAX_URL_PDF_BYTES = 80 * 1024 * 1024
USER_AGENT = (
    "Mozilla/5.0 (compatible; CursorTelegramBot/1.0; +https://github.com/Nikita34196/cursor-telegram-bot)"
)
# OneDrive blocks some bot UAs on the share-redeem flow.
ONEDRIVE_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:135.0) Gecko/20100101 Firefox/135.0"
)
URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
DRIVE_FILE_RE = re.compile(
    r"https?://(?:drive|docs)\.google\.com/(?:file/d/|open\?id=|uc\?.*?id=)([a-zA-Z0-9_-]+)",
    re.IGNORECASE,
)
DRIVE_ID_RE = re.compile(r"(?:/file/d/|[?&]id=)([a-zA-Z0-9_-]+)", re.IGNORECASE)

# Browser app id used by unauthenticated OneDrive / SharePoint personal shares.
ONEDRIVE_BADGER_APP_ID = "1141147648"
ONEDRIVE_BADGER_APP_UUID = "5cbed6ac-a083-4e14-b191-b4ba07653de2"
ONEDRIVE_BADGER_URL = "https://api-badgerp.svc.ms/v1.0/token"
ONEDRIVE_PERSONAL_SHARE_API = "https://my.microsoftpersonalcontent.com/_api/v2.0/shares"

TOO_BIG_FOR_TELEGRAM = (
    "Этот файл больше 20 МБ — Telegram не отдаёт такие вложения боту "
    "(лимит Bot API).\n\n"
    "Отправьте публичную ссылку на PDF — бот скачает его сам (до 80 МБ):\n"
    "• Google Drive (доступ «любой, у кого есть ссылка»)\n"
    "• OneDrive (1drv.ms)\n"
    "• Dropbox\n"
    "• Яндекс Диск\n"
    "• прямой URL на файл .pdf\n\n"
    "Пример: сделай конспект https://drive.google.com/file/d/…/view"
)


class PdfUrlError(Exception):
    """User-facing URL download failure."""


@dataclass
class DownloadedPdf:
    data: bytes
    filename: str
    source_url: str


def extract_urls(text: str) -> list[str]:
    found: list[str] = []
    for raw in URL_RE.findall(text or ""):
        cleaned = raw.rstrip(").,]>\"'")
        if cleaned and cleaned not in found:
            found.append(cleaned)
    return found


def _host(url: str) -> str:
    return urlparse(url).netloc.lower()


def is_onedrive_url(url: str) -> bool:
    host = _host(url)
    if host == "1drv.ms" or host.endswith(".1drv.ms"):
        return True
    if "onedrive.live.com" in host or host.endswith("1drv.ws"):
        return True
    if "sharepoint.com" in host:
        return True
    if "microsoftpersonalcontent.com" in host:
        return True
    return False


def looks_like_pdf_url(url: str) -> bool:
    low = url.lower()
    path = urlparse(url).path.lower()
    host = urlparse(url).netloc.lower()
    if path.endswith(".pdf") or ".pdf?" in low:
        return True
    if "drive.google.com" in host or "docs.google.com" in host:
        return bool(DRIVE_ID_RE.search(url))
    if "dropbox.com" in host or "dropboxusercontent.com" in host:
        return True
    if "disk.yandex." in host or host.endswith("yadi.sk") or "yadi.sk" in host:
        return True
    if is_onedrive_url(url):
        return True
    return False


def extract_pdf_urls(text: str) -> list[str]:
    return [url for url in extract_urls(text) if looks_like_pdf_url(url)]


def strip_urls(text: str, urls: list[str]) -> str:
    leftover = text or ""
    for url in urls:
        leftover = leftover.replace(url, " ")
    return re.sub(r"\s+", " ", leftover).strip()


def drive_file_id(url: str) -> str | None:
    match = DRIVE_FILE_RE.search(url) or DRIVE_ID_RE.search(url)
    if not match:
        return None
    if "drive.google.com" not in url.lower() and "docs.google.com" not in url.lower():
        return None
    return match.group(1)


def normalize_pdf_url(url: str) -> str:
    file_id = drive_file_id(url)
    if file_id:
        return f"https://drive.google.com/uc?export=download&id={file_id}&confirm=t"
    host = urlparse(url).netloc.lower()
    if "dropbox.com" in host:
        if "dl=" in url.lower():
            return re.sub(r"([?&])dl=0", r"\1dl=1", url, flags=re.IGNORECASE)
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}dl=1"
    return url


def _filename_from_response(resp: requests.Response, fallback: str) -> str:
    cd = resp.headers.get("Content-Disposition") or ""
    star = re.search(r"filename\*=UTF-8''([^;]+)", cd, re.IGNORECASE)
    if star:
        name = unquote(star.group(1).strip().strip('"'))
        return name or fallback
    quoted = re.search(r'filename="([^"]+)"', cd, re.IGNORECASE)
    if quoted:
        return quoted.group(1)
    plain = re.search(r"filename=([^;]+)", cd, re.IGNORECASE)
    if plain:
        return plain.group(1).strip().strip('"')
    path = urlparse(resp.url).path
    if path.lower().endswith(".pdf"):
        return unquote(path.rsplit("/", 1)[-1]) or fallback
    return fallback


@dataclass
class _OneDriveShare:
    redeem: str = ""
    cid: str = ""
    resid: str = ""
    authkey: str = ""


def _onedrive_share_from_url(url: str) -> _OneDriveShare:
    query = parse_qs(urlparse(url).query)
    redeem = (query.get("redeem") or [""])[0]
    cid = (query.get("cid") or [""])[0]
    resid = (query.get("resid") or query.get("id") or [""])[0]
    authkey = (query.get("authkey") or [""])[0]
    if not resid and "!" in (query.get("id") or [""])[0]:
        resid = query["id"][0]
    if not cid and "!" in resid:
        cid = resid.split("!", 1)[0]
    return _OneDriveShare(redeem=redeem, cid=cid, resid=resid, authkey=authkey)


def _onedrive_badger_token(session: requests.Session) -> str | None:
    try:
        resp = session.post(
            ONEDRIVE_BADGER_URL,
            headers={"AppId": ONEDRIVE_BADGER_APP_ID, "Content-Type": "application/json"},
            json={"appId": ONEDRIVE_BADGER_APP_UUID},
            timeout=30,
        )
    except requests.RequestException:
        return None
    if not resp.ok:
        return None
    try:
        token = resp.json().get("token")
    except ValueError:
        return None
    return token if isinstance(token, str) and token else None


def _onedrive_item_via_redeem(session: requests.Session, redeem: str, token: str) -> dict | None:
    try:
        resp = session.get(
            f"{ONEDRIVE_PERSONAL_SHARE_API}/u!{redeem}/driveitem",
            headers={"Authorization": f"Badger {token}", "Prefer": "autoredeem"},
            timeout=30,
        )
    except requests.RequestException:
        return None
    if not resp.ok:
        return None
    try:
        payload = resp.json()
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _onedrive_item_via_authkey(session: requests.Session, share: _OneDriveShare) -> dict | None:
    if not (share.cid and share.resid):
        return None
    params = {}
    if share.authkey:
        params["authkey"] = share.authkey
    try:
        resp = session.get(
            f"https://api.onedrive.com/v1.0/drives/{share.cid}/items/{share.resid}",
            params=params or None,
            timeout=30,
        )
    except requests.RequestException:
        return None
    if not resp.ok:
        return None
    try:
        payload = resp.json()
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _download_onedrive_pdf(
    url: str,
    session: requests.Session,
    *,
    max_bytes: int,
) -> DownloadedPdf:
    session.headers.update({"User-Agent": ONEDRIVE_USER_AGENT})
    try:
        landing = session.get(url, timeout=60, allow_redirects=True)
    except requests.RequestException as exc:
        raise PdfUrlError(f"Не удалось открыть ссылку OneDrive: {exc}") from exc

    if is_pdf_bytes(landing.content):
        filename = _filename_from_response(landing, "document.pdf")
        if len(landing.content) > max_bytes:
            raise PdfUrlError(
                f"PDF по ссылке больше {max_bytes // (1024 * 1024)} МБ — это лимит бота на скачивание."
            )
        if not filename.lower().endswith(".pdf"):
            filename = f"{filename}.pdf"
        return DownloadedPdf(data=landing.content, filename=filename, source_url=url)

    share = _onedrive_share_from_url(landing.url)
    if not share.redeem and not share.resid:
        share = _onedrive_share_from_url(url)

    item: dict | None = None
    if share.redeem:
        token = _onedrive_badger_token(session)
        if token:
            item = _onedrive_item_via_redeem(session, share.redeem, token)
    if item is None:
        item = _onedrive_item_via_authkey(session, share)

    download_url = ""
    filename = "document.pdf"
    if item:
        raw_name = item.get("name")
        if isinstance(raw_name, str) and raw_name.strip():
            filename = raw_name.strip()
        raw_dl = item.get("@content.downloadUrl")
        if isinstance(raw_dl, str) and raw_dl.startswith("http"):
            download_url = raw_dl

    if not download_url:
        raise PdfUrlError(
            "Не удалось скачать PDF с OneDrive. Сделайте ссылку публичной "
            "(доступ «любой, у кого есть ссылка») и отправьте её ещё раз."
        )

    try:
        resp = session.get(download_url, timeout=180, stream=True, allow_redirects=True)
        resp.raise_for_status()
        data = _read_limited(resp, max_bytes)
        filename = _filename_from_response(resp, filename)
    except PdfUrlError:
        raise
    except requests.RequestException as exc:
        raise PdfUrlError(f"Не удалось скачать PDF с OneDrive: {exc}") from exc

    if not is_pdf_bytes(data):
        raise PdfUrlError(
            "По ссылке OneDrive пришёл не PDF. Нужна публичная ссылка именно на PDF-файл."
        )
    if not filename.lower().endswith(".pdf"):
        filename = f"{filename}.pdf"
    return DownloadedPdf(data=data, filename=filename, source_url=url)


def _yandex_direct_url(public_url: str, session: requests.Session) -> str | None:
    try:
        resp = session.get(
            "https://cloud-api.yandex.net/v1/disk/public/resources/download",
            params={"public_key": public_url},
            timeout=30,
        )
    except requests.RequestException:
        return None
    if not resp.ok:
        return None
    try:
        href = resp.json().get("href")
    except ValueError:
        return None
    return href if isinstance(href, str) and href.startswith("http") else None


def _read_limited(resp: requests.Response, max_bytes: int) -> bytes:
    length = resp.headers.get("Content-Length")
    if length and length.isdigit() and int(length) > max_bytes:
        raise PdfUrlError(
            f"PDF по ссылке больше {max_bytes // (1024 * 1024)} МБ — это лимит бота на скачивание."
        )
    chunks: list[bytes] = []
    total = 0
    for chunk in resp.iter_content(64 * 1024):
        if not chunk:
            continue
        total += len(chunk)
        if total > max_bytes:
            raise PdfUrlError(
                f"PDF по ссылке больше {max_bytes // (1024 * 1024)} МБ — это лимит бота на скачивание."
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _is_html(data: bytes, content_type: str) -> bool:
    ctype = (content_type or "").lower()
    if "text/html" in ctype:
        return True
    head = data.lstrip()[:32].lower()
    return head.startswith(b"<!doctype") or head.startswith(b"<html")


def _drive_confirm_retry(session: requests.Session, html: bytes, file_id: str) -> bytes | None:
    confirm = re.search(br"confirm=([0-9A-Za-z_-]+)", html)
    if not confirm:
        for cookie in session.cookies:
            if cookie.name.startswith("download_warning"):
                confirm_val = cookie.value
                break
        else:
            return None
    else:
        confirm_val = confirm.group(1).decode("ascii")
    uuid_m = re.search(br'name="uuid"\s+value="([^"]+)"', html)
    params = {"export": "download", "id": file_id, "confirm": confirm_val}
    if uuid_m:
        params["uuid"] = uuid_m.group(1).decode("utf-8", errors="replace")
    resp = session.get(
        "https://drive.google.com/uc",
        params=params,
        headers={"User-Agent": USER_AGENT},
        timeout=180,
        stream=True,
        allow_redirects=True,
    )
    resp.raise_for_status()
    data = _read_limited(resp, MAX_URL_PDF_BYTES)
    return data if is_pdf_bytes(data) else None


def download_pdf_from_url(url: str, *, max_bytes: int = MAX_URL_PDF_BYTES) -> DownloadedPdf:
    if not url.lower().startswith(("http://", "https://")):
        raise PdfUrlError("Нужна ссылка http(s) на PDF.")

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    if is_onedrive_url(url):
        return _download_onedrive_pdf(url, session, max_bytes=max_bytes)

    target = normalize_pdf_url(url)
    host = urlparse(url).netloc.lower()
    if "disk.yandex." in host or "yadi.sk" in host:
        direct = _yandex_direct_url(url, session)
        if direct:
            target = direct

    try:
        resp = session.get(target, timeout=180, stream=True, allow_redirects=True)
        resp.raise_for_status()
        data = _read_limited(resp, max_bytes)
        filename = _filename_from_response(resp, "document.pdf")
        content_type = resp.headers.get("Content-Type", "")
    except PdfUrlError:
        raise
    except requests.RequestException as exc:
        raise PdfUrlError(f"Не удалось скачать PDF по ссылке: {exc}") from exc

    file_id = drive_file_id(url)
    if file_id and _is_html(data, content_type):
        retry = _drive_confirm_retry(session, data, file_id)
        if retry:
            data = retry
            filename = filename if filename.lower().endswith(".pdf") else f"{file_id}.pdf"

    if not is_pdf_bytes(data):
        raise PdfUrlError(
            "По ссылке пришёл не PDF. Для Google Drive / OneDrive включите доступ "
            "«любой, у кого есть ссылка», либо пришлите прямой URL на .pdf."
        )
    if not filename.lower().endswith(".pdf"):
        filename = f"{filename}.pdf"
    return DownloadedPdf(data=data, filename=filename, source_url=url)
