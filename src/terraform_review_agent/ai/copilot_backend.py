"""GitHub Copilot AI backend — rewords findings via the official Copilot SDK.

Used when ``AI_BACKEND=copilot``. Drives the ``github-copilot-sdk`` (a thin
async client over the locally-installed Copilot CLI, spoken to over stdio) in a
single-prompt round-trip: open a session, ``send`` the specialist prompt, gather
the assistant's reply, and parse it back into a :class:`SpecialistAnnotations`.

Reusing a Copilot seat is the whole point — a user already paying for Copilot
needs no separate LLM key. Every failure raises :class:`CopilotError`; the caller
degrades to the un-reworded deterministic findings, so Copilot can never block
the report (§9.2).

Two deliberate choices:

* **Lazy SDK import** (via :func:`_load_sdk`). The package is optional — BYOK
  users never install it — so importing it at module top would break the default
  backend. The single ``_load_sdk`` seam also keeps the SDK's event classes
  patchable in tests without the real package.
* **System text folded into the prompt.** We pass ``system + human`` as one
  ``send`` rather than via ``SystemMessageConfig`` — the reword-only guardrail is
  the narrow :class:`SpecialistAnnotations` return type, not the transport.

> Live behaviour against a real CLI + PAT is a human verification step (see
> HUMAN-TODO.md) — it can't be exercised on a machine without the Copilot CLI.
"""

from __future__ import annotations

import asyncio
import importlib.util
import shutil
from typing import Any, NamedTuple

from pydantic import ValidationError

from terraform_review_agent.ai.base import AIBackend
from terraform_review_agent.config import settings
from terraform_review_agent.utils.state import SpecialistAnnotations

_JSON_INSTRUCTION = (
    "Respond with ONLY a single JSON object, no prose or code fences, matching "
    'this schema: {"annotations": [{"id": <int>, "message": <string>, '
    '"suggestion": <string|null>}], "discovered": []}. Echo each finding\'s id; '
    "omit findings you have nothing to add to."
)


class CopilotError(RuntimeError):
    """Raised when the Copilot SDK/CLI is missing, fails, or returns no JSON."""


class _Sdk(NamedTuple):
    """The handful of ``github-copilot-sdk`` symbols this backend drives."""

    CopilotClient: Any
    RuntimeConnection: Any
    PermissionHandler: Any
    AssistantMessageData: Any
    AssistantMessageDeltaData: Any
    SessionErrorData: Any
    SessionIdleData: Any


def _load_sdk() -> _Sdk:
    """Import the optional Copilot SDK, or raise :class:`CopilotError`.

    The one seam the tests patch — fake SDK symbols swap in here without the real
    (CLI-dependent) package present.
    """

    try:
        from copilot import CopilotClient, RuntimeConnection
        from copilot.session import PermissionHandler
        from copilot.session_events import (
            AssistantMessageData,
            AssistantMessageDeltaData,
            SessionErrorData,
            SessionIdleData,
        )
    except ImportError as exc:  # pragma: no cover - exercised via patched _load_sdk
        raise CopilotError(
            "github-copilot-sdk is not installed (pip install github-copilot-sdk)"
        ) from exc
    return _Sdk(
        CopilotClient,
        RuntimeConnection,
        PermissionHandler,
        AssistantMessageData,
        AssistantMessageDeltaData,
        SessionErrorData,
        SessionIdleData,
    )


def _extract_json_object(text: str) -> str | None:
    """Return the first balanced top-level ``{...}`` in ``text``, or None.

    The model may wrap the JSON in prose or markdown fences; this pulls out the
    object by brace-matching (string-aware, so braces inside string literals
    don't throw off the depth count).
    """

    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


