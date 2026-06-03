"""Feature 5 — view the uploaded original CV file."""

from __future__ import annotations

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from ats.models import CV, Candidate


class CVFileViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("rec", password="pw")
        self.client.force_login(self.user)
        self.cand = Candidate.objects.create(full_name="Anna Nowak", email="anna@x.com")
        self.cv = CV.objects.create(
            candidate=self.cand,
            raw_file=SimpleUploadedFile("anna_cv.pdf", b"%PDF-1.4 original bytes"),
            parsed_text="cv text",
        )
        self.pasted = CV.objects.create(candidate=self.cand, parsed_text="pasted only")

    def tearDown(self):
        for cv in CV.objects.exclude(raw_file=""):
            cv.raw_file.delete(save=False)

    def test_serves_original_file(self):
        resp = self.client.get(reverse("cv_file", args=[self.cv.pk]))
        self.assertEqual(resp.status_code, 200)
        body = b"".join(resp.streaming_content)
        self.assertEqual(body, b"%PDF-1.4 original bytes")
        # Inline, not a forced download.
        self.assertIn("inline", resp.headers.get("Content-Disposition", "inline"))

    def test_404_for_pasted_cv_without_file(self):
        resp = self.client.get(reverse("cv_file", args=[self.pasted.pk]))
        self.assertEqual(resp.status_code, 404)

    def test_login_required(self):
        self.client.logout()
        resp = self.client.get(reverse("cv_file", args=[self.cv.pk]))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.url)

    def test_original_cv_property_picks_latest_with_file(self):
        # pasted CV is newer but has no file; original_cv must skip it.
        self.assertEqual(self.cand.original_cv, self.cv)

    def test_candidate_list_links_to_cv(self):
        resp = self.client.get(reverse("candidate_list"))
        self.assertContains(resp, reverse("cv_file", args=[self.cv.pk]))
