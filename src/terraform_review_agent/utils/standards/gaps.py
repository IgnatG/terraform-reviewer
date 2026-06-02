"""Absence/gap detection — assert expected artefacts exist, else emit a finding.

Unlike scanner findings (which flag something *present and wrong*), these flag
something *missing* (no README, no licence, …). They're repo-level, not scoped
to the PR diff, and classify as ``human_only`` (○) via their control, since
"is this artefact adequate" is a human judgement the engine can't make.
"""

from __future__ import annotations

from pathlib import Path

from terraform_review_agent.utils.standards.pack import RulePack, gap_rule
from terraform_review_agent.utils.state import Finding


def detect_gaps(workspace: str | Path, packs: list[RulePack]) -> list[Finding]:
    """Emit a finding for each expected artefact that no candidate path satisfies."""

    base = Path(workspace)
    findings: list[Finding] = []
    for pack in packs:
        for artefact in pack.expected_artifacts:
            if any((base / candidate).exists() for candidate in artefact.any_of):
                continue
            findings.append(
                Finding(
                    agent="standards",
                    severity=artefact.severity,
                    file=artefact.any_of[0],
                    line=None,
                    rule=gap_rule(pack.id, artefact.id),
                    message=artefact.message,
                    suggestion=artefact.suggestion,
                )
            )
    return findings
