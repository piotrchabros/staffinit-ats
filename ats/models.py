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


class ScreeningSet(models.Model):
    """AI-generated screening questions for one candidate on one role.

    Unlike Score, this is a working prep aid, not a comparable source-of-truth
    artifact — so it is regenerable: one current set per (role, candidate), and
    regenerating overwrites it in place.

    questions shape (JSON): [{"topic": str, "question": str, "what_to_listen_for": str}]
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        GENERATED = "generated", "Generated"
        FAILED = "failed", "Failed"

    role = models.ForeignKey(Role, on_delete=models.CASCADE, related_name="screening_sets")
    candidate = models.ForeignKey(
        Candidate, on_delete=models.CASCADE, related_name="screening_sets"
    )
    cv = models.ForeignKey(CV, on_delete=models.CASCADE, related_name="screening_sets")

    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    questions = models.JSONField(default=list)
    model_version = models.CharField(max_length=128, blank=True)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
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

    def mark_failed(self, error):
        self.error = str(error)[:5000]
        self.status = self.Status.FAILED
        self.save()


class AnonymizedCV(models.Model):
    """An anonymized, structured rewrite of a candidate's CV for client submission.

    PII (name, contact details) is stripped so the agency can present the
    candidate without revealing identity. Regenerable, like ScreeningSet.

    data shape (JSON):
        {headline, summary, years_experience, skills: [str],
         experience: [{role_title, industry, period, highlights: [str]}],
         education: [{qualification, field, period}]}
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        GENERATED = "generated", "Generated"
        FAILED = "failed", "Failed"

    role = models.ForeignKey(Role, on_delete=models.CASCADE, related_name="anonymized_cvs")
    candidate = models.ForeignKey(
        Candidate, on_delete=models.CASCADE, related_name="anonymized_cvs"
    )
    cv = models.ForeignKey(CV, on_delete=models.CASCADE, related_name="anonymized_cvs")

    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    data = models.JSONField(default=dict)
    model_version = models.CharField(max_length=128, blank=True)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
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

    def mark_failed(self, error):
        self.error = str(error)[:5000]
        self.status = self.Status.FAILED
        self.save()


class Evaluation(models.Model):
    """A post-screening evaluation of a candidate, grounded in the call transcript.

    The transcript is recruiter-provided input (unlike screening/anon which derive
    purely from the CV). One per (role, candidate); re-running with an updated
    transcript overwrites the result.

    result shape (JSON):
        {recommendation: "strong_yes|yes|maybe|no", headline, summary,
         strengths: [str], concerns: [str],
         criteria: [{name, assessment, evidence}]}
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        GENERATED = "generated", "Generated"
        FAILED = "failed", "Failed"

    RECOMMENDATIONS = {"strong_yes", "yes", "maybe", "no"}

    role = models.ForeignKey(Role, on_delete=models.CASCADE, related_name="evaluations")
    candidate = models.ForeignKey(
        Candidate, on_delete=models.CASCADE, related_name="evaluations"
    )
    cv = models.ForeignKey(CV, on_delete=models.CASCADE, related_name="evaluations")

    transcript = models.TextField(blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    result = models.JSONField(default=dict)
    model_version = models.CharField(max_length=128, blank=True)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
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

    def mark_failed(self, error):
        self.error = str(error)[:5000]
        self.status = self.Status.FAILED
        self.save()
