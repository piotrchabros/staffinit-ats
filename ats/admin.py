from django.contrib import admin

from .models import (
    AnonymizedCV,
    CV,
    Candidate,
    CandidateUpload,
    Company,
    Deal,
    DealDocument,
    Evaluation,
    Person,
    Role,
    Rubric,
    Score,
    ScreeningSet,
)


@admin.register(Candidate)
class CandidateAdmin(admin.ModelAdmin):
    list_display = ("full_name", "email", "phone", "is_archived", "created_at")
    list_filter = ("is_archived",)
    search_fields = ("full_name", "email")


@admin.register(CV)
class CVAdmin(admin.ModelAdmin):
    list_display = ("candidate", "parser_version", "uploaded_at")
    list_filter = ("parser_version",)
    raw_id_fields = ("candidate",)


@admin.register(Role)
class RoleAdmin(admin.ModelAdmin):
    list_display = ("title", "client", "created_at")
    search_fields = ("title", "client")


@admin.register(Rubric)
class RubricAdmin(admin.ModelAdmin):
    list_display = ("version", "name", "is_active", "created_at")
    list_filter = ("is_active",)


@admin.register(Score)
class ScoreAdmin(admin.ModelAdmin):
    list_display = ("role", "candidate", "rubric", "status", "overall", "scored_at")
    list_filter = ("status", "rubric")
    raw_id_fields = ("role", "candidate", "cv", "rubric")
    # Scores are immutable; admin is read-only to prevent accidental edits.
    readonly_fields = (
        "role", "candidate", "cv", "rubric", "model_version", "status",
        "overall", "per_criterion", "confidence", "error", "token_cost",
        "created_at", "scored_at",
    )

    def has_add_permission(self, request):
        return False


@admin.register(ScreeningSet)
class ScreeningSetAdmin(admin.ModelAdmin):
    list_display = ("role", "candidate", "status", "model_version", "updated_at")
    list_filter = ("status",)
    raw_id_fields = ("role", "candidate", "cv")


@admin.register(AnonymizedCV)
class AnonymizedCVAdmin(admin.ModelAdmin):
    list_display = ("role", "candidate", "status", "model_version", "updated_at")
    list_filter = ("status",)
    raw_id_fields = ("role", "candidate", "cv")


@admin.register(Evaluation)
class EvaluationAdmin(admin.ModelAdmin):
    list_display = ("role", "candidate", "status", "model_version", "updated_at")
    list_filter = ("status",)
    raw_id_fields = ("role", "candidate", "cv")


@admin.register(CandidateUpload)
class CandidateUploadAdmin(admin.ModelAdmin):
    list_display = ("original_filename", "role", "status", "candidate", "created_at")
    list_filter = ("status",)
    raw_id_fields = ("role", "candidate")


class PersonInline(admin.TabularInline):
    model = Person
    extra = 0


@admin.register(Company)
class CompanyAdmin(admin.ModelAdmin):
    list_display = ("name", "website", "is_archived", "created_at")
    list_filter = ("is_archived",)
    search_fields = ("name",)
    inlines = [PersonInline]


@admin.register(Person)
class PersonAdmin(admin.ModelAdmin):
    list_display = ("full_name", "company", "title", "email", "phone")
    search_fields = ("full_name", "email")
    raw_id_fields = ("company",)


class DealDocumentInline(admin.TabularInline):
    model = DealDocument
    extra = 0


@admin.register(Deal)
class DealAdmin(admin.ModelAdmin):
    list_display = (
        "developer_name", "company", "rate_period",
        "salary", "salary_currency", "client_rate", "client_rate_currency", "signed_date",
    )
    list_filter = ("rate_period", "salary_currency", "client_rate_currency", "company")
    search_fields = ("developer_name",)
    raw_id_fields = ("company", "candidate")
    inlines = [DealDocumentInline]
