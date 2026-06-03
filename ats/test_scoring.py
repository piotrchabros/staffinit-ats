"""Lane B tests — scoring service, orchestration, and the procrastinate task.

No real API key needed: the Anthropic client is faked. The service-level tests
prove we only ever store schema-valid, rubric-conformant scores; orchestration
tests prove idempotency + failure handling; the task test runs a real worker
end to end with the fake client.
"""

from __future__ import annotations

import types
from unittest import mock

from django.test import TestCase

from ats.models import CV, Candidate, Role, Rubric, Score
from ats.scoring import service as service_mod
from ats.scoring.orchestration import (
    NoActiveRubric,
    create_pending_score,
    pending_score_ids,
    score_one,
    score_role,
)
from ats.scoring.service import ScoringError, ScoringService

RUBRIC = [
    {"name": "Python", "scale": 5, "weight": 0.6, "description": "Depth in Python"},
    {"name": "AWS", "scale": 5, "weight": 0.4, "description": "Cloud experience"},
]

GOOD_INPUT = {
    "overall": 82,
    "confidence": 0.8,
    "per_criterion": [
        {"name": "Python", "score": 4, "rationale": "5y Django"},
        {"name": "AWS", "score": 3, "rationale": "some ECS"},
    ],
}


def make_response(tool_input, *, model="claude-sonnet-4-6", in_tok=100, out_tok=20,
                  include_tool=True, name="submit_score"):
    blocks = []
    if include_tool:
        blocks.append(types.SimpleNamespace(type="tool_use", name=name, input=tool_input))
    else:
        blocks.append(types.SimpleNamespace(type="text", text="(no tool call)"))
    return types.SimpleNamespace(
        content=blocks,
        model=model,
        usage=types.SimpleNamespace(input_tokens=in_tok, output_tokens=out_tok),
    )


class FakeMessages:
    def __init__(self, response, capture):
        self._response = response
        self._capture = capture

    def create(self, **kwargs):
        self._capture.update(kwargs)
        return self._response


class FakeClient:
    def __init__(self, response):
        self.captured: dict = {}
        self.messages = FakeMessages(response, self.captured)


def fake_service(tool_input=GOOD_INPUT, **resp_kwargs) -> ScoringService:
    return ScoringService(client=FakeClient(make_response(tool_input, **resp_kwargs)))


# --------------------------------------------------------------------------- #
# ScoringService                                                              #
# --------------------------------------------------------------------------- #
class ScoringServiceTests(TestCase):
    def test_parses_valid_tool_output(self):
        svc = fake_service()
        result = svc.score(jd_text="jd", rubric_criteria=RUBRIC, cv_text="cv")
        self.assertEqual(result.overall, 82.0)
        self.assertEqual(set(result.per_criterion), {"Python", "AWS"})
        self.assertEqual(result.per_criterion["Python"]["score"], 4.0)
        self.assertEqual(result.confidence, 0.8)
        self.assertEqual(result.model_version, "claude-sonnet-4-6")
        self.assertEqual(result.token_cost, 120)

    def test_forces_tool_use_and_caches_jd_rubric(self):
        svc = fake_service()
        svc.score(jd_text="jd", rubric_criteria=RUBRIC, cv_text="cv")
        kwargs = svc.client.captured
        self.assertEqual(kwargs["tool_choice"], {"type": "tool", "name": "submit_score"})
        # JD + rubric live in a cached system block.
        self.assertEqual(kwargs["system"][0]["cache_control"], {"type": "ephemeral"})
        self.assertIn("Python", kwargs["system"][0]["text"])

    def test_missing_tool_call_raises(self):
        svc = ScoringService(client=FakeClient(make_response(GOOD_INPUT, include_tool=False)))
        with self.assertRaises(ScoringError):
            svc.score(jd_text="jd", rubric_criteria=RUBRIC, cv_text="cv")

    def test_missing_criterion_raises(self):
        bad = {"overall": 50, "per_criterion": [{"name": "Python", "score": 4, "rationale": "x"}]}
        with self.assertRaises(ScoringError):
            fake_service(bad).score(jd_text="jd", rubric_criteria=RUBRIC, cv_text="cv")

    def test_score_out_of_scale_raises(self):
        bad = {
            "overall": 50,
            "per_criterion": [
                {"name": "Python", "score": 9, "rationale": "x"},  # scale is 5
                {"name": "AWS", "score": 3, "rationale": "y"},
            ],
        }
        with self.assertRaises(ScoringError):
            fake_service(bad).score(jd_text="jd", rubric_criteria=RUBRIC, cv_text="cv")

    def test_overall_out_of_range_raises(self):
        bad = dict(GOOD_INPUT, overall=130)
        with self.assertRaises(ScoringError):
            fake_service(bad).score(jd_text="jd", rubric_criteria=RUBRIC, cv_text="cv")

    def test_confidence_out_of_range_dropped(self):
        result = fake_service(dict(GOOD_INPUT, confidence=42)).score(
            jd_text="jd", rubric_criteria=RUBRIC, cv_text="cv")
        self.assertIsNone(result.confidence)  # not frozen into the immutable Score

    def test_empty_cv_raises_without_calling_model(self):
        svc = fake_service()
        with self.assertRaises(ScoringError):
            svc.score(jd_text="jd", rubric_criteria=RUBRIC, cv_text="   ")
        self.assertEqual(svc.client.captured, {})  # never called the model


