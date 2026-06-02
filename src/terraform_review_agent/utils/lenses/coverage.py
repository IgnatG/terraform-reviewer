"""A3 Test Coverage & Gap Analyser lens (deterministic).

Ingests a coverage report (``COVERAGE_REPORT_PATH``, parsed by
``utils/sources/coverage.py``) and flags the PR's changed source files whose
line coverage falls below ``COVERAGE_MIN_PERCENT``, plus a repo-level coverage
score. Inert unless a report is configured.

Deterministic by design: severity ranks the gap (lower coverage → higher
severity), so the "uncovered critical paths" ordering is the engine's, not an
LLM's — no rewording, no verdict the model could move.
"""

from __future__ import annotations

import structlog

from terraform_review_agent.config import settings
from terraform_review_agent.utils.lenses.base import Lens, LensResult
from terraform_review_agent.utils.sources.coverage import FileCoverage, parse_coverage_file
from terraform_review_agent.utils.state import Finding, ReviewState, Severity

log = structlog.get_logger(__name__)


def _severity_for(percent: float, threshold: float) -> Severity:
    """Lower coverage → higher severity (only called for files under the threshold)."""

    if percent < threshold / 2:
        return "high"
    if percent < threshold:
        return "medium"
    return "low"


def _match(changed: set[str], cov_path: str) -> str | None:
    """Return the changed-file path a coverage entry refers to, or None.

    Coverage tools report paths relative to their own root, which may differ
    from the PR's repo-relative paths. An exact match wins; otherwise a suffix
    overlap counts only when it's *unambiguous* — if several changed files could
    be the same basename (``a/util.py`` vs ``b/util.py``), we skip rather than
    guess (and iterate sorted, so the decision is deterministic).
    """

    if cov_path in changed:
        return cov_path
    suffix = sorted(c for c in changed if c.endswith("/" + cov_path) or cov_path.endswith("/" + c))
    return suffix[0] if len(suffix) == 1 else None


def _uncovered_hint(fc: FileCoverage) -> str:
    if not fc.uncovered_lines:
        return "Add tests covering the untested lines in this file."
    preview = ", ".join(str(n) for n in fc.uncovered_lines[:10])
    more = "" if len(fc.uncovered_lines) <= 10 else ", …"
    return f"Cover the untested lines (e.g. {preview}{more})."


class CoverageLens(Lens):
    """Flag under-covered changed files + a repo coverage score (A3)."""

    id = "coverage"

    def applies_to(self, state: ReviewState) -> bool:
        return state.pr.has_terraform_changes and bool(settings.coverage_report_path)

    def run(self, state: ReviewState) -> LensResult:
        report_path = settings.coverage_report_path
        if not report_path:
            return LensResult()
        try:
            report = parse_coverage_file(report_path)
        except (OSError, ValueError) as exc:
            # A malformed/missing report degrades to no coverage findings rather
            # than blocking the review (mirrors the scanner-skip contract).
            log.warning("coverage.report_unreadable", path=report_path, error=str(exc))
            return LensResult()
        if not report.files:
            return LensResult()

        threshold = settings.coverage_min_percent
        changed = state.pr.changed_paths
        findings: list[Finding] = []
        # Stable order: lowest coverage first (the "critical path" ranking).
        for fc in sorted(report.files, key=lambda f: (f.percent, f.path)):
            target = _match(changed, fc.path)
            if target is None or fc.percent >= threshold:
                continue
            findings.append(
                Finding(
                    agent="coverage",
                    lens="A3",
                    severity=_severity_for(fc.percent, threshold),
                    file=target,
                    rule="coverage:under-covered",
                    message=(
                        f"Line coverage {fc.percent:.0f}% "
                        f"({fc.covered_lines}/{fc.total_lines} lines) is below the "
                        f"{threshold:.0f}% threshold."
                    ),
                    suggestion=_uncovered_hint(fc),
                )
            )

        findings.append(
            Finding(
                agent="coverage",
                lens="A3",
                severity="info",
                file=sorted(changed)[0] if changed else ".",
                rule="coverage:score",
                message=(
                    f"Line coverage: {report.percent:.0f}% "
                    f"({report.covered_lines}/{report.total_lines} lines across "
                    f"{len(report.files)} file(s))."
                ),
            )
        )
        return LensResult(findings=findings)
