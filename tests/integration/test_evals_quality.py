"""CI gate for the deterministic (offline) quality eval.

Runs each golden case through the real graph with AI off and asserts the
finding-set identity, severity preservation, schema validity, and openevals
structural summary match. Self-skips when the ``eval`` extra (openevals) is not
installed. The live-model LLM judges are not exercised here (they need a key).
"""

from __future__ import annotations

import pytest

pytest.importorskip("openevals")
pytest.importorskip("jsonschema")

from evals.evaluators import (
    DETERMINISTIC_EVALUATORS,
    findings_identity_match,
    summary_json_match,
)
from evals.golden import CASES, GoldenCase
from evals.target import run_case


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_quality_deterministic(case: GoldenCase) -> None:
    outputs = run_case(case, use_llm=False)
    reference = case.reference()
    for evaluator in (*DETERMINISTIC_EVALUATORS, summary_json_match):
        verdict = evaluator(
            inputs={"case": case.name}, outputs=outputs, reference_outputs=reference
        )
        assert verdict["score"], f"{case.name} :: {verdict['key']} -> {verdict.get('comment')}"


def test_identity_mismatch_is_caught() -> None:
    """A dropped finding must fail the identity evaluator (the gate has teeth)."""

    case = next(c for c in CASES if c.expected_findings)
    outputs = run_case(case, use_llm=False)
    # Reference that expects one extra finding the run cannot produce.
    broken = case.reference()
    broken["identities"] = [
        *broken["identities"],
        ["tfsec", "tfsec:made-up", "critical", "x.tf", 1],
    ]
    verdict = findings_identity_match(
        inputs={"case": case.name}, outputs=outputs, reference_outputs=broken
    )
    assert verdict["score"] is False


def test_langsmith_run_serializes_targets() -> None:
    """Regression: the offline harness patches process-global state, so the LangSmith
    experiment MUST run targets serially (max_concurrency=1) or cases cross-contaminate."""

    from unittest.mock import MagicMock, patch

    import evals.langsmith_run as lr

    client = MagicMock()
    with patch.object(lr, "_client", return_value=client):
        lr.run(judge=False, model="openai:gpt-4.1")
    _, kwargs = client.evaluate.call_args
    assert kwargs["max_concurrency"] == 1
