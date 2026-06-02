"""CV text extraction — PDF and DOCX, with explicit unparseable handling.

The contract: ALWAYS return a ParseResult, never raise. If the file is a scanned
image PDF, password-protected, corrupt, or an unsupported type, ParseResult.ok is
False with a human reason — the upload flow then offers a manual-paste fallback.
We never feed garbage text to the scorer.

         file bytes ──▶ extract_text(name, data) ──▶ ParseResult
                                │
                 ┌──────────────┼───────────────┐
               .pdf           .docx          other
            pypdf text    python-docx text  ok=False
                │                │           "unsupported"
          empty? encrypted? corrupt? ──▶ ok=False + reason
"""

from __future__ import annotations

import io
from dataclasses import dataclass

PDF_PARSER = "pypdf-v1"
DOCX_PARSER = "python-docx-v1"


@dataclass
class ParseResult:
    text: str
    parser: str
    ok: bool
    reason: str = ""


def extract_text(filename: str, data: bytes) -> ParseResult:
    ext = (filename or "").lower().rsplit(".", 1)[-1] if "." in (filename or "") else ""
    if ext == "pdf":
        return _extract_pdf(data)
    if ext in {"docx"}:
        return _extract_docx(data)
    return ParseResult(
        text="", parser="", ok=False,
        reason=f"unsupported file type '.{ext}' (expected .pdf or .docx)",
    )


def _extract_pdf(data: bytes) -> ParseResult:
    try:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError

        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            # Try empty-password decrypt; if it fails, it's truly locked.
            try:
                if reader.decrypt("") == 0:  # 0 == failed
                    return ParseResult("", PDF_PARSER, ok=False,
                                       reason="password-protected PDF")
            except Exception:
                return ParseResult("", PDF_PARSER, ok=False,
                                   reason="password-protected PDF")

        text = "\n".join((page.extract_text() or "") for page in reader.pages).strip()
    except PdfReadError:
        return ParseResult("", PDF_PARSER, ok=False, reason="corrupt or unreadable PDF")
    except Exception as exc:  # defensive: never let a bad file crash ingestion
        return ParseResult("", PDF_PARSER, ok=False, reason=f"unreadable PDF: {exc}")

    if not text:
        return ParseResult("", PDF_PARSER, ok=False,
                           reason="no extractable text (scanned image?)")
    return ParseResult(text, PDF_PARSER, ok=True)


def _extract_docx(data: bytes) -> ParseResult:
    try:
        import docx  # python-docx

        document = docx.Document(io.BytesIO(data))
        text = "\n".join(p.text for p in document.paragraphs).strip()
    except Exception as exc:
        return ParseResult("", DOCX_PARSER, ok=False, reason=f"unreadable DOCX: {exc}")

    if not text:
        return ParseResult("", DOCX_PARSER, ok=False, reason="no extractable text")
    return ParseResult(text, DOCX_PARSER, ok=True)
