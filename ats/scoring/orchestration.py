"""Scoring orchestration — plain functions, no queue dependency.

The procrastinate task layer (tasks.py) is a thin wrapper over these so the real
logic is unit-testable with a fake service and no worker/API key.

A role's "batch" is simply its Score rows. Uploading a CV for a role creates a
PENDING Score (idempotent on the unique key). Scoring a role processes the
PENDING (and retryable FAILED) rows.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction

from ats.models import (
    CV,
    AnonymizedCV,
    Candidate,
    CandidateUpload,
    Evaluation,
    PipelineCard,
    Role,
    Rubric,
    Score,
    Stage,
    ScreeningSet,
)

DEFAULT_STAGES = ["New", "Screening", "Shortlisted", "Submitted", "Rejected"]


def ensure_default_stages(role: Role) -> None:
    """Give a role its default kanban lanes if it has none yet."""
    if not role.stages.exists():
        Stage.objects.bulk_create(
            [Stage(role=role, name=name, position=i) for i, name in enumerate(DEFAULT_STAGES)]
        )


def ensure_pipeline_card(*, role: Role, candidate: Candidate) -> PipelineCard:
    """Idempotently put a candidate on the role's board, in the first lane."""
    ensure_default_stages(role)
    card = PipelineCard.objects.filter(role=role, candidate=candidate).first()
    if card:
        return card
    first_stage = role.stages.first()
    last_pos = (
        PipelineCard.objects.filter(role=role, stage=first_stage)
        .order_by("-position").values_list("position", flat=True).first()
    )
    return PipelineCard.objects.create(
        role=role, candidate=candidate, stage=first_stage,
        position=(last_pos + 1) if last_pos is not None else 0,
    )

from .anonymize import AnonymizationService
from .contact import extract_contact
from .evaluate import EvaluationService
from .screening import ScreeningService
from .service import ScoringService


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
    # Put the candidate on the role's kanban board (first lane) if not already.
    ensure_pipeline_card(role=role, candidate=candidate)
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
    except Exception as exc:
        # ANY failure (validation OR an Anthropic API error like a missing/invalid
        # key, which the SDK has already retried for transient cases) must mark the
        # row FAILED with the message — never leave it silently stuck PENDING.
        score.mark_failed(exc)
        return score

    # Persist under a row lock and re-check: a concurrent duplicate job for the
    # same score_id may have scored this row while our API call was in flight.
    # Without this, the loser hits the immutability guard and the job errors.
    with transaction.atomic():
        locked = Score.objects.select_for_update().get(pk=score.pk)
        if locked.status == Score.Status.SCORED:
            return locked  # another job won the race — clean no-op
        locked.mark_scored(
            overall=result.overall,
            per_criterion=result.per_criterion,
            confidence=result.confidence,
            model_version=result.model_version,
            token_cost=result.token_cost,
        )
        return locked


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
    except Exception as exc:  # incl. Anthropic API errors -> visible FAILED, not stuck
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
    except Exception as exc:  # incl. Anthropic API errors -> visible FAILED, not stuck
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
    except Exception as exc:  # incl. Anthropic API errors -> visible FAILED, not stuck
        ev.mark_failed(exc)
        return ev
    ev.mark_generated(result=result, model_version=model_version)
    return ev


# --------------------------------------------------------------------------- #
# Bulk upload intake (background)                                             #
# --------------------------------------------------------------------------- #
def process_upload(upload_id: int, *, contact_service=None) -> Score | None:
    """Process one staged CV upload in the background: extract contact from the
    already-parsed text -> create Candidate + CV + pending Score. Returns the
    Score (for the task to enqueue scoring) or None if it failed.

    The CV text is parsed by the web at upload time (parsing is fast/local) and
    stored on the upload row, so the worker needs no filesystem access — it works
    even though the upload volume isn't attached to the worker service.
    """
    upload = CandidateUpload.objects.select_related("role").get(pk=upload_id)
    if upload.status == CandidateUpload.Status.DONE:
        return None
    upload.status = CandidateUpload.Status.PROCESSING
    upload.save(update_fields=["status"])

    text = (upload.parsed_text or "").strip()
    if not text:
        upload.mark_failed("Couldn't read the CV (no extractable text — try a text-based PDF).")
        return None

    info = extract_contact(text, service=contact_service)
    email = (info.get("email") or "").strip().lower()
    if not email:
        upload.mark_failed("No email found in the CV.")
        return None
    name = info.get("full_name") or email.split("@", 1)[0]

    candidate, _ = Candidate.objects.get_or_create(
        email=email, defaults={"full_name": name, "phone": info.get("phone", "")}
    )
    # cv.raw_file references the file the web stored on the upload (same path on
    # the volume); the worker writes only the reference, never the bytes.
    cv = CV.objects.create(
        candidate=candidate, raw_file=(upload.raw_file or None),
        parsed_text=text, parser_version="bulk-upload",
    )

    score = None
    if upload.role_id:  # role upload -> also create a pending Score (the task scores it)
        try:
            score = create_pending_score(role=upload.role, candidate=candidate, cv=cv)
        except NoActiveRubric:
            upload.mark_failed("No active rubric — activate one and re-upload.")
            return None

    upload.candidate = candidate
    upload.error = ""
    upload.status = CandidateUpload.Status.DONE
    upload.save()
    return score  # None for global uploads (no scoring)
