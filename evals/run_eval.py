#!/usr/bin/env python
"""Golden-set scoring eval — runs the REAL scoring prompt and checks invariants.

Run:  uv run python evals/run_eval.py

Skips cleanly (exit 0) if ANTHROPIC_API_KEY is not set, so CI never breaks just
because no key is available. With a key, it hits the real model and asserts:

  1. Every rubric criterion scored within its scale, with a non-empty rationale.
  2. Each case's overall lands inside its expected band.
  3. Ordering holds (strong > mid > weak).
  4. Re-score stability: the same CV scored twice stays within tolerance.

Exit code is non-zero if any invariant fails — wire this into CI on any change to
the scoring prompt or rubric.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Make the project importable when run as a plain script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evals.golden_set import cases as gs  # noqa: E402


def main() -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("SKIP: ANTHROPIC_API_KEY not set — eval not run (this is fine in CI).")
        return 0

    from ats.scoring.service import ScoringError, get_default_service

    service = get_default_service()
    failures: list[str] = []
    overalls: dict[str, float] = {}

    for case in gs.CASES:
        label = case["label"]
        try:
            result = service.score(
                jd_text=case["jd"], rubric_criteria=gs.RUBRIC, cv_text=case["cv"]
            )
        except ScoringError as exc:
            failures.append(f"[{label}] scoring raised: {exc}")
            continue

        overalls[label] = result.overall

        # 1. per-criterion rationale present (scale/coverage already enforced by service)
        for name, crit in result.per_criterion.items():
            if not crit.get("rationale", "").strip():
                failures.append(f"[{label}] criterion '{name}' has empty rationale")

        # 2. overall in expected band
        if not (case["min"] <= result.overall <= case["max"]):
            failures.append(
                f"[{label}] overall {result.overall} outside band "
                f"[{case['min']}, {case['max']}]"
            )
        print(f"  {label:>14}: overall={result.overall:.0f} "
              f"(band {case['min']}-{case['max']})")

    # 3. ordering
    for higher, lower in gs.ORDERING:
        if higher in overalls and lower in overalls:
            if not overalls[higher] > overalls[lower]:
                failures.append(
                    f"ordering: expected {higher} ({overalls[higher]:.0f}) > "
                    f"{lower} ({overalls[lower]:.0f})"
                )

    # 4. re-score stability
    case = next(c for c in gs.CASES if c["label"] == gs.STABILITY_LABEL)
    second = service.score(jd_text=case["jd"], rubric_criteria=gs.RUBRIC, cv_text=case["cv"])
    drift = abs(second.overall - overalls.get(gs.STABILITY_LABEL, second.overall))
    print(f"  stability drift on {gs.STABILITY_LABEL}: {drift:.0f} "
          f"(tolerance {gs.STABILITY_TOLERANCE:.0f})")
    if drift > gs.STABILITY_TOLERANCE:
        failures.append(
            f"stability: {gs.STABILITY_LABEL} drifted {drift:.0f} > "
            f"{gs.STABILITY_TOLERANCE:.0f}"
        )

    print()
    if failures:
        print(f"FAIL — {len(failures)} invariant(s) violated:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"PASS — {len(gs.CASES)} cases, all invariants held.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
