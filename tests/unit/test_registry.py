"""Tests for the lens registry + the generic ``lens_node`` dispatch."""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from terraform_review_agent.config import settings
from terraform_review_agent.utils import nodes
from terraform_review_agent.utils.lenses import enabled_lenses
from terraform_review_agent.utils.lenses.base import Lens, LensResult
from terraform_review_agent.utils.state import (
    ChangedFile,
    CostSummary,
    Finding,
    PRContext,
    ReviewState,
)


def _state(files: list[ChangedFile]) -> ReviewState:
    pr = PRContext(
        repository="acme/example",
        pr_number=1,
        base_sha="a" * 7,
        head_sha="b" * 7,
        base_ref="main",
        head_ref="feature/x",
        changed_files=files,
    )
    return ReviewState(pr=pr)


def test_enabled_lenses_runs_all_applicable_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    # Empty config = every registered lens that applies. No infracost key, so
    # cost doesn't apply; security + style do (terraform changed).
    monkeypatch.setattr(settings, "enabled_lenses", "")
    monkeypatch.setattr(settings, "infracost_api_key", None)
    state = _state([ChangedFile(path="main.tf")])

    assert [lens.id for lens in enabled_lenses(state)] == ["security", "style"]


def test_enabled_lenses_includes_cost_with_infracost_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "enabled_lenses", "")
    monkeypatch.setattr(settings, "infracost_api_key", SecretStr("k"))
    state = _state([ChangedFile(path="main.tf")])

    assert [lens.id for lens in enabled_lenses(state)] == ["security", "cost", "style"]


def test_enabled_lenses_respects_config_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    # Config selects a subset; applicability still applies on top (cost dropped
    # here for lack of a key even though it's listed).
    monkeypatch.setattr(settings, "enabled_lenses", "security, cost")
    monkeypatch.setattr(settings, "infracost_api_key", None)
    state = _state([ChangedFile(path="main.tf")])

    assert [lens.id for lens in enabled_lenses(state)] == ["security"]


def test_enabled_lenses_empty_when_no_terraform(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "enabled_lenses", "")
    monkeypatch.setattr(settings, "infracost_api_key", SecretStr("k"))
    monkeypatch.setattr(settings, "enabled_rule_packs", "")  # standards lens off
    state = _state([ChangedFile(path="README.md")])

    assert enabled_lenses(state) == []


def test_standards_lens_gated_on_active_rule_pack(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "enabled_lenses", "")
    monkeypatch.setattr(settings, "infracost_api_key", None)
    monkeypatch.setattr(settings, "rule_packs_dir", None)
    state = _state([ChangedFile(path="main.tf")])

    monkeypatch.setattr(settings, "enabled_rule_packs", "")
    assert "standards" not in [lens.id for lens in enabled_lenses(state)]

    monkeypatch.setattr(settings, "enabled_rule_packs", "terraform-cis-aws")
    assert "standards" in [lens.id for lens in enabled_lenses(state)]

    # ...but not on a doc-only PR (no terraform changes -> the run is skipped).
    doc_only = _state([ChangedFile(path="README.md")])
    assert "standards" not in [lens.id for lens in enabled_lenses(doc_only)]


def test_wedge_lenses_gated_on_their_definition(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "enabled_lenses", "")
    monkeypatch.setattr(settings, "infracost_api_key", None)
    monkeypatch.setattr(settings, "enabled_rule_packs", "")
    state = _state([ChangedFile(path="main.tf")])

    # Off by default — no definition configured.
    monkeypatch.setattr(settings, "terraform_standard", "")
    monkeypatch.setattr(settings, "cicd_standard", "")
    ids = [lens.id for lens in enabled_lenses(state)]
    assert "terraform-standard" not in ids and "cicd" not in ids

    # Each turns on when its golden definition is named.
    monkeypatch.setattr(settings, "terraform_standard", "default")
    monkeypatch.setattr(settings, "cicd_standard", "default")
    ids = [lens.id for lens in enabled_lenses(state)]
    assert "terraform-standard" in ids and "cicd" in ids

    # ...but never on a doc-only PR (the whole run is skipped).
    doc_only = _state([ChangedFile(path="README.md")])
    ids = [lens.id for lens in enabled_lenses(doc_only)]
    assert "terraform-standard" not in ids and "cicd" not in ids


def test_lens_node_dispatches_and_merges_cost_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    # lens_node looks the lens up by id, runs it, and surfaces both the findings
    # (for the reducer) and the cost_summary when the lens set one.
    summary = CostSummary(total_monthly=10.0, delta_monthly=2.0)
    finding = Finding(
        agent="cost", severity="medium", file="main.tf", rule="infracost:x", message="m"
    )

    class _FakeLens(Lens):
        id = "cost"

        def applies_to(self, state: ReviewState) -> bool:
            return True

        def run(self, state: ReviewState) -> LensResult:
            return LensResult(findings=[finding], cost_summary=summary)

    monkeypatch.setitem(nodes.LENSES_BY_ID, "cost", _FakeLens())
    state = _state([ChangedFile(path="main.tf")])

    out = nodes.lens_node({"lens_id": "cost", "state": state})

    assert out["findings"] == [finding]
    assert out["cost_summary"] == summary


def test_lens_node_omits_cost_summary_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeLens(Lens):
        id = "security"

        def applies_to(self, state: ReviewState) -> bool:
            return True

        def run(self, state: ReviewState) -> LensResult:
            return LensResult(findings=[])

    monkeypatch.setitem(nodes.LENSES_BY_ID, "security", _FakeLens())
    state = _state([ChangedFile(path="main.tf")])

    out = nodes.lens_node({"lens_id": "security", "state": state})

    assert out == {"findings": []}
