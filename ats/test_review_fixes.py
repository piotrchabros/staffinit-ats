"""Regression guards for the code-review fixes on the 5-feature batch."""

from __future__ import annotations

from decimal import Decimal

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from ats.models import CV, Candidate, Company, Deal, DealDocument, Role, Rubric, Score, Stage
from ats.scoring.orchestration import create_pending_score, ensure_default_stages
from ats.views import _overall_key


class BoardControlTests(TestCase):
    """role_detail must still expose retry, manual-paste, and per-criterion detail,
    and must hide archived candidates."""

    def setUp(self):
        self.user = User.objects.create_user("rec", password="pw")
        self.client.force_login(self.user)
        self.rubric = Rubric.objects.create(version=1, criteria=[{"name": "P", "scale": 5}], is_active=True)
        self.role = Role.objects.create(title="Backend", jd_text="jd")
        self.cand = Candidate.objects.create(full_name="Anna Nowak", email="anna@x.com")
        self.cv = CV.objects.create(candidate=self.cand, parsed_text="cv text")
        self.score = create_pending_score(role=self.role, candidate=self.cand, cv=self.cv)

    def _get_board(self):
        return self.client.get(reverse("role_detail", args=[self.role.pk]))

    def test_board_hides_archived_candidate(self):
        Candidate.objects.filter(pk=self.cand.pk).update(is_archived=True)
        self.assertNotContains(self._get_board(), "Anna Nowak")

    def test_failed_score_offers_retry(self):
        self.score.mark_failed("boom")
        resp = self._get_board()
        self.assertContains(resp, "Retry scoring")
        self.assertContains(resp, reverse("retry_score", args=[self.role.pk, self.score.pk]))

    def test_unreadable_cv_offers_paste(self):
        # A CV with a file but no extractable text is "waiting".
        cand2 = Candidate.objects.create(full_name="Bob Waiting", email="bob@x.com")
        cv2 = CV.objects.create(
            candidate=cand2, raw_file=SimpleUploadedFile("bob.pdf", b"scan"), parsed_text=""
        )
        create_pending_score(role=self.role, candidate=cand2, cv=cv2)
        try:
            resp = self._get_board()
            self.assertContains(resp, "Paste CV text")
            self.assertContains(resp, reverse("paste_cv", args=[self.role.pk, cv2.pk]))
        finally:
            cv2.raw_file.delete(save=False)

    def test_per_criterion_breakdown_rendered(self):
        self.score.mark_scored(
            overall=4.0,
            per_criterion={"Python": {"score": 4, "rationale": "Strong async background"}},
        )
        resp = self._get_board()
        self.assertContains(resp, "Python")
        self.assertContains(resp, "Strong async background")


class OverallKeyTests(TestCase):
    def test_real_zero_beats_unscored(self):
        scored0 = Score(status=Score.Status.SCORED, overall=0.0)
        pending = Score(status=Score.Status.PENDING, overall=None)
        self.assertGreater(_overall_key(scored0), _overall_key(pending))

    def test_higher_overall_wins(self):
        self.assertGreater(
            _overall_key(Score(overall=7.0)), _overall_key(Score(overall=3.0))
        )


class StageSeedingTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("rec", password="pw")
        self.client.force_login(self.user)
        self.role = Role.objects.create(title="R", jd_text="jd")

    def test_ensure_default_stages_idempotent(self):
        ensure_default_stages(self.role)
        ensure_default_stages(self.role)  # second call must not duplicate
        self.assertEqual(self.role.stages.count(), 5)

    def test_duplicate_lane_name_rejected(self):
        ensure_default_stages(self.role)
        self.client.post(reverse("add_stage", args=[self.role.pk]), {"name": "New"})
        self.assertEqual(self.role.stages.filter(name="New").count(), 1)


class DealDocumentServingTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("rec", password="pw")
        self.client.force_login(self.user)
        company = Company.objects.create(name="Acme")
        self.deal = Deal.objects.create(
            company=company, developer_name="Bob", salary=Decimal("1"),
            client_rate=Decimal("2"), signed_date="2026-05-01",
        )
        self.doc = DealDocument.objects.create(
            deal=self.deal, file=SimpleUploadedFile("nda.pdf", b"agreement bytes"),
            original_filename="nda.pdf",
        )

    def tearDown(self):
        self.doc.file.delete(save=False)

    def test_serves_inline_to_staff(self):
        resp = self.client.get(reverse("deal_document_file", args=[self.doc.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(b"".join(resp.streaming_content), b"agreement bytes")

    def test_login_required(self):
        self.client.logout()
        resp = self.client.get(reverse("deal_document_file", args=[self.doc.pk]))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.url)

    def test_deal_detail_links_through_auth_view_not_media_url(self):
        resp = self.client.get(reverse("deal_detail", args=[self.deal.pk]))
        self.assertContains(resp, reverse("deal_document_file", args=[self.doc.pk]))
        self.assertNotContains(resp, self.doc.file.url)
