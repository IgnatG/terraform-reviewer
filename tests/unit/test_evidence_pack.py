"""Unit tests for the Phase 8 evidence pack (HTML + CSV)."""

from __future__ import annotations

import csv
import io

from terraform_review_agent.utils.evidence_pack import render_evidence_csv, render_evidence_html
from terraform_review_agent.utils.findings_report import build_findings_report
from terraform_review_agent.utils.standards import StandardMapper
from terraform_review_agent.utils.standards.pack import Control, RuleMapping, RulePack
from terraform_review_agent.utils.state import Finding, PRContext


def _pr() -> PRContext:
    return PRContext(
        repository="acme/infra",
        pr_number=9,
        base_sha="b" * 40,
        head_sha="h" * 40,
        base_ref="main",
        head_ref="feature",
    )


def _report():  # type: ignore[no-untyped-def]
    pack = RulePack(
        id="cis",
        standard="CIS AWS",
        standard_version="3.0.0",
        rule_pack_version="2026.06.0",
        controls=[Control(id="2.1.1", title="encryption", state="verified")],
        mappings=[RuleMapping(control_id="2.1.1", rule="tfsec:AWS01")],
    )
    findings = [
        Finding(
            agent="security",
            severity="high",
            file="main.tf",
            line=3,
            rule="tfsec:AWS01",
            message="Bucket <unencrypted> & exposed",
        ),
        Finding(
            agent="standards",
            severity="info",
            file=".",
            rule="gap:readme",
            message="needs review",
            state="human_only",
        ),
    ]
    return build_findings_report(
        pr=_pr(), findings=findings, cost_summary=None, mapper=StandardMapper([pack])
    )


def test_html_groups_by_standard_and_escapes() -> None:
    html_out = render_evidence_html(_report())
    assert "<!doctype html>" in html_out
    assert "acme/infra" in html_out
    assert "CIS AWS 3.0.0" in html_out  # standard group header
    assert "2.1.1" in html_out  # control id surfaced
    # All three state badges defined; verified + human_only present here.
    assert "✅" in html_out and "○" in html_out
    # Untrusted finding text is HTML-escaped, not injected.
    assert "<unencrypted>" not in html_out
    assert "&lt;unencrypted&gt;" in html_out


def test_csv_has_header_and_rows() -> None:
    rows = list(csv.reader(io.StringIO(render_evidence_csv(_report()))))
    header = rows[0]
    assert header[0] == "id" and "state" in header and "confidence" in header
    assert len(rows) == 3  # header + 2 findings

    state_idx = header.index("state")
    states = {r[state_idx] for r in rows[1:]}
    assert states == {"verified", "human_only"}
    # The comma/angle-brackets in the evidence are safely quoted (csv round-trips).
    ev_idx = header.index("evidence")
    assert any("<unencrypted> & exposed" in r[ev_idx] for r in rows[1:])


def test_csv_neutralizes_formula_injection() -> None:
    findings = [
        Finding(
            agent="security",
            severity="low",
            file="main.tf",
            rule="tfsec:x",
            message="=cmd|' /c calc'!A1",
            suggestion="+1+2",
        ),
    ]
    report = build_findings_report(pr=_pr(), findings=findings, cost_summary=None)
    rows = list(csv.reader(io.StringIO(render_evidence_csv(report))))
    header, row = rows[0], rows[1]
    # A leading =/+ is prefixed with ' so spreadsheets don't execute it.
    assert row[header.index("evidence")].startswith("'=")
    assert row[header.index("remediation_hint")].startswith("'+")
