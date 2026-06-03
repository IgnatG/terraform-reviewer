"""Unit tests for the ``fail-on-severity`` CI gate in :mod:`entrypoint`.

The graph and GitHub client are never exercised here: ``run`` is replaced with
a stub returning a hand-built :class:`ReviewState`, and ``settings.fail_on_severity``
is monkeypatched per case. We assert on the helper directly and on the exit code
``main`` returns.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from terraform_review_agent import entrypoint
from terraform_review_agent.entrypoint import (
    GATING_EXIT_CODE,
    _ensure_workspace,
    _max_severity_finding,
    _post_inline_comments,
    _post_to_dashboard,
)
from terraform_review_agent.utils.state import ChangedFile, Finding, PRContext, ReviewState


def _pr() -> PRContext:
    return PRContext(
        repository="acme/example",
        pr_number=7,
        base_sha="a" * 7,
        head_sha="b" * 7,
        base_ref="main",
        head_ref="feature/x",
    )


_ARGV = ["--repository", "acme/example", "--pr-number", "7"]


def _finding(severity: str, rule: str = "r") -> Finding:
    return Finding(agent="security", severity=severity, file="main.tf", rule=rule, message="m")


def _state(*, findings: list[Finding], skipped: bool = False) -> ReviewState:
    return ReviewState(pr=_pr(), findings=findings, skipped=skipped)


# ---------------------------------------------------------------------------
# _max_severity_finding
# ---------------------------------------------------------------------------


def test_none_threshold_never_gates() -> None:
    assert _max_severity_finding([_finding("critical")], "none") is None


def test_below_threshold_returns_none() -> None:
    # floor "high" → only critical/high trip; a medium finding must not.
    assert _max_severity_finding([_finding("medium")], "high") is None


def test_at_threshold_trips() -> None:
    hit = _max_severity_finding([_finding("high")], "high")
    assert hit is not None and hit.severity == "high"


def test_returns_highest_severity_among_gating() -> None:
    findings = [_finding("high", "a"), _finding("critical", "b"), _finding("medium", "c")]
    hit = _max_severity_finding(findings, "medium")
    assert hit is not None and hit.severity == "critical"


def test_no_findings_returns_none() -> None:
    assert _max_severity_finding([], "info") is None


# ---------------------------------------------------------------------------
# main() exit code
# ---------------------------------------------------------------------------


def test_main_exits_zero_when_threshold_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(entrypoint, "run", lambda *a, **k: _state(findings=[_finding("critical")]))
    monkeypatch.setattr(entrypoint.settings, "fail_on_severity", "none")
    assert entrypoint.main(_ARGV) == 0


def test_main_gates_when_finding_trips(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(entrypoint, "run", lambda *a, **k: _state(findings=[_finding("high")]))
    monkeypatch.setattr(entrypoint.settings, "fail_on_severity", "high")
    assert entrypoint.main(_ARGV) == GATING_EXIT_CODE


def test_main_exits_zero_when_below_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(entrypoint, "run", lambda *a, **k: _state(findings=[_finding("low")]))
    monkeypatch.setattr(entrypoint.settings, "fail_on_severity", "high")
    assert entrypoint.main(_ARGV) == 0


def test_main_skipped_run_never_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        entrypoint, "run", lambda *a, **k: _state(findings=[_finding("critical")], skipped=True)
    )
    monkeypatch.setattr(entrypoint.settings, "fail_on_severity", "critical")
    assert entrypoint.main(_ARGV) == 0


# ---------------------------------------------------------------------------
# _ensure_workspace
# ---------------------------------------------------------------------------


def test_ensure_workspace_uses_existing_checkout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / ".git").mkdir()

    def _no_clone(_pr_arg: PRContext) -> str:
        raise AssertionError("must not clone when the workspace is already a checkout")

    monkeypatch.setattr(entrypoint, "_clone_pr_workspace", _no_clone)

    assert _ensure_workspace(_pr(), str(tmp_path)) == str(tmp_path)


def test_ensure_workspace_clones_when_not_a_checkout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # No .git in base_dir => the PR must be cloned into a fresh workspace.
    monkeypatch.setattr(entrypoint, "_clone_pr_workspace", lambda _pr_arg: "/tmp/cloned-ws")

    assert _ensure_workspace(_pr(), str(tmp_path)) == "/tmp/cloned-ws"


# ---------------------------------------------------------------------------
# _post_to_dashboard (Phase 9)
# ---------------------------------------------------------------------------


def test_post_to_dashboard_noop_without_report(monkeypatch: pytest.MonkeyPatch) -> None:
    # No findings_report_json => nothing to post; must not even build a client.
    def _boom(*_a: object, **_k: object) -> None:
        raise AssertionError("must not construct a client when there's no report")

    monkeypatch.setattr(entrypoint.DashboardClient, "from_settings", _boom)
    _post_to_dashboard(_state(findings=[]))  # findings_report_json is None here


def test_post_to_dashboard_noop_when_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    # A report exists but no dashboard is configured => from_settings returns None.
    monkeypatch.setattr(entrypoint.DashboardClient, "from_settings", lambda *a, **k: None)
    state = _state(findings=[]).model_copy(update={"findings_report_json": '{"x": 1}'})
    _post_to_dashboard(state)  # no client, no parse, no raise


# ---------------------------------------------------------------------------
# _post_inline_comments (Phase 10)
# ---------------------------------------------------------------------------


class _RecordingGH:
    def __init__(self, exc: Exception | None = None) -> None:
        self.posted: list[object] = []
        self._exc = exc

    def post_review_comments(self, repository: str, pr_number: int, comments: list) -> int:  # type: ignore[type-arg]
        if self._exc is not None:
            raise self._exc
        self.posted.append(comments)
        return len(comments)


def _pr_with_patch() -> PRContext:
    return PRContext(
        repository="acme/example",
        pr_number=7,
        base_sha="a" * 7,
        head_sha="b" * 7,
        base_ref="main",
        head_ref="feature/x",
        changed_files=[ChangedFile(path="main.tf", patch="@@ -0,0 +1,2 @@\n+line1\n+line2")],
    )


def _located(file: str, line: int | None, rule: str) -> Finding:
    return Finding(agent="security", severity="high", file=file, line=line, rule=rule, message="m")


def test_post_inline_comments_only_for_diff_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(entrypoint.settings, "inline_comments", True)
    gh = _RecordingGH()
    state = ReviewState(
        pr=_pr_with_patch(),
        findings=[
            _located("main.tf", 1, "tfsec:on-diff"),  # line 1 is in the diff -> commented
            _located("main.tf", 5, "tfsec:off-diff"),  # line 5 not in diff -> skipped
            _located("other.tf", 1, "tfsec:no-patch"),  # file has no patch -> skipped
            _located("main.tf", None, "tfsec:no-line"),  # no line -> skipped
        ],
    )

    _post_inline_comments(gh, "acme/example", 7, state)  # type: ignore[arg-type]

    assert len(gh.posted) == 1
    comments = gh.posted[0]
    assert [(c.path, c.line) for c in comments] == [("main.tf", 1)]
    assert "tfsec:on-diff" in comments[0].body


def test_post_inline_comments_disabled_by_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(entrypoint.settings, "inline_comments", False)
    gh = _RecordingGH()
    state = ReviewState(pr=_pr_with_patch(), findings=[_located("main.tf", 1, "tfsec:x")])

    _post_inline_comments(gh, "acme/example", 7, state)  # type: ignore[arg-type]

    assert gh.posted == []  # flag off -> never calls the client


def test_post_inline_comments_swallows_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    monkeypatch.setattr(entrypoint.settings, "inline_comments", True)
    gh = _RecordingGH(exc=httpx.ConnectError("boom"))
    state = ReviewState(pr=_pr_with_patch(), findings=[_located("main.tf", 1, "tfsec:x")])

    # Must not raise — a failed inline post never fails the run.
    _post_inline_comments(gh, "acme/example", 7, state)  # type: ignore[arg-type]


def test_post_to_dashboard_posts_parsed_report_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The wiring: a configured client receives the report parsed from the state's
    # findings_report_json (not the raw string, not a re-shaped payload).
    from terraform_review_agent.utils.findings_report import (
        FindingsReport,
        build_findings_report,
        render_findings_json,
    )

    report = build_findings_report(pr=_pr(), findings=[_finding("high")], cost_summary=None)
    posted: list[FindingsReport] = []

    class _FakeClient:
        def post_report(self, report: FindingsReport) -> bool:
            posted.append(report)
            return True

    monkeypatch.setattr(entrypoint.DashboardClient, "from_settings", lambda *a, **k: _FakeClient())
    state = _state(findings=[]).model_copy(
        update={"findings_report_json": render_findings_json(report)}
    )
    _post_to_dashboard(state)

    assert len(posted) == 1
    assert posted[0].scan.repository == "acme/example"
    assert posted[0].summary.total == 1
