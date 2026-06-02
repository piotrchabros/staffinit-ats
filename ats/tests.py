"""Lane A model tests — the system-of-record guarantees.

Each test maps to a promise from the eng review:
  - dedup on normalized email
  - idempotency: one score per (role, candidate, rubric, cv)
  - re-score under a new rubric version = new row (not a mutation)
  - SCORED results are immutable
  - exactly one active rubric
  - a rubric referenced by a score cannot be deleted (history preserved)
  - erasure: deleting a Candidate cascades to CVs and Scores
"""

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError
from django.test import TestCase

from .models import CV, Candidate, Role, Rubric, Score


def make_rubric(version=1, active=True):
    return Rubric.objects.create(
        version=version,
        name=f"v{version}",
        criteria=[{"name": "Python", "weight": 1.0, "scale": 5}],
        is_active=active,
    )


class CandidateDedupTests(TestCase):
    def test_email_normalized_on_save(self):
        c = Candidate.objects.create(full_name="Jan K", email="  Jan@Example.COM ")
        c.refresh_from_db()
        self.assertEqual(c.email, "jan@example.com")

    def test_duplicate_email_case_insensitive_rejected(self):
        Candidate.objects.create(full_name="Jan K", email="jan@example.com")
        with transaction.atomic(), self.assertRaises(IntegrityError):
            Candidate.objects.create(full_name="Jan Kowalski", email="JAN@example.com")


class RubricTests(TestCase):
    def test_only_one_active_rubric_allowed(self):
        make_rubric(version=1, active=True)
        with transaction.atomic(), self.assertRaises(IntegrityError):
            make_rubric(version=2, active=True)

    def test_many_inactive_rubrics_allowed(self):
        make_rubric(version=1, active=True)
        make_rubric(version=2, active=False)
        make_rubric(version=3, active=False)
        self.assertEqual(Rubric.objects.filter(is_active=False).count(), 2)
        self.assertEqual(Rubric.active().version, 1)


class ScoreIdempotencyTests(TestCase):
    def setUp(self):
        self.rubric = make_rubric()
        self.cand = Candidate.objects.create(full_name="A", email="a@x.com")
        self.cv = CV.objects.create(candidate=self.cand, parsed_text="cv text")
        self.role = Role.objects.create(title="Backend Dev", jd_text="jd")

    def test_duplicate_score_same_key_rejected(self):
        Score.objects.create(
            role=self.role, candidate=self.cand, cv=self.cv, rubric=self.rubric
        )
        with transaction.atomic(), self.assertRaises(IntegrityError):
            Score.objects.create(
                role=self.role, candidate=self.cand, cv=self.cv, rubric=self.rubric
            )

    def test_rescore_under_new_rubric_version_creates_new_row(self):
        Score.objects.create(
            role=self.role, candidate=self.cand, cv=self.cv, rubric=self.rubric
        )
        rubric_v2 = make_rubric(version=2, active=False)
        # New rubric version -> different unique key -> allowed.
        Score.objects.create(
            role=self.role, candidate=self.cand, cv=self.cv, rubric=rubric_v2
        )
        self.assertEqual(
            Score.objects.filter(role=self.role, candidate=self.cand).count(), 2
        )

    def test_same_person_two_roles_is_one_candidate_two_scores(self):
        role2 = Role.objects.create(title="Frontend Dev", jd_text="jd2")
        Score.objects.create(
            role=self.role, candidate=self.cand, cv=self.cv, rubric=self.rubric
        )
        Score.objects.create(
            role=role2, candidate=self.cand, cv=self.cv, rubric=self.rubric
        )
        self.assertEqual(Candidate.objects.count(), 1)
        self.assertEqual(self.cand.scores.count(), 2)


class ScoreImmutabilityTests(TestCase):
    def setUp(self):
        self.rubric = make_rubric()
        self.cand = Candidate.objects.create(full_name="A", email="a@x.com")
        self.cv = CV.objects.create(candidate=self.cand, parsed_text="cv text")
        self.role = Role.objects.create(title="Backend Dev", jd_text="jd")
        self.score = Score.objects.create(
            role=self.role, candidate=self.cand, cv=self.cv, rubric=self.rubric
        )

    def test_pending_to_scored_then_frozen(self):
        self.score.mark_scored(
            overall=4.2,
            per_criterion={"Python": {"score": 4, "rationale": "solid"}},
            confidence=0.8,
            model_version="claude-opus-4-8",
        )
        self.score.refresh_from_db()
        self.assertEqual(self.score.status, Score.Status.SCORED)
        self.assertEqual(self.score.overall, 4.2)
        self.assertIsNotNone(self.score.scored_at)

    def test_cannot_change_overall_once_scored(self):
        self.score.mark_scored(overall=4.2, per_criterion={})
        self.score.overall = 1.0
        with self.assertRaises(ValidationError):
            self.score.save()

    def test_cannot_move_status_away_from_scored(self):
        self.score.mark_scored(overall=4.2, per_criterion={})
        self.score.status = Score.Status.PENDING
        with self.assertRaises(ValidationError):
            self.score.save()

    def test_failed_can_retry_back_to_pending(self):
        self.score.mark_failed("rate limited")
        self.score.refresh_from_db()
        self.assertEqual(self.score.status, Score.Status.FAILED)
        self.assertEqual(self.score.error, "rate limited")
        # Retry: failed -> pending is allowed (not yet scored).
        self.score.status = Score.Status.PENDING
        self.score.error = ""
        self.score.save()  # must not raise
        self.score.refresh_from_db()
        self.assertEqual(self.score.status, Score.Status.PENDING)


class RubricProtectTests(TestCase):
    def test_cannot_delete_rubric_referenced_by_score(self):
        rubric = make_rubric()
        cand = Candidate.objects.create(full_name="A", email="a@x.com")
        cv = CV.objects.create(candidate=cand, parsed_text="t")
        role = Role.objects.create(title="R", jd_text="jd")
        Score.objects.create(role=role, candidate=cand, cv=cv, rubric=rubric)
        with self.assertRaises(ProtectedError):
            rubric.delete()


class ErasureCascadeTests(TestCase):
    def test_deleting_candidate_cascades_cvs_and_scores(self):
        rubric = make_rubric()
        cand = Candidate.objects.create(full_name="A", email="a@x.com")
        cv = CV.objects.create(candidate=cand, parsed_text="t")
        role = Role.objects.create(title="R", jd_text="jd")
        Score.objects.create(role=role, candidate=cand, cv=cv, rubric=rubric)

        self.assertEqual(CV.objects.count(), 1)
        self.assertEqual(Score.objects.count(), 1)

        cand.delete()

        self.assertEqual(Candidate.objects.count(), 0)
        self.assertEqual(CV.objects.count(), 0)
        self.assertEqual(Score.objects.count(), 0)
        # Rubric survives erasure (it is not personal data).
        self.assertEqual(Rubric.objects.count(), 1)
