"""A2 CI/CD Standardiser lens — workflow posture audit (deterministic).

Inert unless ``CICD_STANDARD`` names a baseline definition. Repo-level posture
(scans every ``.github/workflows`` file, not just changed ones), so it skips
LLM rewording and the changed-file filter. Gated on terraform changes like the
other repo-level lenses, since a non-terraform PR is skipped entirely.
"""

from __future__ import annotations

from terraform_review_agent.utils.lenses.base import Lens, LensResult
from terraform_review_agent.utils.standardisers import check_workflows, load_cicd_baseline
from terraform_review_agent.utils.state import ReviewState


class CICDLens(Lens):
    """Audit ``.github/workflows`` against the CI/CD baseline (A2)."""

    id = "cicd"

    def applies_to(self, state: ReviewState) -> bool:
        return state.pr.has_terraform_changes and load_cicd_baseline() is not None

    def run(self, state: ReviewState) -> LensResult:
        baseline = load_cicd_baseline()
        if baseline is None:
            return LensResult()
        return LensResult(findings=check_workflows(state.workspace, baseline))
