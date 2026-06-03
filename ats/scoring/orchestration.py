"""Scoring orchestration — plain functions, no queue dependency.

The procrastinate task layer (tasks.py) is a thin wrapper over these so the real
logic is unit-testable with a fake service and no worker/API key.

A role's "batch" is simply its Score rows. Uploading a CV for a role creates a
PENDING Score (idempotent on the unique key). Scoring a role processes the
PENDING (and retryable FAILED) rows.
"""

from __future__ import annotations

from dataclasses import dataclass

from ats.models import (
    CV,
    AnonymizedCV,
    Candidate,
    Evaluation,
    Role,
    Rubric,
    Score,
    ScreeningSet,
)

from .anonymize import AnonymizationError, AnonymizationService
from .evaluate import EvaluationError, EvaluationService
from .screening import ScreeningError, ScreeningService
from .service import ScoringError, ScoringService


class NoActiveRubric(Exception):
    """Raised when scoring is attempted with no active rubric configured."""


@dataclass
class RoleScoreSummary:
    role_id: int
    scored: int = 0
    failed: int = 0
    skipped: int = 0  # already SCORED


def create_pending_score(
    *, role: Role, candidate: Candidate, cv: CV, rubric: Rubric | None = None
) -> Score:
    """Idempotently create a PENDING Score for (role, candidate, cv, active rubric).

    Calling twice returns the SAME row (the DB unique key guarantees it), so
    double-submits never create duplicates.
    """
    rubric = rubric or Rubric.active()
    if rubric is None:
        raise NoActiveRubric("No active rubric; activate one before scoring.")
    score, _created = Score.objects.get_or_create(
        role=role,
        candidate=candidate,
        cv=cv,
        rubric=rubric,
        defaults={"status": Score.Status.PENDING},
    )
    return score


def pending_score_ids(role_id: int) -> list[int]:
    """IDs of a role's scorable not-yet-scored rows (PENDING + retryable FAILED).

    Excludes rows whose CV has no extracted text yet — those are waiting on a
    manual paste, so scoring them would only produce a guaranteed failure. Used
    by the procrastinate task to fan out one job per candidate.
    """
    return list(
        Score.objects.filter(role_id=role_id)
        .exclude(status=Score.Status.SCORED)
        .exclude(cv__parsed_text="")
        .values_list("pk", flat=True)
    )


def score_one(score_id: int, *, service: ScoringService | None = None) -> Score:
    """Score a single pending row. Already-SCORED rows are a no-op (retry-safe)."""
    score = (
        Score.objects.select_related("role", "candidate", "cv", "rubric")
        .get(pk=score_id)
    )
    if score.status == Score.Status.SCORED:
        return score  # idempotent: never re-score a frozen row

    if service is None:
        from .service import get_default_service

        service = get_default_service()

    try:
        result = service.score(
            jd_text=score.role.jd_text,
            rubric_criteria=score.rubric.criteria,
            cv_text=score.cv.parsed_text,
        )
    except ScoringError as exc:
        score.mark_failed(exc)
        return score

    score.mark_scored(
        overall=result.overall,
        per_criterion=result.per_criterion,
        confidence=result.confidence,
        model_version=result.model_version,
        token_cost=result.token_cost,
    )
    return score


def score_role(role_id: int, *, service: ScoringService | None = None) -> RoleScoreSummary:
    """Score every not-yet-scored row for a role (PENDING + retryable FAILED).

    Sequential here for testability/simplicity; the procrastinate task fans out
    per candidate so a worker can run them with bounded concurrency.
    """
    summary = RoleScoreSummary(role_id=role_id)
    pending = Score.objects.filter(role_id=role_id).exclude(status=Score.Status.SCORED)
    for score in pending:
        result = score_one(score.pk, service=service)
        if result.status == Score.Status.SCORED:
            summary.scored += 1
        else:
            summary.failed += 1
    summary.skipped = Score.objects.filter(
        role_id=role_id, status=Score.Status.SCORED
    ).count() - summary.scored
    return summary


