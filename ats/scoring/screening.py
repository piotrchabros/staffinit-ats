"""ScreeningService — generate tailored interview screening questions.

Given the JD, the candidate's CV, and the rubric, produce questions specific to
THIS candidate: verify claims, probe depth on must-haves, and surface gaps. Each
question carries what to listen for, so a recruiter (not necessarily a domain
expert) can run the screen.

Injectable client, like the scorer — tests need no API key.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from .service import extract_tool_input

DEFAULT_MODEL = os.environ.get("SCREENING_MODEL", "claude-sonnet-4-6")
MAX_TOKENS = 2048
TOOL_NAME = "submit_questions"
DEFAULT_N = 6


class ScreeningError(Exception):
    """Raised when the model output is missing or malformed."""


@dataclass
class ScreeningService:
    client: Any
    model: str = DEFAULT_MODEL

    def generate(self, *, jd_text: str, cv_text: str, rubric_criteria: list[dict],
                 n: int = DEFAULT_N) -> tuple[list[dict], str]:
        """Return (questions, model_version). questions: list of
        {topic, question, what_to_listen_for}."""
        if not cv_text or not cv_text.strip():
            raise ScreeningError("CV text is empty; nothing to base questions on.")

        response = self.client.messages.create(
            model=self.model,
            max_tokens=MAX_TOKENS,
            # JD + rubric are stable across candidates in the role -> cache them.
            system=[{
                "type": "text",
                "text": _system_prompt(jd_text, rubric_criteria, n),
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": [{"type": "text", "text": f"## Candidate CV\n\n{cv_text.strip()}"}]}],
            tools=[_tool(n)],
            tool_choice={"type": "tool", "name": TOOL_NAME},
        )
        data = extract_tool_input(response, TOOL_NAME)
        if data is None:
            raise ScreeningError("Model did not return submit_questions.")
        questions = _normalize(data)
        if not questions:
            raise ScreeningError("No questions returned.")
        return questions, (getattr(response, "model", "") or self.model)


def _normalize(data: dict) -> list[dict]:
    raw = data.get("questions")
    if not isinstance(raw, list):
        raise ScreeningError("questions must be a list.")
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        question = str(item.get("question", "")).strip()
        if not question:
            continue
        out.append({
            "topic": str(item.get("topic", "")).strip(),
            "question": question,
            "what_to_listen_for": str(item.get("what_to_listen_for", "")).strip(),
        })
    return out


def _tool(n: int) -> dict:
    return {
        "name": TOOL_NAME,
        "description": f"Submit {n} tailored screening questions for this candidate.",
        "input_schema": {
            "type": "object",
            "properties": {
                "questions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "topic": {"type": "string"},
                            "question": {"type": "string"},
                            "what_to_listen_for": {"type": "string"},
                        },
                        "required": ["question", "what_to_listen_for"],
                    },
                }
            },
            "required": ["questions"],
        },
    }


def _system_prompt(jd_text: str, rubric_criteria: list[dict], n: int) -> str:
    crits = ", ".join(c["name"] for c in rubric_criteria) or "the role requirements"
    return (
        f"You are a technical recruiter preparing a screening call. Generate {n} "
        "questions tailored to THIS candidate's CV and the job below. Each question "
        "should do one of: verify a specific claim on their CV, probe depth on a "
        f"must-have ({crits}), or surface a likely gap. Reference concrete details "
        "from the CV — not generic questions. For each, give a short "
        "'what_to_listen_for' so a non-expert can judge the answer.\n\n"
        f"## Job description\n\n{jd_text.strip()}\n\n"
        "Call submit_questions with the list."
    )


def get_default_service(model: str = DEFAULT_MODEL) -> ScreeningService:
    import anthropic

    return ScreeningService(client=anthropic.Anthropic(), model=model)
