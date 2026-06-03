"""Feature 2 — per-role kanban pipeline (stages + cards + drag-drop move)."""

from __future__ import annotations

import json

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from ats.models import CV, Candidate, PipelineCard, Role, Rubric, Stage
from ats.scoring.orchestration import create_pending_score


class KanbanTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("rec", password="pw")
        self.client.force_login(self.user)
        self.rubric = Rubric.objects.create(version=1, criteria=[{"name": "P", "scale": 5}], is_active=True)
        self.role = Role.objects.create(title="Backend", jd_text="jd")
        self.cand = Candidate.objects.create(full_name="Anna Nowak", email="anna@x.com")
        self.cv = CV.objects.create(candidate=self.cand, parsed_text="cv")
        create_pending_score(role=self.role, candidate=self.cand, cv=self.cv)
        self.card = PipelineCard.objects.get(role=self.role, candidate=self.cand)

    def test_default_stages_and_card_in_first_lane(self):
        self.assertEqual(self.role.stages.count(), 5)
        self.assertEqual(self.card.stage, self.role.stages.first())  # "New"

    def test_adding_same_candidate_again_is_one_card(self):
        create_pending_score(role=self.role, candidate=self.cand, cv=self.cv)
        self.assertEqual(PipelineCard.objects.filter(role=self.role, candidate=self.cand).count(), 1)

    def test_board_renders_with_lanes_and_card(self):
        resp = self.client.get(reverse("role_detail", args=[self.role.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "New")
        self.assertContains(resp, "Shortlisted")
        self.assertContains(resp, "Anna Nowak")

    def test_move_card_to_another_lane(self):
        target = self.role.stages.all()[2]  # Shortlisted
        resp = self.client.post(reverse("move_card", args=[self.role.pk]),
                                {"stage_id": target.id, "card_ids": json.dumps([self.card.id])})
        self.assertEqual(resp.status_code, 200)
        self.card.refresh_from_db()
        self.assertEqual(self.card.stage, target)
        self.assertEqual(self.card.position, 0)

    def test_move_card_rejects_foreign_stage(self):
        other_role = Role.objects.create(title="Other", jd_text="x")
        foreign = Stage.objects.create(role=other_role, name="X", position=0)
        resp = self.client.post(reverse("move_card", args=[self.role.pk]),
                                {"stage_id": foreign.id, "card_ids": json.dumps([self.card.id])})
        self.assertEqual(resp.status_code, 404)  # stage not on this role

    def test_add_stage(self):
        self.client.post(reverse("add_stage", args=[self.role.pk]), {"name": "Offer"})
        self.assertTrue(self.role.stages.filter(name="Offer").exists())

    def test_rename_stage(self):
        st = self.role.stages.first()
        self.client.post(reverse("rename_stage", args=[self.role.pk, st.id]), {"name": "Sourced"})
        st.refresh_from_db()
        self.assertEqual(st.name, "Sourced")

    def test_delete_stage_reassigns_cards(self):
        st = self.role.stages.all()[1]
        PipelineCard.objects.filter(pk=self.card.pk).update(stage=st)
        self.client.post(reverse("delete_stage", args=[self.role.pk, st.id]))
        self.assertFalse(Stage.objects.filter(pk=st.pk).exists())
        self.card.refresh_from_db()
        self.assertIsNotNone(self.card.stage)
        self.assertNotEqual(self.card.stage_id, st.id)  # moved elsewhere