# --------------------------------------------------------------------------- #
# Orchestration                                                               #
# --------------------------------------------------------------------------- #
class OrchestrationTests(TestCase):
    def setUp(self):
        self.rubric = Rubric.objects.create(version=1, criteria=RUBRIC, is_active=True)
        self.cand = Candidate.objects.create(full_name="A", email="a@x.com")
        self.cv = CV.objects.create(candidate=self.cand, parsed_text="cv body")
        self.role = Role.objects.create(title="Backend", jd_text="jd")

    def test_create_pending_score_is_idempotent(self):
        s1 = create_pending_score(role=self.role, candidate=self.cand, cv=self.cv)
        s2 = create_pending_score(role=self.role, candidate=self.cand, cv=self.cv)
        self.assertEqual(s1.pk, s2.pk)
        self.assertEqual(Score.objects.count(), 1)
        self.assertEqual(s1.status, Score.Status.PENDING)

    def test_create_pending_score_without_active_rubric_raises(self):
        self.rubric.is_active = False
        self.rubric.save()
        with self.assertRaises(NoActiveRubric):
            create_pending_score(role=self.role, candidate=self.cand, cv=self.cv)

    def test_score_one_success_marks_scored(self):
        s = create_pending_score(role=self.role, candidate=self.cand, cv=self.cv)
        score_one(s.pk, service=fake_service())
        s.refresh_from_db()
        self.assertEqual(s.status, Score.Status.SCORED)
        self.assertEqual(s.overall, 82.0)
        self.assertEqual(s.model_version, "claude-sonnet-4-6")
        self.assertIsNotNone(s.scored_at)

    def test_score_one_failure_marks_failed(self):
        s = create_pending_score(role=self.role, candidate=self.cand, cv=self.cv)
        bad_svc = ScoringService(client=FakeClient(make_response(GOOD_INPUT, include_tool=False)))
        score_one(s.pk, service=bad_svc)
        s.refresh_from_db()
        self.assertEqual(s.status, Score.Status.FAILED)
        self.assertTrue(s.error)

    def test_score_one_marks_failed_on_non_scoring_error(self):
        # An Anthropic API error (e.g. bad/missing key) must surface as FAILED,
        # not leave the row stuck PENDING.
        s = create_pending_score(role=self.role, candidate=self.cand, cv=self.cv)

        class _Boom:
            class messages:
                @staticmethod
                def create(**kw):
                    raise RuntimeError("AuthenticationError: invalid x-api-key")

        score_one(s.pk, service=ScoringService(client=_Boom()))
        s.refresh_from_db()
        self.assertEqual(s.status, Score.Status.FAILED)
        self.assertIn("x-api-key", s.error)

    def test_score_one_noop_if_row_scored_during_api_call(self):
        # Simulate a concurrent duplicate job winning the race while our API call
        # is in flight: score_one must not error on the immutability guard.
        s = create_pending_score(role=self.role, candidate=self.cand, cv=self.cv)

        class _Racing:
            def score(self, **kwargs):
                Score.objects.get(pk=s.pk).mark_scored(overall=99, per_criterion={})
                return types.SimpleNamespace(overall=50, per_criterion={}, confidence=None,
                                             model_version="m", token_cost=1)

        result = score_one(s.pk, service=_Racing())  # must not raise
        s.refresh_from_db()
        self.assertEqual(s.status, Score.Status.SCORED)
        self.assertEqual(s.overall, 99)  # winner's value kept, our 50 discarded

    def test_score_one_on_scored_row_is_noop(self):
        s = create_pending_score(role=self.role, candidate=self.cand, cv=self.cv)
        score_one(s.pk, service=fake_service())
        # A service that would explode if its client were ever used.
        exploding = ScoringService(client=mock.Mock(side_effect=AssertionError))
        result = score_one(s.pk, service=exploding)  # must not call the model
        self.assertEqual(result.status, Score.Status.SCORED)

    def test_pending_score_ids_excludes_scored(self):
        cand2 = Candidate.objects.create(full_name="B", email="b@x.com")
        cv2 = CV.objects.create(candidate=cand2, parsed_text="cv2")
        s1 = create_pending_score(role=self.role, candidate=self.cand, cv=self.cv)
        create_pending_score(role=self.role, candidate=cand2, cv=cv2)
        score_one(s1.pk, service=fake_service())  # s1 now SCORED
        ids = pending_score_ids(self.role.pk)
        self.assertEqual(len(ids), 1)
        self.assertNotIn(s1.pk, ids)

    def test_score_role_summary(self):
        cand2 = Candidate.objects.create(full_name="B", email="b@x.com")
        cv2 = CV.objects.create(candidate=cand2, parsed_text="cv2")
        create_pending_score(role=self.role, candidate=self.cand, cv=self.cv)
        create_pending_score(role=self.role, candidate=cand2, cv=cv2)
        summary = score_role(self.role.pk, service=fake_service())
        self.assertEqual(summary.scored, 2)
        self.assertEqual(summary.failed, 0)


