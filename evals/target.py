"""The eval *target* — run the real review graph over a golden case.

Runs the production-compiled ``agent`` with recorded scanners (so findings are
realistic + deterministic) and the workspace materialised on disk. By default the
AI backend is off, so the output is byte-stable; pass ``use_llm=True`` for the
opt-in quality eval where the live model rewords the findings.

Returns the parsed ``findings.json`` contract plus the rendered comment — the
shape the evaluators in :mod:`evals.evaluators` consume.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from evals._offline import recorded_review
from evals.golden import CASES, FindingIdentity, GoldenCase
from terraform_review_agent.agent import agent
from terraform_review_agent.utils.state import ReviewState

_CASES_BY_NAME = {case.name: case for case in CASES}

_STUB_TF = 'resource "null_resource" "noop" {}\n'


def run_case(case: GoldenCase, *, use_llm: bool = False) -> dict[str, Any]:
    """Run one golden case through the graph; return its parsed report + comment."""

    with tempfile.TemporaryDirectory(prefix="tra-quality-") as tmp:
        workspace = Path(tmp)
        for changed in case.changed_files:
            if changed.path.endswith((".tf", ".tfvars", ".tf.json", ".tfvars.json")):
                target = workspace / changed.path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(_STUB_TF, encoding="utf-8")

        state = ReviewState(pr=case.pr(), workspace=str(workspace))
        # ai_backend_factory=None lets the *real* backend run (live quality eval);
        # the default unavailable stub keeps the run deterministic.
        ai_kwargs: dict[str, Any] = {"ai_backend_factory": None} if use_llm else {}
        with recorded_review(case.scanner_outputs, enabled_lenses=case.enabled_lenses, **ai_kwargs):
            final = ReviewState.model_validate(agent.invoke(state))

    assert final.findings_report_json is not None
    report: dict[str, Any] = json.loads(final.findings_report_json)
    identities = frozenset(FindingIdentity.from_record(f) for f in report["findings"])
    return {
        "report": report,
        "summary": report["summary"],
        "findings": report["findings"],
        "comment": final.comment_markdown or "",
        "identities": identities,
    }


def langsmith_target(inputs: dict[str, Any]) -> dict[str, Any]:
    """LangSmith ``client.evaluate`` target: ``{"case": <name>}`` -> review output.

    ``use_llm`` rides on the input so the same dataset can be run deterministically
    or against the live model.
    """

    case = _CASES_BY_NAME[inputs["case"]]
    result = run_case(case, use_llm=bool(inputs.get("use_llm", False)))
    # Drop the non-JSON-serialisable identity set before handing back to LangSmith.
    return {k: v for k, v in result.items() if k != "identities"}
