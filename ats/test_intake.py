"""Tests for background bulk-upload intake (process_upload + the task).

The web parses the CV at upload and stores the text on the CandidateUpload row;
the worker only reads that text (no filesystem access), so these tests set
parsed_text directly.
"""

from __future__ import annotations

from unittest import mock

import types

from django.test import TestCase

from procrastinate.contrib.django.models import ProcrastinateJob

from ats.models import Candidate, CandidateUpload, Role, Rubric, Score
from ats.scoring.contact import ContactExtractionService
from ats.scoring.orchestration import process_upload


class _FakeContactClient:
    def __init__(self, tool_input):
        resp = types.SimpleNamespace(
            content=[types.SimpleNamespace(type="tool_use", name="submit_contact", input=tool_input)],
            model="claude-haiku-4-5-20251001",
        )

        class _M:
            def create(self, **kw):
                return resp
        self.messages = _M()


def contact_service(tool_input):
    return ContactExtractionService(client=_FakeContactClient(tool_input))


class ProcessUploadTests(TestCase):
    def setUp(self):
        self.rubric = Rubric.objects.create(version=1, criteria=[{"name": "P", "scale": 5}], is_active=True)
        self.role = Role.objects.create(title="Backend", jd_text="jd")

    def _upload(self, parsed_text, name="a.docx"):
        return CandidateUpload.objects.create(
            role=self.role, original_filename=name, parsed_text=parsed_text
        )

    def test_creates_candidate_score_and_marks_done(self):
        up = self._upload("Anna Nowak anna@demo.test 8y Python")
        score = process_upload(up.pk, contact_service=contact_service(
            {"full_name": "Anna Nowak", "email": "anna@demo.test"}))
        self.assertIsNotNone(score)
        up.refresh_from_db()
        self.assertEqual(up.status, CandidateUpload.Status.DONE)
        cand = Candidate.objects.get(email="anna@demo.test")
        self.assertEqual(cand.full_name, "Anna Nowak")  # extracted, not typed
        self.assertEqual(up.candidate, cand)
        self.assertEqual(score.status, Score.Status.PENDING)
        self.assertTrue(score.cv.parsed_text)

    def test_unreadable_file_marks_failed(self):
        up = self._upload("", name="scan.pdf")  # web couldn't extract text
        result = process_upload(up.pk, contact_service=contact_service({"email": "x@x.com"}))
        self.assertIsNone(result)
        up.refresh_from_db()
        self.assertEqual(up.status, CandidateUpload.Status.FAILED)
        self.assertIn("read", up.error.lower())
        self.assertEqual(Candidate.objects.count(), 0)

    def test_role_less_upload_adds_candidate_without_score(self):
        # Global upload (no role) just adds to the database — no scoring.
        up = CandidateUpload.objects.create(
            role=None, original_filename="a.docx", parsed_text="Anna anna2@demo.test 8y Python"
        )
        score = process_upload(up.pk, contact_service=contact_service(
            {"full_name": "Anna", "email": "anna2@demo.test"}))
        self.assertIsNone(score)  # no scoring without a role
        up.refresh_from_db()
        self.assertEqual(up.status, CandidateUpload.Status.DONE)
        cand = Candidate.objects.get(email="anna2@demo.test")
        self.assertEqual(up.candidate, cand)
        self.assertEqual(cand.cvs.count(), 1)
        self.assertEqual(Score.objects.count(), 0)

    def test_no_email_marks_failed(self):
        up = self._upload("a CV with no contact details")
        result = process_upload(up.pk, contact_service=contact_service({"full_name": "X", "email": ""}))
        self.assertIsNone(result)
        up.refresh_from_db()
        self.assertEqual(up.status, CandidateUpload.Status.FAILED)
        self.assertIn("No email", up.error)
        self.assertEqual(Candidate.objects.count(), 0)


class ProcessUploadTaskTests(TestCase):
    def setUp(self):
        self.rubric = Rubric.objects.create(version=1, criteria=[{"name": "P", "scale": 5}], is_active=True)
        self.role = Role.objects.create(title="Backend", jd_text="jd")

    def test_task_creates_candidate_and_defers_scoring(self):
        from ats import tasks

        up = CandidateUpload.objects.create(
            role=self.role, original_filename="a.docx", parsed_text="Anna 8y Python"
        )
        # orchestration did `from .contact import extract_contact`, so patch the
        # name in orchestration (not the contact module).
        with mock.patch("ats.scoring.orchestration.extract_contact",
                        return_value={"full_name": "Anna", "email": "anna@demo.test", "phone": ""}):
            tasks.process_candidate_upload(upload_id=up.pk)
        self.assertTrue(Candidate.objects.filter(email="anna@demo.test").exists())
        self.assertEqual(ProcrastinateJob.objects.filter(task_name="score_candidate").count(), 1)
