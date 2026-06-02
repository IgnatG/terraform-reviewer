"""Check-source normalization layer.

Turns the aggregated output of external tools (MegaLinter, Prowler-IaC,
gitleaks, Trivy …) into the engine's internal :class:`~...state.Finding` set,
and parses coverage reports (lcov / cobertura / jacoco) for the A3 lens.

The lenses consume these via the ingestion runners in ``utils.tools``; this
package is pure parsing (no subprocess, no I/O beyond reading a report file),
so it's trivially testable against recorded reports.
"""

from __future__ import annotations

from terraform_review_agent.utils.sources.coverage import (
    CoverageReport,
    FileCoverage,
    parse_cobertura,
    parse_coverage_file,
    parse_jacoco,
    parse_lcov,
)
from terraform_review_agent.utils.sources.sarif import parse_sarif, parse_sarif_file

__all__ = [
    "CoverageReport",
    "FileCoverage",
    "parse_cobertura",
    "parse_coverage_file",
    "parse_jacoco",
    "parse_lcov",
    "parse_sarif",
    "parse_sarif_file",
]