# --------------------------------------------------------------------------- #
# Screening questions (Feature 1)                                             #
# --------------------------------------------------------------------------- #
def _ensure_artifact(model, *, role: Role, candidate: Candidate, cv: CV):
    """Idempotently get the (role, candidate) GeneratedArtifact row, pinning the CV.
    Shared by the screening + anonymized-CV ensure_* functions."""
    obj, created = model.objects.get_or_create(
        role=role, candidate=candidate, defaults={"cv": cv}
    )
    if not created and obj.cv_id != cv.pk:
        obj.cv = cv
        obj.status = model.Status.PENDING
        obj.save(update_fields=["cv", "status"])
    return obj


def ensure_screening_set(*, role: Role, candidate: Candidate, cv: CV) -> ScreeningSet:
    return _ensure_artifact(ScreeningSet, role=role, candidate=candidate, cv=cv)


def generate_screening(screening_id: int, *, service: ScreeningService | None = None) -> ScreeningSet:
    """Generate questions for a screening set. Reuses the active rubric for topic
    context (criteria are just hints to the prompt)."""
    sset = ScreeningSet.objects.select_related("role", "cv").get(pk=screening_id)
    if service is None:
        from .screening import get_default_service

        service = get_default_service()
    rubric = Rubric.active()
    criteria = rubric.criteria if rubric else []
    try:
        questions, model_version = service.generate(
            jd_text=sset.role.jd_text,
            cv_text=sset.cv.parsed_text,
            rubric_criteria=criteria,
        )
    except ScreeningError as exc:
        sset.mark_failed(exc)
        return sset
    sset.mark_generated(questions=questions, model_version=model_version)
    return sset


# --------------------------------------------------------------------------- #
# Anonymized branded CV (Feature 2)                                           #
# --------------------------------------------------------------------------- #
def ensure_anonymized_cv(*, role: Role, candidate: Candidate, cv: CV) -> AnonymizedCV:
    return _ensure_artifact(AnonymizedCV, role=role, candidate=candidate, cv=cv)


def generate_anonymized_cv(anon_id: int, *, service: AnonymizationService | None = None) -> AnonymizedCV:
    acv = AnonymizedCV.objects.select_related("candidate", "cv").get(pk=anon_id)
    if service is None:
        from .anonymize import get_default_service

        service = get_default_service()
    try:
        data, model_version = service.anonymize(
            cv_text=acv.cv.parsed_text,
            candidate_name=acv.candidate.full_name,
            candidate_email=acv.candidate.email,
        )
    except AnonymizationError as exc:
        acv.mark_failed(exc)
        return acv
    acv.mark_generated(data=data, model_version=model_version)
    return acv


# --------------------------------------------------------------------------- #
# Transcript-based evaluation (Feature 3)                                     #
# --------------------------------------------------------------------------- #
def set_transcript(*, role: Role, candidate: Candidate, cv: CV, transcript: str) -> Evaluation:
    """Upsert the (role, candidate) evaluation with a new transcript, reset to pending."""
    ev, _created = Evaluation.objects.get_or_create(
        role=role, candidate=candidate, defaults={"cv": cv}
    )
    ev.cv = cv
    ev.transcript = transcript.strip()
    ev.status = Evaluation.Status.PENDING
    ev.save()
    return ev


def generate_evaluation(eval_id: int, *, service: EvaluationService | None = None) -> Evaluation:
    ev = Evaluation.objects.select_related("role", "cv").get(pk=eval_id)
    if service is None:
        from .evaluate import get_default_service

        service = get_default_service()
    rubric = Rubric.active()
    criteria = rubric.criteria if rubric else []
    try:
        result, model_version = service.evaluate(
            jd_text=ev.role.jd_text,
            cv_text=ev.cv.parsed_text,
            rubric_criteria=criteria,
            transcript=ev.transcript,
        )
    except EvaluationError as exc:
        ev.mark_failed(exc)
        return ev
    ev.mark_generated(result=result, model_version=model_version)
    return ev
