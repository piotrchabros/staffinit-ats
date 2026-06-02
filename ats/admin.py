from django.contrib import admin

from .models import AnonymizedCV, CV, Candidate, Role, Rubric, Score, ScreeningSet


@admin.register(Candidate)
class CandidateAdmin(admin.ModelAdmin):
    list_display = ("full_name", "email", "phone", "created_at")
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
