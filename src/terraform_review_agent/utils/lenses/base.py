"""The `Lens` interface — one pluggable check that produces findings.

A lens replaces the old hard-coded specialist nodes (security / cost / style).
The registry discovers lenses and the graph fans out one parallel task per
*enabled* lens, so adding a new check (A1-A4 …) is a new `Lens` subclass plus a
registry entry — no graph surgery.

Contract:

- ``id`` — stable identifier, also the :data:`~terraform_review_agent.utils.state.AgentName`
  stamped onto every finding the lens emits.
- ``applies_to(state)`` — cheap predicate deciding whether this lens has anything
  to do for the PR (e.g. cost needs an infracost key + terraform changes). A lens
  that doesn't apply is simply not scheduled.
- ``run(state)`` — do the work and return a :class:`LensResult`. Lenses must be
  side-effect-free w.r.t. shared state: they read ``state`` and return findings,
  never mutate it, so parallel execution stays deterministic.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from pydantic import BaseModel, Field

from terraform_review_agent.utils.state import AgentName, CostSummary, Finding, ReviewState


class LensResult(BaseModel):
    """What a lens returns: its findings plus any lens-specific extras.

    ``cost_summary`` is only set by the cost lens (the sole writer of the headline
    cost numbers); every other lens leaves it ``None``.
    """

    findings: list[Finding] = Field(default_factory=list)
    cost_summary: CostSummary | None = None
    #: AI-backend failures hit while annotating this lens (empty on success / no AI).
    ai_errors: list[str] = Field(default_factory=list)


class Lens(ABC):
    """Base class for a single review lens."""

    #: Stable lens identifier; also the agent label on its findings.
    id: AgentName

    @abstractmethod
    def applies_to(self, state: ReviewState) -> bool:
        """Whether this lens should run for ``state`` (cheap, no scanning)."""

    @abstractmethod
    def run(self, state: ReviewState) -> LensResult:
        """Run the lens over the PR workspace and return its findings."""
