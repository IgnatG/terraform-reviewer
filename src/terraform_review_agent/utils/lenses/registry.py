"""Lens registry — the single place that knows which lenses exist and run.

``ALL_LENSES`` is the catalogue; :func:`enabled_lenses` is what the graph fans
out over for a given PR. A lens runs only when it is both *selected* (in
``settings.enabled_lenses``, or all of them when that's empty) and *applicable*
(``lens.applies_to(state)`` — e.g. cost needs an infracost key). Adding a lens is
a one-line append here plus a new :class:`~...lenses.base.Lens` subclass.
"""

from __future__ import annotations

from terraform_review_agent.config import settings
from terraform_review_agent.utils.lenses.base import Lens
from terraform_review_agent.utils.lenses.cicd import CICDLens
from terraform_review_agent.utils.lenses.cost import CostLens
from terraform_review_agent.utils.lenses.coverage import CoverageLens
from terraform_review_agent.utils.lenses.gds import GDSLens
from terraform_review_agent.utils.lenses.security import SecurityLens
from terraform_review_agent.utils.lenses.standards import StandardsLens
from terraform_review_agent.utils.lenses.style import StyleLens
from terraform_review_agent.utils.lenses.tech_debt import TechDebtLens
from terraform_review_agent.utils.lenses.terraform_standard import TerraformStandardLens
from terraform_review_agent.utils.state import ReviewState

# Registration order is the catalogue order; execution order doesn't matter
# (findings merge through a reducer and are re-sorted before rendering).
ALL_LENSES: tuple[Lens, ...] = (
    SecurityLens(),
    CostLens(),
    StyleLens(),
    StandardsLens(),
    TerraformStandardLens(),
    CICDLens(),
    CoverageLens(),
    TechDebtLens(),
    GDSLens(),
)

LENSES_BY_ID: dict[str, Lens] = {lens.id: lens for lens in ALL_LENSES}


def _selected_ids() -> set[str] | None:
    """Parse ``settings.enabled_lenses`` (CSV) → id set, or ``None`` for all."""

    raw = settings.enabled_lenses.strip()
    if not raw:
        return None
    return {part.strip() for part in raw.split(",") if part.strip()}


def enabled_lenses(state: ReviewState) -> list[Lens]:
    """The lenses to schedule for ``state`` — selected by config *and* applicable."""

    selected = _selected_ids()
    return [
        lens
        for lens in ALL_LENSES
        if (selected is None or lens.id in selected) and lens.applies_to(state)
    ]
