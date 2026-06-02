"""ScoringService — the one place that turns (JD + rubric + CV) into a score.

Design rules from the eng review:
- Structured output via Claude **tool-use**, never free-text parsing. The model
  is forced to call submit_score, so we get schema-valid JSON or an error.
- The JD + rubric go in a **prompt-cached** system block (same across every
  candidate in a role); only the CV varies, so a 50-CV batch reuses the cache.
- The result is **validated against the rubric** (every criterion scored, every
  score within its scale, overall in 0-100). Anything off raises ScoringError so
  the caller marks the row FAILED and it stays visible/retryable — never a silent
  bad score.
- model_version is read back from the response and stored on the Score, so a
  score is always attributable to the exact model that produced it.

The Anthropic client is injected, so tests run with a fake client and no API key.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Protocol

# Default scoring model: Sonnet is the cost/quality sweet spot for batch CV
# scoring. Override with SCORING_MODEL. (Not the most expensive model on purpose.)
DEFAULT_MODEL = os.environ.get("SCORING_MODEL", "claude-sonnet-4-6")
MAX_TOKENS = 2048
OVERALL_MAX = 100.0

TOOL_NAME = "submit_score"


class ScoringError(Exception):
    """Raised when the model output is missing, malformed, or violates the rubric.

    The caller turns this into Score.status = FAILED (visible + retryable).
    """


@dataclass
class ScoreResult:
    overall: float
    per_criterion: dict[str, dict[str, Any]]  # {name: {"score": x, "rationale": ".."}}
    confidence: float | None = None
    model_version: str = ""
    token_cost: int | None = None


class MessagesClient(Protocol):
    """Minimal shape of anthropic.Anthropic().messages we depend on."""

    def create(self, **kwargs: Any) -> Any: ...


@dataclass
class ScoringService:
    client: Any  # something exposing .messages.create(...)
    model: str = DEFAULT_MODEL

    def score(self, *, jd_text: str, rubric_criteria: list[dict], cv_text: str) -> ScoreResult:
        if not cv_text or not cv_text.strip():
            raise ScoringError("CV text is empty; nothing to score.")
        if not rubric_criteria:
            raise ScoringError("Rubric has no criteria.")

        response = self.client.messages.create(
            model=self.model,
            max_tokens=MAX_TOKENS,
            # JD + rubric are identical across the role's candidates -> cache them.
            system=[
                {
                    "type": "text",
                    "text": _system_prompt(jd_text, rubric_criteria),
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": _user_prompt(cv_text)}],
            tools=[_scoring_tool(rubric_criteria)],
            tool_choice={"type": "tool", "name": TOOL_NAME},
        )
        return self._parse(response, rubric_criteria)

    # -- internals -------------------------------------------------------

    def _parse(self, response: Any, rubric_criteria: list[dict]) -> ScoreResult:
        tool_input = extract_tool_input(response, TOOL_NAME)
        if tool_input is None:
            raise ScoringError("Model did not return a submit_score tool call.")

        try:
            overall = float(tool_input["overall"])
            raw_criteria = tool_input["per_criterion"]
        except (KeyError, TypeError, ValueError) as exc:
            raise ScoringError(f"Malformed tool output: {exc}") from exc

        if not (0.0 <= overall <= OVERALL_MAX):
            raise ScoringError(f"overall {overall} outside 0..{OVERALL_MAX}.")

        per_criterion = _normalize_criteria(raw_criteria, rubric_criteria)

        confidence = tool_input.get("confidence")
        if confidence is not None:
            try:
                confidence = float(confidence)
            except (TypeError, ValueError):
                confidence = None
            else:
                # Contract is 0-1; drop out-of-range values rather than freezing
                # a garbage number into the immutable Score.
                if not (0.0 <= confidence <= 1.0):
                    confidence = None

        usage = getattr(response, "usage", None)
        token_cost = None
        if usage is not None:
            token_cost = (getattr(usage, "input_tokens", 0) or 0) + (
                getattr(usage, "output_tokens", 0) or 0
            )

        return ScoreResult(
            overall=overall,
            per_criterion=per_criterion,
            confidence=confidence,
            model_version=getattr(response, "model", "") or self.model,
            token_cost=token_cost,
        )


def _normalize_criteria(raw: Any, rubric_criteria: list[dict]) -> dict[str, dict[str, Any]]:
    """Turn the model's per-criterion array into a name->{score,rationale} dict,
    enforcing: every rubric criterion present, each score within its scale."""
    if not isinstance(raw, list):
        raise ScoringError("per_criterion must be a list.")

    by_name: dict[str, dict[str, Any]] = {}
    for item in raw:
        try:
            name = item["name"]
            score = float(item["score"])
            rationale = str(item.get("rationale", ""))
        except (KeyError, TypeError, ValueError) as exc:
            raise ScoringError(f"Malformed criterion entry {item!r}: {exc}") from exc
        by_name[name] = {"score": score, "rationale": rationale}

    expected = {c["name"]: c for c in rubric_criteria}
    missing = set(expected) - set(by_name)
    if missing:
        raise ScoringError(f"Rubric criteria not scored: {sorted(missing)}")

    for name, crit in expected.items():
        scale = float(crit.get("scale", 5))
        score = by_name[name]["score"]
        if not (0.0 <= score <= scale):
            raise ScoringError(
                f"Criterion '{name}' score {score} outside 0..{scale}."
            )

    # Drop any extra criteria the model invented; keep only rubric-defined ones.
    return {name: by_name[name] for name in expected}


def extract_tool_input(response: Any, tool_name: str) -> dict | None:
    """Pull the input of the named tool_use block from a Claude response.

    Shared by the scorer and the JD extractor. Handles both SDK objects and
    dict-shaped blocks (defensive, for test fakes).
    """
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == tool_name:
            return getattr(block, "input", None)
        if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == tool_name:
            return block.get("input")
    return None


def _scoring_tool(rubric_criteria: list[dict]) -> dict:
    names = [c["name"] for c in rubric_criteria]
    return {
        "name": TOOL_NAME,
        "description": (
            "Submit the structured score for this candidate. Score every rubric "
            "criterion (each on its own 0..scale), give a one-line rationale per "
            "criterion, an overall 0-100 fit score, and a 0-1 confidence."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "overall": {
                    "type": "number",
                    "description": "Overall fit, 0-100 (higher = better match).",
                },
                "confidence": {
                    "type": "number",
                    "description": "Your confidence in this score, 0-1.",
                },
                "per_criterion": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "enum": names},
                            "score": {"type": "number"},
                            "rationale": {"type": "string"},
                        },
                        "required": ["name", "score", "rationale"],
                    },
                },
            },
            "required": ["overall", "per_criterion"],
        },
    }


def _system_prompt(jd_text: str, rubric_criteria: list[dict]) -> str:
    lines = [
        "You are a precise technical recruiter scoring a candidate's CV against a "
        "job description using a fixed rubric. Apply the SAME standard to every "
        "candidate. Be specific; cite evidence from the CV. Do not invent facts.",
        "",
        "## Job description",
        jd_text.strip(),
        "",
        "## Rubric (score each criterion on its scale)",
    ]
    for c in rubric_criteria:
        scale = c.get("scale", 5)
        weight = c.get("weight", "")
        desc = c.get("description", "")
        w = f", weight {weight}" if weight != "" else ""
        lines.append(f"- {c['name']} (0..{scale}{w}): {desc}")
    lines += [
        "",
        "Call submit_score with a score+rationale for every criterion, an overall "
        "0-100 fit score, and a 0-1 confidence.",
    ]
    return "\n".join(lines)


def _user_prompt(cv_text: str) -> list[dict]:
    return [{"type": "text", "text": f"## Candidate CV\n\n{cv_text.strip()}"}]


def get_default_service(model: str = DEFAULT_MODEL) -> ScoringService:
    """Build a service backed by the real Anthropic API. Requires ANTHROPIC_API_KEY."""
    import anthropic

    return ScoringService(client=anthropic.Anthropic(), model=model)
