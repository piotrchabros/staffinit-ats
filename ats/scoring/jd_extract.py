"""JD extraction — pull structured_requirements from a job description.

This is metadata for later filtering/search, NOT a scoring input (the scorer reads
the raw JD). So it is best-effort: if the model output is malformed, we store {}
rather than blocking role creation. The result is editable by the user afterward.

Injectable client, like ScoringService, so tests need no API key.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from .service import extract_tool_input

DEFAULT_MODEL = os.environ.get("JD_EXTRACT_MODEL", "claude-sonnet-4-6")
MAX_TOKENS = 1024
TOOL_NAME = "submit_requirements"


class JDExtractionError(Exception):
    """Raised when the model output is missing or malformed."""


@dataclass
class JDExtractionService:
    client: Any
    model: str = DEFAULT_MODEL

    def extract(self, jd_text: str) -> dict:
        if not jd_text or not jd_text.strip():
            raise JDExtractionError("JD text is empty.")

        response = self.client.messages.create(
            model=self.model,
            max_tokens=MAX_TOKENS,
            messages=[{"role": "user", "content": _prompt(jd_text)}],
            tools=[_tool()],
            tool_choice={"type": "tool", "name": TOOL_NAME},
        )
        data = extract_tool_input(response, TOOL_NAME)
        if data is None:
            raise JDExtractionError("Model did not return submit_requirements.")
        return _normalize(data)


def _normalize(data: dict) -> dict:
    def _str_list(value):
        if not isinstance(value, list):
            return []
        return [str(v).strip() for v in value if str(v).strip()]

    years = data.get("min_years_experience")
    try:
        years = int(years) if years is not None else None
    except (TypeError, ValueError):
        years = None

    return {
        "must_have": _str_list(data.get("must_have")),
        "nice_to_have": _str_list(data.get("nice_to_have")),
        "min_years_experience": years,
        "location": str(data.get("location", "")).strip(),
        "summary": str(data.get("summary", "")).strip(),
    }


def _tool() -> dict:
    return {
        "name": TOOL_NAME,
        "description": "Extract the structured requirements from this job description.",
        "input_schema": {
            "type": "object",
            "properties": {
                "must_have": {"type": "array", "items": {"type": "string"}},
                "nice_to_have": {"type": "array", "items": {"type": "string"}},
                "min_years_experience": {"type": ["integer", "null"]},
                "location": {"type": "string"},
                "summary": {"type": "string"},
            },
            "required": ["must_have", "nice_to_have"],
        },
    }


def _prompt(jd_text: str) -> list[dict]:
    return [
        {
            "type": "text",
            "text": (
                "Extract the structured requirements from the job description below. "
                "Separate hard must-haves from nice-to-haves; pull minimum years of "
                "experience and location if stated. Do not invent requirements.\n\n"
                f"## Job description\n\n{jd_text.strip()}"
            ),
        }
    ]


def get_default_jd_service(model: str = DEFAULT_MODEL) -> JDExtractionService:
    import anthropic

    return JDExtractionService(client=anthropic.Anthropic(), model=model)


def extract_requirements(role, *, service: JDExtractionService | None = None) -> dict:
    """Extract requirements for a Role and store them. Best-effort: on failure,
    stores {} (the JD itself still scores fine) so role creation never blocks.
    """
    try:
        svc = service or get_default_jd_service()
        requirements = svc.extract(role.jd_text)
    except Exception:
        # Best-effort by design: a malformed response, a missing API key, or a
        # network error must NEVER block role creation. Scoring uses the raw JD,
        # so {} here costs nothing but the (editable) metadata. Broad except is
        # intentional.
        requirements = {}
    role.structured_requirements = requirements
    role.save(update_fields=["structured_requirements"])
    return requirements
