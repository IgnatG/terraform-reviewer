"""Pluggable review lenses + the registry the graph fans out over."""

from __future__ import annotations

from terraform_review_agent.utils.lenses.base import Lens, LensResult
from terraform_review_agent.utils.lenses.registry import (
    ALL_LENSES,
    LENSES_BY_ID,
    enabled_lenses,
)

__all__ = [
    "ALL_LENSES",
    "LENSES_BY_ID",
    "Lens",
    "LensResult",
    "enabled_lenses",
]
