"""Shared scanner-running + LLM-rewording helpers used by the lenses.

These are the deterministic plumbing every scanner-backed lens needs:

- :func:`collect` runs a lens's ``@tool`` scanners over the workspace, skipping a
  missing binary rather than failing the whole review.
- :func:`filter_to_changed` scopes repo-wide scanner output down to the files the
  PR actually touched.
- :func:`annotate_with_llm` hands the scanner findings (the canonical set) to the
  AI backend (:mod:`terraform_review_agent.ai`), which may only *reword*
  ``message``/``suggestion`` — severity/file/line/rule and the finding set itself
  are owned by the scanners, so they're identical across runs. Speculative
  LLM-discovered findings are opt-in via ``settings.enable_llm_findings`` (and
  never for cost). A backend that's unconfigured or that fails degrades to the
  un-reworded findings, so AI never blocks the deterministic report (§9.2).
"""

from __future__ import annotations

from typing import Any

import structlog

from terraform_review_agent.ai import get_ai_backend
from terraform_review_agent.config import settings
from terraform_review_agent.utils import prompts
from terraform_review_agent.utils.state import (
    AgentName,
    Finding,
)
from terraform_review_agent.utils.tools import FilePayload, ScannerError

log = structlog.get_logger(__name__)


def collect(scanners: list[tuple[str, Any]], working_dir: str) -> list[Finding]:
    """Run each ``(name, tool)`` against ``working_dir``, skipping missing binaries."""

    findings: list[Finding] = []
    for name, scanner in scanners:
        try:
            findings.extend(scanner.invoke({"working_dir": working_dir}))
        except ScannerError as exc:
            log.warning("scanner.skipped", scanner=name, error=str(exc))
    return findings


def filter_to_changed(findings: list[Finding], changed_paths: set[str]) -> list[Finding]:
    """Scope repo-wide scanner output to the PR's changed files — unless full scan.

    Scanners run over the whole workspace. In ``diff`` mode, findings in unchanged
    files (and findings with no resolvable path) are dropped deterministically so
    the review reflects only what the PR touched. In ``full`` mode (the default
    posture scan) every finding is kept.
    """

    if settings.scan_mode == "full":
        return list(findings)
    return [f for f in findings if f.file in changed_paths]


def _prefer_refined(refined: str | None, original: str | None) -> str | None:
    """Use the LLM's text only when it's non-blank; otherwise keep the scanner's.

    The annotation step is wording-only: a blank/whitespace ``message`` or
    ``suggestion`` from the model means "nothing to add", not "erase the
    scanner's remediation". Only a real, non-empty string overrides the
    deterministic scanner text.
    """

    if refined is not None and refined.strip():
        return refined
    return original


def _namespaced_llm_rule(agent: AgentName, rule: str) -> str:
    """Force a discovered finding's rule into the ``{agent}:llm-`` namespace.

    The prompt asks for this prefix, but the model isn't bound to it. Enforcing
    it in code stops a hallucinated finding from masquerading as scanner output
    (e.g. ``tfsec:...``) or colliding with a real scanner finding's
    ``(file, rule, line)`` dedupe key.
    """

    prefix = f"{agent}:llm-"
    if rule.startswith(prefix):
        return rule
    slug = rule.split(":")[-1].removeprefix("llm-").strip() or "finding"
    return f"{prefix}{slug}"


def annotate_with_llm(
    agent: AgentName,
    raw_findings: list[Finding],
    payloads: list[FilePayload],
) -> list[Finding]:
    """Reword scanner findings with the LLM, keeping the finding set deterministic.

    The scanner findings are canonical: their severity/file/line/rule are
    preserved verbatim and every one is returned. The LLM may only rewrite
    ``message``/``suggestion`` (matched back by the ``id`` we assign here), so
    the *set* of findings is identical run-to-run — only the wording varies.
    Speculative LLM-discovered findings are appended only when
    ``settings.enable_llm_findings`` is set (and never for cost).
    """

    canonical = [f.model_copy(update={"agent": agent}) for f in raw_findings]
    allow_discovery = settings.enable_llm_findings and agent != "cost"
    # Nothing for the AI to do: no findings to reword and discovery is off (or
    # on but with no file content to discover from).
    if not canonical and (not allow_discovery or not payloads):
        return canonical

    backend = get_ai_backend()
    # AI off (no key / no Copilot CLI): emit the deterministic findings as-is.
    # Same finding *set* as AI on — only the wording would have differed.
    if not backend.available():
        return canonical

    system = prompts.specialist_system_prompt(agent, allow_discovery)
    human = prompts.build_specialist_input(canonical, payloads)
    try:
        review = backend.annotate(system, human)
    except Exception as exc:
        # Graceful degradation (§9.2): an AI failure (network, CLI, parse, …)
        # never blocks the report — fall back to the un-reworded scanner findings.
        log.warning("ai.annotate_failed", agent=agent, error=str(exc))
        return canonical

    by_id = {a.id: a for a in review.annotations}
    findings: list[Finding] = []
    for idx, finding in enumerate(canonical):
        annotation = by_id.get(idx)
        if annotation is None:
            findings.append(finding)
            continue
        findings.append(
            finding.model_copy(
                update={
                    "message": _prefer_refined(annotation.message, finding.message),
                    "suggestion": _prefer_refined(annotation.suggestion, finding.suggestion),
                }
            )
        )

    if allow_discovery:
        findings.extend(
            Finding(
                agent=agent,
                severity=item.severity,
                file=item.file,
                line=item.line,
                rule=_namespaced_llm_rule(agent, item.rule),
                message=item.message,
                suggestion=item.suggestion,
            )
            for item in review.discovered
        )
    return findings
