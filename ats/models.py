"""
StaffInit ATS — core data model (the "system of record" spine).

The whole product thesis is that the AI score is cheap and already solved; the
value is a DURABLE, COMPARABLE store. So the model is built around two rules:

  1. Scores are IMMUTABLE once written. Re-scoring under a new rubric version is
     a NEW row, never an in-place edit. Reproducibility = reading the stored row,
     never re-deriving from the (non-deterministic) LLM.
  2. Exactly one Score per (role, candidate, rubric_version, cv_version), enforced
     by a DB unique constraint. Worker retries and double-clicks can't create
     duplicate or conflicting scores.

Entity map:

    Candidate 1───* CV                         (dedup on normalized email)
        │              │
        │              │   a Score pins ONE candidate, role, cv, and rubric version
        *              *
       Score *──────1 Role
        │
        1
        │
      Rubric  (exactly one is_active=True; versioned; never overwritten)

Erasure (GDPR right-to-erasure): deleting a Candidate cascades to its CVs and
Scores. Rubrics are PROTECTed — you cannot delete a rubric version that scores
still reference, so the historical meaning of a score is never lost.
"""

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


class Candidate(models.Model):
    """A person. Deduped on normalized (lowercased, trimmed) email.

    The same person applying to two roles is ONE Candidate with two Score rows,
    not two Candidates.
    """

    full_name = models.CharField(max_length=255)
    # Email is the dedup key. Normalized in save(); unique at the DB level.
    email = models.EmailField(unique=True)
    phone = models.CharField(max_length=64, blank=True)
    # Soft-delete: archived candidates are hidden from the default database view
    # but kept (scores, CVs, deals stay intact). Erasure is a separate hard delete.
    is_archived = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        ordering = ["full_name"]

    def __str__(self):
        return f"{self.full_name} <{self.email}>"

    def save(self, *args, **kwargs):
        # Normalize the dedup key so "Jan@x.com" and "jan@x.com " collide.
        if self.email:
            self.email = self.email.strip().lower()
        super().save(*args, **kwargs)

    @property
    def original_cv(self):
        """The most recent CV that has an uploaded original file, or None.

        Iterates the (prefetched, -uploaded_at ordered) cvs in Python so it reuses
        a prefetch cache without an extra query per candidate.
        """
        for cv in self.cvs.all():
            if cv.raw_file:
                return cv
        return None


class CV(models.Model):
    """One uploaded (or pasted) CV for a candidate.

    "Versioned" = a NEW row per re-upload of a newer CV for the same candidate.
    The CV used by a scoring run is whichever row that run pinned (Score.cv);
    the "current" CV is simply the latest row by uploaded_at.
    """

    candidate = models.ForeignKey(
        Candidate, on_delete=models.CASCADE, related_name="cvs"
    )
    # raw_file is optional: the manual-paste fallback (for unparseable PDFs,
    # built in Lane C) stores parsed_text with no file.
    raw_file = models.FileField(upload_to="cvs/%Y/%m/", null=True, blank=True)
    parsed_text = models.TextField(
        blank=True, help_text="Extracted plain text the scorer reads."
    )
    parser_version = models.CharField(
        max_length=64, blank=True, help_text="Which extractor produced parsed_text."
    )
    uploaded_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        ordering = ["-uploaded_at"]
        indexes = [models.Index(fields=["candidate", "-uploaded_at"])]

    def __str__(self):
        return f"CV<{self.candidate_id}> @ {self.uploaded_at:%Y-%m-%d}"

    @property
    def needs_manual_text(self) -> bool:
        """True when a file was uploaded but no text could be extracted.

        The UI uses this to prompt for a manual paste (scanned/corrupt PDFs).
        Scoring never runs on an empty CV (it raises ScoringError), so these
        rows simply wait for text.
        """
        return bool(self.raw_file) and not (self.parsed_text or "").strip()


