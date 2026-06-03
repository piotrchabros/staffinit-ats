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

import json
import os

from django.conf import settings as django_settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from ats import tasks
from ats.forms import (
    AddCandidateForm,
    CompanyForm,
    DealForm,
    NewUserForm,
    PasteTextForm,
    PersonForm,
    RoleForm,
)
from ats.ingestion.parse import extract_text
from ats.scoring import contact
from ats.models import (
    AnonymizedCV,
    CV,
    Candidate,
    CandidateUpload,
    Company,
    Deal,
    DealDocument,
    Evaluation,
    Person,
    PipelineCard,
    Role,
    Rubric,
    Score,
    Stage,
    ScreeningSet,
)
from ats.scoring.orchestration import (
    NoActiveRubric,
    create_pending_score,
    ensure_anonymized_cv,
    ensure_default_stages,
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


def _form_errors(form) -> str:
    """Flatten a form's errors into one user-facing string for messages.error."""
    return "; ".join(f"{k}: {', '.join(v)}" for k, v in form.errors.items())


def _overall_key(score):
    """Sort key for picking the best Score: SCORED rows always beat unscored, and
    a real overall of 0.0 ranks correctly (never confused with 'no score')."""
    return (score.overall is not None, score.overall if score.overall is not None else 0.0)


def _serve_fieldfile(fieldfile):
    """Stream a model FileField's bytes inline to the caller, or 404.

    Used for every PII / confidential file (CVs, signed agreements) so they are
    served through an auth-checked view rather than a public MEDIA_URL.
    """
    if not fieldfile:
        raise Http404("No file.")
    try:
        fh = fieldfile.open("rb")
    except (FileNotFoundError, OSError):
        raise Http404("The file is no longer available.")
    return FileResponse(fh, as_attachment=False, filename=os.path.basename(fieldfile.name) or "file")


def _attach_documents(deal, files):
    """Create a DealDocument per uploaded file. Shared by add_deal + add_deal_document."""
    for f in files:
        DealDocument.objects.create(
            deal=deal, file=f, original_filename=(getattr(f, "name", "") or "")[:255]
        )
    return len(files)


# --------------------------------------------------------------------------- #
# User management — provision / remove logins (superuser-only)                 #
# --------------------------------------------------------------------------- #
def _require_root(request):
    """403 unless the caller is a superuser ("root"). Use after @login_required so
    an anonymous user is sent to login, but an authenticated non-admin is denied."""
    if not request.user.is_superuser:
        raise PermissionDenied("Only an administrator can manage users.")


@login_required
def user_list(request):
    """List all logins with an add form. Superuser-only."""
    _require_root(request)
    users = User.objects.order_by("username")
    return render(request, "ats/user_list.html", {
        "users": users, "form": NewUserForm(),
    })


@login_required
@require_POST
def add_user(request):
    _require_root(request)
    form = NewUserForm(request.POST)
    if form.is_valid():
        user = form.save()
        messages.success(request, f"Created user “{user.username}”.")
    else:
        messages.error(request, _form_errors(form))
    return redirect("user_list")


@login_required
@require_POST
def delete_user(request, pk):
    _require_root(request)
    user = get_object_or_404(User, pk=pk)
    # Can't delete yourself — that would risk locking the last admin out.
    if user.pk == request.user.pk:
        messages.error(request, "You can’t delete your own account.")
        return redirect("user_list")
    username = user.username
    user.delete()
    messages.success(request, f"Deleted user “{username}”.")
    return redirect("user_list")


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
    """The role's pipeline — a kanban board of candidate cards across stages."""
    role = get_object_or_404(Role, pk=pk)
    ensure_default_stages(role)
    stages = list(role.stages.all())
    first_stage_id = stages[0].id if stages else None

    # One pass over this role's scores builds, per candidate: the best score
    # (SCORED beats unscored; a real 0.0 ranks correctly), a FAILED score to
    # offer a retry, and a CV awaiting manual text (unreadable upload). These
    # drive the per-card score / retry / paste controls, so we need the rows
    # (not just a max), but we fetch only the fields used.
    best, failed, waiting = {}, {}, {}
    scores = (
        Score.objects.filter(role=role)
        .select_related("cv")
        .only("candidate_id", "status", "overall", "per_criterion",
              "cv__parsed_text", "cv__raw_file")
    )
    for s in scores:
        cur = best.get(s.candidate_id)
        if cur is None or _overall_key(s) > _overall_key(cur):
            best[s.candidate_id] = s
        if s.status == Score.Status.FAILED:
            failed[s.candidate_id] = s
        if s.cv.needs_manual_text:
            waiting[s.candidate_id] = s.cv

    # Archived candidates are hidden from the board (soft-delete contract).
    cards = list(
        PipelineCard.objects.filter(role=role, candidate__is_archived=False)
        .select_related("candidate", "stage")
        .prefetch_related("candidate__cvs")  # backs candidate.original_cv on each card
    )
    by_stage = {st.id: [] for st in stages}
    for c in cards:
        c.best_score = best.get(c.candidate_id)
        c.failed_score = failed.get(c.candidate_id)
        c.waiting_cv = waiting.get(c.candidate_id)
        # Cards orphaned by a deleted lane (stage_id None) fall to the first lane.
        by_stage[c.stage_id if c.stage_id in by_stage else first_stage_id].append(c)
    board = [(st, by_stage[st.id]) for st in stages]

    uploads = CandidateUpload.objects.filter(role=role)
    return render(request, "ats/role_detail.html", {
        "role": role,
        "board": board,
        "stages": stages,
        "card_count": len(cards),
        "active_rubric": Rubric.active(),
        "add_form": AddCandidateForm(),
        "processing_count": uploads.filter(
            status__in=[CandidateUpload.Status.PENDING, CandidateUpload.Status.PROCESSING]
        ).count(),
        "failed_uploads": uploads.filter(status=CandidateUpload.Status.FAILED),
    })


@login_required
@require_POST
def move_card(request, pk):
    """AJAX: persist a card's lane + ordering after a drag-drop."""
    role = get_object_or_404(Role, pk=pk)
    stage = get_object_or_404(Stage, pk=request.POST.get("stage_id"), role=role)
    try:
        card_ids = json.loads(request.POST.get("card_ids", "[]"))
    except (ValueError, TypeError):
        return JsonResponse({"ok": False, "error": "bad card_ids"}, status=400)
    # Atomic so an interrupted request can't leave the lane half-reordered.
    with transaction.atomic():
        for i, cid in enumerate(card_ids):
            PipelineCard.objects.filter(pk=cid, role=role).update(stage=stage, position=i)
    return JsonResponse({"ok": True})


@login_required
@require_POST
def add_stage(request, pk):
    role = get_object_or_404(Role, pk=pk)
    name = (request.POST.get("name") or "").strip()[:100]
    if name:
        last = role.stages.order_by("-position").values_list("position", flat=True).first()
        try:
            # atomic so a constraint violation rolls back cleanly without
            # poisoning the surrounding transaction.
            with transaction.atomic():
                Stage.objects.create(role=role, name=name, position=(last + 1) if last is not None else 0)
        except IntegrityError:  # uniq_stage_role_name — a lane with this name exists
            messages.error(request, f"This role already has a “{name}” lane.")
    return redirect("role_detail", pk=role.pk)


@login_required
@require_POST
def rename_stage(request, pk, stage_id):
    role = get_object_or_404(Role, pk=pk)
    stage = get_object_or_404(Stage, pk=stage_id, role=role)
    name = (request.POST.get("name") or "").strip()[:100]
    if name and name != stage.name:
        stage.name = name
        try:
            with transaction.atomic():
                stage.save(update_fields=["name"])
        except IntegrityError:  # uniq_stage_role_name
            messages.error(request, f"This role already has a “{name}” lane.")
    return redirect("role_detail", pk=role.pk)


@login_required
@require_POST
def delete_stage(request, pk, stage_id):
    role = get_object_or_404(Role, pk=pk)
    stage = get_object_or_404(Stage, pk=stage_id, role=role)
    other = role.stages.exclude(pk=stage.pk).first()
    if other is None:
        messages.error(request, "A role needs at least one lane.")
        return redirect("role_detail", pk=role.pk)
    # Move this lane's cards to another lane, then delete it.
    PipelineCard.objects.filter(role=role, stage=stage).update(stage=other)
    stage.delete()
    messages.success(request, f"Lane removed; its candidates moved to “{other.name}”.")
    return redirect("role_detail", pk=role.pk)


def _stage_upload(f, *, role=None):
    """Parse a dropped CV (fast, local), store the original file, and queue the
    background job that extracts contact + creates the Candidate (+ Score if a
    role). Shared by role-scoped and global (role-less) upload."""
    data = f.read()
    result = extract_text((getattr(f, "name", "") or ""), data)
    try:
        f.seek(0)
    except (AttributeError, OSError):
        pass
    upload = CandidateUpload.objects.create(
        role=role,
        raw_file=f,
        original_filename=(getattr(f, "name", "") or "")[:255],
        parsed_text=result.text if result.ok else "",
    )
    tasks.process_candidate_upload.defer(upload_id=upload.pk)
    return upload


@login_required
def candidate_list(request):
    """Global, searchable candidate database (across all roles).

    Archived (soft-deleted) candidates are hidden unless ?archived=1.
    """
    q = (request.GET.get("q") or "").strip()
    show_archived = request.GET.get("archived") == "1"
    candidates = Candidate.objects.filter(is_archived=show_archived)
    if q:
        candidates = candidates.filter(
            Q(full_name__icontains=q) | Q(email__icontains=q) | Q(cvs__parsed_text__icontains=q)
        ).distinct()
    candidates = (
        candidates.annotate(n_cvs=Count("cvs", distinct=True))
        .prefetch_related("cvs")  # backs Candidate.original_cv without N+1
        .order_by("full_name")[:300]
    )

    global_uploads = CandidateUpload.objects.filter(role__isnull=True)
    return render(request, "ats/candidate_list.html", {
        "candidates": candidates,
        "q": q,
        "show_archived": show_archived,
        "archived_count": Candidate.objects.filter(is_archived=True).count(),
        "processing_count": global_uploads.filter(
            status__in=[CandidateUpload.Status.PENDING, CandidateUpload.Status.PROCESSING]
        ).count(),
        "failed_uploads": global_uploads.filter(status=CandidateUpload.Status.FAILED),
    })


@login_required
def cv_file(request, cv_id):
    """Stream a CV's original uploaded file (inline) to logged-in staff.

    Candidate CVs are PII, so the file is served through this auth-gated view
    rather than a public MEDIA_URL — only authenticated recruiters can open it.
    """
    cv = get_object_or_404(CV, pk=cv_id)
    if not cv.raw_file:
        raise Http404("No original file for this CV (it was pasted as text).")
    return _serve_fieldfile(cv.raw_file)


@login_required
def deal_document_file(request, doc_id):
    """Stream a signed-agreement / deal document to logged-in staff.

    These files hold confidential commercial terms (salary, client rate), so —
    like CVs — they are served through this auth-gated view, never a public
    MEDIA_URL (MEDIA is not served by the production image at all).
    """
    doc = get_object_or_404(DealDocument, pk=doc_id)
    return _serve_fieldfile(doc.file)


@login_required
@require_POST
def archive_candidate(request, pk):
    """Soft-delete: hide a candidate from the default database view."""
    candidate = get_object_or_404(Candidate, pk=pk)
    candidate.is_archived = True
    candidate.save(update_fields=["is_archived"])
    messages.success(request, f"Archived {candidate.full_name}.")
    return redirect("candidate_list")


@login_required
@require_POST
def unarchive_candidate(request, pk):
    candidate = get_object_or_404(Candidate, pk=pk)
    candidate.is_archived = False
    candidate.save(update_fields=["is_archived"])
    messages.success(request, f"Restored {candidate.full_name}.")
    return redirect(f"{reverse('candidate_list')}?archived=1")


@login_required
@require_POST
def candidate_upload(request):
    """Role-less bulk upload — just add candidates to the database (no scoring)."""
    files = request.FILES.getlist("cv_files")
    for f in files:
        _stage_upload(f, role=None)
    if files:
        messages.success(
            request,
            f"Uploading {len(files)} CV(s) — extracting details in the background. "
            "Refresh to see them appear.",
        )
    else:
        messages.error(request, "Drop at least one CV file.")
    return redirect("candidate_list")


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
        messages.error(request, _form_errors(form))
        return redirect("role_detail", pk=role.pk)

    files = request.FILES.getlist("cv_files")
    pasted = form.cleaned_data.get("pasted_text", "").strip()

    # Files: parse here (fast, local), store the original, then defer the slow
    # LLM work (contact extraction + scoring) to the worker.
    for f in files:
        _stage_upload(f, role=role)
    if files:
        messages.success(
            request,
            f"Uploading {len(files)} CV(s) — extracting details and scoring in the "
            "background. Refresh to see them appear.",
        )

    # Paste: a single manual CV, processed inline (fast). Manual name/email here
    # are the fallback when the pasted text has no detectable email.
    if pasted:
        outcome, label = _intake_cv(
            role, "paste", pasted,
            fb_name=form.cleaned_data.get("full_name", "").strip(),
            fb_email=form.cleaned_data.get("email", "").strip().lower(),
        )
        if outcome == "queued":
            messages.success(request, f"Added & queued for scoring: {label}.")
        elif outcome == "waiting":
            messages.info(request, f"Added, awaiting CV text: {label}.")
        else:
            messages.warning(request, f"Skipped (no email found): {label}.")
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


# --------------------------------------------------------------------------- #
# Mini-CRM: companies -> people -> deals (+ documents)                        #
# --------------------------------------------------------------------------- #
@login_required
def company_list(request):
    """Searchable list of customer companies, with deal counts.

    Archived (soft-deleted) companies are hidden unless ?archived=1.
    """
    q = (request.GET.get("q") or "").strip()
    show_archived = request.GET.get("archived") == "1"
    companies = Company.objects.filter(is_archived=show_archived)
    if q:
        companies = companies.filter(
            Q(name__icontains=q) | Q(people__full_name__icontains=q)
        ).distinct()
    companies = companies.annotate(
        n_people=Count("people", distinct=True),
        n_deals=Count("deals", distinct=True),
    ).order_by("name")
    return render(request, "ats/company_list.html", {
        "companies": companies, "q": q, "form": CompanyForm(),
        "show_archived": show_archived,
        "archived_count": Company.objects.filter(is_archived=True).count(),
    })


@login_required
@require_POST
def add_company(request):
    form = CompanyForm(request.POST)
    if form.is_valid():
        company = form.save()
        messages.success(request, f"Added company “{company.name}”.")
        return redirect("company_detail", pk=company.pk)
    messages.error(request, _form_errors(form))
    return redirect("company_list")


@login_required
@require_POST
def archive_company(request, pk):
    """Soft-delete: hide a company (and implicitly its contacts/deals) from the
    default CRM view. Everything is kept and can be restored."""
    company = get_object_or_404(Company, pk=pk)
    company.is_archived = True
    company.save(update_fields=["is_archived"])
    messages.success(request, f"Archived “{company.name}”.")
    return redirect("company_list")


@login_required
@require_POST
def unarchive_company(request, pk):
    company = get_object_or_404(Company, pk=pk)
    company.is_archived = False
    company.save(update_fields=["is_archived"])
    messages.success(request, f"Restored “{company.name}”.")
    return redirect(f"{reverse('company_list')}?archived=1")


@login_required
def company_detail(request, pk):
    """A company with its contacts and signed deals (+ add forms)."""
    company = get_object_or_404(Company, pk=pk)
    deals = company.deals.prefetch_related("documents")
    return render(request, "ats/company_detail.html", {
        "company": company,
        "people": company.people.all(),
        "deals": deals,
        "person_form": PersonForm(),
        "deal_form": DealForm(),
    })


@login_required
@require_POST
def add_person(request, pk):
    company = get_object_or_404(Company, pk=pk)
    form = PersonForm(request.POST)
    if form.is_valid():
        person = form.save(commit=False)
        person.company = company
        person.save()
        messages.success(request, f"Added contact {person.full_name}.")
    else:
        messages.error(request, _form_errors(form))
    return redirect("company_detail", pk=company.pk)


@login_required
@require_POST
def delete_person(request, pk):
    """Remove a contact from its company. Contacts hold no downstream records
    (deals link to the company, not the person), so this is a plain hard delete."""
    person = get_object_or_404(Person, pk=pk)
    company_pk = person.company_id
    name = person.full_name
    person.delete()
    messages.success(request, f"Deleted contact {name}.")
    return redirect("company_detail", pk=company_pk)


@login_required
@require_POST
def add_deal(request, pk):
    company = get_object_or_404(Company, pk=pk)
    form = DealForm(request.POST)
    if form.is_valid():
        deal = form.save(commit=False)
        deal.company = company
        deal.save()
        # Optional documents dropped alongside the deal form.
        _attach_documents(deal, request.FILES.getlist("documents"))
        messages.success(request, f"Signed deal recorded: {deal.developer_name}.")
        return redirect("deal_detail", pk=deal.pk)
    messages.error(request, _form_errors(form))
    return redirect("company_detail", pk=company.pk)


@login_required
def deal_detail(request, pk):
    """A single deal with its economics and uploaded agreements."""
    deal = get_object_or_404(Deal.objects.select_related("company"), pk=pk)
    return render(request, "ats/deal_detail.html", {
        "deal": deal, "documents": deal.documents.all(),
    })


@login_required
@require_POST
def add_deal_document(request, pk):
    deal = get_object_or_404(Deal, pk=pk)
    n = _attach_documents(deal, request.FILES.getlist("documents"))
    if n:
        messages.success(request, f"Uploaded {n} document(s).")
    else:
        messages.error(request, "Choose at least one file to upload.")
    return redirect("deal_detail", pk=deal.pk)
