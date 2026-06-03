"""Feature 4 — archive (soft-delete) candidates."""

from __future__ import annotations

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from ats.models import Candidate


class ArchiveTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("rec", password="pw")
        self.client.force_login(self.user)
        self.active = Candidate.objects.create(full_name="Active Person", email="active@x.com")
        self.archived = Candidate.objects.create(
            full_name="Archived Person", email="archived@x.com", is_archived=True
        )

    def test_default_view_hides_archived(self):
        resp = self.client.get(reverse("candidate_list"))
        self.assertContains(resp, "Active Person")
        self.assertNotContains(resp, "Archived Person")
        self.assertContains(resp, "View 1 archived candidate")

    def test_archived_view_shows_only_archived(self):
        resp = self.client.get(reverse("candidate_list"), {"archived": "1"})
        self.assertContains(resp, "Archived Person")
        self.assertNotContains(resp, "Active Person")

    def test_archive_action(self):
        resp = self.client.post(reverse("archive_candidate", args=[self.active.pk]))
        self.assertEqual(resp.status_code, 302)
        self.active.refresh_from_db()
        self.assertTrue(self.active.is_archived)

    def test_unarchive_action_redirects_to_archived_view(self):
        resp = self.client.post(reverse("unarchive_candidate", args=[self.archived.pk]))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("archived=1", resp.url)
        self.archived.refresh_from_db()
        self.assertFalse(self.archived.is_archived)

    def test_archive_requires_post(self):
        resp = self.client.get(reverse("archive_candidate", args=[self.active.pk]))
        self.assertEqual(resp.status_code, 405)  # GET not allowed
