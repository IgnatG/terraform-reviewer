"""Quality eval runner (local, no LangSmith required).

Runs each golden case through the real graph and applies the evaluators:

* **default** — deterministic, offline, no API key (finding-set identity, severity
  preservation, schema validity, openevals structural summary match). This is the
  CI gate.
* ``--judge`` — additionally runs the live model (rewording on) and the openevals
  LLM-as-judge correctness/hallucination evaluators. Needs a provider key.

Usage::

    python -m evals.run_quality            # deterministic (CI-safe)
    python -m evals.run_quality --judge    # + live-model LLM judges (needs a key)
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from typing import Any

from evals.evaluators import (
    DETERMINISTIC_EVALUATORS,
    make_llm_judges,
    summary_json_match,
)
from evals.golden import CASES, GoldenCase
from evals.target import run_case


@dataclass
class EvalOutcome:
    """One evaluator's verdict on one case."""

    case: str
    key: str
    score: Any
    comment: str | None


def _passed(score: Any) -> bool:
    """Treat None/0/False as not-passed; a float >= 0.5 (judge) as passed."""

    if isinstance(score, bool):
        return score
    if isinstance(score, int | float):
        return score >= 0.5
    return bool(score)


def evaluate_case(case: GoldenCase, *, judge: bool, model: str) -> list[EvalOutcome]:
    outputs = run_case(case, use_llm=judge)
    reference = case.reference()
    evaluators: list[Any] = [*DETERMINISTIC_EVALUATORS, summary_json_match]
    if judge:
        evaluators += make_llm_judges(model)

    outcomes: list[EvalOutcome] = []
    for evaluator in evaluators:
        verdict = evaluator(
            inputs={"case": case.name}, outputs=outputs, reference_outputs=reference
        )
        outcomes.append(
            EvalOutcome(
                case=case.name,
                key=verdict.get("key", getattr(evaluator, "__name__", "eval")),
                score=verdict.get("score"),
                comment=verdict.get("comment"),
            )
        )
    return outcomes


def run_all(*, judge: bool = False, model: str = "openai:gpt-4.1") -> list[EvalOutcome]:
    outcomes: list[EvalOutcome] = []
    for case in CASES:
        outcomes.extend(evaluate_case(case, judge=judge, model=model))
    return outcomes


def _print_report(outcomes: list[EvalOutcome]) -> int:
    failures = 0
    for o in outcomes:
        ok = _passed(o.score)
        failures += 0 if ok else 1
        status = "PASS" if ok else "FAIL"
        print(f"[{status}] {o.case} :: {o.key} (score={o.score})")
        if not ok and o.comment:
            print(f"        {o.comment}")
    total = len(outcomes)
    print(f"\n{total - failures}/{total} checks passed.")
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Terraform-review quality eval")
    parser.add_argument(
        "--judge",
        action="store_true",
        help="run the live model + openevals LLM judges (needs a provider key)",
    )
    parser.add_argument("--model", default="openai:gpt-4.1", help="judge model (with --judge)")
    args = parser.parse_args(argv)

    if args.judge:
        # The judge model reads its key from the environment; forward it from .env.
        from evals._env import bridge_env_from_settings

        bridge_env_from_settings()

    outcomes = run_all(judge=args.judge, model=args.model)
    return 1 if _print_report(outcomes) else 0


if __name__ == "__main__":
    sys.exit(main())
