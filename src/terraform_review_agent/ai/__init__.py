"""Swappable AI backend (Phase 6) — the reword-only layer.

``get_ai_backend()`` returns the backend selected by ``AI_BACKEND`` (``byok``
default · ``copilot``). All backends implement :class:`AIBackend`, whose narrow
``SpecialistAnnotations`` return type is the guardrail: the AI can only reword
prose, never change a verdict.
"""

from __future__ import annotations

from terraform_review_agent.ai.base import AIBackend
from terraform_review_agent.ai.copilot_backend import CopilotBackend, CopilotError
from terraform_review_agent.ai.langchain_backend import LangChainBackend
from terraform_review_agent.config import settings


def get_ai_backend() -> AIBackend:
    """The AI backend for this run, per ``AI_BACKEND`` (``byok`` | ``copilot``)."""

    if settings.ai_backend == "copilot":
        return CopilotBackend()
    return LangChainBackend()


__all__ = [
    "AIBackend",
    "CopilotBackend",
    "CopilotError",
    "LangChainBackend",
    "get_ai_backend",
]
