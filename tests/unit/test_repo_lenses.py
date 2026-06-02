"""Unit tests for the Phase 7 repo lenses (A3 coverage, A4 tech-debt, A5 GDS)."""

from __future__ import annotations

from pathlib import Path

import pytest

from terraform_review_agent.config import settings
from terraform_review_agent.utils.lenses.coverage import CoverageLens
from terraform_review_agent.utils.lenses.gds import GDSLens
from terraform_review_agent.utils.lenses.tech_debt import TechDebtLens
from terraform_review_agent.utils.sources.jscpd import parse_jscpd
from terraform_review_agent.utils.standardisers import load_gds_definition
from terraform_review_agent.utils.standardisers.gds import evaluate_gds
from terraform_review_agent.utils.state import ChangedFile, PRContext, ReviewState


def _state(workspace: Path, paths: list[str]) -> ReviewState:
    pr = PRContext(
        repository="acme/example",
        pr_number=1,
        base_sha="a" * 7,
        head_sha="b" * 7,
        base_ref="main",
        head_ref="feature/x",
        changed_files=[ChangedFile(path=p) for p in paths],
    )
    return ReviewState(pr=pr, workspace=str(workspace))


def _rule(findings: list, name: str) -> list:  # type: ignore[type-arg]
    return [f for f in findings if f.rule == name]


# ---------------------------------------------------------------------------
# A3 — coverage
# ---------------------------------------------------------------------------


def _lcov(tmp_path: Path, body: str) -> str:
    p = tmp_path / "lcov.info"
    p.write_text(body)
    return str(p)


def test_a3_flags_under_covered_changed_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # foo.py: 1/5 lines covered = 20% (< 80 threshold, < 40 -> high).
    report = _lcov(
        tmp_path, "SF:src/foo.py\nDA:1,1\nDA:2,0\nDA:3,0\nDA:4,0\nDA:5,0\nend_of_record\n"
    )
    monkeypatch.setattr(settings, "coverage_report_path", report)
    monkeypatch.setattr(settings, "coverage_min_percent", 80.0)
    state = _state(tmp_path, ["main.tf", "src/foo.py"])

    findings = CoverageLens().run(state).findings
    under = _rule(findings, "coverage:under-covered")
    assert len(under) == 1
    assert under[0].file == "src/foo.py"
    assert under[0].severity == "high"
    assert under[0].lens == "A3" and under[0].agent == "coverage"
    score = _rule(findings, "coverage:score")
    assert score and "20%" in score[0].message


