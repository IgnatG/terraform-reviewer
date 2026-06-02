"""Export a :class:`FindingsReport` as a SARIF 2.1.0 log (Phase 8).

The inverse of ``utils/sources/sarif.py``: it turns the engine's findings into
the format GitHub's code-scanning tab + inline PR annotations consume. One run,
one driver (``terraform-review-agent``), with the original ``{source}:{rule}``
preserved as the SARIF ``ruleId`` so a round-trip recovers the producing tool.

Deterministic: rules and results follow the report's stable ordering, and each
result carries the finding's content-hash id as a ``partialFingerprint`` so
code-scanning dedupes/track findings across runs.
"""

from __future__ import annotations

import json
from typing import Any

from terraform_review_agent.utils.findings_report import FindingsReport, _engine_version
from terraform_review_agent.utils.state import SEVERITY_ORDER, Severity

_INFORMATION_URI = "https://github.com/IgnatG/terraform-reviewer"

# Our severity → SARIF result.level.
_LEVEL: dict[Severity, str] = {
    "critical": "error",
    "high": "error",
    "medium": "warning",
    "low": "note",
    "info": "none",
}

# A CVSS-style 0-10 number so code-scanning can sort by severity (it reads the
# `security-severity` property, not `level`).
_SECURITY_SEVERITY: dict[Severity, str] = {
    "critical": "9.5",
    "high": "8.0",
    "medium": "5.0",
    "low": "3.0",
    "info": "0.0",
}


def build_sarif(report: FindingsReport) -> dict[str, Any]:
    """Build a SARIF 2.1.0 log dict from a findings report."""

    # The rule descriptor's severity must represent the rule, not whichever
    # finding happened to be first — take the most severe across the rule's
    # findings (lower SEVERITY_ORDER rank = more severe). Code-scanning reads
    # `security-severity` from the rule, so this drives its sorting.
    rule_severity: dict[str, Severity] = {}
    for f in report.findings:
        current = rule_severity.get(f.rule_id)
        if current is None or SEVERITY_ORDER[f.severity] < SEVERITY_ORDER[current]:
            rule_severity[f.rule_id] = f.severity

    # Distinct rules in first-seen (report) order → stable `rules` array.
    rules: list[dict[str, Any]] = []
    rule_index: dict[str, int] = {}
    for f in report.findings:
        if f.rule_id in rule_index:
            continue
        rule_index[f.rule_id] = len(rules)
        rules.append(
            {
                "id": f.rule_id,
                "name": f.rule_id,
                # Generic, rule-level text — a per-finding message would be
                # misleading as the rule's description.
                "shortDescription": {"text": f.rule_id},
                "properties": {"security-severity": _SECURITY_SEVERITY[rule_severity[f.rule_id]]},
            }
        )

    results: list[dict[str, Any]] = []
    for f in report.findings:
        physical: dict[str, Any] = {"artifactLocation": {"uri": f.location.file or "."}}
        if f.location.line is not None:
            physical["region"] = {"startLine": f.location.line}
        props: dict[str, Any] = {
            "security-severity": _SECURITY_SEVERITY[f.severity],
            "state": f.state,
        }
        if f.lens:
            props["lens"] = f.lens
        if f.standard:
            props["standard"] = f.standard
        if f.control_id:
            props["control_id"] = f.control_id
        if f.confidence is not None:
            props["confidence"] = f.confidence
        results.append(
            {
                "ruleId": f.rule_id,
                "ruleIndex": rule_index[f.rule_id],
                "level": _LEVEL[f.severity],
                "message": {"text": f.evidence},
                "locations": [{"physicalLocation": physical}],
                "partialFingerprints": {"terraformReviewAgent/v1": f.id},
                "properties": props,
            }
        )

    return {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "terraform-review-agent",
                        "version": _engine_version(),
                        "informationUri": _INFORMATION_URI,
                        "rules": rules,
                    }
                },
                "results": results,
            }
        ],
    }


def render_sarif_json(report: FindingsReport) -> str:
    """Serialize the SARIF log to its canonical on-disk / artefact JSON string."""

    return json.dumps(build_sarif(report), indent=2) + "\n"
