"""Security lens — tfsec + checkov, then LLM rewording into security findings."""

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
    run_checkov,
    run_prowler_iac,
    run_tfsec,
    run_trivy,
)


class SecurityLens(Lens):
    """Misconfigurations / insecure defaults / vulns.

    In-image scanners (tfsec, checkov, trivy) plus an ingested Prowler-IaC SARIF
    report when supplied (self-skips when unconfigured). Secret scanning is
    intentionally excluded: a secrets scanner surfaces credential *values* as
    findings, which the LLM rewording step would then receive — so it's kept out
    of the AI path.
    """

    id = "security"

    def applies_to(self, state: ReviewState) -> bool:
        return bool(state.pr.changed_terraform_paths)

    def run(self, state: ReviewState) -> LensResult:
        if not state.pr.changed_terraform_paths:
            return LensResult()
        # Vuln sources aren't Terraform-specific, so scope findings to all changed
        # files, not just the .tf ones. (tfsec/checkov only emit in .tf files,
        # which are a subset, so this doesn't widen their results.)
        changed = state.pr.changed_paths
        # The whole-codebase LLM review (llm-full-review) feeds every .tf file in
        # the repo to the LLM and forces discovery on; findings it surfaces in
        # unchanged files must survive the post-filter below.
        full_review = settings.llm_full_review
        # Scanners read the workspace from disk, so they run whether or not we
        # could build LLM payloads — a large PR with omitted patches yields
        # empty payloads but must still be scanned.
        payloads = prepare_file_payloads(state.pr, state.workspace, whole_repo=full_review)
        raw = filter_to_changed(
            collect(
                [
                    ("tfsec", run_tfsec),
                    ("checkov", run_checkov),
                    ("prowler", run_prowler_iac),
                    ("trivy", run_trivy),
                ],
                state.workspace,
            ),
            changed,
        )
        if not (raw or payloads):
            return LensResult()
        ai_errors: list[str] = []
        findings = annotate_with_llm(
            "security", raw, payloads, full_review=full_review, error_sink=ai_errors
        )
        scoped = findings if full_review else filter_to_changed(findings, changed)
        return LensResult(findings=scoped, ai_errors=ai_errors)
