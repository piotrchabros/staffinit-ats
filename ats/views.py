"""Lane E views — role list/create + the scorecard, plus the upload flow that
wires ingestion -> pending Score -> background scoring together.

All views require login (Lane D): candidate PII never renders for anonymous users.

Scorecard request flow:

    role_detail ──GET──▶ scores for (role, selected rubric version), overall desc
        │
        ├─ add_candidate ─▶ Candidate(get_or_create) + ingest CV + pending Score
        │                    └─ CV has text? defer score_candidate : wait for paste
        ├─ paste_cv ───────▶ fill CV text + pending Score + defer score_candidate
        ├─ score_role ─────▶ defer score_role task (fans out pending)
        └─ retry_score ────▶ defer score_candidate for a FAILED row
"""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, F, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from ats import tasks
from ats.forms import AddCandidateForm, PasteTextForm, RoleForm
from ats.ingestion.ingest import ingest_cv_file, ingest_pasted_cv
from ats.models import CV, Candidate, Role, Rubric, Score, ScreeningSet
from ats.scoring.orchestration import (
    NoActiveRubric,
    create_pending_score,
    ensure_screening_set,
    pending_score_ids,
)


@login_required
def role_list(request):
    roles = Role.objects.annotate(
        n_candidates=Count("scores", distinct=True),
        n_scored=Count("scores", filter=Q(scores__status=Score.Status.SCORED)),
    ).order_by("-created_at")
    return render(request, "ats/role_list.html", {"roles": roles})


@login_required
def role_create(request):
    if request.method == "POST":
        form = RoleForm(request.POST)
        if form.is_valid():
            role = form.save()
            # JD extraction is a live API call -> run it in the background so role
            # creation never blocks the request (and tests never hit the network).
            tasks.extract_role_requirements.defer(role_id=role.pk)
            messages.success(request, f"Role '{role.title}' created.")
            return redirect("role_detail", pk=role.pk)
    else:
        form = RoleForm()
    return render(request, "ats/role_form.html", {"form": form})


@login_required
def role_detail(request, pk):
    role = get_object_or_404(Role, pk=pk)

    versions = list(
        Score.objects.filter(role=role)
        .values_list("rubric__version", flat=True)
        .distinct()
        .order_by("-rubric__version")
    )
    selected = request.GET.get("rubric")
    selected_version = (
        int(selected) if (selected and selected.isdigit())
        else (versions[0] if versions else None)
    )

    scores = Score.objects.filter(role=role).select_related("candidate", "cv", "rubric")
    if selected_version is not None:
        scores = scores.filter(rubric__version=selected_version)
    # Scored first (overall desc), then pending/failed; stable by name.
    scores = scores.order_by(F("overall").desc(nulls_last=True), "candidate__full_name")

    counts = {
        "scored": scores.filter(status=Score.Status.SCORED).count(),
        "pending": scores.filter(status=Score.Status.PENDING).count(),
        "failed": scores.filter(status=Score.Status.FAILED).count(),
    }

    return render(
        request,
        "ats/role_detail.html",
        {
            "role": role,
            "scores": scores,
            "counts": counts,
            "versions": versions,
            "selected_version": selected_version,
            "active_rubric": Rubric.active(),
            "add_form": AddCandidateForm(),
            "paste_form": PasteTextForm(),
        },
    )


