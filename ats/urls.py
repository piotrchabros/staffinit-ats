from django.urls import path

from . import views

urlpatterns = [
    path("", views.role_list, name="role_list"),
    path("candidates/", views.candidate_list, name="candidate_list"),
    path("candidates/upload/", views.candidate_upload, name="candidate_upload"),
    path("roles/new/", views.role_create, name="role_create"),
    path("roles/<int:pk>/", views.role_detail, name="role_detail"),
    path("roles/<int:pk>/add-candidate/", views.add_candidate, name="add_candidate"),
    path("roles/<int:pk>/cv/<int:cv_id>/paste/", views.paste_cv, name="paste_cv"),
    path("roles/<int:pk>/score/", views.score_role, name="score_role"),
    path("roles/<int:pk>/score/<int:score_id>/retry/", views.retry_score, name="retry_score"),
    path("roles/<int:pk>/candidate/<int:candidate_id>/screening/", views.screening_detail, name="screening_detail"),
    path("roles/<int:pk>/candidate/<int:candidate_id>/screening/generate/", views.generate_screening, name="generate_screening"),
    path("roles/<int:pk>/candidate/<int:candidate_id>/anon-cv/", views.anonymized_cv_detail, name="anonymized_cv_detail"),
    path("roles/<int:pk>/candidate/<int:candidate_id>/anon-cv/generate/", views.generate_anonymized_cv, name="generate_anonymized_cv"),
    path("roles/<int:pk>/candidate/<int:candidate_id>/evaluation/", views.evaluation_detail, name="evaluation_detail"),
    path("roles/<int:pk>/candidate/<int:candidate_id>/evaluation/generate/", views.generate_evaluation, name="generate_evaluation"),
]
