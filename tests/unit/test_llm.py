"""Unit tests for the LLM provider factory (:mod:`terraform_review_agent.llm`).

The provider-specific LangChain clients are faked (capturing their kwargs) so no
network or real model is constructed. The contract under test: each provider
branch threads the right key/model, a missing key raises, an unknown provider
raises, and OpenAI/Azure *reasoning* models omit the temperature/seed knobs that
would 400.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import SecretStr

from terraform_review_agent.config import settings
from terraform_review_agent.llm import _is_openai_reasoning_model, get_llm


def _fake_client(monkeypatch: pytest.MonkeyPatch, module_attr: str) -> dict[str, Any]:
    """Patch ``langchain_*.<attr>`` with a kwargs-capturing fake; return the capture."""

    captured: dict[str, Any] = {}

    class _Fake:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    if module_attr in {"ChatOpenAI", "AzureChatOpenAI"}:
        import langchain_openai

        monkeypatch.setattr(langchain_openai, module_attr, _Fake)
    elif module_attr == "ChatAnthropic":
        import langchain_anthropic

        monkeypatch.setattr(langchain_anthropic, module_attr, _Fake)
    elif module_attr == "ChatGoogleGenerativeAI":
        import langchain_google_genai

        monkeypatch.setattr(langchain_google_genai, module_attr, _Fake)
    return captured


# ---------------------------------------------------------------------------
# reasoning-model detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("model", ["gpt-5", "gpt-5.4-mini", "o1", "o3-mini", "o4-mini", "O3"])
def test_reasoning_models_detected(model: str) -> None:
    assert _is_openai_reasoning_model(model) is True


@pytest.mark.parametrize("model", ["gpt-4.1", "gpt-4o", "gpt-4o-mini", "claude-sonnet-4-6"])
def test_non_reasoning_models_not_detected(model: str) -> None:
    assert _is_openai_reasoning_model(model) is False


# ---------------------------------------------------------------------------
# openai branch
# ---------------------------------------------------------------------------


def test_openai_reasoning_model_omits_temperature_and_seed(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _fake_client(monkeypatch, "ChatOpenAI")
    monkeypatch.setattr(settings, "openai_api_key", SecretStr("k"))
    get_llm(provider="openai", model="gpt-5")
    assert captured["model"] == "gpt-5"
    # Reasoning models 400 on these — they must not be sent.
    assert "temperature" not in captured
    assert "seed" not in captured


def test_openai_non_reasoning_model_sets_temperature_and_seed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _fake_client(monkeypatch, "ChatOpenAI")
    monkeypatch.setattr(settings, "openai_api_key", SecretStr("k"))
    get_llm(provider="openai", model="gpt-4.1", temperature=0.0, seed=7)
    assert captured["temperature"] == 0.0
    assert captured["seed"] == 7


# ---------------------------------------------------------------------------
# azure branch
# ---------------------------------------------------------------------------


def test_azure_reasoning_model_omits_temperature_and_seed(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _fake_client(monkeypatch, "AzureChatOpenAI")
    monkeypatch.setattr(settings, "azure_api_key", SecretStr("k"))
    monkeypatch.setattr(settings, "azure_openai_endpoint", "https://r.openai.azure.com")
    monkeypatch.setattr(settings, "azure_openai_deployment", "o3-deploy")
    get_llm(provider="azure", model="o3-mini")
    assert "temperature" not in captured
    assert "seed" not in captured
    assert captured["azure_deployment"] == "o3-deploy"


# ---------------------------------------------------------------------------
# anthropic / google branches
# ---------------------------------------------------------------------------


def test_anthropic_branch_threads_key_and_model(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _fake_client(monkeypatch, "ChatAnthropic")
    monkeypatch.setattr(settings, "anthropic_api_key", SecretStr("k"))
    get_llm(provider="anthropic", model="claude-sonnet-4-6")
    assert captured["model"] == "claude-sonnet-4-6"
    # Anthropic has no seed knob; it must not be forwarded.
    assert "seed" not in captured


def test_google_branch_threads_key_and_model(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _fake_client(monkeypatch, "ChatGoogleGenerativeAI")
    monkeypatch.setattr(settings, "google_api_key", SecretStr("k"))
    get_llm(provider="google", model="gemini-2.5-pro")
    assert captured["model"] == "gemini-2.5-pro"
    assert "seed" not in captured


# ---------------------------------------------------------------------------
# error paths
# ---------------------------------------------------------------------------


def test_missing_provider_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "anthropic_api_key", None)
    with pytest.raises(RuntimeError, match="Missing API key"):
        get_llm(provider="anthropic")


def test_unknown_provider_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    # provider_key already rejects unknown providers before the branch dispatch.
    with pytest.raises(ValueError, match="Unsupported LLM provider"):
        get_llm(provider="bedrock")  # type: ignore[arg-type]
