"""Style lens — tflint + terraform fmt, then LLM into concise style findings."""

from __future__ import annotations

from terraform_review_agent.config import settings
from terraform_review_agent.utils.lenses._annotate import (
    annotate_with_llm,
    collect,
    filter_to_changed,
)
from terraform_review_agent.utils.lenses.base import Lens, LensResult
from terraform_review_agent.utils.state import ReviewState
from terraform_review_agent.utils.tools import (
    prepare_file_payloads,
    run_megalinter,
    run_terraform_fmt,
    run_tflint,
)


class StyleLens(Lens):
    """Lint findings + formatting drift via tflint + terraform fmt.

    Also ingests a MegaLinter SARIF report (multi-linter style/quality) when one
    is supplied; that source self-skips when unconfigured.
    """

    id = "style"

    def applies_to(self, state: ReviewState) -> bool:
        return bool(state.pr.changed_terraform_paths)

    def run(self, state: ReviewState) -> LensResult:
        if not state.pr.changed_terraform_paths:
            return LensResult()
        # MegaLinter lints many file types, so scope to all changed files; the
        # terraform scanners only emit in .tf files (a subset), so this doesn't
        # widen their results.
        changed = state.pr.changed_paths
        # Whole-codebase LLM review (llm-full-review): feed every .tf file and
        # force discovery on, keeping findings in unchanged files (post-filter
        # skipped below).
        full_review = settings.llm_full_review
        # Scanners read the workspace from disk; run them even when payloads are
        # empty (large PR with omitted patches) so coverage isn't silently dropped.
        payloads = prepare_file_payloads(state.pr, state.workspace, whole_repo=full_review)
        raw = filter_to_changed(
            collect(
                [
                    ("tflint", run_tflint),
                    ("terraform-fmt", run_terraform_fmt),
                    ("megalinter", run_megalinter),
                ],
                state.workspace,
            ),
            changed,
        )
        if not (raw or payloads):
            return LensResult()
        ai_errors: list[str] = []
        findings = annotate_with_llm(
            "style", raw, payloads, full_review=full_review, error_sink=ai_errors
        )
        scoped = findings if full_review else filter_to_changed(findings, changed)
        return LensResult(findings=scoped, ai_errors=ai_errors)
