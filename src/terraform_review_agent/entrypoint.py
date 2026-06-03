"""CLI entrypoint invoked by the reusable GitHub Actions workflow.

Reads PR coordinates from the environment (``GITHUB_REPOSITORY``,
``GITHUB_PR_NUMBER``, ``GITHUB_TOKEN``), fetches PR context, runs the compiled
LangGraph agent, and — when the graph produced markdown — upserts a sticky
review comment.

Phase 2 wires the plumbing end-to-end; specialist nodes still produce empty
findings, so the comment body will be empty until Phases 4-5 land.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import httpx
import structlog

from terraform_review_agent.agent import agent
from terraform_review_agent.config import FailOnSeverity, Settings, settings
from terraform_review_agent.dashboard_client import DashboardClient
from terraform_review_agent.github_client import GitHubClient, ReviewComment, inline_marker
from terraform_review_agent.utils.diff import commentable_lines
from terraform_review_agent.utils.evidence_pack import render_evidence_csv, render_evidence_html
from terraform_review_agent.utils.findings_report import FindingsReport
from terraform_review_agent.utils.render import dedupe_findings, sort_findings
from terraform_review_agent.utils.sarif_export import render_sarif_json
from terraform_review_agent.utils.state import SEVERITY_ORDER, Finding, PRContext, ReviewState

log = structlog.get_logger(__name__)

# Exit code returned when findings trip the configured `fail_on_severity` floor,
# so consumers can gate CI on it. Distinct from 1 (unexpected error).
GATING_EXIT_CODE = 2


def _max_severity_finding(findings: list[Finding], threshold: FailOnSeverity) -> Finding | None:
    """Return the highest-severity finding at or above ``threshold``, else ``None``.

    ``"none"`` disables gating. Severity ranks ascend by leniency (critical=0),
    so a finding trips the gate when its rank is ``<=`` the threshold's rank.
    """

    if threshold == "none":
        return None
    floor = SEVERITY_ORDER[threshold]
    gating = [f for f in findings if f.severity_rank <= floor]
    if not gating:
        return None
    return min(gating, key=lambda f: f.severity_rank)


@dataclass(frozen=True)
class CLIArgs:
    repository: str
    pr_number: int


def _parse_args(argv: list[str] | None = None) -> CLIArgs:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repository",
        help="`owner/repo` slug (defaults to $GITHUB_REPOSITORY).",
        default=None,
    )
    parser.add_argument(
        "--pr-number",
        type=int,
        help="PR number to review (defaults to $GITHUB_PR_NUMBER).",
        default=None,
    )
    parsed = parser.parse_args(argv)

    repository = parsed.repository or settings.github_repository
    pr_number = parsed.pr_number or settings.github_pr_number
    if not repository:
        raise SystemExit("repository is required (pass --repository or set GITHUB_REPOSITORY)")
    if not pr_number:
        raise SystemExit("pr-number is required (pass --pr-number or set GITHUB_PR_NUMBER)")
    return CLIArgs(repository=repository, pr_number=pr_number)


def _configure_logging(cfg: Settings) -> None:
    level = getattr(logging, cfg.log_level.upper(), logging.INFO)
    logging.basicConfig(level=level, format="%(message)s", stream=sys.stderr)
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
    )


def _git(args: list[str], *, cwd: str | None = None) -> int:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
    ).returncode


def _clone_pr_workspace(pr: PRContext) -> str:
    """Clone ``pr``'s repo and check out its merge ref into a temp dir.

    Used for non-CI runs (e.g. ``make run``) where the workspace doesn't already
    contain the PR. Authenticates with ``GITHUB_TOKEN`` and prefers the merge ref
    (PR merged into base, so the cost agent can derive the base from ``HEAD^1``),
    falling back to the head ref for unmergeable PRs.
    """

    if settings.github_token is None:
        raise SystemExit("GITHUB_TOKEN is required to fetch the PR workspace")
    token = settings.github_token.get_secret_value()
    dest = tempfile.mkdtemp(prefix="tfr-pr-")
    auth_url = f"https://x-access-token:{token}@github.com/{pr.repository}.git"

    log.info("cloning pr workspace", repo=pr.repository, pr=pr.pr_number, dest=dest)
    if _git(["clone", "--quiet", auth_url, dest]) != 0:
        raise SystemExit(f"failed to clone {pr.repository}")
    # Prefer the merge ref; fall back to the head ref for unmergeable PRs.
    if (
        _git(["fetch", "--quiet", "origin", f"pull/{pr.pr_number}/merge"], cwd=dest) != 0
        and _git(["fetch", "--quiet", "origin", f"pull/{pr.pr_number}/head"], cwd=dest) != 0
    ):
        raise SystemExit(f"failed to fetch refs for PR #{pr.pr_number}")
    if _git(["checkout", "--quiet", "FETCH_HEAD"], cwd=dest) != 0:
        raise SystemExit("failed to check out the PR ref")
    return dest


def _write_text(path_str: str, content: str, label: str) -> None:
    out = Path(path_str)
    if out.parent != Path():
        out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(content, encoding="utf-8")
    log.info("wrote output", label=label, path=str(out))


def _write_outputs(final: ReviewState) -> None:
    """Write every output surface from the aggregator's report (the I/O boundary).

    findings.json (the contract) + the SARIF export (code-scanning) + the
    HTML/CSV evidence pack. No-op if the graph produced no report (it always
    should, but stay defensive — a skipped run still emits one).
    """

    if not final.findings_report_json:
        log.info("no findings report produced; skipping write")
        return
    _write_text(settings.findings_output_path, final.findings_report_json, "findings.json")
    report = FindingsReport.model_validate_json(final.findings_report_json)
    _write_text(settings.sarif_output_path, render_sarif_json(report), "sarif")
    _write_text(settings.evidence_html_path, render_evidence_html(report), "evidence.html")
    _write_text(settings.evidence_csv_path, render_evidence_csv(report), "evidence.csv")


def _post_to_dashboard(final: ReviewState) -> None:
    """Best-effort push of the findings report to the hosted dashboard ingest.

    No-op when no dashboard is configured (``DASHBOARD_INGEST_URL`` unset) or no
    report was produced. Posted on every scan — including skipped ones — so the
    dashboard records "this repo was scanned, 0 findings" too. Never raises: the
    client swallows failures so dashboard downtime can't fail the run.
    """

    if not final.findings_report_json:
        return
    client = DashboardClient.from_settings()
    if client is None:
        return
    client.post_report(FindingsReport.model_validate_json(final.findings_report_json))


def _inline_key(finding: Finding) -> str:
    """Stable per-finding key for the idempotent inline-comment marker."""

    raw = f"{finding.file}|{finding.rule}|{finding.line}"
    return hashlib.sha1(raw.encode("utf-8"), usedforsecurity=False).hexdigest()[:12]


def _inline_comment_body(finding: Finding) -> str:
    """The body for one inline review comment (carries the dedupe marker)."""

    parts = [inline_marker(_inline_key(finding)), "", f"**{finding.message}**"]
    if finding.suggestion:
        parts += ["", f"💡 {finding.suggestion}"]
    parts += ["", f"<sub>`{finding.rule}` · {finding.agent}</sub>"]
    return "\n".join(parts)


def _post_inline_comments(
    gh: GitHubClient, repository: str, pr_number: int, final: ReviewState
) -> None:
    """Post an inline review comment per finding that sits on a changed line.

    Behind ``settings.inline_comments`` (on by default). Only findings whose
    (file, line) is on the PR diff are eligible — GitHub rejects comments off the
    diff, so the rest stay in the sticky summary. Best-effort: a failure is logged
    and never fails the run. The client dedupes by marker, so re-runs don't repost.
    """

    if not settings.inline_comments:
        return
    patches = {f.path: f.patch for f in final.pr.changed_files}
    comments: list[ReviewComment] = []
    # dedupe_findings collapses the (file, rule, line) key, so each survivor maps
    # to a distinct inline comment.
    for f in sort_findings(dedupe_findings(final.all_findings())):
        if f.line is None or f.line not in commentable_lines(patches.get(f.file)):
            continue
        comments.append(ReviewComment(path=f.file, line=f.line, body=_inline_comment_body(f)))
    if not comments:
        return
    try:
        gh.post_review_comments(repository, pr_number, comments)
    except httpx.HTTPError as exc:
        log.warning("inline comments failed; continuing", repo=repository, error=str(exc))


def _ensure_workspace(pr: PRContext, base_dir: str) -> str:
    """Return a workspace containing the PR's files.

    If ``base_dir`` is already a git checkout (the CI case, where the job checked
    out the merge ref) use it as-is; otherwise clone the PR so the scanners and
    the cost baseline have real files to work with.
    """

    if (Path(base_dir) / ".git").is_dir():
        return base_dir
    return _clone_pr_workspace(pr)


def run(
    repository: str,
    pr_number: int,
    *,
    client: GitHubClient | None = None,
) -> ReviewState:
    """Run one review pass and (when markdown was produced) post the comment.

    Returns the final :class:`ReviewState` for caller-side assertions / tests.
    """

    gh = client or GitHubClient.from_settings()
    pr_context: PRContext = gh.fetch_pr_context(repository, pr_number)
    log.info(
        "fetched pr context",
        repo=repository,
        pr=pr_number,
        files=len(pr_context.changed_files),
    )

    workspace = _ensure_workspace(pr_context, settings.workspace_dir)

    raw_final = agent.invoke(
        ReviewState(
            pr=pr_context,
            workspace=workspace,
            cost_baseline_path=settings.infracost_baseline_path,
        )
    )
    final = ReviewState.model_validate(raw_final)

    # Always persist the output surfaces (findings.json + SARIF + evidence pack),
    # even on a skipped run — downstream consumers (dashboard, code-scanning,
    # Remediator) expect them per scan. This is the I/O boundary; the aggregator
    # only serialized the report.
    _write_outputs(final)
    _post_to_dashboard(final)

    if final.skipped:
        log.info("skipping review", reason=final.skip_reason)
        return final

    if final.comment_markdown:
        comment_id = gh.upsert_sticky_comment(repository, pr_number, final.comment_markdown)
        _post_inline_comments(gh, repository, pr_number, final)
        return final.model_copy(update={"posted_comment_id": comment_id})

    log.info("no comment markdown produced; skipping upsert")
    return final


def main(argv: list[str] | None = None) -> int:
    _configure_logging(settings)
    args = _parse_args(argv)
    final = run(args.repository, args.pr_number)

    if final.skipped:
        return 0

    gating = _max_severity_finding(final.all_findings(), settings.fail_on_severity)
    if gating is not None:
        log.warning(
            "failing run: finding meets fail_on_severity floor",
            threshold=settings.fail_on_severity,
            severity=gating.severity,
            rule=gating.rule,
            file=gating.file,
        )
        return GATING_EXIT_CODE
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
