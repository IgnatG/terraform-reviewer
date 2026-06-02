"""BYOK AI backend — any configured LangChain provider (the default).

Wraps :func:`terraform_review_agent.llm.get_llm` with structured output so the
model is forced to return :class:`SpecialistAnnotations`. Works with OpenAI,
Anthropic, Gemini, and Azure OpenAI, selected by ``DEFAULT_LLM_PROVIDER``.
"""

from __future__ import annotations

from langchain_core.messages import HumanMessage, SystemMessage

from terraform_review_agent.ai.base import AIBackend
from terraform_review_agent.config import settings
from terraform_review_agent.llm import get_llm
from terraform_review_agent.utils.state import SpecialistAnnotations


class LangChainBackend(AIBackend):
    """Bring-your-own-key backend over the LangChain provider factory."""

    def available(self) -> bool:
        # Needs an API key for the chosen provider; Azure additionally needs an
        # endpoint (the deployment falls back to the model id).
        if settings.provider_key() is None:
            return False
        azure_missing_endpoint = (
            settings.default_llm_provider == "azure" and not settings.azure_openai_endpoint
        )
        return not azure_missing_endpoint

    def annotate(self, system: str, human: str) -> SpecialistAnnotations:
        structured = get_llm().with_structured_output(SpecialistAnnotations)
        result = structured.invoke([SystemMessage(content=system), HumanMessage(content=human)])
        # Some providers return a dict rather than the model instance.
        return (
            result
            if isinstance(result, SpecialistAnnotations)
            else SpecialistAnnotations.model_validate(result)
        )