@login_required
@require_POST
def add_candidate(request, pk):
    role = get_object_or_404(Role, pk=pk)
    if Rubric.active() is None:
        messages.error(
            request,
            "No active rubric. Create/activate one in the admin before adding candidates.",
        )
        return redirect("role_detail", pk=role.pk)

    form = AddCandidateForm(request.POST, request.FILES)
    if not form.is_valid():
        messages.error(
            request, "; ".join(f"{k}: {', '.join(v)}" for k, v in form.errors.items())
        )
        return redirect("role_detail", pk=role.pk)

    email = form.cleaned_data["email"].strip().lower()
    candidate, _ = Candidate.objects.get_or_create(
        email=email, defaults={"full_name": form.cleaned_data["full_name"]}
    )

    cv_file = form.cleaned_data.get("cv_file")
    if cv_file:
        cv, result = ingest_cv_file(candidate, cv_file)
        if not result.ok:
            messages.warning(
                request, f"{candidate.full_name}: {result.reason} — paste the CV text below."
            )
    else:
        cv = ingest_pasted_cv(candidate, form.cleaned_data["pasted_text"])

    score = create_pending_score(role=role, candidate=candidate, cv=cv)
    if cv.parsed_text.strip():
        tasks.score_candidate.defer(score_id=score.pk)
        messages.success(request, f"{candidate.full_name} added and queued for scoring.")
    else:
        messages.info(request, f"{candidate.full_name} added — waiting for CV text.")
    return redirect("role_detail", pk=role.pk)


@login_required
@require_POST
def paste_cv(request, pk, cv_id):
    role = get_object_or_404(Role, pk=pk)
    cv = get_object_or_404(CV, pk=cv_id)
    form = PasteTextForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Paste text is required.")
        return redirect("role_detail", pk=role.pk)

    cv.parsed_text = form.cleaned_data["parsed_text"].strip()
    cv.parser_version = "manual-paste"
    cv.save(update_fields=["parsed_text", "parser_version"])

    try:
        score = create_pending_score(role=role, candidate=cv.candidate, cv=cv)
    except NoActiveRubric:
        messages.error(request, "No active rubric; cannot score.")
        return redirect("role_detail", pk=role.pk)

    tasks.score_candidate.defer(score_id=score.pk)
    messages.success(
        request, f"{cv.candidate.full_name}: CV text saved and queued for scoring."
    )
    return redirect("role_detail", pk=role.pk)


@login_required
@require_POST
def score_role(request, pk):
    role = get_object_or_404(Role, pk=pk)
    n = len(pending_score_ids(role.pk))  # accurate count; .defer() returns a job id
    tasks.score_role.defer(role_id=role.pk)
    messages.success(request, f"Queued scoring for {n} candidate(s).")
    return redirect("role_detail", pk=role.pk)


@login_required
@require_POST
def retry_score(request, pk, score_id):
    role = get_object_or_404(Role, pk=pk)
    score = get_object_or_404(Score, pk=score_id, role=role)
    tasks.score_candidate.defer(score_id=score.pk)
    messages.success(request, f"Re-queued {score.candidate.full_name} for scoring.")
    return redirect("role_detail", pk=role.pk)


@login_required
def screening_detail(request, pk, candidate_id):
    """Screening prep page for one candidate on a role."""
    role = get_object_or_404(Role, pk=pk)
    candidate = get_object_or_404(Candidate, pk=candidate_id)
    sset = ScreeningSet.objects.filter(role=role, candidate=candidate).first()
    score = (
        Score.objects.filter(role=role, candidate=candidate)
        .select_related("cv").order_by("-created_at").first()
    )
    return render(request, "ats/screening.html", {
        "role": role, "candidate": candidate, "screening": sset, "score": score,
    })


@login_required
@require_POST
def generate_screening(request, pk, candidate_id):
    role = get_object_or_404(Role, pk=pk)
    candidate = get_object_or_404(Candidate, pk=candidate_id)
    score = (
        Score.objects.filter(role=role, candidate=candidate)
        .select_related("cv").order_by("-created_at").first()
    )
    if score is None or not score.cv.parsed_text.strip():
        messages.error(request, "This candidate has no scored CV text to base questions on.")
        return redirect("screening_detail", pk=role.pk, candidate_id=candidate.pk)
    sset = ensure_screening_set(role=role, candidate=candidate, cv=score.cv)
    tasks.generate_screening_task.defer(screening_id=sset.pk)
    messages.success(request, f"Generating screening questions for {candidate.full_name}.")
    return redirect("screening_detail", pk=role.pk, candidate_id=candidate.pk)
