"""Unit tests for the Phase 8 SARIF export (FindingsReport → SARIF 2.1.0)."""

from __future__ import annotations

import json
from pathlib import Path

from terraform_review_agent.utils.findings_report import build_findings_report
from terraform_review_agent.utils.sarif_export import build_sarif, render_sarif_json
from terraform_review_agent.utils.sources.sarif import parse_sarif
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


def _findings() -> list[Finding]:
    return [
        Finding(
            agent="security",
            severity="critical",
            file="main.tf",
            line=3,
            rule="tfsec:AWS01",
            message="Bucket unencrypted",
        ),
        Finding(agent="style", severity="low", file="vars.tf", rule="tflint:x", message="nit"),
        Finding(
            agent="gds",
            lens="A5",
            severity="info",
            file=".",
            rule="gds:wcag",
            message="needs manual review",
            state="human_only",
        ),
    ]


def test_sarif_structure_and_severity_levels() -> None:
    report = build_findings_report(pr=_pr(), findings=_findings(), cost_summary=None)
    sarif = build_sarif(report)

    assert sarif["version"] == "2.1.0"
    driver = sarif["runs"][0]["tool"]["driver"]
    assert driver["name"] == "terraform-review-agent"
    # One rule per distinct rule id.
    assert {r["id"] for r in driver["rules"]} == {"tfsec:AWS01", "tflint:x", "gds:wcag"}

    results = {r["ruleId"]: r for r in sarif["runs"][0]["results"]}
    assert results["tfsec:AWS01"]["level"] == "error"  # critical -> error
    assert results["tflint:x"]["level"] == "note"  # low -> note
    assert results["gds:wcag"]["level"] == "none"  # info -> none
    # The content-hash id is the partial fingerprint for code-scanning dedupe.
    assert results["tfsec:AWS01"]["partialFingerprints"]["terraformReviewAgent/v1"]
    assert results["gds:wcag"]["properties"]["state"] == "human_only"
    assert results["tfsec:AWS01"]["locations"][0]["physicalLocation"]["region"]["startLine"] == 3


def test_render_sarif_json_is_valid_json() -> None:
    report = build_findings_report(pr=_pr(), findings=_findings(), cost_summary=None)
    payload = json.loads(render_sarif_json(report))
    assert payload["runs"][0]["results"]


def test_sarif_export_round_trips_through_the_parser() -> None:
    # Exporting then re-parsing recovers each finding's severity + original rule id,
    # proving the export is faithful SARIF. (Our parser namespaces the rule under
    # the driver name — "terraform-review-agent:<ruleId>" — since it's built to
    # ingest *other* tools' SARIF, so we match on the original id as a suffix.)
    report = build_findings_report(pr=_pr(), findings=_findings(), cost_summary=None)
    recovered = parse_sarif(build_sarif(report), Path("."))

    by_msg = {f.message: f for f in recovered}
    assert by_msg["Bucket unencrypted"].severity == "critical"
    assert by_msg["Bucket unencrypted"].rule.endswith("tfsec:AWS01")
    assert by_msg["nit"].severity == "low"
    assert by_msg["needs manual review"].severity == "info"
