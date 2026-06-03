"""Phase 1 — the findings.json contract.

Validates that a built report conforms to ``schemas/findings.schema.json`` and
that the mapping/determinism guarantees hold (stable ids + ordering, source
derivation, verified-by-default state, LLM findings downgraded to evidence).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import jsonschema

from terraform_review_agent.utils.findings_report import (
    SCHEMA_VERSION,
    build_findings_report,
    render_findings_json,
)
from terraform_review_agent.utils.standards import StandardMapper
from terraform_review_agent.utils.standards.pack import (
    Control,
    ExpectedArtifact,
    RuleMapping,
    RulePack,
    gap_rule,
)
from terraform_review_agent.utils.state import CostSummary, Finding, PRContext

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schemas" / "findings.schema.json"

FIXED_TIME = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)


def _schema() -> dict[str, object]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _pr() -> PRContext:
    return PRContext(
        repository="acme/widgets",
        pr_number=42,
        base_sha="b" * 40,
        head_sha="h" * 40,
        base_ref="main",
        head_ref="feature",
    )


def _findings() -> list[Finding]:
    return [
        Finding(
            agent="security",
            severity="high",
            file="main.tf",
            line=12,
            rule="tfsec:AWS017",
            message="S3 bucket is not encrypted.",
            suggestion="Add server_side_encryption_configuration.",
        ),
        Finding(
            agent="style",
            severity="low",
            file="variables.tf",
            line=None,
            rule="tflint:terraform_deprecated_interpolation",
            message="Deprecated interpolation syntax.",
            suggestion=None,
        ),
        Finding(
            agent="security",
            severity="medium",
            file="iam.tf",
            line=3,
            rule="security:llm-overbroad-policy",
            message="IAM policy looks overly broad.",
            suggestion="Scope the actions.",
        ),
    ]


def test_report_validates_against_schema() -> None:
    report = build_findings_report(
        pr=_pr(),
        findings=_findings(),
        cost_summary=CostSummary(total_monthly=120.0, delta_monthly=15.0),
        mode="diff",
        scan_time=FIXED_TIME,
    )
    payload = json.loads(render_findings_json(report))
    jsonschema.validate(payload, _schema())  # raises on any contract violation

    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["scan"]["repository"] == "acme/widgets"
    assert payload["scan"]["commit_sha"] == "h" * 40
    assert payload["scan"]["scan_time"] == "2026-06-01T12:00:00Z"
    assert payload["scan"]["mode"] == "diff"
    assert payload["summary"]["total"] == 3
    assert payload["summary"]["by_severity"] == {"high": 1, "medium": 1, "low": 1}
    assert payload["summary"]["cost_total_monthly"] == 120.0


def test_empty_scan_still_valid() -> None:
    report = build_findings_report(pr=_pr(), findings=[], cost_summary=None, scan_time=FIXED_TIME)
    payload = json.loads(render_findings_json(report))
    jsonschema.validate(payload, _schema())
    assert payload["summary"]["total"] == 0
    assert payload["findings"] == []


def test_mapping_and_source_derivation() -> None:
    report = build_findings_report(pr=_pr(), findings=_findings(), cost_summary=None)
    by_rule = {f.rule_id: f for f in report.findings}

    tfsec = by_rule["tfsec:AWS017"]
    assert tfsec.source == "tfsec"
    assert tfsec.category == "security"
    assert tfsec.state == "verified"
    assert tfsec.confidence == 1.0
    assert tfsec.evidence == "S3 bucket is not encrypted."
    assert tfsec.location.file == "main.tf"
    assert tfsec.location.line == 12
    # Reserved for later phases — unset until the lens registry / mapping land.
    assert tfsec.lens is None
    assert tfsec.standard is None and tfsec.control_id is None

    llm = by_rule["security:llm-overbroad-policy"]
    assert llm.source == "llm"
    assert llm.state == "evidence"
    assert llm.confidence == 0.5


def test_report_with_mapper_populates_controls_and_three_states() -> None:
    # Phase 4: with an active rule pack, findings map to standard controls and
    # the report carries all three states (verified / evidence / human_only).
    pack = RulePack(
        id="cis",
        standard="CIS",
        standard_version="3.0.0",
        rule_pack_version="2026.06.0",
        source_url="https://cis",
        controls=[
            Control(id="2.1.2", title="logging", state="verified", source_url="https://cis/2.1.2"),
            Control(id="DOC.1", title="readme", state="human_only"),
        ],
        mappings=[RuleMapping(control_id="2.1.2", rule="checkov:CKV_AWS_18")],
        expected_artifacts=[
            ExpectedArtifact(
                id="readme",
                control_id="DOC.1",
                any_of=["README.md"],
                severity="low",
                message="no readme",
            )
        ],
    )
    findings = [
        Finding(
            agent="security",
            severity="high",
            file="main.tf",
            line=4,
            rule="checkov:CKV_AWS_18",
            message="no logging",
        ),
        Finding(
            agent="security",
            severity="medium",
            file="iam.tf",
            rule="security:llm-x",
            message="broad",
        ),
        Finding(
            agent="standards",
            severity="low",
            file="README.md",
            rule=gap_rule("cis", "readme"),
            message="no readme",
        ),
    ]
    report = build_findings_report(
        pr=_pr(),
        findings=findings,
        cost_summary=None,
        mapper=StandardMapper([pack]),
        scan_time=FIXED_TIME,
    )
    jsonschema.validate(json.loads(render_findings_json(report)), _schema())

    by_rule = {f.rule_id: f for f in report.findings}
    ckv = by_rule["checkov:CKV_AWS_18"]
    assert (ckv.standard, ckv.standard_version, ckv.control_id) == ("CIS", "3.0.0", "2.1.2")
    assert ckv.rule_pack_version == "2026.06.0"
    assert ckv.state == "verified"

    gap = by_rule[gap_rule("cis", "readme")]
    assert gap.category == "standards"
    assert gap.control_id == "DOC.1"
    assert gap.state == "human_only"

    # Unmapped LLM finding keeps the source-derived (evidence) state, no control.
    llm = by_rule["security:llm-x"]
    assert llm.state == "evidence"
    assert llm.control_id is None

    assert {f.state for f in report.findings} == {"verified", "evidence", "human_only"}


def test_lens_code_propagates_to_record() -> None:
    # Phase 5: an A-coded lens stamps `lens` on the Finding; the report carries it.
    findings = [
        Finding(
            agent="terraform-standard",
            lens="A1",
            severity="low",
            file="modules/vpc/variables.tf",
            rule="terraform-house:missing-file",
            message="Module `modules/vpc` is missing the standard file `variables.tf`.",
        ),
    ]
    report = build_findings_report(pr=_pr(), findings=findings, cost_summary=None)
    record = report.findings[0]
    assert record.lens == "A1"
    assert record.category == "terraform-standard"
    assert record.source == "terraform-house"


def test_findings_ordered_and_ids_stable() -> None:
    report_a = build_findings_report(pr=_pr(), findings=_findings(), cost_summary=None)
    report_b = build_findings_report(
        pr=_pr(), findings=list(reversed(_findings())), cost_summary=None
    )

    severities = [f.severity for f in report_a.findings]
    assert severities == ["high", "medium", "low"]  # severity-ranked, deterministic

    # Same inputs (any order) → same ids in the same order.
    assert [f.id for f in report_a.findings] == [f.id for f in report_b.findings]
