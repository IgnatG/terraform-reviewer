"""Hermetic offline boundary for eval runs.

Mirrors the integration-test fakes (``tests/integration/test_graph_end_to_end.py``):
the scanner subprocess + AI backend are patched so a graph run is reproducible
with no network and no scanner binaries.

Two flavours:

* :func:`offline_review` — every scanner returns *empty* output and the AI backend
  is unavailable. Used by the trajectory eval, which only cares which lens *nodes*
  run, not what they find.
* :func:`recorded_review` — scanners return *recorded* output (so lenses produce
  realistic findings) and the AI backend is configurable: an unavailable stub (the
  default — deterministic, no LLM) or ``None`` to let the real backend run (the
  opt-in quality eval with a live model).

Both use ``unittest.mock.patch`` (not the pytest ``monkeypatch`` fixture) so the
same context managers work from the pytest suite and the standalone CLIs.
"""

from __future__ import annotations

import contextlib
import subprocess
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any
from unittest.mock import patch

# Clean stdout each scanner's parser accepts as "ran fine, found nothing".
# `checkov`/`terraform fmt` must return empty stdout (their parsers treat any
# non-empty line as a result/path); the JSON scanners take an empty container.
_CLEAN_STDOUT: dict[str, str] = {
    "tfsec": "{}",
    "checkov": "",
    "tflint": "{}",
    "terraform": "",
    "trivy": '{"runs": []}',
    "infracost": "{}",
    "git": "",
}

#: A recorded scanner result: ``binary name -> (stdout, returncode)``. Return codes
#: matter — ``tflint`` exits 2 on findings, ``terraform fmt -check`` exits 3.
ScannerOutputs = Mapping[str, tuple[str, int]]

RunFn = Callable[..., "subprocess.CompletedProcess[str]"]


def _clean_run(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
    """Stand in for ``subprocess.run`` — return empty output for any scanner."""

    binary = Path(cmd[0]).name
    return subprocess.CompletedProcess(
        args=cmd, returncode=0, stdout=_CLEAN_STDOUT.get(binary, "{}"), stderr=""
    )


def _recorded_run(outputs: ScannerOutputs) -> RunFn:
    """Build a ``subprocess.run`` stand-in that replays ``outputs`` per binary."""

    def _run(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        binary = Path(cmd[0]).name
        stdout, code = outputs.get(binary, ("{}", 0))
        return subprocess.CompletedProcess(args=cmd, returncode=code, stdout=stdout, stderr="")

    return _run


def _fake_which(binary: str) -> str:
    """Stand in for ``shutil.which`` — every scanner binary "exists"."""

    return f"/usr/bin/{binary}"


class _UnavailableBackend:
    """AI backend that is never available — forces the deterministic (no-LLM) path."""

    def available(self) -> bool:
        return False

    def annotate(self, system: str, human: str) -> Any:  # pragma: no cover - never called
        raise AssertionError("AI backend must not run during a deterministic offline eval")


@contextlib.contextmanager
def _patched(
    *,
    run: RunFn,
    ai_backend_factory: Callable[[], Any] | None,
    enabled_lenses: str,
    infracost_api_key: str | None,
) -> Iterator[None]:
    """Patch the scanner + AI boundaries and lens selection; restore on exit.

    ``ai_backend_factory`` of ``None`` leaves the real backend in place (the
    opt-in live-LLM path); otherwise the factory replaces ``get_ai_backend``.
    """

    import terraform_review_agent.utils.tools as tools
    from terraform_review_agent.config import settings
    from terraform_review_agent.utils.lenses import _annotate
    from terraform_review_agent.utils.lenses import cost as cost_mod

    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(tools.shutil, "which", _fake_which))
        stack.enter_context(patch.object(tools.subprocess, "run", run))
        # The cost lens builds an infracost usage file by shelling out; short-circuit it.
        stack.enter_context(patch.object(cost_mod, "build_synced_usage_file", lambda _wd: None))
        if ai_backend_factory is not None:
            stack.enter_context(patch.object(_annotate, "get_ai_backend", ai_backend_factory))
        stack.enter_context(patch.object(settings, "enabled_lenses", enabled_lenses))
        stack.enter_context(patch.object(settings, "enable_llm_findings", False))
        stack.enter_context(patch.object(settings, "infracost_api_key", infracost_api_key))
        yield


@contextlib.contextmanager
def offline_review(
    *, enabled_lenses: str = "", infracost_api_key: str | None = None
) -> Iterator[None]:
    """Hermetic run with empty scanners + AI off (the trajectory-eval boundary)."""

    with _patched(
        run=_clean_run,
        ai_backend_factory=_UnavailableBackend,
        enabled_lenses=enabled_lenses,
        infracost_api_key=infracost_api_key,
    ):
        yield


@contextlib.contextmanager
def recorded_review(
    outputs: ScannerOutputs,
    *,
    enabled_lenses: str = "",
    infracost_api_key: str | None = None,
    ai_backend_factory: Callable[[], Any] | None = _UnavailableBackend,
) -> Iterator[None]:
    """Hermetic run with *recorded* scanner output (the quality-eval boundary).

    ``ai_backend_factory`` defaults to the unavailable stub (deterministic, no
    LLM). Pass ``None`` to let the real AI backend run — the opt-in live-model
    quality eval.
    """

    with _patched(
        run=_recorded_run(outputs),
        ai_backend_factory=ai_backend_factory,
        enabled_lenses=enabled_lenses,
        infracost_api_key=infracost_api_key,
    ):
        yield
