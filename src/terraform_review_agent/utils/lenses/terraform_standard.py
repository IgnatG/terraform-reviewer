"""A1 Terraform Standardiser lens — golden module-structure diff (deterministic).

Inert unless ``TERRAFORM_STANDARD`` names a house-standard definition. Like the
standards lens it's repo-level (checks the touched modules' structure, not the
diff), so it skips LLM rewording and the changed-file filter.
"""

from __future__ import annotations

from terraform_review_agent.utils.lenses.base import Lens, LensResult
from terraform_review_agent.utils.standardisers import check_modules, load_terraform_standard
from terraform_review_agent.utils.state import ReviewState


class TerraformStandardLens(Lens):
    """Diff each touched Terraform module against the house standard (A1)."""

    id = "terraform-standard"

    def applies_to(self, state: ReviewState) -> bool:
        return state.pr.has_terraform_changes and load_terraform_standard() is not None

    def run(self, state: ReviewState) -> LensResult:
        std = load_terraform_standard()
        if std is None:
            return LensResult()
        return LensResult(
            findings=check_modules(state.workspace, state.pr.changed_terraform_paths, std)
        )
