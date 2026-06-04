"""Unit tests for :mod:`terraform_review_agent.config` (Settings)."""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from terraform_review_agent.config import Settings


def _settings(**kw: object) -> Settings:
    # Build a fresh Settings without reading the developer's real .env / environment.
    return Settings(_env_file=None, **kw)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# provider_key dispatch
# ---------------------------------------------------------------------------


def test_provider_key_returns_each_providers_key() -> None:
    s = _settings(
        openai_api_key=SecretStr("o"),
        anthropic_api_key=SecretStr("a"),
        google_api_key=SecretStr("g"),
        azure_api_key=SecretStr("z"),
    )
    assert s.provider_key("openai").get_secret_value() == "o"  # type: ignore[union-attr]
    assert s.provider_key("anthropic").get_secret_value() == "a"  # type: ignore[union-attr]
    assert s.provider_key("google").get_secret_value() == "g"  # type: ignore[union-attr]
    assert s.provider_key("azure").get_secret_value() == "z"  # type: ignore[union-attr]


def test_provider_key_defaults_to_configured_provider() -> None:
    s = _settings(default_llm_provider="google", google_api_key=SecretStr("g"))
    # No explicit provider -> uses default_llm_provider.
    assert s.provider_key().get_secret_value() == "g"  # type: ignore[union-attr]


def test_provider_key_is_none_when_unset() -> None:
    assert _settings(default_llm_provider="openai", openai_api_key=None).provider_key() is None


def test_provider_key_rejects_unknown_provider() -> None:
    with pytest.raises(ValueError, match="Unsupported LLM provider"):
        _settings().provider_key("bedrock")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# default_llm_seed sentinel validator
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sentinel", ["none", "off", "disabled", "NONE", " Off "])
def test_seed_sentinel_disables_seed(sentinel: str) -> None:
    assert _settings(default_llm_seed=sentinel).default_llm_seed is None


def test_seed_accepts_a_real_int() -> None:
    assert _settings(default_llm_seed=11).default_llm_seed == 11
    # A numeric string still coerces to int (only the sentinels map to None).
    assert _settings(default_llm_seed="3").default_llm_seed == 3


def test_seed_default_is_seven() -> None:
    assert _settings().default_llm_seed == 7
