"""Security lens — tfsec + checkov + trivy (+ Prowler-IaC SARIF), then LLM rewording."""

from __future__ import annotations

from typing import Any

from terraform_review_agent.utils.lenses.scanner import ScannerLens
from terraform_review_agent.utils.tools import (
    run_checkov,
    run_prowler_iac,
    run_tfsec,
    run_trivy,
)


class SecurityLens(ScannerLens):
    """Misconfigurations / insecure defaults / vulns.

    In-image scanners (tfsec, checkov, trivy) plus an ingested Prowler-IaC SARIF
    report when supplied (self-skips when unconfigured). Secret scanning is
    intentionally excluded: a secrets scanner surfaces credential *values* as
    findings, which the LLM rewording step would then receive — so it's kept out
    of the AI path.
    """

    id = "security"

    def scanners(self) -> list[tuple[str, Any]]:
        return [
            ("tfsec", run_tfsec),
            ("checkov", run_checkov),
            ("prowler", run_prowler_iac),
            ("trivy", run_trivy),
        ]