class Rubric(models.Model):
    """The single global scoring rubric, versioned.

    v1 has exactly one ACTIVE rubric shared across all roles (per-role override
    is deferred). Changing the rubric = create a new version and activate it; old
    versions are never edited, so every Score stays interpretable against the
    exact criteria it used.

    criteria shape (JSON):
        [{"name": "Python", "description": "...", "weight": 0.3, "scale": 5}, ...]
    """

    version = models.PositiveIntegerField(unique=True)
    name = models.CharField(max_length=255, blank=True)
    criteria = models.JSONField(default=list)
    is_active = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        ordering = ["-version"]
        constraints = [
            # At most one active rubric. Partial unique index (Postgres): only
            # rows where is_active=True participate, so many inactive rows are fine.
            models.UniqueConstraint(
                fields=["is_active"],
                condition=models.Q(is_active=True),
                name="one_active_rubric",
            ),
        ]

    def __str__(self):
        flag = " (active)" if self.is_active else ""
        return f"Rubric v{self.version}{flag}"

    @classmethod
    def active(cls):
        return cls.objects.filter(is_active=True).first()


class Role(models.Model):
    """A job opening: the JD plus extracted requirements. Candidates are scored
    against the JD using the active rubric.
    """

    title = models.CharField(max_length=255)
    client = models.CharField(max_length=255, blank=True)
    jd_text = models.TextField()
    # Auto-extracted from jd_text (Lane C), then editable. Scoring uses jd_text
    # directly, so this is metadata for later filtering — not required to score.
    structured_requirements = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.title


class Score(models.Model):
    """An immutable scoring result for a candidate on a role under one rubric.

    Lifecycle:

        create(status=PENDING)
              |
        background worker scores
              |-- success --> mark_scored()  status=SCORED   (result fields frozen)
              \\-- failure --> mark_failed()   status=FAILED   (error set, retryable)

        FAILED --retry--> PENDING --> SCORED

    Once SCORED, the result fields (overall, per_criterion, confidence) and the
    status are frozen. A different rubric version produces a DIFFERENT row (the
    unique key includes rubric), so re-scoring never mutates history.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SCORED = "scored", "Scored"
        FAILED = "failed", "Failed"

    # Identity / idempotency key components.
    role = models.ForeignKey(Role, on_delete=models.CASCADE, related_name="scores")
    candidate = models.ForeignKey(
        Candidate, on_delete=models.CASCADE, related_name="scores"
    )
    cv = models.ForeignKey(CV, on_delete=models.CASCADE, related_name="scores")
    # PROTECT: never delete a rubric version a score depends on.
    rubric = models.ForeignKey(Rubric, on_delete=models.PROTECT, related_name="scores")

    # Provenance — what produced this score.
    model_version = models.CharField(max_length=128, blank=True)

    # Result (set once, on SCORED).
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.PENDING
    )
    overall = models.FloatField(null=True, blank=True)
    per_criterion = models.JSONField(default=dict)  # {name: {"score": x, "rationale": ".."}}
    confidence = models.FloatField(null=True, blank=True)
    error = models.TextField(blank=True)
    token_cost = models.IntegerField(null=True, blank=True)  # optional telemetry

    created_at = models.DateTimeField(default=timezone.now, editable=False)
    scored_at = models.DateTimeField(null=True, blank=True)

    # Result fields frozen once SCORED.
    _IMMUTABLE_AFTER_SCORED = ("overall", "per_criterion", "confidence", "rubric_id")

    class Meta:
        ordering = ["-overall"]
        constraints = [
            # The core promise: one score per (role, candidate, rubric ver, cv ver).
            models.UniqueConstraint(
                fields=["role", "candidate", "rubric", "cv"],
                name="uniq_score_role_candidate_rubric_cv",
            ),
        ]
        indexes = [
            models.Index(fields=["role", "-overall"]),
            models.Index(fields=["role", "status"]),
        ]

    def __str__(self):
        return f"Score<role={self.role_id} cand={self.candidate_id} {self.status}>"

    def save(self, *args, **kwargs):
        # Enforce immutability of a SCORED row. Status may move
        # PENDING->SCORED/FAILED and FAILED->PENDING (retry), but a SCORED row's
        # result is frozen — protecting the single-source-of-truth guarantee.
        if self.pk is not None:
            prev = type(self).objects.filter(pk=self.pk).first()
            if prev is not None and prev.status == self.Status.SCORED:
                if self.status != self.Status.SCORED:
                    raise ValidationError(
                        "A SCORED result is immutable; re-score under a new "
                        "rubric version instead of changing this row."
                    )
                for field in self._IMMUTABLE_AFTER_SCORED:
                    if getattr(prev, field) != getattr(self, field):
                        raise ValidationError(
                            f"Cannot change '{field}' on a SCORED result; "
                            "scores are immutable."
                        )
        super().save(*args, **kwargs)

    def mark_scored(self, *, overall, per_criterion, confidence=None,
                    model_version="", token_cost=None):
        self.overall = overall
        self.per_criterion = per_criterion
        self.confidence = confidence
        if model_version:
            self.model_version = model_version
        self.token_cost = token_cost
        self.error = ""
        self.status = self.Status.SCORED
        self.scored_at = timezone.now()
        self.save()

    def mark_failed(self, error):
        self.error = str(error)[:5000]
        self.status = self.Status.FAILED
        self.save()


class GeneratedArtifact(models.Model):
    """Abstract base for the regenerable, AI-generated per-(role, candidate)
    artifacts: screening questions, anonymized CV, and evaluation.

    Shares the lifecycle (status / model_version / error / timestamps) and
    mark_failed. Each concrete model adds its own FKs, payload field, unique
    constraint, and mark_generated (the payload field name differs).

    Score is deliberately NOT a GeneratedArtifact: it is an immutable, comparable
    source-of-truth row with a different status set (SCORED, not GENERATED) and
    its own immutability guard.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        GENERATED = "generated", "Generated"
        FAILED = "failed", "Failed"

    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    model_version = models.CharField(max_length=128, blank=True)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True
        ordering = ["-updated_at"]

    def mark_failed(self, error):
        self.error = str(error)[:5000]
        self.status = self.Status.FAILED
        self.save()


