"""Standards lens — absence/gap detection against the active rule packs.

Unlike the scanner-backed lenses, this one flags *missing* expected artefacts
(README, licence, …) rather than misconfigured present ones. It's repo-level
(not scoped to the PR diff) and runs only when a rule pack with expected
artefacts is active, so it's inert by default.
"""

from __future__ import annotations

from terraform_review_agent.utils.lenses.base import Lens, LensResult
from terraform_review_agent.utils.standards import detect_gaps, load_active_packs
from terraform_review_agent.utils.state import ReviewState


class StandardsLens(Lens):
    """Emit human_only findings for expected artefacts the repo is missing."""

    id = "standards"

    def applies_to(self, state: ReviewState) -> bool:
        # Gate on terraform changes like the other lenses: a doc-only PR is
        # `skipped` (no comment/gating), so emitting gaps then would only land
        # them in the artefact invisibly.
        return state.pr.has_terraform_changes and any(
            pack.expected_artifacts for pack in load_active_packs()
        )

    def run(self, state: ReviewState) -> LensResult:
        # Gap findings are deterministic + curated by the rule pack, so they
        # skip LLM rewording and the changed-file filter (absence has no diff).
        return LensResult(findings=detect_gaps(state.workspace, load_active_packs()))
