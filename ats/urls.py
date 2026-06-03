from django.urls import path

from . import views

urlpatterns = [
    path("", views.role_list, name="role_list"),
    path("candidates/", views.candidate_list, name="candidate_list"),
    path("candidates/upload/", views.candidate_upload, name="candidate_upload"),
    path("candidates/<int:pk>/archive/", views.archive_candidate, name="archive_candidate"),
    path("candidates/<int:pk>/unarchive/", views.unarchive_candidate, name="unarchive_candidate"),
    path("cv/<int:cv_id>/file/", views.cv_file, name="cv_file"),
    # User management (superuser-only)
    path("users/", views.user_list, name="user_list"),
    path("users/add/", views.add_user, name="add_user"),
    path("users/<int:pk>/delete/", views.delete_user, name="delete_user"),
    path("roles/new/", views.role_create, name="role_create"),
    path("roles/<int:pk>/", views.role_detail, name="role_detail"),
    path("roles/<int:pk>/cards/move/", views.move_card, name="move_card"),
    path("roles/<int:pk>/stages/add/", views.add_stage, name="add_stage"),
    path("roles/<int:pk>/stages/<int:stage_id>/rename/", views.rename_stage, name="rename_stage"),
    path("roles/<int:pk>/stages/<int:stage_id>/delete/", views.delete_stage, name="delete_stage"),
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
    # Mini-CRM
    path("crm/", views.company_list, name="company_list"),
    path("crm/companies/add/", views.add_company, name="add_company"),
    path("crm/companies/<int:pk>/", views.company_detail, name="company_detail"),
    path("crm/companies/<int:pk>/archive/", views.archive_company, name="archive_company"),
    path("crm/companies/<int:pk>/unarchive/", views.unarchive_company, name="unarchive_company"),
    path("crm/companies/<int:pk>/people/add/", views.add_person, name="add_person"),
    path("crm/people/<int:pk>/delete/", views.delete_person, name="delete_person"),
    path("crm/companies/<int:pk>/deals/add/", views.add_deal, name="add_deal"),
    path("crm/deals/<int:pk>/", views.deal_detail, name="deal_detail"),
    path("crm/deals/<int:pk>/documents/add/", views.add_deal_document, name="add_deal_document"),
    path("crm/documents/<int:doc_id>/file/", views.deal_document_file, name="deal_document_file"),
]
