"""Contact extraction — pull the candidate's name + email + phone from a CV.

Lets the recruiter just drop a CV file; identity is read from it instead of typed.
Uses a cheap/fast model (Haiku) and a regex email backstop, and is best-effort:
on any failure it returns what it can (often the regex email) rather than raising.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

from .service import extract_tool_input

DEFAULT_MODEL = os.environ.get("CONTACT_MODEL", "claude-haiku-4-5-20251001")
MAX_TOKENS = 512
TOOL_NAME = "submit_contact"

# Pragmatic email matcher for the regex backstop.
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


@dataclass
class ContactExtractionService:
    client: Any
    model: str = DEFAULT_MODEL

    def extract(self, cv_text: str) -> dict:
        response = self.client.messages.create(
            model=self.model,
            max_tokens=MAX_TOKENS,
            messages=[{"role": "user", "content": [
                {"type": "text", "text": _prompt(cv_text)}
            ]}],
            tools=[_tool()],
            tool_choice={"type": "tool", "name": TOOL_NAME},
        )
        return extract_tool_input(response, TOOL_NAME) or {}


def extract_contact(cv_text: str, *, service: ContactExtractionService | None = None) -> dict:
    """Best-effort {full_name, email, phone} from CV text. Never raises.

    The LLM gets name + email + phone; a regex backstop fills the email if the
    model missed it. On any error (no key, network) we still return the regex email.
    """
    text = (cv_text or "").strip()
    if not text:
        return {"full_name": "", "email": "", "phone": ""}

    data: dict = {}
    try:
        svc = service or get_default_service()
        data = svc.extract(text) or {}
    except Exception:
        data = {}

    email = str(data.get("email", "")).strip()
    if not email:
        m = EMAIL_RE.search(text)
        email = m.group(0) if m else ""

    return {
        "full_name": str(data.get("full_name", "")).strip(),
        "email": email.lower(),
        "phone": str(data.get("phone", "")).strip(),
    }


def _tool() -> dict:
    return {
        "name": TOOL_NAME,
        "description": "Extract the candidate's contact details from their CV.",
        "input_schema": {
            "type": "object",
            "properties": {
                "full_name": {"type": "string", "description": "The candidate's full name."},
                "email": {"type": "string", "description": "Their email address, or empty if none."},
                "phone": {"type": "string", "description": "Their phone number, or empty if none."},
            },
            "required": ["full_name", "email"],
        },
    }


def _prompt(cv_text: str) -> str:
    return (
        "Extract the candidate's full name, email address, and phone number from "
        "the CV below. Use exactly what appears in the CV; if a field is absent, "
        "return an empty string. Do not invent anything.\n\n"
        f"## CV\n\n{cv_text.strip()[:6000]}"
    )


def get_default_service(model: str = DEFAULT_MODEL) -> ContactExtractionService:
    import anthropic

    return ContactExtractionService(client=anthropic.Anthropic(), model=model)
