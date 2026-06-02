"""The AI-backend interface — a swappable, reword-only layer (Phase 6).

A backend's *only* job is to reword scanner findings. Given the specialist
system + human prompt, it returns a
:class:`~terraform_review_agent.utils.state.SpecialistAnnotations`: per-finding
``message``/``suggestion`` rewrites (keyed by the id the prompt assigned) plus
optional, separately-gated discoveries.

The backend never receives or returns a finding's ``severity`` / ``state`` /
``control_id`` / ``location`` — those stay owned by the scanners and the
standard-mapping layer. So by construction the AI cannot change a verdict; it
can only improve prose (the §2.5 guardrail, enforced by this narrow return type
rather than by trusting the model).

Two implementations: :class:`~terraform_review_agent.ai.langchain_backend.LangChainBackend`
(BYOK — OpenAI/Anthropic/Gemini/Azure) and
:class:`~terraform_review_agent.ai.copilot_backend.CopilotBackend` (the bundled
GitHub Copilot CLI).
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from terraform_review_agent.utils.state import SpecialistAnnotations


class AIBackend(ABC):
    """A reword-only AI layer behind a stable interface."""

    @abstractmethod
    def available(self) -> bool:
        """Whether this backend is configured enough to run (key/CLI present).

        When false, the caller skips rewording and emits the deterministic
        scanner findings as-is — the "AI off" path, which yields an identical
        finding *set* to "AI on" (the backend only ever rewrites prose).
        """

    @abstractmethod
    def annotate(self, system: str, human: str) -> SpecialistAnnotations:
        """Reword the findings described by the prompt; raise on any failure.

        Failures propagate so the caller can degrade gracefully (§9.2) — an AI
        error must never block the deterministic report.
        """
