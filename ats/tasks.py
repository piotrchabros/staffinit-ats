"""procrastinate task layer — thin wrappers over scoring.orchestration.

Kept deliberately thin: all real logic lives in ats.scoring.orchestration (plain
functions, unit-tested without a worker or API key). These wrappers exist only to
run that logic in the background queue.

App-level module (ats/tasks.py) so procrastinate's Django autodiscovery finds it.

Concurrency: score_role fans out one score_candidate job per candidate; the worker
caps how many run at once (`manage.py procrastinate worker --concurrency N`), which
is how we bound parallel Claude calls without a broker.
"""

from __future__ import annotations

from procrastinate.contrib.django import app

from ats.scoring.orchestration import (
    generate_anonymized_cv,
    generate_screening,
    pending_score_ids,
    score_one,
)


@app.task(name="extract_requirements")
def extract_role_requirements(*, role_id: int) -> None:
    """Best-effort JD extraction in the background (a live API call), so role
    creation doesn't block the request and tests never hit the network."""
    from ats.models import Role
    from ats.scoring.jd_extract import extract_requirements

    extract_requirements(Role.objects.get(pk=role_id))


@app.task(name="score_candidate")
def score_candidate(*, score_id: int) -> None:
    score_one(score_id)


@app.task(name="generate_screening")
def generate_screening_task(*, screening_id: int) -> None:
    generate_screening(screening_id)


@app.task(name="generate_anonymized_cv")
def generate_anonymized_cv_task(*, anon_id: int) -> None:
    generate_anonymized_cv(anon_id)


@app.task(name="score_role")
def score_role(*, role_id: int) -> int:
    """Enqueue a scoring job per not-yet-scored candidate; returns the count."""
    ids = pending_score_ids(role_id)
    for sid in ids:
        score_candidate.defer(score_id=sid)
    return len(ids)
