"""LLM provider factory.

Single entry point :func:`get_llm` returns a configured chat model for one of
the three supported providers (``openai`` / ``anthropic`` / ``google``).
Defaults are drawn from :data:`config.settings`; individual call sites may
override provider, model, and temperature.
"""

from __future__ import annotations

from langchain_core.language_models import BaseChatModel

from terraform_review_agent.config import LLMProvider, settings


def get_llm(
    provider: LLMProvider | None = None,
    model: str | None = None,
    temperature: float | None = None,
    seed: int | None = None,
) -> BaseChatModel:
    """Return a chat model for ``provider`` configured with ``model``/``temperature``.

    Lazy-imports the provider-specific LangChain integration so a missing
    package only matters when that provider is actually requested. Raises
    :class:`RuntimeError` when the corresponding API key is unset and
    :class:`ValueError` for an unknown provider.

    ``seed`` (default :data:`settings.default_llm_seed`) is forwarded to OpenAI
    for best-effort reproducible sampling; Anthropic and Google have no
    equivalent knob, so it is ignored there.
    """

    chosen_provider: LLMProvider = provider or settings.default_llm_provider
    chosen_model = model or settings.default_llm_model
    chosen_temperature = (
        temperature if temperature is not None else settings.default_llm_temperature
    )
    chosen_seed = seed if seed is not None else settings.default_llm_seed

    api_key = settings.provider_key(chosen_provider)
    if api_key is None:
        raise RuntimeError(
            f"Missing API key for LLM provider {chosen_provider!r}. "
            f"Set the corresponding *_API_KEY environment variable."
        )

    if chosen_provider == "openai":
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=chosen_model,
            temperature=chosen_temperature,
            api_key=api_key,
            seed=chosen_seed,
        )

    if chosen_provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(
            model=chosen_model,
            temperature=chosen_temperature,
            api_key=api_key,
            timeout=None,
            stop=None,
        )

    if chosen_provider == "google":
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(
            model=chosen_model,
            temperature=chosen_temperature,
            google_api_key=api_key,
        )

    if chosen_provider == "azure":
        from langchain_openai import AzureChatOpenAI

        if not settings.azure_openai_endpoint:
            raise RuntimeError(
                "Azure provider selected but AZURE_OPENAI_ENDPOINT is unset. "
                "Set the endpoint (and AZURE_OPENAI_DEPLOYMENT) to use Azure OpenAI."
            )
        # Azure routes by *deployment* name, not model; fall back to the model id
        # when no explicit deployment is configured.
        return AzureChatOpenAI(
            azure_deployment=settings.azure_openai_deployment or chosen_model,
            azure_endpoint=settings.azure_openai_endpoint,
            api_version=settings.azure_openai_api_version,
            api_key=api_key,
            temperature=chosen_temperature,
            seed=chosen_seed,
        )

    raise ValueError(f"Unsupported LLM provider: {chosen_provider!r}")
