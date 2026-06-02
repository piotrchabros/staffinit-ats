"""Feature 2 tests — anonymized branded CV generation + PII scrub."""

from __future__ import annotations

import types
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from procrastinate.contrib.django.models import ProcrastinateJob

from ats.models import AnonymizedCV, CV, Candidate, Role, Rubric, Score
from ats.scoring import anonymize as anon_mod
from ats.scoring.anonymize import AnonymizationError, AnonymizationService, scrub_pii
from ats.scoring.orchestration import ensure_anonymized_cv, generate_anonymized_cv

GOOD = {
    "headline": "Senior Backend Engineer",
    "summary": "Experienced Python engineer.",
    "years_experience": 8,
    "skills": ["Python", "AWS", ""],
    "experience": [
        {"role_title": "Lead Engineer", "industry": "fintech", "period": "2019-2024",
         "highlights": ["Built microservices", ""]},
    ],
    "education": [{"qualification": "BSc", "field": "CS", "period": "2012"}],
}


def response(tool_input, *, include_tool=True, model="claude-sonnet-4-6"):
    blocks = [types.SimpleNamespace(type="tool_use", name="submit_anonymized_cv", input=tool_input)] \
        if include_tool else [types.SimpleNamespace(type="text", text="no tool")]
    return types.SimpleNamespace(content=blocks, model=model,
                                 usage=types.SimpleNamespace(input_tokens=1, output_tokens=1))


class _Msgs:
    def __init__(self, r): self._r = r
    def create(self, **kw): return self._r


class FakeClient:
    def __init__(self, r): self.messages = _Msgs(r)


def fake_service(tool_input=GOOD, **kw):
    return AnonymizationService(client=FakeClient(response(tool_input, **kw)))


class ScrubTests(TestCase):
    def test_redacts_name_and_email_left_by_model(self):
        leaky = {
            "headline": "Anna Nowak, Senior Engineer",
            "summary": "Reach Anna at anna@x.com. Strong Python.",
            "skills": ["Python"],
            "experience": [{"role_title": "Lead", "highlights": ["Mentored Anna's team"]}],
        }
        clean = scrub_pii(leaky, name="Anna Nowak", email="anna@x.com")
        blob = str(clean)
        self.assertNotIn("Anna", blob)
        self.assertNotIn("Nowak", blob)
        self.assertNotIn("anna@x.com", blob)
        self.assertIn("[redacted]", clean["summary"])

    def test_two_char_name_token_is_redacted(self):
        # Short real names (e.g. "Bo An", "Li Wu") must be redacted.
        clean = scrub_pii({"summary": "Bo An led the team"}, name="Bo An")
        self.assertNotIn("Bo", clean["summary"])
        self.assertNotIn("An ", clean["summary"])

    def test_single_char_initial_not_redacted(self):
        # 1-char initials are skipped so they don't nuke stray single letters.
        clean = scrub_pii({"summary": "A strong AI expert"}, name="A")
        self.assertIn("AI expert", clean["summary"])


class AnonymizationServiceTests(TestCase):
    def test_normalizes_and_strips_blanks(self):
        data, model = fake_service().anonymize(cv_text="cv", candidate_name="Anna Nowak", candidate_email="anna@x.com")
        self.assertEqual(data["skills"], ["Python", "AWS"])  # blank dropped
        self.assertEqual(data["experience"][0]["highlights"], ["Built microservices"])
        self.assertEqual(data["years_experience"], 8)
        self.assertEqual(model, "claude-sonnet-4-6")

    def test_empty_cv_raises(self):
        with self.assertRaises(AnonymizationError):
            fake_service().anonymize(cv_text="  ")

    def test_missing_tool_raises(self):
        svc = AnonymizationService(client=FakeClient(response(GOOD, include_tool=False)))
        with self.assertRaises(AnonymizationError):
            svc.anonymize(cv_text="cv")

    def test_scrub_applied_in_pipeline(self):
        leaky = dict(GOOD, headline="Anna Nowak — Senior Backend Engineer")
        data, _ = fake_service(leaky).anonymize(cv_text="cv", candidate_name="Anna Nowak", candidate_email="")
        self.assertNotIn("Nowak", data["headline"])


class AnonOrchestrationTests(TestCase):
    def setUp(self):
        self.cand = Candidate.objects.create(full_name="Anna Nowak", email="anna@x.com")
        self.cv = CV.objects.create(candidate=self.cand, parsed_text="Anna Nowak, 8y Python at Acme.")
        self.role = Role.objects.create(title="Backend", jd_text="jd")

    def test_ensure_idempotent(self):
        a1 = ensure_anonymized_cv(role=self.role, candidate=self.cand, cv=self.cv)
        a2 = ensure_anonymized_cv(role=self.role, candidate=self.cand, cv=self.cv)
        self.assertEqual(a1.pk, a2.pk)
        self.assertEqual(AnonymizedCV.objects.count(), 1)

    def test_generate_success(self):
        a = ensure_anonymized_cv(role=self.role, candidate=self.cand, cv=self.cv)
        generate_anonymized_cv(a.pk, service=fake_service())
        a.refresh_from_db()
        self.assertEqual(a.status, AnonymizedCV.Status.GENERATED)
        self.assertEqual(a.data["headline"], "Senior Backend Engineer")

    def test_generate_failure(self):
        a = ensure_anonymized_cv(role=self.role, candidate=self.cand, cv=self.cv)
        bad = AnonymizationService(client=FakeClient(response(GOOD, include_tool=False)))
        generate_anonymized_cv(a.pk, service=bad)
        a.refresh_from_db()
        self.assertEqual(a.status, AnonymizedCV.Status.FAILED)


class AnonViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("rec", password="pw")
        self.client.force_login(self.user)
        self.rubric = Rubric.objects.create(version=1, criteria=[{"name": "Python", "scale": 5}], is_active=True)
        self.cand = Candidate.objects.create(full_name="Anna", email="a@x.com")
        self.cv = CV.objects.create(candidate=self.cand, parsed_text="8y Python")
        self.role = Role.objects.create(title="Backend", jd_text="jd")
        self.score = Score.objects.create(role=self.role, candidate=self.cand, cv=self.cv, rubric=self.rubric)
        self.score.mark_scored(overall=80, per_criterion={})

    def test_requires_login(self):
        self.client.logout()
        resp = self.client.get(reverse("anonymized_cv_detail", args=[self.role.pk, self.cand.pk]))
        self.assertEqual(resp.status_code, 302)

    def test_detail_renders(self):
        resp = self.client.get(reverse("anonymized_cv_detail", args=[self.role.pk, self.cand.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "anonymized")

    def test_generate_enqueues(self):
        resp = self.client.post(reverse("generate_anonymized_cv", args=[self.role.pk, self.cand.pk]))
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(AnonymizedCV.objects.filter(role=self.role, candidate=self.cand).exists())
        self.assertEqual(ProcrastinateJob.objects.filter(task_name="generate_anonymized_cv").count(), 1)
