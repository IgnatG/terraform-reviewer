"""A5 GDS Readiness Scanner lens (deterministic).

Inert unless ``GDS_STANDARD`` names a points definition. Repo-level (checks the
workspace, not the diff), so it skips LLM rewording and the changed-file filter
— each point's verdict and three-state class come straight from the definition.
"""

from __future__ import annotations

from terraform_review_agent.utils.lenses.base import Lens, LensResult
from terraform_review_agent.utils.standardisers import evaluate_gds, load_gds_definition
from terraform_review_agent.utils.state import ReviewState


class GDSLens(Lens):
    """Per-point GDS / TCoP readiness with honest ✅/◐/○ states (A5)."""

    id = "gds"

    def applies_to(self, state: ReviewState) -> bool:
        return state.pr.has_terraform_changes and load_gds_definition() is not None

    def run(self, state: ReviewState) -> LensResult:
        definition = load_gds_definition()
        if definition is None:
            return LensResult()
        return LensResult(findings=evaluate_gds(state.workspace, definition))
