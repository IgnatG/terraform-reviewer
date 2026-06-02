"""Unit tests for the Phase 6 swappable AI backend (BYOK + Copilot).

The contract under test: the AI backend can only *reword* (its return type is
``SpecialistAnnotations`` — message/suggestion, never severity/state/location),
BYOK and Copilot are selectable, and an unconfigured/failed backend degrades to
the deterministic findings rather than blocking the report.
"""

from __future__ import annotations

import subprocess
from typing import Any

import pytest
from pydantic import SecretStr

from terraform_review_agent.ai import (
    CopilotBackend,
    LangChainBackend,
    get_ai_backend,
)
from terraform_review_agent.ai import copilot_backend as copilot_mod
from terraform_review_agent.ai import langchain_backend as lc_mod
from terraform_review_agent.ai.copilot_backend import CopilotError, _extract_json_object
from terraform_review_agent.config import settings
from terraform_review_agent.utils.state import FindingAnnotation, SpecialistAnnotations

# ---------------------------------------------------------------------------
# guardrail (structural)
# ---------------------------------------------------------------------------


def test_backend_return_type_cannot_carry_a_verdict() -> None:
    # The reword-only guardrail is enforced by the type: a backend returns
    # SpecialistAnnotations, whose entries are id/message/suggestion only — there
    # is no field through which the AI could set severity/state/control_id/location.
    assert set(SpecialistAnnotations.model_fields) == {"annotations", "discovered"}
    assert set(FindingAnnotation.model_fields) == {"id", "message", "suggestion"}


# ---------------------------------------------------------------------------
# factory
# ---------------------------------------------------------------------------


def test_factory_selects_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ai_backend", "byok")
    assert isinstance(get_ai_backend(), LangChainBackend)
    monkeypatch.setattr(settings, "ai_backend", "copilot")
    assert isinstance(get_ai_backend(), CopilotBackend)


# ---------------------------------------------------------------------------
# BYOK (LangChain) backend
# ---------------------------------------------------------------------------


def test_byok_available_requires_provider_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "default_llm_provider", "anthropic")
    monkeypatch.setattr(settings, "anthropic_api_key", None)
    assert LangChainBackend().available() is False
    monkeypatch.setattr(settings, "anthropic_api_key", SecretStr("k"))
    assert LangChainBackend().available() is True


def test_byok_azure_also_needs_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "default_llm_provider", "azure")
    monkeypatch.setattr(settings, "azure_api_key", SecretStr("k"))
    monkeypatch.setattr(settings, "azure_openai_endpoint", None)
    assert LangChainBackend().available() is False  # key but no endpoint
    monkeypatch.setattr(settings, "azure_openai_endpoint", "https://r.openai.azure.com")
    assert LangChainBackend().available() is True


def test_azure_get_llm_uses_endpoint_and_deployment_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The azure branch must thread the endpoint/api-version and fall back to the
    # model id when no explicit deployment is configured.
    import langchain_openai

    from terraform_review_agent import llm as llm_mod

    captured: dict[str, Any] = {}

    class _FakeAzure:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(langchain_openai, "AzureChatOpenAI", _FakeAzure)
    monkeypatch.setattr(settings, "azure_api_key", SecretStr("k"))
    monkeypatch.setattr(settings, "azure_openai_endpoint", "https://r.openai.azure.com")
    monkeypatch.setattr(settings, "azure_openai_deployment", None)

    llm_mod.get_llm(provider="azure", model="gpt-4o")

    assert captured["azure_deployment"] == "gpt-4o"  # falls back to the model id
    assert captured["azure_endpoint"] == "https://r.openai.azure.com"


def test_azure_get_llm_requires_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "azure_api_key", SecretStr("k"))
    monkeypatch.setattr(settings, "azure_openai_endpoint", None)
    from terraform_review_agent import llm as llm_mod

    with pytest.raises(RuntimeError, match="AZURE_OPENAI_ENDPOINT"):
        llm_mod.get_llm(provider="azure", model="gpt-4o")


def test_byok_annotate_coerces_dict_output(monkeypatch: pytest.MonkeyPatch) -> None:
    # Some providers return a dict from with_structured_output, not the model.
    class _Structured:
        def invoke(self, _messages: Any) -> dict[str, Any]:
            return {"annotations": [{"id": 0, "message": "m", "suggestion": None}]}

    class _LLM:
        def with_structured_output(self, _schema: Any) -> _Structured:
            return _Structured()

    monkeypatch.setattr(lc_mod, "get_llm", lambda *a, **k: _LLM())
    result = LangChainBackend().annotate("sys", "human")
    assert isinstance(result, SpecialistAnnotations)
    assert result.annotations[0].message == "m"


# ---------------------------------------------------------------------------
# Copilot backend
# ---------------------------------------------------------------------------


