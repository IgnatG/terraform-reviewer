"""Unit tests for the standard-mapping + gap layer (rule packs)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from terraform_review_agent.config import settings
from terraform_review_agent.utils.standards import (
    StandardMapper,
    detect_gaps,
    load_active_packs,
)
from terraform_review_agent.utils.standards.pack import (
    Control,
    ExpectedArtifact,
    RuleMapping,
    RulePack,
    gap_rule,
)


def _pack(**kw: object) -> RulePack:
    base: dict[str, object] = {
        "id": "p",
        "standard": "Std",
        "standard_version": "1.0",
        "rule_pack_version": "2026.0",
    }
    base.update(kw)
    return RulePack.model_validate(base)


# ---------------------------------------------------------------------------
# loader / selection
# ---------------------------------------------------------------------------


def test_builtin_pack_is_discoverable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "enabled_rule_packs", "*")
    monkeypatch.setattr(settings, "rule_packs_dir", None)
    ids = [p.id for p in load_active_packs()]
    assert "terraform-cis-aws" in ids


def test_empty_selection_activates_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "enabled_rule_packs", "")
    assert load_active_packs() == []


def test_csv_selection_filters_by_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "enabled_rule_packs", "terraform-cis-aws, nope")
    monkeypatch.setattr(settings, "rule_packs_dir", None)
    assert [p.id for p in load_active_packs()] == ["terraform-cis-aws"]


def test_unknown_pack_id_logs_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    # A requested id matching no discovered pack silently resolves to inert; warn
    # so a typo'd / missing pack id is visible instead of looking like "off".
    from terraform_review_agent.utils.standards import loader as loader_mod

    events: list[tuple[str, tuple[str, ...]]] = []
    monkeypatch.setattr(
        loader_mod.log,
        "warning",
        lambda event, **kw: events.append((event, tuple(kw.get("requested", ())))),
    )
    monkeypatch.setattr(settings, "rule_packs_dir", None)
    monkeypatch.setattr(settings, "enabled_rule_packs", "terraform-cis-aws, typo-pack")

    active = load_active_packs()

    assert [p.id for p in active] == ["terraform-cis-aws"]  # the real one still loads
    assert ("rule_pack.unknown_id", ("typo-pack",)) in events


def test_all_known_pack_ids_log_no_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    from terraform_review_agent.utils.standards import loader as loader_mod

    warned: list[str] = []
    monkeypatch.setattr(loader_mod.log, "warning", lambda event, **kw: warned.append(event))
    monkeypatch.setattr(settings, "rule_packs_dir", None)
    monkeypatch.setattr(settings, "enabled_rule_packs", "terraform-cis-aws")

    load_active_packs()
    assert "rule_pack.unknown_id" not in warned


def test_external_dir_packs_loaded_and_invalid_skipped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "good.json").write_text(
        '{"id":"custom","standard":"S","standard_version":"1","rule_pack_version":"1"}'
    )
    (tmp_path / "bad.json").write_text("{not valid pack")
    monkeypatch.setattr(settings, "rule_packs_dir", str(tmp_path))
    monkeypatch.setattr(settings, "enabled_rule_packs", "*")

    ids = [p.id for p in load_active_packs()]
    # The valid external pack loads; the malformed one is skipped, not fatal.
    assert "custom" in ids
    assert "terraform-cis-aws" in ids  # built-in still discovered


# ---------------------------------------------------------------------------
# mapping
# ---------------------------------------------------------------------------


def test_exact_rule_wins_over_prefix() -> None:
    pack = _pack(
        controls=[Control(id="C1", title="exact"), Control(id="C2", title="prefix")],
        mappings=[
            RuleMapping(control_id="C1", rule="a:b"),
            RuleMapping(control_id="C2", rule_prefix="a:"),
        ],
    )
    assert StandardMapper([pack]).map_rule("a:b").control_id == "C1"


def test_longest_prefix_wins() -> None:
    pack = _pack(
        controls=[Control(id="C1", title="short"), Control(id="C2", title="long")],
        mappings=[
            RuleMapping(control_id="C1", rule_prefix="a:"),
            RuleMapping(control_id="C2", rule_prefix="a:b"),
        ],
    )
    assert StandardMapper([pack]).map_rule("a:bc").control_id == "C2"


def test_mapping_carries_control_state_and_pack_metadata() -> None:
    pack = _pack(
        standard="CIS",
        standard_version="3.0.0",
        rule_pack_version="2026.06.0",
        controls=[Control(id="DOC.1", title="readme", state="human_only", source_url="u")],
        mappings=[RuleMapping(control_id="DOC.1", rule="gap:x")],
    )
    m = StandardMapper([pack]).map_rule("gap:x")
    assert m is not None
    assert (m.standard, m.standard_version, m.rule_pack_version) == ("CIS", "3.0.0", "2026.06.0")
    assert m.state == "human_only"
    assert m.source_url == "u"


def test_unmapped_rule_returns_none() -> None:
    pack = _pack(
        controls=[Control(id="C1", title="x")],
        mappings=[RuleMapping(control_id="C1", rule="a:b")],
    )
    assert StandardMapper([pack]).map_rule("z:other") is None


def test_pack_rejects_mapping_to_unknown_control() -> None:
    with pytest.raises(ValidationError, match="unknown control_id"):
        _pack(mappings=[RuleMapping(control_id="GHOST", rule="a:b")])


def test_pack_rejects_artifact_with_unknown_control() -> None:
    with pytest.raises(ValidationError, match="unknown control_id"):
        _pack(
            expected_artifacts=[
                ExpectedArtifact(id="x", control_id="GHOST", any_of=["X"], message="m")
            ]
        )


def test_pack_rejects_absolute_or_traversing_artifact_paths() -> None:
    for bad in ("/etc/passwd", "../escape", "a/../../b"):
        with pytest.raises(ValidationError, match="workspace-relative"):
            ExpectedArtifact(id="x", control_id="C", any_of=[bad], message="m")


def test_expected_artifacts_are_mappable_via_gap_rule() -> None:
    pack = _pack(
        controls=[Control(id="DOC.1", title="readme", state="human_only")],
        expected_artifacts=[
            ExpectedArtifact(
                id="readme", control_id="DOC.1", any_of=["README.md"], message="no readme"
            )
        ],
    )
    m = StandardMapper([pack]).map_rule(gap_rule("p", "readme"))
    assert m is not None and m.control_id == "DOC.1" and m.state == "human_only"


# ---------------------------------------------------------------------------
# gap detection
# ---------------------------------------------------------------------------


def _gap_pack() -> RulePack:
    return _pack(
        controls=[Control(id="DOC.1", title="readme", state="human_only")],
        expected_artifacts=[
            ExpectedArtifact(
                id="readme",
                control_id="DOC.1",
                any_of=["README.md", "docs/README.md"],
                severity="low",
                message="Repository has no README.",
                suggestion="Add one.",
            )
        ],
    )


def test_gap_emitted_when_artifact_absent(tmp_path: Path) -> None:
    findings = detect_gaps(tmp_path, [_gap_pack()])
    assert len(findings) == 1
    f = findings[0]
    assert f.agent == "standards"
    assert f.rule == gap_rule("p", "readme")
    assert f.severity == "low"
    assert f.file == "README.md"  # the canonical (first) candidate
    assert f.message == "Repository has no README."


def test_no_gap_when_any_candidate_present(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "README.md").write_text("hi")  # second candidate satisfies it
    assert detect_gaps(tmp_path, [_gap_pack()]) == []
