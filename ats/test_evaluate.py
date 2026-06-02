"""Feature 3 tests — transcript-based candidate evaluation."""

from __future__ import annotations

import types

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from procrastinate.contrib.django.models import ProcrastinateJob

from ats.models import CV, Candidate, Evaluation, Role, Rubric, Score
from ats.scoring.evaluate import EvaluationError, EvaluationService
from ats.scoring.orchestration import generate_evaluation, set_transcript

GOOD = {
    "recommendation": "yes",
    "headline": "Strong backend fit with a minor cloud gap",
    "summary": "Confident Python answers; shaky on Kubernetes.",
    "strengths": ["Clear ECS architecture explanation", ""],
    "concerns": ["No Kubernetes experience"],
    "criteria": [
        {"name": "Technical fit", "assessment": "Strong", "evidence": "Described RDS Proxy for pooling."},
        {"name": "", "assessment": "skip", "evidence": "x"},
    ],
}


def response(tool_input, *, include_tool=True, model="claude-sonnet-4-6"):
    blocks = [types.SimpleNamespace(type="tool_use", name="submit_evaluation", input=tool_input)] \
        if include_tool else [types.SimpleNamespace(type="text", text="no tool")]
    return types.SimpleNamespace(content=blocks, model=model,
                                 usage=types.SimpleNamespace(input_tokens=1, output_tokens=1))


class _Msgs:
    def __init__(self, r): self._r = r
    def create(self, **kw): return self._r


class FakeClient:
    def __init__(self, r): self.messages = _Msgs(r)


def fake_service(tool_input=GOOD, **kw):
    return EvaluationService(client=FakeClient(response(tool_input, **kw)))


class EvaluationServiceTests(TestCase):
    def test_normalizes(self):
        data, model = fake_service().evaluate(
            jd_text="jd", cv_text="cv", rubric_criteria=[{"name": "Technical fit", "scale": 5}],
            transcript="...call...")
        self.assertEqual(data["recommendation"], "yes")
        self.assertEqual(data["strengths"], ["Clear ECS architecture explanation"])  # blank dropped
        self.assertEqual(len(data["criteria"]), 1)  # nameless dropped
        self.assertEqual(model, "claude-sonnet-4-6")

    def test_empty_transcript_raises(self):
        with self.assertRaises(EvaluationError):
            fake_service().evaluate(jd_text="jd", cv_text="cv", rubric_criteria=[], transcript="  ")

    def test_missing_tool_raises(self):
        svc = EvaluationService(client=FakeClient(response(GOOD, include_tool=False)))
        with self.assertRaises(EvaluationError):
            svc.evaluate(jd_text="jd", cv_text="cv", rubric_criteria=[], transcript="t")

    def test_invalid_recommendation_raises(self):
        svc = fake_service(dict(GOOD, recommendation="probably"))
        with self.assertRaises(EvaluationError):
            svc.evaluate(jd_text="jd", cv_text="cv", rubric_criteria=[], transcript="t")


class EvaluationOrchestrationTests(TestCase):
    def setUp(self):
        self.cand = Candidate.objects.create(full_name="Anna", email="a@x.com")
        self.cv = CV.objects.create(candidate=self.cand, parsed_text="8y Python")
        self.role = Role.objects.create(title="Backend", jd_text="jd")

    def test_set_transcript_upserts_and_resets(self):
        e1 = set_transcript(role=self.role, candidate=self.cand, cv=self.cv, transcript="  call one  ")
        self.assertEqual(e1.transcript, "call one")
        self.assertEqual(e1.status, Evaluation.Status.PENDING)
        e2 = set_transcript(role=self.role, candidate=self.cand, cv=self.cv, transcript="call two")
        self.assertEqual(e1.pk, e2.pk)
        self.assertEqual(Evaluation.objects.count(), 1)
        self.assertEqual(e2.transcript, "call two")

    def test_generate_success(self):
        ev = set_transcript(role=self.role, candidate=self.cand, cv=self.cv, transcript="call")
        generate_evaluation(ev.pk, service=fake_service())
        ev.refresh_from_db()
        self.assertEqual(ev.status, Evaluation.Status.GENERATED)
        self.assertEqual(ev.result["recommendation"], "yes")

    def test_generate_failure(self):
        ev = set_transcript(role=self.role, candidate=self.cand, cv=self.cv, transcript="call")
        bad = EvaluationService(client=FakeClient(response(GOOD, include_tool=False)))
        generate_evaluation(ev.pk, service=bad)
        ev.refresh_from_db()
        self.assertEqual(ev.status, Evaluation.Status.FAILED)


class EvaluationViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("rec", password="pw")
        self.client.force_login(self.user)
        self.rubric = Rubric.objects.create(version=1, criteria=[{"name": "Technical fit", "scale": 5}], is_active=True)
        self.cand = Candidate.objects.create(full_name="Anna", email="a@x.com")
        self.cv = CV.objects.create(candidate=self.cand, parsed_text="8y Python")
        self.role = Role.objects.create(title="Backend", jd_text="jd")
        self.score = Score.objects.create(role=self.role, candidate=self.cand, cv=self.cv, rubric=self.rubric)
        self.score.mark_scored(overall=80, per_criterion={})

    def test_requires_login(self):
        self.client.logout()
        resp = self.client.get(reverse("evaluation_detail", args=[self.role.pk, self.cand.pk]))
        self.assertEqual(resp.status_code, 302)

    def test_detail_renders(self):
        resp = self.client.get(reverse("evaluation_detail", args=[self.role.pk, self.cand.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Screening evaluation")

    def test_generate_requires_transcript(self):
        self.client.post(reverse("generate_evaluation", args=[self.role.pk, self.cand.pk]), {"transcript": "  "})
        self.assertFalse(Evaluation.objects.filter(role=self.role, candidate=self.cand).exists())

    def test_generate_saves_transcript_and_enqueues(self):
        resp = self.client.post(reverse("generate_evaluation", args=[self.role.pk, self.cand.pk]),
                                {"transcript": "Interviewer: tell me about ECS..."})
        self.assertEqual(resp.status_code, 302)
        ev = Evaluation.objects.get(role=self.role, candidate=self.cand)
        self.assertIn("ECS", ev.transcript)
        self.assertEqual(ProcrastinateJob.objects.filter(task_name="generate_evaluation").count(), 1)