def test_extract_json_object_handles_fences_prose_and_strings() -> None:
    # Bare object.
    assert _extract_json_object('{"a": 1}') == '{"a": 1}'
    # Wrapped in prose + a markdown fence.
    wrapped = 'Sure!\n```json\n{"annotations": []}\n```\nthanks'
    assert _extract_json_object(wrapped) == '{"annotations": []}'
    # Braces inside string literals don't end the object early.
    nested = '{"message": "use a { brace } here", "id": 0}'
    assert _extract_json_object(nested) == nested
    # No object at all.
    assert _extract_json_object("no json here") is None


def test_copilot_available_needs_token_and_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "copilot_cli_command", "copilot")
    monkeypatch.setattr(copilot_mod.shutil, "which", lambda _b: "/usr/bin/copilot")
    monkeypatch.setattr(settings, "copilot_github_token", None)
    assert CopilotBackend().available() is False  # CLI present but no token
    monkeypatch.setattr(settings, "copilot_github_token", SecretStr("tok"))
    assert CopilotBackend().available() is True
    monkeypatch.setattr(copilot_mod.shutil, "which", lambda _b: None)
    assert CopilotBackend().available() is False  # token but no CLI


def _fake_cli(stdout: str, returncode: int = 0) -> Any:
    def _run(_cmd: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=_cmd, returncode=returncode, stdout=stdout, stderr=""
        )

    return _run


def test_copilot_annotate_parses_cli_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(copilot_mod.shutil, "which", lambda _b: "/usr/bin/copilot")
    monkeypatch.setattr(settings, "copilot_github_token", SecretStr("tok"))
    monkeypatch.setattr(
        copilot_mod.subprocess,
        "run",
        _fake_cli(
            'Here you go:\n{"annotations": [{"id": 0, "message": "clearer", "suggestion": null}]}'
        ),
    )
    result = CopilotBackend().annotate("sys", "human")
    assert result.annotations[0].id == 0
    assert result.annotations[0].message == "clearer"


def test_copilot_annotate_raises_on_nonzero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(copilot_mod.shutil, "which", lambda _b: "/usr/bin/copilot")
    monkeypatch.setattr(settings, "copilot_github_token", SecretStr("tok"))
    monkeypatch.setattr(copilot_mod.subprocess, "run", _fake_cli("boom", returncode=1))
    with pytest.raises(CopilotError, match="exited 1"):
        CopilotBackend().annotate("sys", "human")


def test_copilot_annotate_raises_on_no_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(copilot_mod.shutil, "which", lambda _b: "/usr/bin/copilot")
    monkeypatch.setattr(settings, "copilot_github_token", SecretStr("tok"))
    monkeypatch.setattr(copilot_mod.subprocess, "run", _fake_cli("no json at all"))
    with pytest.raises(CopilotError, match="no JSON"):
        CopilotBackend().annotate("sys", "human")


def test_copilot_annotate_raises_without_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(copilot_mod.shutil, "which", lambda _b: "/usr/bin/copilot")
    monkeypatch.setattr(settings, "copilot_github_token", None)
    with pytest.raises(CopilotError, match="COPILOT_GITHUB_TOKEN"):
        CopilotBackend().annotate("sys", "human")


def test_copilot_annotate_raises_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    # A hung CLI must surface as CopilotError (which the caller degrades on),
    # never as a raw TimeoutExpired escaping the backend.
    monkeypatch.setattr(copilot_mod.shutil, "which", lambda _b: "/usr/bin/copilot")
    monkeypatch.setattr(settings, "copilot_github_token", SecretStr("tok"))

    def _timeout(*_a: Any, **_kw: Any) -> Any:
        raise subprocess.TimeoutExpired(cmd="copilot", timeout=1)

    monkeypatch.setattr(copilot_mod.subprocess, "run", _timeout)
    with pytest.raises(CopilotError, match="timed out"):
        CopilotBackend().annotate("sys", "human")


def test_copilot_passes_token_via_env_not_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    # Security contract: the token goes in the subprocess env, never on argv.
    monkeypatch.setattr(copilot_mod.shutil, "which", lambda _b: "/usr/bin/copilot")
    monkeypatch.setattr(settings, "copilot_github_token", SecretStr("s3cr3t"))
    captured: dict[str, Any] = {}

    def _run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        captured["cmd"] = cmd
        captured["env"] = kw.get("env")
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout='{"annotations": []}', stderr=""
        )

    monkeypatch.setattr(copilot_mod.subprocess, "run", _run)
    CopilotBackend().annotate("sys", "human")

    assert captured["env"]["COPILOT_GITHUB_TOKEN"] == "s3cr3t"
    assert "s3cr3t" not in " ".join(captured["cmd"])


# ---------------------------------------------------------------------------
# AI on vs AI off — same finding set
# ---------------------------------------------------------------------------


def test_annotations_only_touch_prose() -> None:
    # A populated annotation set still only describes message/suggestion edits,
    # so applying it can never alter the finding set or its severities/locations.
    review = SpecialistAnnotations(
        annotations=[FindingAnnotation(id=0, message="reworded", suggestion="fix")]
    )
    assert review.annotations[0].id == 0
    # No attribute exists to smuggle a severity/state through.
    assert not hasattr(review.annotations[0], "severity")
    assert not hasattr(review.annotations[0], "state")
