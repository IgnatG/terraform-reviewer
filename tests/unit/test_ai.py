"""Unit tests for the Phase 6 swappable AI backend (BYOK + Copilot).

The contract under test: the AI backend can only *reword* (its return type is
``SpecialistAnnotations`` — message/suggestion, never severity/state/location),
BYOK and Copilot are selectable, and an unconfigured/failed backend degrades to
the deterministic findings rather than blocking the report.
"""

from __future__ import annotations

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


def test_copilot_available_needs_token_cli_and_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "copilot_cli_command", "copilot")
    monkeypatch.setattr(copilot_mod.shutil, "which", lambda _b: "/usr/bin/copilot")
    monkeypatch.setattr(copilot_mod.importlib.util, "find_spec", lambda _n: object())
    monkeypatch.setattr(settings, "copilot_github_token", None)
    assert CopilotBackend().available() is False  # CLI + SDK present but no token
    monkeypatch.setattr(settings, "copilot_github_token", SecretStr("tok"))
    assert CopilotBackend().available() is True
    monkeypatch.setattr(copilot_mod.shutil, "which", lambda _b: None)
    assert CopilotBackend().available() is False  # token + SDK but no CLI
    monkeypatch.setattr(copilot_mod.shutil, "which", lambda _b: "/usr/bin/copilot")
    monkeypatch.setattr(copilot_mod.importlib.util, "find_spec", lambda _n: None)
    assert CopilotBackend().available() is False  # token + CLI but SDK not installed


# --- Fake Copilot SDK ------------------------------------------------------
# The real SDK drives the Copilot CLI over stdio; these fakes stand in for its
# async client/session/event surface so the backend's wiring + parsing are
# testable without the CLI (live behaviour is a HUMAN-TODO verification step).


class _FakeAssistantMessage:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeIdle:
    pass


class _FakeEvent:
    def __init__(self, data: Any) -> None:
        self.data = data


class _FakePermissionHandler:
    approve_all = "approve-all-sentinel"


class _FakeRuntimeConnection:
    @staticmethod
    def for_stdio(path: Any = None, args: Any = None) -> str:
        return f"stdio:{path}"


def _install_fake_sdk(
    monkeypatch: pytest.MonkeyPatch, *, response_events: list[Any]
) -> dict[str, Any]:
    """Patch ``_load_sdk`` to a fake; return a dict recording what the backend sent."""

    record: dict[str, Any] = {}

    class FakeSession:
        def __init__(self) -> None:
            self._cb: Any = None

        def on(self, cb: Any) -> None:
            self._cb = cb

        async def send(self, prompt: str) -> None:
            record["prompt"] = prompt
            for data in response_events:
                self._cb(_FakeEvent(data))

        async def __aenter__(self) -> FakeSession:
            return self

        async def __aexit__(self, *_a: Any) -> bool:
            return False

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            record["client_kwargs"] = kwargs

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *_a: Any) -> bool:
            return False

        async def create_session(self, **kwargs: Any) -> FakeSession:
            record["session_kwargs"] = kwargs
            return FakeSession()

    sdk = copilot_mod._Sdk(
        CopilotClient=FakeClient,
        RuntimeConnection=_FakeRuntimeConnection,
        PermissionHandler=_FakePermissionHandler,
        AssistantMessageData=_FakeAssistantMessage,
        SessionIdleData=_FakeIdle,
    )
    monkeypatch.setattr(copilot_mod, "_load_sdk", lambda: sdk)
    return record


def test_copilot_annotate_parses_sdk_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(copilot_mod.shutil, "which", lambda _b: "/usr/bin/copilot")
    monkeypatch.setattr(settings, "copilot_github_token", SecretStr("tok"))
    # Two assistant chunks that only form valid JSON once concatenated, then idle.
    _install_fake_sdk(
        monkeypatch,
        response_events=[
            _FakeAssistantMessage('Here you go:\n{"annotations": [{"id": 0, '),
            _FakeAssistantMessage('"message": "clearer", "suggestion": null}]}'),
            _FakeIdle(),
        ],
    )
    result = CopilotBackend().annotate("sys", "human")
    assert result.annotations[0].id == 0
    assert result.annotations[0].message == "clearer"


def test_copilot_annotate_raises_on_no_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(copilot_mod.shutil, "which", lambda _b: "/usr/bin/copilot")
    monkeypatch.setattr(settings, "copilot_github_token", SecretStr("tok"))
    _install_fake_sdk(
        monkeypatch, response_events=[_FakeAssistantMessage("no json at all"), _FakeIdle()]
    )
    with pytest.raises(CopilotError, match="no JSON"):
        CopilotBackend().annotate("sys", "human")


def test_copilot_annotate_raises_without_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(copilot_mod.shutil, "which", lambda _b: "/usr/bin/copilot")
    monkeypatch.setattr(settings, "copilot_github_token", None)
    _install_fake_sdk(monkeypatch, response_events=[_FakeIdle()])
    with pytest.raises(CopilotError, match="COPILOT_GITHUB_TOKEN"):
        CopilotBackend().annotate("sys", "human")


def test_copilot_annotate_raises_when_sdk_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    # The optional package isn't installed → degrade, never crash the report.
    monkeypatch.setattr(copilot_mod.shutil, "which", lambda _b: "/usr/bin/copilot")
    monkeypatch.setattr(settings, "copilot_github_token", SecretStr("tok"))

    def _missing() -> Any:
        raise CopilotError("github-copilot-sdk is not installed")

    monkeypatch.setattr(copilot_mod, "_load_sdk", _missing)
    with pytest.raises(CopilotError, match="not installed"):
        CopilotBackend().annotate("sys", "human")


def test_copilot_annotate_raises_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    # A hung session must surface as CopilotError (which the caller degrades on),
    # never as a raw TimeoutError escaping the backend.
    monkeypatch.setattr(copilot_mod.shutil, "which", lambda _b: "/usr/bin/copilot")
    monkeypatch.setattr(settings, "copilot_github_token", SecretStr("tok"))
    _install_fake_sdk(monkeypatch, response_events=[_FakeIdle()])

    async def _raise_timeout(awaitable: Any, timeout: Any) -> Any:
        awaitable.close()  # avoid "coroutine was never awaited" noise
        raise TimeoutError

    monkeypatch.setattr(copilot_mod.asyncio, "wait_for", _raise_timeout)
    with pytest.raises(CopilotError, match="timed out"):
        CopilotBackend().annotate("sys", "human")


def test_copilot_passes_token_and_model_via_sdk_not_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Security/behaviour contract: the token rides the SDK's github_token kwarg
    # (never argv/connection string), tools are auto-approved so the agentic CLI
    # can't block, and the configured model + folded prompt reach the session.
    monkeypatch.setattr(copilot_mod.shutil, "which", lambda _b: "/usr/bin/copilot")
    monkeypatch.setattr(settings, "copilot_github_token", SecretStr("s3cr3t"))
    monkeypatch.setattr(settings, "default_llm_model", "gpt-5")
    record = _install_fake_sdk(monkeypatch, response_events=[_FakeIdle()])
    CopilotBackend().annotate("sys", "human")

    assert record["client_kwargs"]["github_token"] == "s3cr3t"
    assert "s3cr3t" not in str(record["client_kwargs"]["connection"])
    assert record["session_kwargs"]["model"] == "gpt-5"
    assert record["session_kwargs"]["on_permission_request"] == "approve-all-sentinel"
    assert record["prompt"].startswith("sys\n\nhuman")


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