class ScreeningSet(GeneratedArtifact):
    """AI-generated screening questions for one candidate on one role. Regenerable;
    one current set per (role, candidate), overwritten in place.

    questions shape (JSON): [{"topic": str, "question": str, "what_to_listen_for": str}]
    """

    role = models.ForeignKey(Role, on_delete=models.CASCADE, related_name="screening_sets")
    candidate = models.ForeignKey(
        Candidate, on_delete=models.CASCADE, related_name="screening_sets"
    )
    cv = models.ForeignKey(CV, on_delete=models.CASCADE, related_name="screening_sets")
    questions = models.JSONField(default=list)

    class Meta(GeneratedArtifact.Meta):
        constraints = [
            models.UniqueConstraint(
                fields=["role", "candidate"], name="uniq_screening_role_candidate"
            ),
        ]

    def __str__(self):
        return f"ScreeningSet<role={self.role_id} cand={self.candidate_id} {self.status}>"

    def mark_generated(self, *, questions, model_version=""):
        self.questions = questions
        if model_version:
            self.model_version = model_version
        self.error = ""
        self.status = self.Status.GENERATED
        self.save()


class AnonymizedCV(GeneratedArtifact):
    """An anonymized, structured rewrite of a candidate's CV for client submission.
    PII (name, contact, employer names) is stripped. Regenerable.

    data shape (JSON):
        {headline, summary, years_experience, skills: [str],
         experience: [{role_title, industry, period, highlights: [str]}],
         education: [{qualification, field, period}]}
    """

    role = models.ForeignKey(Role, on_delete=models.CASCADE, related_name="anonymized_cvs")
    candidate = models.ForeignKey(
        Candidate, on_delete=models.CASCADE, related_name="anonymized_cvs"
    )
    cv = models.ForeignKey(CV, on_delete=models.CASCADE, related_name="anonymized_cvs")
    data = models.JSONField(default=dict)

    class Meta(GeneratedArtifact.Meta):
        constraints = [
            models.UniqueConstraint(
                fields=["role", "candidate"], name="uniq_anoncv_role_candidate"
            ),
        ]

    def __str__(self):
        return f"AnonymizedCV<role={self.role_id} cand={self.candidate_id} {self.status}>"

    def mark_generated(self, *, data, model_version=""):
        self.data = data
        if model_version:
            self.model_version = model_version
        self.error = ""
        self.status = self.Status.GENERATED
        self.save()


