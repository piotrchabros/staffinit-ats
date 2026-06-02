"""Feature 1 tests — AI screening-question generation.

Fake Claude client; no API key needed.
"""

from __future__ import annotations

import types
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from procrastinate.contrib.django.models import ProcrastinateJob

from ats.models import CV, Candidate, Role, Rubric, Score, ScreeningSet
from ats.scoring import screening as screening_mod
from ats.scoring.orchestration import ensure_screening_set, generate_screening
from ats.scoring.screening import ScreeningError, ScreeningService

GOOD = {"questions": [
    {"topic": "Python", "question": "Walk me through the ECS service you built.",
     "what_to_listen_for": "Concrete architecture details, ownership."},
    {"topic": "Leadership", "question": "How did you lead the team of 4?",
     "what_to_listen_for": "Specifics on mentoring and decisions."},
]}


def response(tool_input, *, include_tool=True, model="claude-sonnet-4-6"):
    blocks = []
    if include_tool:
        blocks.append(types.SimpleNamespace(type="tool_use", name="submit_questions", input=tool_input))
    else:
        blocks.append(types.SimpleNamespace(type="text", text="no tool"))
    return types.SimpleNamespace(content=blocks, model=model,
                                 usage=types.SimpleNamespace(input_tokens=1, output_tokens=1))


class _Msgs:
    def __init__(self, resp): self._resp = resp
    def create(self, **kw): return self._resp


class FakeClient:
    def __init__(self, resp): self.messages = _Msgs(resp)


def fake_service(tool_input=GOOD, **kw):
    return ScreeningService(client=FakeClient(response(tool_input, **kw)))


class ScreeningServiceTests(TestCase):
    def test_parses_questions(self):
        qs, model = fake_service().generate(jd_text="jd", cv_text="cv", rubric_criteria=[])
        self.assertEqual(len(qs), 2)
        self.assertEqual(qs[0]["topic"], "Python")
        self.assertTrue(qs[0]["what_to_listen_for"])
        self.assertEqual(model, "claude-sonnet-4-6")

    def test_caches_jd_and_forces_tool(self):
        svc = fake_service()
        svc.generate(jd_text="JD here", cv_text="cv", rubric_criteria=[{"name": "Python", "scale": 5}])
        # capture kwargs
        captured = {}
        svc.client.messages.create = lambda **kw: captured.update(kw) or response(GOOD)
        svc.generate(jd_text="JD here", cv_text="cv", rubric_criteria=[])
        self.assertEqual(captured["tool_choice"], {"type": "tool", "name": "submit_questions"})
        self.assertEqual(captured["system"][0]["cache_control"], {"type": "ephemeral"})

    def test_empty_cv_raises(self):
        with self.assertRaises(ScreeningError):
            fake_service().generate(jd_text="jd", cv_text="  ", rubric_criteria=[])

    def test_missing_tool_raises(self):
        svc = ScreeningService(client=FakeClient(response(GOOD, include_tool=False)))
        with self.assertRaises(ScreeningError):
            svc.generate(jd_text="jd", cv_text="cv", rubric_criteria=[])

    def test_blank_questions_filtered_and_empty_raises(self):
        svc = fake_service({"questions": [{"question": "  ", "what_to_listen_for": "x"}]})
        with self.assertRaises(ScreeningError):
            svc.generate(jd_text="jd", cv_text="cv", rubric_criteria=[])


class ScreeningOrchestrationTests(TestCase):
    def setUp(self):
        self.rubric = Rubric.objects.create(version=1, criteria=[{"name": "Python", "scale": 5}], is_active=True)
        self.cand = Candidate.objects.create(full_name="Anna", email="a@x.com")
        self.cv = CV.objects.create(candidate=self.cand, parsed_text="8y Python, AWS ECS, led team.")
        self.role = Role.objects.create(title="Backend", jd_text="Python/AWS senior role")

    def test_ensure_is_idempotent(self):
        s1 = ensure_screening_set(role=self.role, candidate=self.cand, cv=self.cv)
        s2 = ensure_screening_set(role=self.role, candidate=self.cand, cv=self.cv)
        self.assertEqual(s1.pk, s2.pk)
        self.assertEqual(ScreeningSet.objects.count(), 1)

    def test_new_cv_resets_to_pending(self):
        s = ensure_screening_set(role=self.role, candidate=self.cand, cv=self.cv)
        s.mark_generated(questions=GOOD["questions"], model_version="m")
        cv2 = CV.objects.create(candidate=self.cand, parsed_text="updated CV")
        s2 = ensure_screening_set(role=self.role, candidate=self.cand, cv=cv2)
        self.assertEqual(s.pk, s2.pk)
        self.assertEqual(s2.status, ScreeningSet.Status.PENDING)
        self.assertEqual(s2.cv_id, cv2.pk)

    def test_generate_success(self):
        s = ensure_screening_set(role=self.role, candidate=self.cand, cv=self.cv)
        generate_screening(s.pk, service=fake_service())
        s.refresh_from_db()
        self.assertEqual(s.status, ScreeningSet.Status.GENERATED)
        self.assertEqual(len(s.questions), 2)
        self.assertEqual(s.model_version, "claude-sonnet-4-6")

    def test_generate_failure_marks_failed(self):
        s = ensure_screening_set(role=self.role, candidate=self.cand, cv=self.cv)
        bad = ScreeningService(client=FakeClient(response(GOOD, include_tool=False)))
        generate_screening(s.pk, service=bad)
        s.refresh_from_db()
        self.assertEqual(s.status, ScreeningSet.Status.FAILED)
        self.assertTrue(s.error)


class ScreeningViewTests(TestCase):
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
        resp = self.client.get(reverse("screening_detail", args=[self.role.pk, self.cand.pk]))
        self.assertEqual(resp.status_code, 302)

    def test_detail_renders(self):
        resp = self.client.get(reverse("screening_detail", args=[self.role.pk, self.cand.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Screening questions")

    def test_generate_enqueues(self):
        resp = self.client.post(reverse("generate_screening", args=[self.role.pk, self.cand.pk]))
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(ScreeningSet.objects.filter(role=self.role, candidate=self.cand).exists())
        self.assertEqual(ProcrastinateJob.objects.filter(task_name="generate_screening").count(), 1)

    def test_generate_without_scored_cv_text_blocked(self):
        # A candidate with an empty-text CV (e.g. unparseable) can't be screened.
        c2 = Candidate.objects.create(full_name="Eva", email="e@x.com")
        cv2 = CV.objects.create(candidate=c2, parsed_text="")
        Score.objects.create(role=self.role, candidate=c2, cv=cv2, rubric=self.rubric)
        self.client.post(reverse("generate_screening", args=[self.role.pk, c2.pk]))
        self.assertFalse(ScreeningSet.objects.filter(candidate=c2).exists())
