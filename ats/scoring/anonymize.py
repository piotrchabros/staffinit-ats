"""AnonymizationService — rewrite a CV into a structured, PII-stripped form.

The LLM removes the candidate's name and contact details and returns a structured
CV. On top of that we run a DETERMINISTIC scrub: any occurrence of the candidate's
email or name tokens that the model left in is redacted. Belt and braces, because
leaking identity to a client is the exact failure this feature must not have.

Injectable client — tests need no API key.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

from .service import extract_tool_input

DEFAULT_MODEL = os.environ.get("ANONYMIZE_MODEL", "claude-sonnet-4-6")
MAX_TOKENS = 2048
TOOL_NAME = "submit_anonymized_cv"
REDACTED = "[redacted]"


class AnonymizationError(Exception):
    pass


@dataclass
class AnonymizationService:
    client: Any
    model: str = DEFAULT_MODEL

    def anonymize(self, *, cv_text: str, candidate_name: str = "",
                  candidate_email: str = "") -> tuple[dict, str]:
        if not cv_text or not cv_text.strip():
            raise AnonymizationError("CV text is empty; nothing to anonymize.")

        response = self.client.messages.create(
            model=self.model,
            max_tokens=MAX_TOKENS,
            system=[{"type": "text", "text": _system_prompt()}],
            messages=[{"role": "user", "content": [{"type": "text", "text": f"## CV\n\n{cv_text.strip()}"}]}],
            tools=[_tool()],
            tool_choice={"type": "tool", "name": TOOL_NAME},
        )
        data = extract_tool_input(response, TOOL_NAME)
        if data is None:
            raise AnonymizationError("Model did not return submit_anonymized_cv.")
        clean = _normalize(data)
        clean = scrub_pii(clean, name=candidate_name, email=candidate_email)
        return clean, (getattr(response, "model", "") or self.model)


def scrub_pii(data: dict, *, name: str = "", email: str = "") -> dict:
    """Deterministically redact any leftover email / name tokens in all strings."""
    patterns = []
    if email and email.strip():
        patterns.append(re.compile(re.escape(email.strip()), re.IGNORECASE))
    for token in (name or "").split():
        if len(token) >= 2:  # redact real name tokens incl. short ones (e.g. "Bo", "Li"); skip 1-char initials
            patterns.append(re.compile(rf"\b{re.escape(token)}\b", re.IGNORECASE))

    def redact(value):
        if isinstance(value, str):
            for pat in patterns:
                value = pat.sub(REDACTED, value)
            return value
        if isinstance(value, list):
            return [redact(v) for v in value]
        if isinstance(value, dict):
            return {k: redact(v) for k, v in value.items()}
        return value

    return redact(data)


def _normalize(data: dict) -> dict:
    def slist(v):
        return [str(x).strip() for x in v if str(x).strip()] if isinstance(v, list) else []

    years = data.get("years_experience")
    try:
        years = int(years) if years is not None else None
    except (TypeError, ValueError):
        years = None

    experience = []
    for e in data.get("experience", []) if isinstance(data.get("experience"), list) else []:
        if not isinstance(e, dict):
            continue
        experience.append({
            "role_title": str(e.get("role_title", "")).strip(),
            "industry": str(e.get("industry", "")).strip(),
            "period": str(e.get("period", "")).strip(),
            "highlights": slist(e.get("highlights")),
        })

    education = []
    for ed in data.get("education", []) if isinstance(data.get("education"), list) else []:
        if not isinstance(ed, dict):
            continue
        education.append({
            "qualification": str(ed.get("qualification", "")).strip(),
            "field": str(ed.get("field", "")).strip(),
            "period": str(ed.get("period", "")).strip(),
        })

    return {
        "headline": str(data.get("headline", "")).strip(),
        "summary": str(data.get("summary", "")).strip(),
        "years_experience": years,
        "skills": slist(data.get("skills")),
        "experience": experience,
        "education": education,
    }


def _tool() -> dict:
    return {
        "name": TOOL_NAME,
        "description": "Submit the anonymized, structured CV with all PII removed.",
        "input_schema": {
            "type": "object",
            "properties": {
                "headline": {"type": "string", "description": "Role-level headline, no name."},
                "summary": {"type": "string"},
                "years_experience": {"type": ["integer", "null"]},
                "skills": {"type": "array", "items": {"type": "string"}},
                "experience": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "role_title": {"type": "string"},
                            "industry": {"type": "string"},
                            "period": {"type": "string"},
                            "highlights": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["role_title"],
                    },
                },
                "education": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "qualification": {"type": "string"},
                            "field": {"type": "string"},
                            "period": {"type": "string"},
                        },
                    },
                },
            },
            "required": ["headline", "summary", "skills", "experience"],
        },
    }


def _system_prompt() -> str:
    return (
        "You are preparing an anonymized CV for a recruitment agency to send to a "
        "client. Rewrite the CV into the structured fields, REMOVING all personally "
        "identifying information: the candidate's name, email, phone, address, "
        "personal URLs/socials, and the names of any current/past employers "
        "(replace employer names with the industry/sector, e.g. 'a fintech "
        "scale-up'). Keep skills, responsibilities, achievements, seniority, and "
        "tenure. Do not invent anything. Never include the candidate's name "
        "anywhere. Call submit_anonymized_cv."
    )


def get_default_service(model: str = DEFAULT_MODEL) -> AnonymizationService:
    import anthropic

    return AnonymizationService(client=anthropic.Anthropic(), model=model)
