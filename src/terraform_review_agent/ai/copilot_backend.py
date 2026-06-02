"""GitHub Copilot AI backend — rewords findings via the bundled Copilot CLI.

Used when ``AI_BACKEND=copilot``. Shells out to the Copilot CLI (command +
timeout configurable) in non-interactive mode with ``COPILOT_GITHUB_TOKEN`` in
its environment, asks for a single JSON object matching
:class:`SpecialistAnnotations`, and parses it back.

The exact CLI invocation is isolated in :meth:`CopilotBackend._invoke_cli` —
the one seam to adjust for the installed CLI — so the JSON-extraction and the
reword-only guardrail (the validated return type) are independent of it. Every
failure raises :class:`CopilotError`; the caller degrades to the un-reworded
deterministic findings, so Copilot can never block the report (§9.2).

> Live behaviour against a real CLI + PAT is a human verification step (see
> HUMAN-TODO.md) — it can't be exercised on a machine without the Copilot CLI.
"""

from __future__ import annotations

import os
import shutil
import subprocess

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
    """Raised when the Copilot CLI is missing, fails, or returns no usable JSON."""


def _extract_json_object(text: str) -> str | None:
    """Return the first balanced top-level ``{...}`` in ``text``, or None.

    The CLI may wrap the JSON in prose or markdown fences; this pulls out the
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
    """Reword findings by driving the GitHub Copilot CLI as a subprocess."""

    def available(self) -> bool:
        return bool(settings.copilot_github_token and shutil.which(settings.copilot_cli_command))

    def annotate(self, system: str, human: str) -> SpecialistAnnotations:
        prompt = f"{system}\n\n{human}\n\n{_JSON_INSTRUCTION}"
        raw = self._invoke_cli(prompt)
        payload = _extract_json_object(raw)
        if payload is None:
            raise CopilotError("Copilot CLI returned no JSON object")
        return SpecialistAnnotations.model_validate_json(payload)

    def _invoke_cli(self, prompt: str) -> str:
        """Run the Copilot CLI once with ``prompt`` and return its stdout.

        The single seam to adapt to the installed CLI: the command, the
        single-prompt flag (``-p``), and the token env var live here.
        """

        binary = shutil.which(settings.copilot_cli_command)
        if binary is None:
            raise CopilotError(f"Copilot CLI not found on PATH: {settings.copilot_cli_command!r}")
        if settings.copilot_github_token is None:
            raise CopilotError("COPILOT_GITHUB_TOKEN is not set")
        env = {
            **os.environ,
            "COPILOT_GITHUB_TOKEN": settings.copilot_github_token.get_secret_value(),
        }
        try:
            completed = subprocess.run(
                [binary, "-p", prompt],
                capture_output=True,
                text=True,
                timeout=settings.copilot_timeout_seconds,
                check=False,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            raise CopilotError(
                f"Copilot CLI timed out after {settings.copilot_timeout_seconds}s"
            ) from exc
        if completed.returncode != 0:
            tail = (completed.stderr or completed.stdout or "").strip()[:400]
            raise CopilotError(f"Copilot CLI exited {completed.returncode}: {tail}")
        return completed.stdout