# --------------------------------------------------------------------------- #
# procrastinate task layer (thin wrappers)                                     #
# --------------------------------------------------------------------------- #
class ScoreTaskTests(TestCase):
    def setUp(self):
        self.rubric = Rubric.objects.create(version=1, criteria=RUBRIC, is_active=True)
        self.cand = Candidate.objects.create(full_name="A", email="a@x.com")
        self.cv = CV.objects.create(candidate=self.cand, parsed_text="cv body")
        self.role = Role.objects.create(title="Backend", jd_text="jd")

    def test_score_candidate_task_runs_inline(self):
        from ats import tasks

        score = create_pending_score(role=self.role, candidate=self.cand, cv=self.cv)
        # Tasks are callable directly (synchronous run). Patch the service the
        # task pulls so it uses the fake client, not the real API.
        with mock.patch.object(service_mod, "get_default_service",
                               lambda *a, **k: fake_service()):
            tasks.score_candidate(score_id=score.pk)
        score.refresh_from_db()
        self.assertEqual(score.status, Score.Status.SCORED)
        self.assertEqual(score.overall, 82.0)

    def test_score_role_task_defers_one_job_per_pending(self):
        from procrastinate.contrib.django.models import ProcrastinateJob

        from ats import tasks

        cand2 = Candidate.objects.create(full_name="B", email="b@x.com")
        cv2 = CV.objects.create(candidate=cand2, parsed_text="cv2")
        create_pending_score(role=self.role, candidate=self.cand, cv=self.cv)
        create_pending_score(role=self.role, candidate=cand2, cv=cv2)

        count = tasks.score_role(role_id=self.role.pk)  # runs inline, fans out

        self.assertEqual(count, 2)
        self.assertEqual(
            ProcrastinateJob.objects.filter(task_name="score_candidate").count(), 2
        )
