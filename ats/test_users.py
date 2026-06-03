"""User management — provision / remove logins (superuser-only)."""

from __future__ import annotations

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse


class UserManagementTests(TestCase):
    def setUp(self):
        self.root = User.objects.create_superuser("root", password="pw-rootpw-123")
        self.plain = User.objects.create_user("rec", password="pw-recpw-123")

    # --- access control ----------------------------------------------------- #
    def test_login_required(self):
        resp = self.client.get(reverse("user_list"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.url)

    def test_non_admin_forbidden(self):
        self.client.force_login(self.plain)
        self.assertEqual(self.client.get(reverse("user_list")).status_code, 403)
        # ...and can't act on the write endpoints either.
        self.assertEqual(
            self.client.post(reverse("add_user")).status_code, 403
        )
        self.assertEqual(
            self.client.post(reverse("delete_user", args=[self.root.pk])).status_code, 403
        )

    def test_admin_sees_list(self):
        self.client.force_login(self.root)
        resp = self.client.get(reverse("user_list"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "rec")
        self.assertContains(resp, "root")

    # --- add ---------------------------------------------------------------- #
    def test_add_user(self):
        self.client.force_login(self.root)
        resp = self.client.post(reverse("add_user"), {
            "username": "newbie", "email": "newbie@x.test",
            "password1": "s3cret-pass-99", "password2": "s3cret-pass-99",
        })
        self.assertEqual(resp.status_code, 302)
        u = User.objects.get(username="newbie")
        self.assertEqual(u.email, "newbie@x.test")
        self.assertFalse(u.is_superuser)
        # The new login actually works.
        self.assertTrue(self.client.login(username="newbie", password="s3cret-pass-99"))

    def test_add_admin_user(self):
        self.client.force_login(self.root)
        self.client.post(reverse("add_user"), {
            "username": "boss", "is_admin": "on",
            "password1": "s3cret-pass-99", "password2": "s3cret-pass-99",
        })
        u = User.objects.get(username="boss")
        self.assertTrue(u.is_superuser)
        self.assertTrue(u.is_staff)

    def test_add_user_password_mismatch_rejected(self):
        self.client.force_login(self.root)
        self.client.post(reverse("add_user"), {
            "username": "oops",
            "password1": "s3cret-pass-99", "password2": "different-99",
        })
        self.assertFalse(User.objects.filter(username="oops").exists())

    # --- delete ------------------------------------------------------------- #
    def test_delete_user(self):
        self.client.force_login(self.root)
        resp = self.client.post(reverse("delete_user", args=[self.plain.pk]))
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(User.objects.filter(pk=self.plain.pk).exists())

    def test_cannot_delete_self(self):
        self.client.force_login(self.root)
        self.client.post(reverse("delete_user", args=[self.root.pk]))
        # Still there — self-delete is blocked to avoid locking the admin out.
        self.assertTrue(User.objects.filter(pk=self.root.pk).exists())

    def test_delete_user_get_not_allowed(self):
        self.client.force_login(self.root)
        resp = self.client.get(reverse("delete_user", args=[self.plain.pk]))
        self.assertEqual(resp.status_code, 405)
        self.assertTrue(User.objects.filter(pk=self.plain.pk).exists())
