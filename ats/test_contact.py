"""Tests for CV contact extraction (name/email/phone)."""

from __future__ import annotations

import types

from django.test import SimpleTestCase

from ats.scoring.contact import ContactExtractionService, extract_contact


def _resp(tool_input, *, include_tool=True):
    blocks = [types.SimpleNamespace(type="tool_use", name="submit_contact", input=tool_input)] \
        if include_tool else [types.SimpleNamespace(type="text", text="x")]
    return types.SimpleNamespace(content=blocks, model="claude-haiku-4-5-20251001")


class _Client:
    def __init__(self, resp=None, raises=False):
        self._resp, self._raises = resp, raises

        class _M:
            def create(_s, **kw):
                if raises:
                    raise RuntimeError("AuthenticationError")
                return self._resp
        self.messages = _M()


class ContactExtractionTests(SimpleTestCase):
    def test_extracts_and_normalizes(self):
        svc = ContactExtractionService(client=_Client(_resp(
            {"full_name": "Anna Nowak", "email": "Anna@Example.COM", "phone": "+48 600"})))
        out = extract_contact("cv text with stuff", service=svc)
        self.assertEqual(out["full_name"], "Anna Nowak")
        self.assertEqual(out["email"], "anna@example.com")  # lowercased
        self.assertEqual(out["phone"], "+48 600")

    def test_regex_backstop_when_model_returns_no_email(self):
        svc = ContactExtractionService(client=_Client(_resp(
            {"full_name": "Bob", "email": ""})))
        out = extract_contact("Bob, reach me at bob.smith@corp.io please", service=svc)
        self.assertEqual(out["email"], "bob.smith@corp.io")

    def test_regex_backstop_when_service_errors(self):
        svc = ContactExtractionService(client=_Client(raises=True))
        out = extract_contact("CV. Email: eva@demo.test", service=svc)
        self.assertEqual(out["email"], "eva@demo.test")  # LLM failed -> regex still works
        self.assertEqual(out["full_name"], "")

    def test_empty_text(self):
        self.assertEqual(extract_contact("  "), {"full_name": "", "email": "", "phone": ""})