class CopilotBackend(AIBackend):
    """Reword findings by driving the GitHub Copilot SDK over the local CLI."""

    def available(self) -> bool:
        """True when a token, the Copilot CLI, and the SDK are all present.

        Any missing piece means we degrade to the un-reworded findings rather
        than erroring — the "AI off" path.
        """

        return bool(
            settings.copilot_github_token
            and shutil.which(settings.copilot_cli_command)
            and importlib.util.find_spec("copilot") is not None
        )

    def annotate(self, system: str, human: str) -> SpecialistAnnotations:
        prompt = f"{system}\n\n{human}\n\n{_JSON_INSTRUCTION}"
        raw = self._invoke(prompt)
        payload = _extract_json_object(raw)
        if payload is None:
            # Surface a snippet of the actual reply — for an agentic CLI the most
            # likely cause is prose/refusal instead of JSON, and this is the only
            # way to see what came back without a live re-run.
            snippet = raw.strip()[:400] or "<empty>"
            raise CopilotError(f"Copilot SDK returned no JSON object (raw reply: {snippet!r})")
        try:
            return SpecialistAnnotations.model_validate_json(payload)
        except ValidationError as exc:
            # A balanced-but-malformed object (wrong shape/types) is still a
            # Copilot failure — wrap it so the contract ("every failure raises
            # CopilotError") holds and the caller degrades gracefully.
            raise CopilotError(f"Copilot SDK returned malformed JSON: {exc}") from exc

    def _invoke(self, prompt: str) -> str:
        """Run the one-shot SDK round-trip synchronously, returning the reply text.

        Wraps the async session in ``asyncio.run`` (the review graph runs
        synchronously, so there is no live event loop to clash with). Every
        failure — missing token, SDK/CLI error, timeout — becomes a
        :class:`CopilotError` so the caller degrades gracefully.
        """

        try:
            return asyncio.run(self._roundtrip(prompt))
        except CopilotError:
            raise
        except TimeoutError as exc:
            raise CopilotError(
                f"Copilot SDK timed out after {settings.copilot_timeout_seconds}s"
            ) from exc
        except Exception as exc:  # any SDK/runtime failure degrades gracefully
            raise CopilotError(f"Copilot SDK call failed: {exc}") from exc

    async def _roundtrip(self, prompt: str) -> str:
        """Open a session, send ``prompt``, and collect the assistant's reply.

        The token rides the SDK's ``github_token`` kwarg (never argv); tools are
        auto-approved (``approve_all``) so the agentic CLI doesn't block on a
        confirmation it can't receive. ``model`` reuses ``DEFAULT_LLM_MODEL`` —
        for Copilot it must be a Copilot-catalog id (e.g. ``gpt-5``).

        Text is gathered from *both* channels: the assistant's text streams as
        ``AssistantMessageDeltaData`` chunks, while the final
        ``AssistantMessageData.content`` may or may not be populated — we prefer
        the completed message and fall back to the joined deltas. A
        ``SessionErrorData`` (bad model id, auth, model failure) is raised rather
        than silently yielding an empty reply.

        The timeout wraps the *whole* exchange — opening the session, sending the
        prompt, and waiting for the reply — so a CLI that hangs while creating the
        session or accepting the prompt is bounded too, not only one that stalls
        after ``send``. On timeout the cancelled ``async with`` unwinds the
        client/session, which is the SDK's cleanup path for the child process.
        """

        sdk = _load_sdk()
        token = settings.copilot_github_token
        if token is None:
            raise CopilotError("COPILOT_GITHUB_TOKEN is not set")
        secret = token.get_secret_value()

        connection = sdk.RuntimeConnection.for_stdio(
            path=shutil.which(settings.copilot_cli_command)
        )
        messages: list[str] = []
        deltas: list[str] = []
        errors: list[str] = []
        done = asyncio.Event()

        def on_event(event: Any) -> None:
            data = getattr(event, "data", None)
            if isinstance(data, sdk.AssistantMessageDeltaData):
                deltas.append(data.delta_content)
            elif isinstance(data, sdk.AssistantMessageData):
                messages.append(data.content)
            elif isinstance(data, sdk.SessionErrorData):
                errors.append(getattr(data, "message", None) or str(data))
                done.set()
            elif isinstance(data, sdk.SessionIdleData):
                done.set()

        async def _exchange() -> None:
            async with (
                sdk.CopilotClient(github_token=secret, connection=connection) as client,
                await client.create_session(
                    on_permission_request=sdk.PermissionHandler.approve_all,
                    model=settings.default_llm_model,
                ) as session,
            ):
                session.on(on_event)
                await session.send(prompt)
                await done.wait()

        await asyncio.wait_for(_exchange(), timeout=settings.copilot_timeout_seconds)

        if errors:
            raise CopilotError(f"Copilot session error: {errors[0]}")
        return "".join(messages).strip() or "".join(deltas)
