"""Scoring orchestration — plain functions, no queue dependency.

The procrastinate task layer (tasks.py) is a thin wrapper over these so the real
logic is unit-testable with a fake service and no worker/API key.

A role's "batch" is simply its Score rows. Uploading a CV for a role creates a
PENDING Score (idempotent on the unique key). Scoring a role processes the
PENDING (and retryable FAILED) rows.
"""

from __future__ import annotations

from dataclasses import dataclass

from ats.models import CV, Candidate, Role, Rubric, Score

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
