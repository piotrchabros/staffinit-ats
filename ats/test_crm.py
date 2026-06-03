"""Feature 3 — mini-CRM (companies -> people -> deals + documents)."""

from __future__ import annotations

from decimal import Decimal

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from ats.models import Company, Deal, DealDocument, Person


class CRMTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("rec", password="pw")
        self.client.force_login(self.user)
        self.company = Company.objects.create(name="Acme Corp", website="https://acme.test")

    def test_login_required(self):
        self.client.logout()
        resp = self.client.get(reverse("company_list"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.url)

    def test_company_list_and_search(self):
        Person.objects.create(company=self.company, full_name="Jane Buyer")
        resp = self.client.get(reverse("company_list"), {"q": "jane"})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Acme Corp")
        # A non-matching query hides it.
        resp = self.client.get(reverse("company_list"), {"q": "nomatch"})
        self.assertNotContains(resp, "Acme Corp")

    def test_add_company(self):
        resp = self.client.post(reverse("add_company"), {"name": "Globex", "website": "", "notes": ""})
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(Company.objects.filter(name="Globex").exists())

    def test_add_person(self):
        self.client.post(reverse("add_person", args=[self.company.pk]),
                         {"full_name": "Jane Buyer", "title": "CTO", "email": "jane@acme.test", "phone": "", "notes": ""})
        p = Person.objects.get(full_name="Jane Buyer")
        self.assertEqual(p.company, self.company)
        self.assertEqual(p.title, "CTO")

    def test_add_deal_with_document(self):
        doc = SimpleUploadedFile("agreement.pdf", b"%PDF-1.4 fake", content_type="application/pdf")
        resp = self.client.post(reverse("add_deal", args=[self.company.pk]), {
            "developer_name": "Bob Dev", "role_title": "Senior Backend",
            "salary": "18000", "client_rate": "26000", "currency": "PLN",
            "signed_date": "2026-05-01", "notes": "", "documents": doc,
        })
        self.assertEqual(resp.status_code, 302)
        deal = Deal.objects.get(developer_name="Bob Dev")
        self.assertEqual(deal.company, self.company)
        self.assertEqual(deal.margin, Decimal("8000"))
        self.assertEqual(DealDocument.objects.filter(deal=deal).count(), 1)
        # Redirects to the new deal's page.
        self.assertEqual(resp.url, reverse("deal_detail", args=[deal.pk]))

    def test_deal_detail_renders(self):
        deal = Deal.objects.create(
            company=self.company, developer_name="Bob Dev",
            salary=Decimal("18000"), client_rate=Decimal("26000"), signed_date="2026-05-01",
        )
        resp = self.client.get(reverse("deal_detail", args=[deal.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Bob Dev")
        self.assertContains(resp, "8000")  # margin

    def test_add_deal_document_to_existing_deal(self):
        deal = Deal.objects.create(
            company=self.company, developer_name="Bob Dev",
            salary=Decimal("18000"), client_rate=Decimal("26000"), signed_date="2026-05-01",
        )
        doc = SimpleUploadedFile("nda.pdf", b"%PDF-1.4 fake", content_type="application/pdf")
        self.client.post(reverse("add_deal_document", args=[deal.pk]), {"documents": doc})
        self.assertEqual(deal.documents.count(), 1)

    def test_archive_company_hides_but_keeps_it(self):
        Person.objects.create(company=self.company, full_name="Jane Buyer")
        deal = Deal.objects.create(
            company=self.company, developer_name="Bob Dev",
            salary=Decimal("18000"), client_rate=Decimal("26000"), signed_date="2026-05-01",
        )

        resp = self.client.post(reverse("archive_company", args=[self.company.pk]))
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, reverse("company_list"))

        self.company.refresh_from_db()
        self.assertTrue(self.company.is_archived)
        # Soft-delete: contacts and deals survive intact.
        self.assertTrue(Person.objects.filter(company=self.company).exists())
        self.assertTrue(Deal.objects.filter(pk=deal.pk).exists())

        # Hidden from the default list, shown under ?archived=1. Assert on the
        # detail-link URL, not the name (the success flash message also carries it).
        detail_url = reverse("company_detail", args=[self.company.pk])
        self.assertNotContains(self.client.get(reverse("company_list")), detail_url)
        self.assertContains(
            self.client.get(reverse("company_list"), {"archived": "1"}), detail_url
        )

    def test_unarchive_company(self):
        self.company.is_archived = True
        self.company.save(update_fields=["is_archived"])
        resp = self.client.post(reverse("unarchive_company", args=[self.company.pk]))
        self.assertEqual(resp.status_code, 302)
        self.company.refresh_from_db()
        self.assertFalse(self.company.is_archived)
        self.assertContains(
            self.client.get(reverse("company_list")),
            reverse("company_detail", args=[self.company.pk]),
        )

    def test_archive_company_get_not_allowed(self):
        resp = self.client.get(reverse("archive_company", args=[self.company.pk]))
        self.assertEqual(resp.status_code, 405)
        self.company.refresh_from_db()
        self.assertFalse(self.company.is_archived)

    def test_margin_none_when_unset(self):
        # Both required at the DB level, but the property guards None defensively.
        deal = Deal(company=self.company, developer_name="X", signed_date="2026-05-01")
        deal.salary = None
        deal.client_rate = None
        self.assertIsNone(deal.margin)

    def tearDown(self):
        # Clean up any files written to MEDIA_ROOT by the upload tests.
        for d in DealDocument.objects.all():
            d.file.delete(save=False)
