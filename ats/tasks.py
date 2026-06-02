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

from ats.scoring.orchestration import pending_score_ids, score_one


@app.task(name="score_candidate")
def score_candidate(*, score_id: int) -> None:
    score_one(score_id)


@app.task(name="score_role")
def score_role(*, role_id: int) -> int:
    """Enqueue a scoring job per not-yet-scored candidate; returns the count."""
    ids = pending_score_ids(role_id)
    for sid in ids:
        score_candidate.defer(score_id=sid)
    return len(ids)
