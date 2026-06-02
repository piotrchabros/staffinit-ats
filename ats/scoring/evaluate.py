"""EvaluationService — evaluate a candidate from a screening-call transcript.

Given the JD, the CV, the rubric, and the call transcript, produce a structured
evaluation grounded in what was actually SAID in the call (strengths/concerns cite
the transcript), not in the CV alone.

Injectable client — tests need no API key.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from .service import extract_tool_input

DEFAULT_MODEL = os.environ.get("EVALUATION_MODEL", "claude-sonnet-4-6")
MAX_TOKENS = 2048
TOOL_NAME = "submit_evaluation"
RECOMMENDATIONS = ("strong_yes", "yes", "maybe", "no")


class EvaluationError(Exception):
    pass


@dataclass
class EvaluationService:
    client: Any
    model: str = DEFAULT_MODEL

    def evaluate(self, *, jd_text: str, cv_text: str, rubric_criteria: list[dict],
                 transcript: str) -> tuple[dict, str]:
        if not transcript or not transcript.strip():
            raise EvaluationError("Transcript is empty; nothing to evaluate.")

        response = self.client.messages.create(
            model=self.model,
            max_tokens=MAX_TOKENS,
            # JD + rubric + CV are stable for this candidate; transcript varies.
            system=[{
                "type": "text",
                "text": _system_prompt(jd_text, rubric_criteria, cv_text),
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": [
                {"type": "text", "text": f"## Screening call transcript\n\n{transcript.strip()}"}
            ]}],
            tools=[_tool(rubric_criteria)],
            tool_choice={"type": "tool", "name": TOOL_NAME},
        )
        data = extract_tool_input(response, TOOL_NAME)
        if data is None:
            raise EvaluationError("Model did not return submit_evaluation.")
        return _normalize(data, rubric_criteria), (getattr(response, "model", "") or self.model)


def _normalize(data: dict, rubric_criteria: list[dict]) -> dict:
    def slist(v):
        return [str(x).strip() for x in v if str(x).strip()] if isinstance(v, list) else []

    rec = str(data.get("recommendation", "")).strip().lower()
    if rec not in RECOMMENDATIONS:
        raise EvaluationError(f"Invalid recommendation: {rec!r}")

    criteria = []
    for c in data.get("criteria", []) if isinstance(data.get("criteria"), list) else []:
        if not isinstance(c, dict):
            continue
        name = str(c.get("name", "")).strip()
        if not name:
            continue
        criteria.append({
            "name": name,
            "assessment": str(c.get("assessment", "")).strip(),
            "evidence": str(c.get("evidence", "")).strip(),
        })

    return {
        "recommendation": rec,
        "headline": str(data.get("headline", "")).strip(),
        "summary": str(data.get("summary", "")).strip(),
        "strengths": slist(data.get("strengths")),
        "concerns": slist(data.get("concerns")),
        "criteria": criteria,
    }


def _tool(rubric_criteria: list[dict]) -> dict:
    names = [c["name"] for c in rubric_criteria]
    name_schema = {"type": "string"}
    if names:
        name_schema["enum"] = names
    return {
        "name": TOOL_NAME,
        "description": "Submit the structured post-screening evaluation.",
        "input_schema": {
            "type": "object",
            "properties": {
                "recommendation": {"type": "string", "enum": list(RECOMMENDATIONS)},
                "headline": {"type": "string"},
                "summary": {"type": "string"},
                "strengths": {"type": "array", "items": {"type": "string"}},
                "concerns": {"type": "array", "items": {"type": "string"}},
                "criteria": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": name_schema,
                            "assessment": {"type": "string"},
                            "evidence": {"type": "string", "description": "Quote/paraphrase from the transcript."},
                        },
                        "required": ["name", "assessment"],
                    },
                },
            },
            "required": ["recommendation", "summary", "strengths", "concerns"],
        },
    }


def _system_prompt(jd_text: str, rubric_criteria: list[dict], cv_text: str) -> str:
    crit_lines = "\n".join(f"- {c['name']}" for c in rubric_criteria) or "- (role requirements)"
    return (
        "You are evaluating a candidate after a screening call. Use the job "
        "description, the candidate's CV, and the call transcript. Ground your "
        "judgement in what was actually SAID in the call — every strength and "
        "concern should be supported by the transcript, and each criterion "
        "assessment should cite transcript evidence (quote or close paraphrase). "
        "Do not invent answers the candidate did not give. Give an overall "
        "recommendation: strong_yes, yes, maybe, or no.\n\n"
        f"## Job description\n\n{jd_text.strip()}\n\n"
        f"## Rubric criteria to assess\n{crit_lines}\n\n"
        f"## Candidate CV\n\n{cv_text.strip()}\n\n"
        "Call submit_evaluation."
    )


def get_default_service(model: str = DEFAULT_MODEL) -> EvaluationService:
    import anthropic

    return EvaluationService(client=anthropic.Anthropic(), model=model)