class Evaluation(GeneratedArtifact):
    """A post-screening evaluation grounded in the call transcript. The transcript
    is recruiter-provided input (unlike screening/anon, which derive from the CV).
    One per (role, candidate); re-running with a new transcript overwrites it.

    result shape (JSON):
        {recommendation: "strong_yes|yes|maybe|no", headline, summary,
         strengths: [str], concerns: [str],
         criteria: [{name, assessment, evidence}]}
    """

    RECOMMENDATIONS = {"strong_yes", "yes", "maybe", "no"}

    role = models.ForeignKey(Role, on_delete=models.CASCADE, related_name="evaluations")
    candidate = models.ForeignKey(
        Candidate, on_delete=models.CASCADE, related_name="evaluations"
    )
    cv = models.ForeignKey(CV, on_delete=models.CASCADE, related_name="evaluations")
    transcript = models.TextField(blank=True)
    result = models.JSONField(default=dict)

    class Meta(GeneratedArtifact.Meta):
        constraints = [
            models.UniqueConstraint(
                fields=["role", "candidate"], name="uniq_evaluation_role_candidate"
            ),
        ]

    def __str__(self):
        return f"Evaluation<role={self.role_id} cand={self.candidate_id} {self.status}>"

    def mark_generated(self, *, result, model_version=""):
        self.result = result
        if model_version:
            self.model_version = model_version
        self.error = ""
        self.status = self.Status.GENERATED
        self.save()


