"""Upload validation and text extraction. Uploaded files are hostile input."""

from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from pypdf import PdfReader

from opsassist.errors import PayloadTooLarge, ValidationFailed

CONTENT_MARKDOWN = "text/markdown"
CONTENT_TEXT = "text/plain"
CONTENT_PDF = "application/pdf"

_EXTENSIONS = {
    ".md": CONTENT_MARKDOWN,
    ".markdown": CONTENT_MARKDOWN,
    ".txt": CONTENT_TEXT,
    ".pdf": CONTENT_PDF,
}
_UNSAFE_FILENAME = re.compile(r"[^\w.\- ()]+")

# pypdf logs malformed-file detail, including document fragments, through this logger.
logging.getLogger("pypdf").setLevel(logging.ERROR)


class ExtractionError(Exception):
    """The file itself is unusable. Retrying will not help. Message is shown to users."""


@dataclass(frozen=True)
class ValidatedUpload:
    filename: str
    title: str
    content_type: str


@dataclass(frozen=True)
class ExtractedPage:
    number: int
    text: str


@dataclass(frozen=True)
class ExtractedDocument:
    text: str = ""
    pages: tuple[ExtractedPage, ...] = ()


def _sanitize_filename(raw_name: str) -> str:
    name = PurePosixPath(raw_name.replace("\\", "/")).name
    name = _UNSAFE_FILENAME.sub("_", name).strip(" .")
    if not name:
        raise ValidationFailed("Filename is empty after sanitization")
    return name[:255]


def _decode_text(data: bytes) -> str:
    if b"\x00" in data:
        raise ExtractionError("File contains binary data, not text")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ExtractionError("Text files must be UTF-8 encoded") from exc


def validate_upload(raw_filename: str, data: bytes, *, max_bytes: int) -> ValidatedUpload:
    """Check name, size and content type. The extension is not trusted on its own."""
    filename = _sanitize_filename(raw_filename)
    suffix = PurePosixPath(filename).suffix.lower()
    content_type = _EXTENSIONS.get(suffix)
    if content_type is None:
        allowed = ", ".join(sorted(_EXTENSIONS))
        raise ValidationFailed(f"Unsupported file type. Allowed: {allowed}")
    if len(data) > max_bytes:
        raise PayloadTooLarge(f"File exceeds the {max_bytes} byte limit")
    if not data:
        raise ValidationFailed("File is empty")
    if content_type == CONTENT_PDF:
        if not data.startswith(b"%PDF-"):
            raise ValidationFailed("File has a .pdf extension but is not a PDF")
    else:
        try:
            _decode_text(data)
        except ExtractionError as exc:
            raise ValidationFailed(str(exc)) from exc
    return ValidatedUpload(
        filename=filename, title=PurePosixPath(filename).stem or filename, content_type=content_type
    )


def extract(content_type: str, data: bytes, *, max_pages: int, max_chars: int) -> ExtractedDocument:
    if content_type == CONTENT_PDF:
        return _extract_pdf(data, max_pages=max_pages, max_chars=max_chars)
    text = _decode_text(data).replace("\r\n", "\n").replace("\r", "\n")
    if len(text) > max_chars:
        raise ExtractionError(f"Document exceeds the {max_chars} character limit")
    if not text.strip():
        raise ExtractionError("Document contains no text")
    return ExtractedDocument(text=text)


def _extract_pdf(data: bytes, *, max_pages: int, max_chars: int) -> ExtractedDocument:
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise ExtractionError("Encrypted PDFs are not supported")
        if len(reader.pages) > max_pages:
            raise ExtractionError(f"PDF exceeds the {max_pages} page limit")
        pages: list[ExtractedPage] = []
        total = 0
        for number, page in enumerate(reader.pages, start=1):
            text = page.extract_text() or ""
            total += len(text)
            if total > max_chars:
                raise ExtractionError(f"Document exceeds the {max_chars} character limit")
            if text.strip():
                pages.append(ExtractedPage(number=number, text=text))
    except ExtractionError:
        raise
    except Exception as exc:
        # pypdf raises many exception types on malformed input. All mean an unusable file.
        raise ExtractionError("The PDF could not be parsed") from exc
    if not pages:
        raise ExtractionError("PDF has no extractable text. Scanned PDFs need OCR, not supported.")
    return ExtractedDocument(pages=tuple(pages))