def test_a3_well_covered_file_only_scores(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    report = _lcov(tmp_path, "SF:src/foo.py\nDA:1,1\nDA:2,1\nend_of_record\n")
    monkeypatch.setattr(settings, "coverage_report_path", report)
    monkeypatch.setattr(settings, "coverage_min_percent", 80.0)
    state = _state(tmp_path, ["main.tf", "src/foo.py"])

    findings = CoverageLens().run(state).findings
    assert _rule(findings, "coverage:under-covered") == []
    assert _rule(findings, "coverage:score")


def test_a3_unchanged_file_not_flagged(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # The under-covered file isn't in the PR -> no per-file finding (just score).
    report = _lcov(tmp_path, "SF:src/other.py\nDA:1,0\nDA:2,0\nend_of_record\n")
    monkeypatch.setattr(settings, "coverage_report_path", report)
    state = _state(tmp_path, ["main.tf"])
    findings = CoverageLens().run(state).findings
    assert _rule(findings, "coverage:under-covered") == []


def test_a3_gated_on_report(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(settings, "coverage_report_path", None)
    assert CoverageLens().applies_to(_state(tmp_path, ["main.tf"])) is False


# ---------------------------------------------------------------------------
# A4 — tech-debt
# ---------------------------------------------------------------------------


def test_jscpd_parser_reads_percentage_and_clones() -> None:
    report = parse_jscpd(
        {
            "statistics": {"total": {"percentage": 12.5}},
            "duplicates": [
                {
                    "lines": 10,
                    "firstFile": {"name": "src/foo.py", "start": 5},
                    "secondFile": {"name": "src/bar.py", "start": 40},
                }
            ],
        }
    )
    assert report.duplication_percent == 12.5
    assert report.clones[0].file == "src/foo.py" and report.clones[0].other_start_line == 40


def test_a4_emits_duplication_finding_and_score(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    jscpd = tmp_path / "jscpd.json"
    jscpd.write_text(
        '{"statistics":{"total":{"percentage":12.5}},"duplicates":'
        '[{"lines":10,"firstFile":{"name":"src/foo.py","start":5},'
        '"secondFile":{"name":"src/bar.py","start":40}}]}'
    )
    monkeypatch.setattr(settings, "jscpd_report_path", str(jscpd))
    monkeypatch.setattr(settings, "sonarqube_sarif_path", None)
    state = _state(tmp_path, ["main.tf", "src/foo.py"])

    findings = TechDebtLens().run(state).findings
    dup = [f for f in findings if f.rule.startswith("jscpd:duplication")]
    assert len(dup) == 1 and dup[0].lens == "A4" and dup[0].file == "src/foo.py"
    score = _rule(findings, "tech-debt:score")
    assert score and "12.5% code duplication" in score[0].message


def test_a4_scopes_duplication_to_changed_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    jscpd = tmp_path / "jscpd.json"
    jscpd.write_text(
        '{"statistics":{"total":{"percentage":3.0}},"duplicates":'
        '[{"lines":8,"firstFile":{"name":"legacy/old.py","start":1},'
        '"secondFile":{"name":"legacy/older.py","start":1}}]}'
    )
    monkeypatch.setattr(settings, "jscpd_report_path", str(jscpd))
    monkeypatch.setattr(settings, "sonarqube_sarif_path", None)
    state = _state(tmp_path, ["main.tf"])  # legacy/old.py not in the PR

    findings = TechDebtLens().run(state).findings
    assert [f for f in findings if f.rule.startswith("jscpd:duplication")] == []  # scoped out
    assert _rule(findings, "tech-debt:score")  # repo-level score still emitted


def test_a4_gated_on_a_signal(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(settings, "jscpd_report_path", None)
    monkeypatch.setattr(settings, "sonarqube_sarif_path", None)
    assert TechDebtLens().applies_to(_state(tmp_path, ["main.tf"])) is False


def test_a4_multiple_clones_of_same_block_do_not_collapse(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The same block (same file + start line) cloned into two other files must
    # surface as two findings, not collapse under the (file, rule, line) key.
    jscpd = tmp_path / "jscpd.json"
    jscpd.write_text(
        '{"statistics":{"total":{"percentage":9.0}},"duplicates":['
        '{"lines":10,"firstFile":{"name":"src/foo.py","start":5},'
        '"secondFile":{"name":"src/bar.py","start":40}},'
        '{"lines":10,"firstFile":{"name":"src/foo.py","start":5},'
        '"secondFile":{"name":"src/baz.py","start":70}}]}'
    )
    monkeypatch.setattr(settings, "jscpd_report_path", str(jscpd))
    monkeypatch.setattr(settings, "sonarqube_sarif_path", None)
    state = _state(tmp_path, ["main.tf", "src/foo.py"])

    dup = [f for f in TechDebtLens().run(state).findings if f.rule.startswith("jscpd:duplication")]
    assert len(dup) == 2
    assert len({f.rule for f in dup}) == 2  # distinct rule ids -> survive dedupe


def test_a3_ambiguous_basename_is_not_misattributed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Two changed files share the basename util.py; a bare "util.py" coverage
    # entry must not be attributed to either (ambiguous -> skipped).
    report = _lcov(tmp_path, "SF:util.py\nDA:1,0\nDA:2,0\nend_of_record\n")
    monkeypatch.setattr(settings, "coverage_report_path", report)
    state = _state(tmp_path, ["main.tf", "a/util.py", "b/util.py"])
    findings = CoverageLens().run(state).findings
    assert _rule(findings, "coverage:under-covered") == []


# ---------------------------------------------------------------------------
# A5 — GDS readiness
# ---------------------------------------------------------------------------


def test_a5_reports_three_states_honestly(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text('{"dependencies": {"govuk-frontend": "^5.0.0"}}')
    (tmp_path / "LICENSE").write_text("MIT")
    (tmp_path / "README.md").write_text("# Service")
    # No accessibility statement file -> that verified point is "not met".
    monkeypatch.setattr(settings, "gds_standard", "default")
    definition = load_gds_definition()
    assert definition is not None

    findings = evaluate_gds(tmp_path, definition)
    by_rule = {f.rule: f for f in findings}

    assert by_rule["gds:components-govuk-frontend"].state == "verified"
    assert by_rule["gds:components-govuk-frontend"].severity == "info"  # met
    assert by_rule["gds:opensource-licence"].severity == "info"  # met
    assert by_rule["gds:accessibility-statement-present"].severity == "medium"  # not met
    # Out-of-scope points are reported honestly, never faked as passing.
    assert by_rule["gds:accessibility-wcag-rendered"].state == "human_only"
    assert by_rule["gds:secure-secrets-in-history"].state == "evidence"
    score = by_rule["gds:score"]
    assert "3/4 code-evidenceable points met" in score.message
    assert "3 point(s) need manual/rendered review" in score.message


def test_a5_lens_gated_on_definition(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(settings, "gds_standard", "")
    assert GDSLens().applies_to(_state(tmp_path, ["main.tf"])) is False
    monkeypatch.setattr(settings, "gds_standard", "default")
    assert GDSLens().applies_to(_state(tmp_path, ["main.tf"])) is True