class CandidateUpload(models.Model):
    """Staging row for an uploaded CV, processed in the background.

    The web parses the CV + stores the original file at upload; a background job
    extracts name/email, creates the Candidate + CV (+ Score if tied to a role),
    and enqueues scoring. role is optional: a global upload (no role) just adds
    the candidate to the database. Failures stay visible here for follow-up.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PROCESSING = "processing", "Processing"
        DONE = "done", "Done"
        FAILED = "failed", "Failed"

    # Optional: global uploads (just add to the candidate database) have no role.
    role = models.ForeignKey(
        Role, on_delete=models.CASCADE, related_name="uploads", null=True, blank=True
    )
    # Original file, stored on the web volume for download. The worker only
    # references its path (never reads it), so this stays single-instance safe.
    raw_file = models.FileField(upload_to="cvs/%Y/%m/", null=True, blank=True)
    original_filename = models.CharField(max_length=255, blank=True)
    # The web parses the CV at upload and stores the text here; the worker reads
    # only this (no filesystem access needed).
    parsed_text = models.TextField(blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    error = models.TextField(blank=True)
    # The candidate created from this upload (null until done). SET_NULL so erasing
    # a candidate doesn't delete the audit row.
    candidate = models.ForeignKey(
        Candidate, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["role", "status"])]

    def __str__(self):
        return f"CandidateUpload<{self.original_filename or self.pk} {self.status}>"

    def mark_failed(self, reason):
        self.error = str(reason)[:5000]
        self.status = self.Status.FAILED
        self.save()


class Stage(models.Model):
    """A pipeline lane on a role's kanban board. Each role has its own ordered
    set of stages (configurable: add / rename / delete)."""

    role = models.ForeignKey(Role, on_delete=models.CASCADE, related_name="stages")
    name = models.CharField(max_length=100)
    position = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        ordering = ["position", "id"]
        constraints = [
            # Lane names are unique per role. This also makes the default-lane
            # seeding race-safe: a concurrent web+worker bulk_create(ignore_conflicts)
            # can't produce duplicate "New"/"Screening"/... lanes.
            models.UniqueConstraint(fields=["role", "name"], name="uniq_stage_role_name"),
        ]

    def __str__(self):
        return f"{self.role_id}:{self.name}"


class PipelineCard(models.Model):
    """A candidate's tile on a role's kanban board. One per (role, candidate);
    its stage + position change as the recruiter drags it between lanes."""

    role = models.ForeignKey(Role, on_delete=models.CASCADE, related_name="cards")
    candidate = models.ForeignKey(Candidate, on_delete=models.CASCADE, related_name="pipeline_cards")
    stage = models.ForeignKey(Stage, on_delete=models.SET_NULL, null=True, blank=True, related_name="cards")
    position = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        ordering = ["position", "id"]
        constraints = [
            models.UniqueConstraint(fields=["role", "candidate"], name="uniq_card_role_candidate"),
        ]

    def __str__(self):
        return f"Card<role={self.role_id} cand={self.candidate_id}>"


# --------------------------------------------------------------------------- #
# Mini-CRM: customers (Company) -> contacts (Person) -> signed Deals + docs    #
# --------------------------------------------------------------------------- #
class Company(models.Model):
    """A customer company we place developers with."""

    name = models.CharField(max_length=255, unique=True)
    website = models.URLField(blank=True)
    notes = models.TextField(blank=True)
    # Soft-delete: archived companies are hidden from the default CRM view but
    # kept intact (contacts, deals, and signed agreements survive) and can be
    # restored. Mirrors Candidate.is_archived.
    is_archived = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "companies"

    def __str__(self):
        return self.name


class Person(models.Model):
    """A human contact at a customer company (hiring manager, procurement, etc.)."""

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="people")
    full_name = models.CharField(max_length=255)
    title = models.CharField(max_length=255, blank=True, help_text="Their role at the company.")
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=64, blank=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        ordering = ["full_name"]
        verbose_name_plural = "people"

    def __str__(self):
        return f"{self.full_name} @ {self.company.name}"


class Deal(models.Model):
    """A signed placement: a developer we contracted out to a customer.

    We track both sides of the economics — what we pay the developer (`salary`)
    and what the customer pays us (`client_rate`). Each side carries its OWN
    currency, because we may pay the developer in one currency and bill the
    client in another. `rate_period` records whether the amounts are monthly or
    hourly. The developer optionally links to a Candidate in the ATS;
    `developer_name` is a snapshot so the deal record survives candidate erasure
    (GDPR) and covers developers who were never in the pipeline. Signed
    agreements attach as DealDocument rows.
    """

    class RatePeriod(models.TextChoices):
        MONTHLY = "monthly", "Monthly"
        HOURLY = "hourly", "Hourly"

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="deals")
    # Link to the ATS candidate when known; SET_NULL keeps the deal (and the
    # name snapshot) intact if the candidate is later erased.
    candidate = models.ForeignKey(
        Candidate, on_delete=models.SET_NULL, null=True, blank=True, related_name="deals"
    )
    developer_name = models.CharField(max_length=255, help_text="The developer signed (snapshot).")
    role_title = models.CharField(max_length=255, blank=True, help_text="What they were placed as.")
    # Whether the amounts below are a monthly or an hourly rate.
    rate_period = models.CharField(
        max_length=16, choices=RatePeriod.choices, default=RatePeriod.MONTHLY
    )
    # salary = what we pay the dev; client_rate = what the customer pays us. Each
    # has its own currency. margin is only derivable when the two currencies match.
    salary = models.DecimalField(max_digits=12, decimal_places=2)
    salary_currency = models.CharField(max_length=8, default="PLN")
    client_rate = models.DecimalField(max_digits=12, decimal_places=2)
    client_rate_currency = models.CharField(max_length=8, default="PLN")
    signed_date = models.DateField()
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        ordering = ["-signed_date", "-id"]

    def __str__(self):
        return f"{self.developer_name} → {self.company.name} ({self.signed_date})"

    @property
    def margin(self):
        """What we make on the placement (client_rate - salary), or None.

        None when either amount is unset OR the two sides are in different
        currencies (you can't subtract across currencies without an FX rate).
        """
        if self.client_rate is None or self.salary is None:
            return None
        if self.salary_currency != self.client_rate_currency:
            return None
        return self.client_rate - self.salary

    @property
    def period_suffix(self):
        """\"/hr\" or \"/mo\" — appended to amounts in the UI."""
        return "/hr" if self.rate_period == self.RatePeriod.HOURLY else "/mo"


class DealDocument(models.Model):
    """An uploaded agreement / document attached to a deal."""

    deal = models.ForeignKey(Deal, on_delete=models.CASCADE, related_name="documents")
    file = models.FileField(upload_to="deals/%Y/%m/")
    original_filename = models.CharField(max_length=255, blank=True)
    uploaded_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        ordering = ["-uploaded_at"]

    def __str__(self):
        return self.original_filename or f"Document<{self.pk}>"
