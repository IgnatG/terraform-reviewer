"""SARIF 2.1.0 → :class:`Finding` normalizer.

SARIF is the common output format for MegaLinter, Prowler, gitleaks, Trivy and
most modern scanners, so one parser covers every SARIF-emitting check source.
The original producing tool (each run's ``tool.driver.name``) and rule id are
preserved as the finding's ``{source}:{rule}`` so the findings-report layer can
recover them.

Severity precedence (highest-confidence signal first):

1. ``security-severity`` property (a CVSS-style 0-10 number) on the result, then
   on its rule — used by code-scanning tools (Trivy, CodeQL, …).
2. the result ``level`` (``error``/``warning``/``note``/``none``).
3. the rule's ``defaultConfiguration.level``.
4. the SARIF default (``warning`` → medium).

Suppressed results (non-empty ``suppressions``) are dropped.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from terraform_review_agent.utils.state import AgentName, Finding, Severity

# SARIF result.level → our severity.
_LEVEL_SEVERITY: dict[str, Severity] = {
    "error": "high",
    "warning": "medium",
    "note": "low",
    "none": "info",
}

# Per the SARIF spec, a result with no level and no rule default is "warning".
_DEFAULT_LEVEL = "warning"

_SLUG_RE = re.compile(r"[^a-z0-9._-]+")


def _source_slug(driver_name: str) -> str:
    """Turn a tool driver name into a colon-free source id (``MegaLinter`` → ``megalinter``)."""

    slug = _SLUG_RE.sub("-", driver_name.strip().lower()).strip("-")
    return slug or "sarif"


def _severity_from_cvss(raw: Any) -> Severity | None:
    """Map a SARIF ``security-severity`` (CVSS 0-10 string) to our vocabulary."""

    try:
        score = float(raw)
    except (TypeError, ValueError):
        return None
    if score >= 9.0:
        return "critical"
    if score >= 7.0:
        return "high"
    if score >= 4.0:
        return "medium"
    if score > 0.0:
        return "low"
    # A 0 (or negative) score is "no CVSS signal" — return None so the caller
    # falls through to the result `level` instead of pinning the finding to info.
    return None


def _uri_to_relpath(uri: str, working_dir: Path) -> str:
    """Normalize a SARIF artifact URI to a workspace-relative POSIX path.

    Handles ``file://`` URIs, percent-encoding, and absolute paths that sit
    inside the workspace (reported as relative).
    """

    if not uri:
        return ""
    if uri.startswith("file:"):
        uri = unquote(urlparse(uri).path)
        # urlparse keeps a leading slash on file:///C:/... (Windows) -> strip it.
        if re.match(r"/[A-Za-z]:", uri):
            uri = uri[1:]
    else:
        uri = unquote(uri)
    p = Path(uri)
    if p.is_absolute():
        try:
            return p.relative_to(working_dir.resolve()).as_posix()
        except ValueError:
            return p.as_posix().lstrip("/")
    return p.as_posix()


def _rule_index(driver: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Index a run's reporting descriptors (rules) by id for metadata lookup."""

    rules: dict[str, dict[str, Any]] = {}
    for rule in driver.get("rules") or []:
        rule_id = rule.get("id")
        if isinstance(rule_id, str):
            rules[rule_id] = rule
    return rules


def _severity_for(result: dict[str, Any], rule: dict[str, Any]) -> Severity:
    """Resolve a result's severity using the documented precedence."""

    props = result.get("properties") or {}
    sev = _severity_from_cvss(props.get("security-severity"))
    if sev is not None:
        return sev

    level = result.get("level")
    if isinstance(level, str) and level in _LEVEL_SEVERITY:
        return _LEVEL_SEVERITY[level]

    rule_props = rule.get("properties") or {}
    sev = _severity_from_cvss(rule_props.get("security-severity"))
    if sev is not None:
        return sev

    rule_level = (rule.get("defaultConfiguration") or {}).get("level")
    if isinstance(rule_level, str) and rule_level in _LEVEL_SEVERITY:
        return _LEVEL_SEVERITY[rule_level]

    return _LEVEL_SEVERITY[_DEFAULT_LEVEL]


def _message(result: dict[str, Any], rule: dict[str, Any], rule_id: str) -> str:
    text = (result.get("message") or {}).get("text")
    if isinstance(text, str) and text.strip():
        return text.strip()
    for key in ("shortDescription", "fullDescription"):
        desc = (rule.get(key) or {}).get("text")
        if isinstance(desc, str) and desc.strip():
            return desc.strip()
    return rule_id


def _location(result: dict[str, Any], working_dir: Path) -> tuple[str, int | None]:
    for loc in result.get("locations") or []:
        physical = loc.get("physicalLocation") or {}
        uri = (physical.get("artifactLocation") or {}).get("uri")
        if not uri:
            continue
        line = (physical.get("region") or {}).get("startLine")
        return _uri_to_relpath(str(uri), working_dir), line if isinstance(line, int) else None
    return "", None


def parse_sarif(
    data: dict[str, Any],
    working_dir: Path | str,
    *,
    category: AgentName = "security",
) -> list[Finding]:
    """Normalize a parsed SARIF log into findings.

    ``category`` is the agent label stamped on each finding (the owning lens
    re-stamps it anyway, but it keeps standalone parses well-formed). The
    producing tool + rule id are preserved in ``Finding.rule`` as
    ``{source}:{ruleId}``.
    """

    cwd = Path(working_dir)
    findings: list[Finding] = []
    for run in data.get("runs") or []:
        driver = ((run.get("tool") or {}).get("driver")) or {}
        source = _source_slug(str(driver.get("name") or "sarif"))
        rules = _rule_index(driver)

        for result in run.get("results") or []:
            # Skip results the tool itself marked as suppressed.
            if result.get("suppressions"):
                continue
            rule_id = result.get("ruleId")
            if not isinstance(rule_id, str) or not rule_id:
                rule_id = "unknown"
            rule = rules.get(rule_id, {})
            file, line = _location(result, cwd)
            help_uri = rule.get("helpUri")
            findings.append(
                Finding(
                    agent=category,
                    severity=_severity_for(result, rule),
                    file=file,
                    line=line,
                    rule=f"{source}:{rule_id}",
                    message=_message(result, rule, rule_id),
                    suggestion=help_uri if isinstance(help_uri, str) and help_uri else None,
                )
            )
    return findings


def parse_sarif_file(
    path: str | Path,
    working_dir: Path | str,
    *,
    category: AgentName = "security",
) -> list[Finding]:
    """Read and parse a SARIF report file. Raises on missing file / invalid JSON."""

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return parse_sarif(data, working_dir, category=category)
