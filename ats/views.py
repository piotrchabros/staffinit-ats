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

from django.conf import settings as django_settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, F, Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from ats import tasks
from ats.forms import AddCandidateForm, PasteTextForm, RoleForm
from ats.ingestion.parse import extract_text
from ats.scoring import contact
from ats.models import (
    AnonymizedCV,
    CV,
    Candidate,
    Evaluation,
    Role,
    Rubric,
    Score,
    ScreeningSet,
)
from ats.scoring.orchestration import (
    NoActiveRubric,
    create_pending_score,
    ensure_anonymized_cv,
    ensure_screening_set,
    pending_score_ids,
    set_transcript,
)


def _role_candidate_or_404(role, candidate_id):
    """Return a Candidate that is actually on this role (has a Score), else 404.

    Authz scoping: prevents viewing/acting on candidates reached by guessing an
    id that belongs to a different role (cross-role PII + actions).
    """
    candidate = get_object_or_404(Candidate, pk=candidate_id)
    if not Score.objects.filter(role=role, candidate=candidate).exists():
        raise Http404("Candidate is not on this role.")
    return candidate


def _latest_score(role, candidate):
    """The most recent Score for (role, candidate), with its CV. One definition,
    used by every per-candidate view so they always act on the same CV."""
    return (
        Score.objects.filter(role=role, candidate=candidate)
        .select_related("cv")
        .order_by("-created_at")
        .first()
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

    # One aggregate query instead of three separate COUNTs.
    counts = scores.aggregate(
        scored=Count("pk", filter=Q(status=Score.Status.SCORED)),
        pending=Count("pk", filter=Q(status=Score.Status.PENDING)),
        failed=Count("pk", filter=Q(status=Score.Status.FAILED)),
    )

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

    files = request.FILES.getlist("cv_files")
    pasted = form.cleaned_data.get("pasted_text", "").strip()
    inputs = [("file", f) for f in files]
    if pasted:
        inputs.append(("paste", pasted))
    # The manual name/email are a fallback only when there's a single input —
    # one fallback email can't apply to a batch of files.
    single = len(inputs) == 1
    fb_name = form.cleaned_data.get("full_name", "").strip() if single else ""
    fb_email = form.cleaned_data.get("email", "").strip().lower() if single else ""

    queued, waiting, skipped = [], [], []
    for kind, payload in inputs:
        outcome, label = _intake_cv(role, kind, payload, fb_name=fb_name, fb_email=fb_email)
        {"queued": queued, "waiting": waiting, "skipped": skipped}[outcome].append(label)

    if queued:
        messages.success(request, f"Added & queued for scoring: {', '.join(queued)}.")
    if waiting:
        messages.info(request, f"Added, awaiting CV text (unreadable file): {', '.join(waiting)}.")
    if skipped:
        messages.warning(request, f"Skipped (no email found): {', '.join(skipped)}.")
    return redirect("role_detail", pk=role.pk)


def _intake_cv(role, kind, payload, *, fb_name, fb_email):
    """Bring one CV (uploaded file or pasted text) into the role.

    Auto-extracts name/email from the CV; falls back to fb_name/fb_email. Returns
    (outcome, label) where outcome is 'queued' | 'waiting' | 'skipped'.
    """
    if kind == "file":
        data = payload.read()
        result = extract_text(getattr(payload, "name", ""), data)
        text = result.text if result.ok else ""
        parser = result.parser
        source_label = getattr(payload, "name", "file")
    else:  # paste
        text = payload
        parser = "manual-paste"
        source_label = "pasted CV"

    info = contact.extract_contact(text) if text.strip() else {}
    email = (info.get("email") or fb_email or "").strip().lower()
    name = (info.get("full_name") or fb_name or "").strip()
    if not email:
        # Can't create/dedup a candidate without an email.
        return ("skipped", source_label)
    if not name:
        name = email.split("@", 1)[0]  # last resort

    candidate, _ = Candidate.objects.get_or_create(
        email=email, defaults={"full_name": name, "phone": info.get("phone", "")}
    )

    if kind == "file":
        try:
            payload.seek(0)
        except (AttributeError, OSError):
            pass
        cv = CV.objects.create(
            candidate=candidate, raw_file=payload, parsed_text=text, parser_version=parser
        )
    else:
        cv = CV.objects.create(candidate=candidate, parsed_text=text, parser_version=parser)

    score = create_pending_score(role=role, candidate=candidate, cv=cv)
    if cv.parsed_text.strip():
        tasks.score_candidate.defer(score_id=score.pk)
        return ("queued", candidate.full_name)
    return ("waiting", candidate.full_name)  # unreadable file -> needs manual paste


@login_required
@require_POST
def paste_cv(request, pk, cv_id):
    role = get_object_or_404(Role, pk=pk)
    cv = get_object_or_404(CV, pk=cv_id)
    # Authz scoping: the CV must already be part of THIS role's pipeline (it has a
    # Score on this role), else a user could overwrite an unrelated CV's text and
    # attach it to this role.
    if not Score.objects.filter(role=role, cv=cv).exists():
        raise Http404("CV is not part of this role.")
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
    candidate = _role_candidate_or_404(role, candidate_id)
    sset = ScreeningSet.objects.filter(role=role, candidate=candidate).first()
    score = _latest_score(role, candidate)
    return render(request, "ats/screening.html", {
        "role": role, "candidate": candidate, "screening": sset, "score": score,
    })


@login_required
@require_POST
def generate_screening(request, pk, candidate_id):
    role = get_object_or_404(Role, pk=pk)
    candidate = _role_candidate_or_404(role, candidate_id)
    score = _latest_score(role, candidate)
    if score is None or not score.cv.parsed_text.strip():
        messages.error(request, "This candidate has no scored CV text to base questions on.")
        return redirect("screening_detail", pk=role.pk, candidate_id=candidate.pk)
    sset = ensure_screening_set(role=role, candidate=candidate, cv=score.cv)
    tasks.generate_screening_task.defer(screening_id=sset.pk)
    messages.success(request, f"Generating screening questions for {candidate.full_name}.")
    return redirect("screening_detail", pk=role.pk, candidate_id=candidate.pk)


@login_required
def anonymized_cv_detail(request, pk, candidate_id):
    """Branded, anonymized CV page for client submission."""
    role = get_object_or_404(Role, pk=pk)
    candidate = _role_candidate_or_404(role, candidate_id)
    acv = AnonymizedCV.objects.filter(role=role, candidate=candidate).first()
    return render(request, "ats/anonymized_cv.html", {
        "role": role, "candidate": candidate, "anon": acv,
        "agency_name": getattr(django_settings, "AGENCY_NAME", "StaffInit"),
    })


@login_required
@require_POST
def generate_anonymized_cv(request, pk, candidate_id):
    role = get_object_or_404(Role, pk=pk)
    candidate = _role_candidate_or_404(role, candidate_id)
    score = _latest_score(role, candidate)
    if score is None or not score.cv.parsed_text.strip():
        messages.error(request, "This candidate has no CV text to anonymize.")
        return redirect("anonymized_cv_detail", pk=role.pk, candidate_id=candidate.pk)
    acv = ensure_anonymized_cv(role=role, candidate=candidate, cv=score.cv)
    tasks.generate_anonymized_cv_task.defer(anon_id=acv.pk)
    messages.success(request, f"Generating anonymized CV for {candidate.full_name}.")
    return redirect("anonymized_cv_detail", pk=role.pk, candidate_id=candidate.pk)


@login_required
def evaluation_detail(request, pk, candidate_id):
    """Transcript-based evaluation page for one candidate."""
    role = get_object_or_404(Role, pk=pk)
    candidate = _role_candidate_or_404(role, candidate_id)
    ev = Evaluation.objects.filter(role=role, candidate=candidate).first()
    return render(request, "ats/evaluation.html", {
        "role": role, "candidate": candidate, "evaluation": ev,
    })


@login_required
@require_POST
def generate_evaluation(request, pk, candidate_id):
    role = get_object_or_404(Role, pk=pk)
    candidate = _role_candidate_or_404(role, candidate_id)
    transcript = request.POST.get("transcript", "").strip()
    if not transcript:
        messages.error(request, "Paste the screening-call transcript first.")
        return redirect("evaluation_detail", pk=role.pk, candidate_id=candidate.pk)
    score = _latest_score(role, candidate)
    if score is None:
        messages.error(request, "Score this candidate first so the evaluation has a CV.")
        return redirect("evaluation_detail", pk=role.pk, candidate_id=candidate.pk)
    ev = set_transcript(role=role, candidate=candidate, cv=score.cv, transcript=transcript)
    tasks.generate_evaluation_task.defer(eval_id=ev.pk)
    messages.success(request, f"Evaluating {candidate.full_name} from the transcript.")
    return redirect("evaluation_detail", pk=role.pk, candidate_id=candidate.pk)
