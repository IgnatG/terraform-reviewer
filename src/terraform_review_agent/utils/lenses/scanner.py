"""Shared base for scanner-backed lenses (security, style).

A scanner lens runs a fixed set of OSS scanners over the workspace, scopes their
output to the PR's changed files, and hands the canonical findings to the AI
backend for *wording-only* rewording. Security and style differ only in **which**
scanners they run, so the whole :meth:`ScannerLens.run` body lives here once —
adding a scanner-backed lens is a subclass that returns its scanner list.
"""

from __future__ import annotations

from abc import abstractmethod
from typing import Any

from terraform_review_agent.config import settings
from terraform_review_agent.utils.lenses._annotate import (
    annotate_with_llm,
    collect,
    filter_to_changed,
)
from terraform_review_agent.utils.lenses.base import Lens, LensResult
from terraform_review_agent.utils.state import ReviewState
from terraform_review_agent.utils.tools import prepare_file_payloads


class ScannerLens(Lens):
    """A lens that runs OSS scanners then LLM-rewords their findings.

    Subclasses supply :meth:`scanners`; the registry-stamped ``id`` is the agent
    label on every finding. The finding *set* is fixed by the scanners — the LLM
    only rewrites ``message``/``suggestion`` (and may append discovered findings
    when ``enable_llm_findings`` / ``llm_full_review`` is on). A backend that's
    unconfigured or fails degrades to the un-reworded scanner findings.
    """

    @abstractmethod
    def scanners(self) -> list[tuple[str, Any]]:
        """The ``(name, @tool)`` scanners this lens runs over the workspace.

        Resolved fresh on each call (not cached at class-definition time) so the
        module-level scanner references stay monkeypatchable in tests.
        """

    def applies_to(self, state: ReviewState) -> bool:
        return bool(state.pr.changed_terraform_paths)

    def run(self, state: ReviewState) -> LensResult:
        if not state.pr.changed_terraform_paths:
            return LensResult()
        # Scope to *all* changed files, not just the .tf ones — some sources
        # (vuln/lint) aren't Terraform-specific. The .tf-only scanners emit a
        # subset, so this doesn't widen their results.
        changed = state.pr.changed_paths
        # Whole-codebase LLM review (llm-full-review) feeds every .tf in the repo
        # and forces discovery on; findings it surfaces in unchanged files must
        # survive the post-filter below.
        full_review = settings.llm_full_review
        # Scanners read the workspace from disk, so they run whether or not we
        # could build LLM payloads — a large PR with omitted patches yields empty
        # payloads but must still be scanned.
        payloads = prepare_file_payloads(state.pr, state.workspace, whole_repo=full_review)
        raw = filter_to_changed(collect(self.scanners(), state.workspace), changed)
        if not (raw or payloads):
            return LensResult()
        ai_errors: list[str] = []
        findings = annotate_with_llm(
            self.id, raw, payloads, full_review=full_review, error_sink=ai_errors
        )
        scoped = findings if full_review else filter_to_changed(findings, changed)
        return LensResult(findings=scoped, ai_errors=ai_errors)
