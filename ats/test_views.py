"""Lane E view tests — auth gating + the full upload/score flow through HTTP.

Scoring itself runs in the background worker (not exercised here); these tests
verify the views create the right rows and enqueue the right jobs. No API key
needed: nothing calls the model inline (JD extraction is best-effort and swallows
the missing key).
"""

from __future__ import annotations

import io
import tempfile

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


@override_settings(MEDIA_ROOT=MEDIA_TMP)
class FlowTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("recruiter", password="pw")
        self.client.force_login(self.user)
        self.rubric = Rubric.objects.create(
            version=1, criteria=[{"name": "Python", "scale": 5}], is_active=True
        )
        self.role = Role.objects.create(title="Backend", jd_text="Python/AWS role")

    def test_role_create(self):
        resp = self.client.post(reverse("role_create"),
                                {"title": "Frontend", "client": "Acme", "jd_text": "React role"})
        self.assertEqual(resp.status_code, 302)
        role = Role.objects.get(title="Frontend")
        # JD extraction is best-effort with no API key -> stored {}.
        self.assertEqual(role.structured_requirements, {})

    def test_scorecard_renders(self):
        resp = self.client.get(reverse("role_detail", args=[self.role.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Backend")
        self.assertContains(resp, "Add candidate")

    def test_add_candidate_pasted_creates_pending_score_and_enqueues(self):
        resp = self.client.post(
            reverse("add_candidate", args=[self.role.pk]),
            {"full_name": "Jan K", "email": "Jan@Example.com", "pasted_text": "Senior Python dev"},
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
            {"full_name": "Jan K", "email": "jan@example.com", "pasted_text": "dev"},
        )
        self.assertEqual(Score.objects.count(), 0)
        self.assertEqual(Candidate.objects.count(), 0)

    def test_add_candidate_unparseable_file_waits_for_paste(self):
        bad_pdf = SimpleUploadedFile("scan.pdf", b"%PDF-1.4 not a real pdf",
                                     content_type="application/pdf")
        self.client.post(
            reverse("add_candidate", args=[self.role.pk]),
            {"full_name": "Eva", "email": "eva@x.com", "cv_file": bad_pdf},
        )
        cand = Candidate.objects.get(email="eva@x.com")
        score = Score.objects.get(role=self.role, candidate=cand)
        self.assertEqual(score.status, Score.Status.PENDING)
        self.assertTrue(score.cv.needs_manual_text)
        # Not enqueued — no text to score yet.
        self.assertEqual(ProcrastinateJob.objects.filter(task_name="score_candidate").count(), 0)

    def test_paste_cv_fills_text_and_enqueues(self):
        cand = Candidate.objects.create(full_name="Eva", email="eva@x.com")
        cv = CV.objects.create(candidate=cand, raw_file=SimpleUploadedFile("x.pdf", b"x"),
                               parsed_text="", parser_version="pypdf-v1")
        self.assertTrue(cv.needs_manual_text)
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
