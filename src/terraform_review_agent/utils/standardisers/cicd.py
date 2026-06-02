"""A2 CI/CD Standardiser — audit ``.github/workflows`` against a golden baseline.

Repo-level posture (not diff-scoped): parse every workflow YAML and flag the
insecure patterns the baseline forbids —

* ``pull_request_target`` triggers (run with a write token + secrets on
  untrusted PR code),
* third-party actions pinned to a tag/branch instead of a full 40-char commit
  SHA (a moving ref is a supply-chain risk), and
* a missing top-level ``permissions`` block (leaves the broad default
  ``GITHUB_TOKEN``).

Emits a deviation per issue plus one repo-level posture score. A workflow whose
YAML fails to parse is logged and skipped rather than failing the run.

Only GitHub Actions is parsed today; Azure DevOps / GitLab baselines are a
later addition (build plan Phase 5 scope note).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import structlog
import yaml
from pydantic import BaseModel

from terraform_review_agent.utils.state import Finding, Severity

log = structlog.get_logger(__name__)

_WORKFLOW_DIR = ".github/workflows"
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class CICDBaseline(BaseModel):
    """Golden CI/CD posture A2 audits each workflow against."""

    id: str
    name: str
    version: str
    source_url: str | None = None
    forbid_pull_request_target: bool = True
    require_pinned_action_shas: bool = True
    require_top_level_permissions: bool = True
    pull_request_target_severity: Severity = "high"
    unpinned_action_severity: Severity = "medium"
    missing_permissions_severity: Severity = "low"


def _workflow_files(workspace: Path) -> list[Path]:
    wf_dir = workspace / _WORKFLOW_DIR
    if not wf_dir.is_dir():
        return []
    files = [*wf_dir.glob("*.yml"), *wf_dir.glob("*.yaml")]
    return sorted(f for f in files if f.is_file())


def _triggers(doc: dict[object, object]) -> set[str]:
    """The trigger names under ``on:``.

    PyYAML parses the bare key ``on`` as the YAML-1.1 boolean ``True``, so look
    it up under both keys. ``on`` may be a string, a list, or a mapping.
    """

    on_value = doc.get("on", doc.get(True))
    if isinstance(on_value, str):
        return {on_value}
    if isinstance(on_value, list):
        return {str(x) for x in on_value}
    if isinstance(on_value, dict):
        return {str(k) for k in on_value}
    return set()


def _iter_uses(doc: dict[object, object]) -> Iterator[str]:
    """Every step's ``uses`` value across all jobs in a workflow doc."""

    jobs = doc.get("jobs")
    if not isinstance(jobs, dict):
        return
    for job in jobs.values():
        if not isinstance(job, dict):
            continue
        steps = job.get("steps")
        if not isinstance(steps, list):
            continue
        for step in steps:
            if isinstance(step, dict):
                uses = step.get("uses")
                if isinstance(uses, str):
                    yield uses


def _is_external_action(uses: str) -> bool:
    """True for a marketplace/reusable action referenced as ``owner/repo...@ref``.

    Local (``./``) and ``docker://`` references aren't SHA-pinnable the same way
    and are out of scope for this check.
    """

    return "@" in uses and not uses.startswith(("./", "../", ".", "docker://"))


def _is_pinned_sha(ref: str) -> bool:
    return bool(_SHA_RE.match(ref))


def check_workflows(workspace: str | Path, baseline: CICDBaseline) -> list[Finding]:
    """Emit deviation findings + a posture score for the repo's CI/CD workflows."""

    base = Path(workspace)
    workflow_files = _workflow_files(base)
    findings: list[Finding] = []
    total_checks = 0
    parsed_any = False

    for wf in workflow_files:
        rel = wf.relative_to(base).as_posix()
        try:
            loaded = yaml.safe_load(wf.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
            log.warning("cicd.workflow_unreadable", file=rel, error=str(exc))
            continue
        if not isinstance(loaded, dict):
            continue
        parsed_any = True

        if baseline.forbid_pull_request_target:
            total_checks += 1
            if "pull_request_target" in _triggers(loaded):
                findings.append(
                    Finding(
                        agent="cicd",
                        lens="A2",
                        severity=baseline.pull_request_target_severity,
                        file=rel,
                        rule=f"{baseline.id}:pull-request-target",
                        message=(
                            f"Workflow `{rel}` uses the `pull_request_target` trigger, which "
                            "runs with a write token and secrets on untrusted PR code."
                        ),
                        suggestion=(
                            "Use `pull_request`, or avoid checking out and executing PR code "
                            "in a `pull_request_target` workflow."
                        ),
                    )
                )

        if baseline.require_top_level_permissions:
            total_checks += 1
            if "permissions" not in loaded:
                findings.append(
                    Finding(
                        agent="cicd",
                        lens="A2",
                        severity=baseline.missing_permissions_severity,
                        file=rel,
                        rule=f"{baseline.id}:missing-permissions",
                        message=(
                            f"Workflow `{rel}` sets no top-level `permissions`, so it runs "
                            "with the broad default GITHUB_TOKEN."
                        ),
                        suggestion="Add a least-privilege top-level `permissions:` block.",
                    )
                )

        if baseline.require_pinned_action_shas:
            for uses in _iter_uses(loaded):
                if not _is_external_action(uses):
                    continue
                total_checks += 1
                action, _, ref = uses.rpartition("@")
                if _is_pinned_sha(ref):
                    continue
                findings.append(
                    Finding(
                        agent="cicd",
                        lens="A2",
                        severity=baseline.unpinned_action_severity,
                        file=rel,
                        # Action in the rule so multiple unpinned uses in one file
                        # don't collapse under the (file, rule, line) dedupe key.
                        rule=f"{baseline.id}:unpinned-action:{action}",
                        message=(
                            f"Action `{action}` in `{rel}` is pinned to `{ref}`, "
                            "not a full commit SHA."
                        ),
                        suggestion="Pin third-party actions to a full 40-character commit SHA.",
                    )
                )

    if not parsed_any:
        return []

    passed = total_checks - len(findings)
    pct = round(100 * passed / total_checks) if total_checks else 100
    findings.append(
        Finding(
            agent="cicd",
            lens="A2",
            severity="info",
            file=workflow_files[0].relative_to(base).as_posix(),
            rule=f"{baseline.id}:score",
            message=(
                f"CI/CD baseline ({baseline.name}): {passed}/{total_checks} checks passed "
                f"across {len(workflow_files)} workflow file(s) ({pct}%)."
            ),
        )
    )
    return findings
