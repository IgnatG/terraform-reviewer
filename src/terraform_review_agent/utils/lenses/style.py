"""Style lens — tflint + terraform fmt (+ MegaLinter SARIF), then LLM rewording."""

from __future__ import annotations

from typing import Any

from terraform_review_agent.utils.lenses.scanner import ScannerLens
from terraform_review_agent.utils.tools import (
    run_megalinter,
    run_terraform_fmt,
    run_tflint,
)


class StyleLens(ScannerLens):
    """Lint findings + formatting drift via tflint + terraform fmt.

    Also ingests a MegaLinter SARIF report (multi-linter style/quality) when one
    is supplied; that source self-skips when unconfigured.
    """

    id = "style"

    def scanners(self) -> list[tuple[str, Any]]:
        return [
            ("tflint", run_tflint),
            ("terraform-fmt", run_terraform_fmt),
            ("megalinter", run_megalinter),
        ]
