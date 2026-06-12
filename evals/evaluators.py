"""Evaluators for the quality eval.

Two families, all in LangSmith's ``(inputs, outputs, reference_outputs)`` shape so
they plug straight into ``client.evaluate`` *and* run standalone:

* **Deterministic** (offline, no API key) — finding-set identity, severity
  preservation, and ``findings.json`` schema validity. These are the CI gate.
* **LLM-as-judge** (opt-in, needs a model) — openevals ``create_json_match_evaluator``
  (structural, model-free) plus ``create_llm_as_judge`` correctness / hallucination
  judges over the reworded comment.

Each evaluator returns ``{"key", "score", "comment"}``; ``score`` is a bool/float
(LangSmith-friendly).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schemas" / "findings.schema.json"

# A finding identity tuple: (source, rule_id, severity, file, line).
Identity = tuple[str, str, str, str, int | None]


def _identities_from_outputs(outputs: dict[str, Any]) -> set[Identity]:
    """Finding identities from a target result (``identities`` set or ``findings`` list)."""

    if "identities" in outputs:
        return {(i.source, i.rule_id, i.severity, i.file, i.line) for i in outputs["identities"]}
    out: set[Identity] = set()
    for record in outputs.get("findings", []):
        loc = record["location"]
        out.add(
            (record["source"], record["rule_id"], record["severity"], loc["file"], loc.get("line"))
        )
    return out


def _identities_from_reference(reference_outputs: dict[str, Any]) -> set[Identity]:
    return {tuple(row) for row in reference_outputs.get("identities", [])}  # type: ignore[misc]


# --- deterministic evaluators -------------------------------------------------


def findings_identity_match(
    *, outputs: dict[str, Any], reference_outputs: dict[str, Any], **_: Any
) -> dict[str, Any]:
    """The exact finding set (severity/file/line/rule) the scanners must produce."""

    actual = _identities_from_outputs(outputs)
    expected = _identities_from_reference(reference_outputs)
    missing = expected - actual
    extra = actual - expected
    passed = not missing and not extra
    comment = "exact match" if passed else f"missing={sorted(missing)} extra={sorted(extra)}"
    return {"key": "findings_identity_match", "score": passed, "comment": comment}


def severities_preserved(
    *, outputs: dict[str, Any], reference_outputs: dict[str, Any], **_: Any
) -> dict[str, Any]:
    """Severity counts match — the LLM must never shift a scanner's severity."""

    actual = outputs.get("summary", {}).get("by_severity", {})
    expected = reference_outputs.get("summary", {}).get("by_severity", {})
    passed = actual == expected
    return {
        "key": "severities_preserved",
        "score": passed,
        "comment": f"expected={expected} actual={actual}",
    }


def findings_schema_valid(*, outputs: dict[str, Any], **_: Any) -> dict[str, Any]:
    """The emitted ``findings.json`` validates against the versioned contract."""

    import jsonschema

    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    try:
        jsonschema.validate(instance=outputs["report"], schema=schema)
    except jsonschema.ValidationError as exc:
        return {"key": "findings_schema_valid", "score": False, "comment": exc.message}
    return {"key": "findings_schema_valid", "score": True, "comment": "valid"}


DETERMINISTIC_EVALUATORS = (
    findings_identity_match,
    severities_preserved,
    findings_schema_valid,
)


# --- openevals: structural JSON match (model-free, deterministic) --------------


def summary_json_match(
    *, outputs: dict[str, Any], reference_outputs: dict[str, Any], **_: Any
) -> dict[str, Any]:
    """openevals ``create_json_match_evaluator`` over the summary block (no model)."""

    from openevals.json import create_json_match_evaluator

    # `aggregator="all"` requires every key to match; the cost headline keys are
    # null for these (cost-free) cases and aren't part of the expectation, so
    # exclude them rather than baking nulls into every reference.
    evaluator = create_json_match_evaluator(
        aggregator="all", exclude_keys=["cost_total_monthly", "cost_delta_monthly"]
    )
    result = evaluator(
        outputs=outputs.get("summary", {}),
        reference_outputs=reference_outputs.get("summary", {}),
    )
    # openevals returns a dict (or list of dicts); normalise to a single verdict.
    verdict = result[0] if isinstance(result, list) else result
    return {
        "key": "summary_json_match",
        "score": verdict.get("score"),
        "comment": verdict.get("comment"),
    }


# --- openevals: LLM-as-judge (opt-in, needs a model) --------------------------


def make_llm_judges(model: str) -> list[Any]:
    """Correctness + hallucination judges over the reworded comment (needs a key).

    These grade the *quality* of the reworded review text against the deterministic
    finding facts — they never gate the finding set (that's the deterministic
    evaluators' job). Returned as LangSmith-compatible callables.
    """

    from openevals.llm import create_llm_as_judge
    from openevals.prompts import CORRECTNESS_PROMPT, HALLUCINATION_PROMPT

    correctness = create_llm_as_judge(
        prompt=CORRECTNESS_PROMPT, model=model, feedback_key="comment_correctness"
    )
    hallucination = create_llm_as_judge(
        prompt=HALLUCINATION_PROMPT, model=model, feedback_key="comment_hallucination"
    )

    def correctness_judge(
        *, outputs: dict[str, Any], reference_outputs: dict[str, Any], **_: Any
    ) -> dict[str, Any]:
        return correctness(
            inputs=reference_outputs.get("summary", {}),
            outputs=outputs.get("comment", ""),
            reference_outputs=reference_outputs.get("identities", []),
        )

    def hallucination_judge(
        *, outputs: dict[str, Any], reference_outputs: dict[str, Any], **_: Any
    ) -> dict[str, Any]:
        # HALLUCINATION_PROMPT needs all four vars (inputs/outputs/context/reference_outputs);
        # context + reference are the deterministic finding facts the comment must not exceed.
        facts = json.dumps(outputs.get("findings", []))
        return hallucination(
            inputs="Summarise the Terraform review findings.",
            outputs=outputs.get("comment", ""),
            context=facts,
            reference_outputs=reference_outputs.get("identities", []),
        )

    return [correctness_judge, hallucination_judge]
