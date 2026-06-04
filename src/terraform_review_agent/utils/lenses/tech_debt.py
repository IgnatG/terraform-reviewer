"""A4 Tech-Debt Scorecard lens (deterministic).

Ingests tech-debt signals — jscpd code duplication (``JSCPD_REPORT_PATH``) and
SonarQube issues exported as SARIF (``SONARQUBE_SARIF_PATH``) — and emits a
per-issue finding (scoped to the PR's changed files) plus a repo-level
scorecard summarising the signals. Inert unless a signal is configured.

dep-age (dependency staleness) is a planned third signal; it needs a
package-ecosystem-specific tool, so it's deferred (the score notes only the
signals present). The historical *trend* needs the dashboard (Phase 9).
"""

from __future__ import annotations

import structlog

from terraform_review_agent.config import settings
from terraform_review_agent.utils.lenses._annotate import filter_to_changed
from terraform_review_agent.utils.lenses.base import Lens, LensResult
from terraform_review_agent.utils.sources.jscpd import parse_jscpd_file
from terraform_review_agent.utils.sources.sarif import parse_sarif_file
from terraform_review_agent.utils.state import Finding, ReviewState

log = structlog.get_logger(__name__)


def _duplication_findings(state: ReviewState) -> tuple[list[Finding], float | None, bool]:
    """jscpd clones + the overall duplication %, plus whether the signal ran.

    The third element distinguishes "ran and found 0% / failed to parse" so the
    scorecard reflects which signals actually ran, not just which found something.
    """

    path = settings.jscpd_report_path
    if not path:
        return [], None, False
    try:
        report = parse_jscpd_file(path)
    except (OSError, ValueError) as exc:
        log.warning("tech_debt.jscpd_unreadable", path=path, error=str(exc))
        return [], None, False
    findings = [
        Finding(
            agent="tech-debt",
            lens="A4",
            severity="low",
            file=clone.file,
            line=clone.start_line,
            # Fold the *other* location into the rule so several clones of the
            # same block (same file+start) don't collapse under the
            # (file, rule, line) dedupe key.
            rule=f"jscpd:duplication:{clone.other_file}:{clone.other_start_line or 0}",
            message=(
                f"Duplicated block of {clone.lines} lines, also at "
                f"`{clone.other_file}`"
                + (f":{clone.other_start_line}" if clone.other_start_line else "")
                + "."
            ),
            suggestion="Extract the shared logic into a single reusable unit.",
        )
        for clone in report.clones
    ]
    return findings, report.duplication_percent, True


def _sonar_findings(state: ReviewState) -> tuple[list[Finding], bool]:
    """SonarQube SARIF issues (re-stamped A4) + whether the signal ran."""

    path = settings.sonarqube_sarif_path
    if not path:
        return [], False
    try:
        raw = parse_sarif_file(path, state.workspace, category="tech-debt")
    except (OSError, ValueError) as exc:
        log.warning("tech_debt.sonar_unreadable", path=path, error=str(exc))
        return [], False
    return [f.model_copy(update={"lens": "A4"}) for f in raw], True


class TechDebtLens(Lens):
    """Duplication + Sonar issues → per-issue findings + a debt scorecard (A4)."""

    id = "tech-debt"

    def applies_to(self, state: ReviewState) -> bool:
        return state.pr.has_terraform_changes and bool(
            settings.jscpd_report_path or settings.sonarqube_sarif_path
        )

    def run(self, state: ReviewState) -> LensResult:
        changed = state.pr.changed_paths
        dup_findings, dup_percent, dup_ran = _duplication_findings(state)
        sonar, sonar_ran = _sonar_findings(state)
        # No signal actually ran (unset or unreadable) → nothing to report.
        if not (dup_ran or sonar_ran):
            return LensResult()
        # Reports cover the whole repo; scope per-issue findings to the PR's diff.
        scoped = filter_to_changed([*dup_findings, *sonar], changed)

        parts: list[str] = []
        if dup_ran:
            parts.append(f"{dup_percent or 0.0:.1f}% code duplication")
        if sonar_ran:
            parts.append(f"{len(sonar)} Sonar issue(s)")
        scoped.append(
            Finding(
                agent="tech-debt",
                lens="A4",
                # Repo-level scorecard: anchored at the repo root (".") rather than
                # an arbitrary changed file, so it isn't posted inline on an
                # unrelated file's line.
                severity="info",
                file=".",
                rule="tech-debt:score",
                message="Tech-debt scorecard: " + ", ".join(parts) + ".",
            )
        )
        return LensResult(findings=scoped)
