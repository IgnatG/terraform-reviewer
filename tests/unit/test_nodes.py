"""Unit tests for the graph nodes in :mod:`utils.nodes`.

These exercise the node functions directly (the integration test drives the
compiled graph end-to-end); here we pin the skip branch and the per-lens state
merge — including the ``ai_errors`` / ``cost_summary`` plumbing — without running
real lenses or scanners.
"""

from __future__ import annotations

import pytest

from terraform_review_agent.utils import nodes as nodes_mod
from terraform_review_agent.utils.lenses.base import LensResult
from terraform_review_agent.utils.nodes import (
    aggregator_node,
    lens_node,
    post_comment_node,
    start_node,
)
from terraform_review_agent.utils.state import (
    ChangedFile,
    CostSummary,
    Finding,
    PRContext,
    ReviewState,
)


def _state(files: list[ChangedFile]) -> ReviewState:
    return ReviewState(
        pr=PRContext(
            repository="acme/example",
            pr_number=3,
            base_sha="a" * 7,
            head_sha="b" * 7,
            base_ref="main",
            head_ref="feature/x",
            changed_files=files,
        )
    )


# ---------------------------------------------------------------------------
# start_node
# ---------------------------------------------------------------------------


def test_start_node_skips_when_no_terraform() -> None:
    out = start_node(_state([ChangedFile(path="README.md")]))
    assert out == {"skipped": True, "skip_reason": "no terraform files changed"}


def test_start_node_proceeds_on_terraform_change() -> None:
    out = start_node(_state([ChangedFile(path="main.tf")]))
    assert out == {"skipped": False}


# ---------------------------------------------------------------------------
# lens_node — merges findings / cost_summary / ai_errors from one lens
# ---------------------------------------------------------------------------


class _FakeLens:
    id = "cost"

    def __init__(self, result: LensResult) -> None:
        self._result = result

    def run(self, _state: ReviewState) -> LensResult:
        return self._result


def test_lens_node_merges_all_result_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _state([ChangedFile(path="main.tf")])
    finding = Finding(agent="cost", severity="medium", file="main.tf", rule="x", message="m")
    summary = CostSummary(total_monthly=10.0, delta_monthly=5.0)
    fake = _FakeLens(LensResult(findings=[finding], cost_summary=summary, ai_errors=["cost: boom"]))
    monkeypatch.setattr(nodes_mod, "LENSES_BY_ID", {"cost": fake})

    out = lens_node({"lens_id": "cost", "state": state})

    assert out["findings"] == [finding]
    assert out["cost_summary"] == summary
    assert out["ai_errors"] == ["cost: boom"]


def test_lens_node_omits_unset_optional_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    # A lens that sets neither cost_summary nor ai_errors must not write those keys
    # (so the reducers/sole-writer contract holds).
    state = _state([ChangedFile(path="main.tf")])
    fake = _FakeLens(LensResult(findings=[]))
    monkeypatch.setattr(nodes_mod, "LENSES_BY_ID", {"style": fake})

    out = lens_node({"lens_id": "style", "state": state})

    assert out == {"findings": []}


# ---------------------------------------------------------------------------
# aggregator_node / post_comment_node
# ---------------------------------------------------------------------------


def test_aggregator_node_renders_comment_and_report() -> None:
    state = _state([ChangedFile(path="main.tf")])
    state = state.model_copy(
        update={
            "findings": [
                Finding(
                    agent="security",
                    severity="high",
                    file="main.tf",
                    rule="tfsec:x",
                    message="Public bucket",
                )
            ]
        }
    )

    out = aggregator_node(state)

    assert out.get("comment_markdown")
    # The findings report is the serialized JSON contract and must round-trip.
    from terraform_review_agent.utils.findings_report import FindingsReport

    report = FindingsReport.model_validate_json(out["findings_report_json"])
    assert report.summary.total == 1


def test_post_comment_node_is_a_noop_stub() -> None:
    assert post_comment_node(_state([ChangedFile(path="main.tf")])) == {"posted_comment_id": None}
