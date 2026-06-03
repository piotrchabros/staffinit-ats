"""Lane E view tests — auth gating + the full upload/score flow through HTTP.

Scoring itself runs in the background worker (not exercised here); these tests
verify the views create the right rows and enqueue the right jobs. No API key
needed: nothing calls the model inline (JD extraction is best-effort and swallows
the missing key).
"""

from __future__ import annotations

import io
import tempfile
from unittest import mock

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from procrastinate.contrib.django.models import ProcrastinateJob

from ats.models import CV, Candidate, Role, Rubric, Score

MEDIA_TMP = tempfile.mkdtemp()


def make_docx(text: str) -> bytes:
    import docx

    d = docx.Document()
    d.add_paragraph(text)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


class AuthGateTests(TestCase):
    def test_role_list_requires_login(self):
        resp = self.client.get(reverse("role_list"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login/", resp["Location"])


class AuthzScopingTests(TestCase):
    """Per-candidate / per-CV views must scope to the role (no IDOR by id)."""

    def setUp(self):
        self.user = User.objects.create_user("rec", password="pw")
        self.client.force_login(self.user)
        self.rubric = Rubric.objects.create(version=1, criteria=[{"name": "P", "scale": 5}], is_active=True)
        self.role = Role.objects.create(title="A", jd_text="jd")
        # A candidate scored on a DIFFERENT role (not on self.role).
        self.other_role = Role.objects.create(title="B", jd_text="jd2")
        self.stranger = Candidate.objects.create(full_name="Z", email="z@x.com")
        self.stranger_cv = CV.objects.create(candidate=self.stranger, parsed_text="cv")
        Score.objects.create(role=self.other_role, candidate=self.stranger, cv=self.stranger_cv, rubric=self.rubric)

    def test_screening_detail_404_for_candidate_not_on_role(self):
        resp = self.client.get(reverse("screening_detail", args=[self.role.pk, self.stranger.pk]))
        self.assertEqual(resp.status_code, 404)

    def test_anon_detail_404_for_candidate_not_on_role(self):
        resp = self.client.get(reverse("anonymized_cv_detail", args=[self.role.pk, self.stranger.pk]))
        self.assertEqual(resp.status_code, 404)

    def test_evaluation_generate_404_for_candidate_not_on_role(self):
        resp = self.client.post(reverse("generate_evaluation", args=[self.role.pk, self.stranger.pk]),
                                {"transcript": "hi"})
        self.assertEqual(resp.status_code, 404)

    def test_paste_cv_404_for_cv_not_on_role(self):
        # stranger_cv has a Score on other_role, not on self.role.
        resp = self.client.post(reverse("paste_cv", args=[self.role.pk, self.stranger_cv.pk]),
                                {"parsed_text": "injected"})
        self.assertEqual(resp.status_code, 404)
        self.stranger_cv.refresh_from_db()
        self.assertEqual(self.stranger_cv.parsed_text, "cv")  # untouched


@override_settings(MEDIA_ROOT=MEDIA_TMP)
class FlowTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("recruiter", password="pw")
        self.client.force_login(self.user)
        self.rubric = Rubric.objects.create(
            version=1, criteria=[{"name": "Python", "scale": 5}], is_active=True
        )
        self.role = Role.objects.create(title="Backend", jd_text="Python/AWS role")

    def test_role_create_defers_jd_extraction(self):
        resp = self.client.post(reverse("role_create"),
                                {"title": "Frontend", "client": "Acme", "jd_text": "React role"})
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(Role.objects.filter(title="Frontend").exists())
        # JD extraction runs in the background (a live API call) — the view just
        # enqueues it; the test never touches the network.
        self.assertEqual(
            ProcrastinateJob.objects.filter(task_name="extract_requirements").count(), 1
        )

    def test_scorecard_renders(self):
        resp = self.client.get(reverse("role_detail", args=[self.role.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Backend")
        self.assertContains(resp, "Add candidates")

    def test_add_candidate_file_auto_extracts_name_and_email(self):
        # The whole point: drop a CV, no typing — name/email come from the file.
        upload = SimpleUploadedFile(
            "anna.docx", make_docx("Anna Nowak — 8y Python, anna@demo.test"),
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        with mock.patch("ats.scoring.contact.extract_contact",
                        return_value={"full_name": "Anna Nowak", "email": "anna@demo.test", "phone": ""}):
            resp = self.client.post(reverse("add_candidate", args=[self.role.pk]),
                                    {"cv_files": [upload]})
        self.assertEqual(resp.status_code, 302)
        cand = Candidate.objects.get(email="anna@demo.test")
        self.assertEqual(cand.full_name, "Anna Nowak")  # extracted, not typed
        self.assertEqual(Score.objects.filter(role=self.role, candidate=cand).count(), 1)
        self.assertEqual(ProcrastinateJob.objects.filter(task_name="score_candidate").count(), 1)

    def test_add_candidate_multiple_files(self):
        f1 = SimpleUploadedFile("a.docx", make_docx("A cv"))
        f2 = SimpleUploadedFile("b.docx", make_docx("B cv"))
        with mock.patch("ats.scoring.contact.extract_contact", side_effect=[
            {"full_name": "Cand A", "email": "a@demo.test", "phone": ""},
            {"full_name": "Cand B", "email": "b@demo.test", "phone": ""},
        ]):
            self.client.post(reverse("add_candidate", args=[self.role.pk]),
                             {"cv_files": [f1, f2]})
        self.assertEqual(Candidate.objects.filter(email__in=["a@demo.test", "b@demo.test"]).count(), 2)
        self.assertEqual(ProcrastinateJob.objects.filter(task_name="score_candidate").count(), 2)

    def test_add_candidate_skipped_when_no_email_extractable(self):
        upload = SimpleUploadedFile("x.docx", make_docx("a CV with no email here"))
        with mock.patch("ats.scoring.contact.extract_contact",
                        return_value={"full_name": "", "email": "", "phone": ""}):
            self.client.post(reverse("add_candidate", args=[self.role.pk]), {"cv_files": [upload]})
        self.assertEqual(Candidate.objects.count(), 0)  # can't create without an email

    def test_add_candidate_pasted_uses_fallback_when_extraction_empty(self):
        # Single paste with no extractable contact -> the optional manual fields fill in.
        with mock.patch("ats.scoring.contact.extract_contact",
                        return_value={"full_name": "", "email": "", "phone": ""}):
            resp = self.client.post(
                reverse("add_candidate", args=[self.role.pk]),
                {"pasted_text": "Senior Python dev", "full_name": "Jan K", "email": "Jan@Example.com"},
            )
        self.assertEqual(resp.status_code, 302)
        cand = Candidate.objects.get(email="jan@example.com")  # normalized
        score = Score.objects.get(role=self.role, candidate=cand)
        self.assertEqual(score.status, Score.Status.PENDING)
        self.assertEqual(score.cv.parser_version, "manual-paste")
        self.assertEqual(ProcrastinateJob.objects.filter(task_name="score_candidate").count(), 1)

    def test_add_candidate_without_active_rubric_is_blocked(self):
        self.rubric.is_active = False
        self.rubric.save()
        self.client.post(
            reverse("add_candidate", args=[self.role.pk]),
            {"pasted_text": "dev", "full_name": "Jan K", "email": "jan@example.com"},
        )
        self.assertEqual(Score.objects.count(), 0)
        self.assertEqual(Candidate.objects.count(), 0)

    def test_add_candidate_unparseable_file_waits_for_paste(self):
        bad_pdf = SimpleUploadedFile("scan.pdf", b"%PDF-1.4 not a real pdf",
                                     content_type="application/pdf")
        # Unreadable file (no text) + single input -> the fallback email is used,
        # the row waits for a manual paste. extract_contact isn't called (no text).
        self.client.post(
            reverse("add_candidate", args=[self.role.pk]),
            {"cv_files": [bad_pdf], "full_name": "Eva", "email": "eva@x.com"},
        )
        cand = Candidate.objects.get(email="eva@x.com")
        score = Score.objects.get(role=self.role, candidate=cand)
        self.assertEqual(score.status, Score.Status.PENDING)
        self.assertTrue(score.cv.needs_manual_text)
        self.assertEqual(ProcrastinateJob.objects.filter(task_name="score_candidate").count(), 0)

    def test_paste_cv_fills_text_and_enqueues(self):
        cand = Candidate.objects.create(full_name="Eva", email="eva@x.com")
        cv = CV.objects.create(candidate=cand, raw_file=SimpleUploadedFile("x.pdf", b"x"),
                               parsed_text="", parser_version="pypdf-v1")
        self.assertTrue(cv.needs_manual_text)
        # add_candidate creates a pending Score linking the CV to the role before
        # the paste; the paste view now requires that scoping.
        Score.objects.create(role=self.role, candidate=cand, cv=cv, rubric=self.rubric)
        self.client.post(
            reverse("paste_cv", args=[self.role.pk, cv.pk]),
            {"parsed_text": "Pasted senior python CV"},
        )
        cv.refresh_from_db()
        self.assertIn("Pasted senior", cv.parsed_text)
        self.assertTrue(Score.objects.filter(role=self.role, candidate=cand).exists())
        self.assertEqual(ProcrastinateJob.objects.filter(task_name="score_candidate").count(), 1)

    def test_score_role_action_enqueues_role_job(self):
        cand = Candidate.objects.create(full_name="A", email="a@x.com")
        cv = CV.objects.create(candidate=cand, parsed_text="has text")
        Score.objects.create(role=self.role, candidate=cand, cv=cv, rubric=self.rubric)
        resp = self.client.post(reverse("score_role", args=[self.role.pk]))
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(ProcrastinateJob.objects.filter(task_name="score_role").count(), 1)

    def test_retry_failed_score_enqueues(self):
        cand = Candidate.objects.create(full_name="A", email="a@x.com")
        cv = CV.objects.create(candidate=cand, parsed_text="has text")
        score = Score.objects.create(role=self.role, candidate=cand, cv=cv, rubric=self.rubric)
        score.mark_failed("rate limited")
        self.client.post(reverse("retry_score", args=[self.role.pk, score.pk]))
        self.assertEqual(ProcrastinateJob.objects.filter(task_name="score_candidate").count(), 1)
