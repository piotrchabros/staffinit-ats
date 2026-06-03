"""Feature 1 — global candidate database (search + role-less upload)."""

from __future__ import annotations

import tempfile

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from procrastinate.contrib.django.models import ProcrastinateJob

from ats.models import CV, Candidate, CandidateUpload

MEDIA = tempfile.mkdtemp()


class CandidateListAuthTests(TestCase):
    def test_requires_login(self):
        resp = self.client.get(reverse("candidate_list"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login/", resp["Location"])


@override_settings(MEDIA_ROOT=MEDIA)
class CandidateSearchTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("rec", password="pw")
        self.client.force_login(self.user)
        self.anna = Candidate.objects.create(full_name="Anna Nowak", email="anna@x.com")
        CV.objects.create(candidate=self.anna, parsed_text="Senior Python and AWS engineer")
        self.bob = Candidate.objects.create(full_name="Bob Lee", email="bob@corp.io")
        CV.objects.create(candidate=self.bob, parsed_text="Java and Spring developer")

    def _names(self, q=""):
        resp = self.client.get(reverse("candidate_list"), {"q": q} if q else {})
        self.assertEqual(resp.status_code, 200)
        return {c.full_name for c in resp.context["candidates"]}

    def test_lists_all_without_query(self):
        self.assertEqual(self._names(), {"Anna Nowak", "Bob Lee"})

    def test_search_by_name(self):
        self.assertEqual(self._names("nowak"), {"Anna Nowak"})

    def test_search_by_email(self):
        self.assertEqual(self._names("corp.io"), {"Bob Lee"})

    def test_search_by_cv_content(self):
        self.assertEqual(self._names("aws"), {"Anna Nowak"})
        self.assertEqual(self._names("spring"), {"Bob Lee"})


@override_settings(MEDIA_ROOT=MEDIA)
class CandidateUploadTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("rec", password="pw")
        self.client.force_login(self.user)

    def test_upload_stages_role_less_without_scoring(self):
        f1 = SimpleUploadedFile("a.pdf", b"%PDF dummy", content_type="application/pdf")
        f2 = SimpleUploadedFile("b.pdf", b"%PDF dummy", content_type="application/pdf")
        resp = self.client.post(reverse("candidate_upload"), {"cv_files": [f1, f2]})
        self.assertEqual(resp.status_code, 302)
        uploads = CandidateUpload.objects.all()
        self.assertEqual(uploads.count(), 2)
        self.assertTrue(all(u.role_id is None for u in uploads))
        self.assertTrue(all(u.raw_file for u in uploads))  # original stored
        self.assertEqual(ProcrastinateJob.objects.filter(task_name="process_candidate_upload").count(), 2)
        # No candidates/scores yet — that's the worker's job; and global = no scoring.
        self.assertEqual(Candidate.objects.count(), 0)
        self.assertEqual(ProcrastinateJob.objects.filter(task_name="score_candidate").count(), 0)
