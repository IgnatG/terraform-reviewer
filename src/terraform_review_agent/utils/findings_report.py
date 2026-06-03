"""Findings-report assembly — the versioned JSON contract (the spine).

The Assessor emits a single ``findings.json`` per scan, consumed by the report
renderer, the hosted dashboard, and the Remediator. This module maps the
internal scanner :class:`~terraform_review_agent.utils.state.Finding` set + PR
context into that contract.

Deterministic by design: findings are stably ordered and given content-derived
ids, so re-running the same scan yields byte-identical ``findings`` (only
``scan.scan_time`` varies). Fields the later phases own — ``lens`` (Phase 2),
``standard``/``control_id``/``state`` (Phase 4) — are reserved here and left at
their defaults so the contract is stable from day one.

Schema of record: ``schemas/findings.schema.json`` (kept in sync by
``tests/unit/test_findings_report.py``).
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from typing import Literal

from pydantic import BaseModel, Field

from terraform_review_agent.utils.standards.mapping import StandardMapper
from terraform_review_agent.utils.state import (
    SEVERITY_ORDER,
    CostSummary,
    Finding,
    PRContext,
    Severity,
)

SCHEMA_VERSION = "1.0"

FindingState = Literal["verified", "evidence", "human_only"]
ScanMode = Literal["diff", "full"]


def _engine_version() -> str:
    try:
        return version("terraform-review-agent")
    except PackageNotFoundError:  # pragma: no cover - editable/dev edge
        return "0.0.0"


def _finding_id(source: str, rule: str, file: str, line: int | None) -> str:
    """Stable id from the finding's identity, so reruns produce the same ids."""

    raw = f"{source}|{rule}|{file}|{'' if line is None else line}"
    # Content addressing only — not a security digest.
    return hashlib.sha1(raw.encode("utf-8"), usedforsecurity=False).hexdigest()[:12]


def _is_llm_finding(finding: Finding) -> bool:
    """True for a speculative LLM-discovered finding (``{agent}:llm-…`` rule)."""

    return ":llm-" in finding.rule


def _source_of(finding: Finding) -> str:
    """Producing tool, derived from the namespaced rule (e.g. ``tfsec:AWS017``).

    Scanner findings carry a ``{tool}:{id}`` rule; LLM-discovered ones use
    ``{agent}:llm-…`` and are reported with source ``llm``. Falls back to the
    owning agent when a rule is somehow unprefixed.
    """

    if _is_llm_finding(finding):
        return "llm"
    if ":" in finding.rule:
        return finding.rule.split(":", 1)[0]
    return finding.agent


class FindingLocation(BaseModel):
    """Where a finding sits — file is always known, line/url may not be."""

    file: str
    line: int | None = None
    url: str | None = None


class FindingRecord(BaseModel):
    """One finding in the emitted contract (superset of the internal Finding)."""

    id: str
    lens: str | None = None  # A1-A4 — set by the A-coded lenses (A1/A2 today)
    category: str  # current producer: security | cost | style
    standard: str | None = None  # populated by the standard-mapping layer (Phase 4)
    standard_version: str | None = None
    control_id: str | None = None
    source: str  # producing tool: tfsec | checkov | infracost | tflint | terraform-fmt | llm
    rule_id: str
    rule_pack_version: str | None = None
    state: FindingState = "verified"
    severity: Severity
    confidence: float | None = None
    evidence: str
    location: FindingLocation
    remediation_hint: str | None = None


class ScanMetadata(BaseModel):
    """Top-level provenance for the scan that produced this report."""

    repository: str
    pr_number: int
    commit_sha: str  # PR head
    base_sha: str
    scan_time: str  # ISO-8601 UTC
    engine_version: str
    mode: ScanMode


class FindingsSummary(BaseModel):
    """At-a-glance counts + the cost headline."""

    total: int
    by_severity: dict[str, int] = Field(default_factory=dict)
    cost_total_monthly: float | None = None
    cost_delta_monthly: float | None = None


