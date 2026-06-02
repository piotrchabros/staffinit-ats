"""Lane C tests — CV ingestion (PDF/DOCX + unparseable fallback) and JD extraction.

Real PDFs/DOCX are generated in-test (fpdf2 / python-docx) so extraction is
exercised end to end, not mocked. JD extraction uses a fake Claude client.
"""

from __future__ import annotations

import io
import tempfile
import types

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from ats.ingestion.ingest import ingest_cv_file, ingest_pasted_cv
from ats.ingestion.parse import extract_text
from ats.models import Candidate, Role
from ats.scoring.jd_extract import (
    JDExtractionError,
    JDExtractionService,
    extract_requirements,
)

MEDIA_TMP = tempfile.mkdtemp()


def make_docx(text: str) -> bytes:
    import docx

    document = docx.Document()
    document.add_paragraph(text)
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


def make_pdf(text: str | None) -> bytes:
    from fpdf import FPDF

    pdf = FPDF()
    pdf.add_page()
    if text:
        pdf.set_font("Helvetica", size=12)
        pdf.multi_cell(0, 10, text)
    return bytes(pdf.output())


# --------------------------------------------------------------------------- #
# parse.extract_text                                                          #
# --------------------------------------------------------------------------- #
class ParseTests(TestCase):
    def test_docx_happy(self):
        result = extract_text("cv.docx", make_docx("Jane Doe Senior Python Engineer"))
        self.assertTrue(result.ok)
        self.assertIn("Senior Python", result.text)
        self.assertEqual(result.parser, "python-docx-v1")

    def test_pdf_happy(self):
        result = extract_text("cv.pdf", make_pdf("John Smith AWS Backend"))
        self.assertTrue(result.ok)
        self.assertIn("AWS Backend", result.text)
        self.assertEqual(result.parser, "pypdf-v1")

    def test_pdf_with_no_text_is_unparseable(self):
        result = extract_text("scan.pdf", make_pdf(None))  # image-only / scanned
        self.assertFalse(result.ok)
        self.assertIn("scanned", result.reason)

    def test_corrupt_pdf_is_unparseable(self):
        result = extract_text("broken.pdf", b"%PDF-1.4 this is not a real pdf")
        self.assertFalse(result.ok)
        self.assertTrue(result.reason)

    def test_unsupported_extension(self):
        result = extract_text("resume.txt", b"plain text")
        self.assertFalse(result.ok)
        self.assertIn("unsupported", result.reason)


# --------------------------------------------------------------------------- #
# ingest                                                                      #
# --------------------------------------------------------------------------- #
@override_settings(MEDIA_ROOT=MEDIA_TMP)
class IngestTests(TestCase):
    def setUp(self):
        self.cand = Candidate.objects.create(full_name="A", email="a@x.com")

    def test_ingest_parseable_file_fills_text(self):
        upload = SimpleUploadedFile(
            "cv.docx", make_docx("Senior Python Engineer 8 years"),
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        cv, result = ingest_cv_file(self.cand, upload)
        self.assertTrue(result.ok)
        self.assertIn("Senior Python", cv.parsed_text)
        self.assertFalse(cv.needs_manual_text)
        self.assertTrue(cv.raw_file)  # file stored

    def test_ingest_unparseable_file_needs_manual_text(self):
        upload = SimpleUploadedFile("scan.pdf", make_pdf(None), content_type="application/pdf")
        cv, result = ingest_cv_file(self.cand, upload)
        self.assertFalse(result.ok)
        self.assertEqual(cv.parsed_text, "")
        self.assertTrue(cv.needs_manual_text)  # file attached, no text -> prompt paste
        self.assertTrue(cv.raw_file)

    def test_ingest_pasted_cv(self):
        cv = ingest_pasted_cv(self.cand, "  Pasted CV text  ")
        self.assertEqual(cv.parsed_text, "Pasted CV text")
        self.assertEqual(cv.parser_version, "manual-paste")
        self.assertFalse(cv.needs_manual_text)  # no file, has text


# --------------------------------------------------------------------------- #
# JD extraction                                                               #
# --------------------------------------------------------------------------- #
def jd_response(tool_input, *, include_tool=True):
    blocks = []
    if include_tool:
        blocks.append(types.SimpleNamespace(type="tool_use", name="submit_requirements",
                                            input=tool_input))
    else:
        blocks.append(types.SimpleNamespace(type="text", text="no tool"))
    return types.SimpleNamespace(content=blocks, model="claude-sonnet-4-6",
                                 usage=types.SimpleNamespace(input_tokens=1, output_tokens=1))


class _FakeMessages:
    def __init__(self, response):
        self._response = response

    def create(self, **kwargs):
        return self._response


class FakeClient:
    def __init__(self, response):
        self.messages = _FakeMessages(response)


GOOD_JD = {
    "must_have": ["Python", "AWS"],
    "nice_to_have": ["Leadership"],
    "min_years_experience": 5,
    "location": "Remote",
    "summary": "Senior backend engineer",
}


class JDExtractionTests(TestCase):
    def test_extracts_and_normalizes(self):
        svc = JDExtractionService(client=FakeClient(jd_response(GOOD_JD)))
        out = svc.extract("Senior Backend Engineer (Python/AWS)...")
        self.assertEqual(out["must_have"], ["Python", "AWS"])
        self.assertEqual(out["min_years_experience"], 5)
        self.assertEqual(out["location"], "Remote")

    def test_empty_jd_raises(self):
        svc = JDExtractionService(client=FakeClient(jd_response(GOOD_JD)))
        with self.assertRaises(JDExtractionError):
            svc.extract("   ")

    def test_missing_tool_raises(self):
        svc = JDExtractionService(client=FakeClient(jd_response(GOOD_JD, include_tool=False)))
        with self.assertRaises(JDExtractionError):
            svc.extract("a real jd")

    def test_extract_requirements_stores_on_role(self):
        role = Role.objects.create(title="Backend", jd_text="Python/AWS role")
        svc = JDExtractionService(client=FakeClient(jd_response(GOOD_JD)))
        extract_requirements(role, service=svc)
        role.refresh_from_db()
        self.assertEqual(role.structured_requirements["must_have"], ["Python", "AWS"])

    def test_extract_requirements_best_effort_on_failure(self):
        # Malformed model output must NOT block role creation; stores {}.
        role = Role.objects.create(title="Backend", jd_text="Python/AWS role")
        svc = JDExtractionService(client=FakeClient(jd_response(GOOD_JD, include_tool=False)))
        result = extract_requirements(role, service=svc)
        self.assertEqual(result, {})
        role.refresh_from_db()
        self.assertEqual(role.structured_requirements, {})
