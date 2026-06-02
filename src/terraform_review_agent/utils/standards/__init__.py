"""Standard-mapping + gap layer (the moat).

Rule packs (versioned, cited) tie raw findings to standard **controls** and
declare **expected artefacts** whose absence is a finding. The mapper populates
``standard``/``control_id``/``state`` on the emitted report; the gap detector
feeds the StandardsLens.
"""

from __future__ import annotations

from terraform_review_agent.utils.standards.gaps import detect_gaps
from terraform_review_agent.utils.standards.loader import all_packs, load_active_packs
from terraform_review_agent.utils.standards.mapping import (
    StandardMapper,
    StandardMapping,
    build_active_mapper,
)
from terraform_review_agent.utils.standards.pack import (
    Control,
    ExpectedArtifact,
    RuleMapping,
    RulePack,
    gap_rule,
)

__all__ = [
    "Control",
    "ExpectedArtifact",
    "RuleMapping",
    "RulePack",
    "StandardMapper",
    "StandardMapping",
    "all_packs",
    "build_active_mapper",
    "detect_gaps",
    "gap_rule",
    "load_active_packs",
]
