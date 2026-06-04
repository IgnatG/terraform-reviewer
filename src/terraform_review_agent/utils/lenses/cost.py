"""Cost lens — infracost diff against the base ref, then LLM annotation."""

from __future__ import annotations

import shutil
from pathlib import Path

import structlog

from terraform_review_agent.config import settings
from terraform_review_agent.utils.lenses._annotate import annotate_with_llm
from terraform_review_agent.utils.lenses.base import Lens, LensResult
from terraform_review_agent.utils.state import CostReport, ReviewState
from terraform_review_agent.utils.tools import (
    ScannerError,
    build_infracost_baseline,
    build_synced_usage_file,
    prepare_file_payloads,
    run_infracost_diff,
)

log = structlog.get_logger(__name__)


def _cleanup_scratch(file_path: str | None, dir_prefix: str) -> None:
    """Remove the throwaway scratch dir holding ``file_path``, if we created it.

    ``build_synced_usage_file`` / ``build_infracost_baseline`` write into
    ``mkdtemp`` dirs (``tfr-usage-*`` / ``tfr-cost-*``). They're consumed by the
    infracost diff and then dead weight, so we delete them after the run rather
    than leaking one per review on a long-lived runner. The ``dir_prefix`` guard
    means we only ever remove a dir we own — never a CI-injected baseline path or
    anything else that happens to be passed in.
    """

    if not file_path:
        return
    parent = Path(file_path).parent
    if parent.name.startswith(dir_prefix) and parent.is_dir():
        shutil.rmtree(parent, ignore_errors=True)


class CostLens(Lens):
    """Monthly cost deltas vs. the base branch via ``infracost diff``.

    Gated on the infracost API key — when it's unset the lens doesn't apply. The
    base breakdown comes from ``cost_baseline_path`` when one was supplied (CI may
    inject it), otherwise it's generated on the fly from the workspace's git
    history. A usage file is auto-synced from the PR's Terraform (no per-repo
    setup) so usage-based resources are priced from infracost's defaults on both
    the base and head; if the sync fails the totals fall back to fixed costs.
    Returns both the per-resource findings and a ``cost_summary`` (the head's
    absolute monthly total + the change), so the report can show both.
    """

    id = "cost"

    def applies_to(self, state: ReviewState) -> bool:
        return settings.infracost_api_key is not None and state.pr.has_terraform_changes

    def run(self, state: ReviewState) -> LensResult:
        if settings.infracost_api_key is None:
            log.info("cost.skipped", reason="no infracost api key")
            return LensResult()
        if not state.pr.has_terraform_changes:
            return LensResult()
        # Empty payloads (large PR, omitted patches) must not skip infracost — it
        # prices the workspace on disk and doesn't need the LLM payloads.
        payloads = prepare_file_payloads(state.pr, state.workspace)
        usage_file = build_synced_usage_file(state.workspace)
        generated_baseline: str | None = None
        try:
            if state.cost_baseline_path:
                baseline = state.cost_baseline_path
            else:
                # No CI-injected baseline → build one in a scratch dir we own and
                # clean up in `finally`.
                baseline = build_infracost_baseline(
                    state.workspace, state.pr.repository, usage_file_path=usage_file
                )
                generated_baseline = baseline
            result = run_infracost_diff.invoke(
                {
                    "working_dir": state.workspace,
                    "baseline_path": baseline,
                    "usage_file_path": usage_file,
                }
            )
        except ScannerError as exc:
            log.warning("scanner.skipped", scanner="infracost", error=str(exc))
            return LensResult()
        finally:
            # The synced usage file + any baseline we generated live in throwaway
            # mkdtemp dirs; remove them so repeated runs don't accumulate scratch.
            _cleanup_scratch(usage_file, "tfr-usage-")
            _cleanup_scratch(generated_baseline, "tfr-cost-")

        report = result if isinstance(result, CostReport) else CostReport.model_validate(result)
        summary = report.summary
        log.info(
            "cost.ran",
            total_monthly=summary.total_monthly if summary else None,
            delta_monthly=summary.delta_monthly if summary else None,
            usage_file_synced=usage_file is not None,
        )
        ai_errors: list[str] = []
        findings = (
            annotate_with_llm("cost", report.findings, payloads, error_sink=ai_errors)
            if report.findings
            else []
        )
        return LensResult(findings=findings, cost_summary=summary, ai_errors=ai_errors)