class FindingsReport(BaseModel):
    """The whole emitted document — see ``schemas/findings.schema.json``."""

    schema_version: str = SCHEMA_VERSION
    scan: ScanMetadata
    summary: FindingsSummary
    findings: list[FindingRecord]


# Per-finding confidence, derived from the three-state class: a machine-verified
# finding is certain; an evidence finding wants a human glance; a human_only point
# is a judgement the engine can't score (None = not applicable).
_STATE_CONFIDENCE: dict[FindingState, float | None] = {
    "verified": 1.0,
    "evidence": 0.5,
    "human_only": None,
}


def _to_record(finding: Finding, mapper: StandardMapper | None = None) -> FindingRecord:
    source = _source_of(finding)
    is_llm = _is_llm_finding(finding)
    mapping = mapper.map_rule(finding.rule) if mapper is not None else None
    # State precedence: a lens that knows the classification asserts it directly
    # (gap checks); else a mapped control owns it; else fall back to the
    # producer default (deterministic scanner -> verified, speculative LLM ->
    # evidence).
    if finding.state is not None:
        state: FindingState = finding.state
    elif mapping is not None:
        state = mapping.state
    else:
        state = "evidence" if is_llm else "verified"
    return FindingRecord(
        id=_finding_id(source, finding.rule, finding.file, finding.line),
        lens=finding.lens,
        category=finding.agent,
        standard=mapping.standard if mapping else None,
        standard_version=mapping.standard_version if mapping else None,
        control_id=mapping.control_id if mapping else None,
        source=source,
        rule_id=finding.rule,
        rule_pack_version=mapping.rule_pack_version if mapping else None,
        state=state,
        severity=finding.severity,
        confidence=_STATE_CONFIDENCE[state],
        evidence=finding.message,
        location=FindingLocation(file=finding.file, line=finding.line),
        remediation_hint=finding.suggestion,
    )


def build_findings_report(
    *,
    pr: PRContext,
    findings: list[Finding],
    cost_summary: CostSummary | None,
    mode: ScanMode = "full",
    scan_time: datetime | None = None,
    mapper: StandardMapper | None = None,
) -> FindingsReport:
    """Assemble the report from the graph's final findings + PR context.

    Findings are sorted by (severity, file, line, rule) for a stable order. When
    ``mapper`` is given, each finding is mapped to its standard control and the
    control's three-state classification; otherwise standard/control fields stay
    null (pre-Phase-4 behaviour).
    """

    def _sort_key(f: Finding) -> tuple[int, str, int, str]:
        return (SEVERITY_ORDER[f.severity], f.file, -1 if f.line is None else f.line, f.rule)

    ordered = sorted(findings, key=_sort_key)
    by_severity: dict[str, int] = {}
    for finding in ordered:
        by_severity[finding.severity] = by_severity.get(finding.severity, 0) + 1

    ts = (scan_time or datetime.now(UTC)).astimezone(UTC)
    return FindingsReport(
        scan=ScanMetadata(
            repository=pr.repository,
            pr_number=pr.pr_number,
            commit_sha=pr.head_sha,
            base_sha=pr.base_sha,
            scan_time=ts.isoformat().replace("+00:00", "Z"),
            engine_version=_engine_version(),
            mode=mode,
        ),
        summary=FindingsSummary(
            total=len(ordered),
            by_severity=by_severity,
            cost_total_monthly=cost_summary.total_monthly if cost_summary else None,
            cost_delta_monthly=cost_summary.delta_monthly if cost_summary else None,
        ),
        findings=[_to_record(finding, mapper) for finding in ordered],
    )


def render_findings_json(report: FindingsReport) -> str:
    """Serialize the report to the canonical on-disk / artefact JSON string."""

    return json.dumps(report.model_dump(mode="json"), indent=2) + "\n"
