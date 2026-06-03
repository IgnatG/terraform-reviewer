"""End-to-end integration tests for the compiled review graph.

Unlike the unit tests in ``tests/unit`` (which exercise one node at a time),
these drive the *real* compiled ``agent`` graph and the real entrypoint, with
only two boundaries faked:

* **Scanners** — ``subprocess.run`` and ``shutil.which`` are replaced so the
  scanner ``@tool`` wrappers parse *recorded* tool output (canned tfsec /
  checkov / tflint / terraform-fmt / infracost JSON) instead of shelling out.
  The real parsers, severity normalization, path scoping, dedupe, and markdown
  renderer all run.
* **LLM** — ``lenses._annotate.get_llm`` returns a fake whose structured-output
  call records the prompt and returns a canned :class:`SpecialistAnnotations`.
  No network, no API key.

So a passing test means: recorded scanner output → real parse → fan-out across
the lenses → real aggregate/dedupe/render → final comment markdown, all wired
through the compiled graph exactly as production runs it.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from terraform_review_agent import entrypoint
from terraform_review_agent.agent import agent
from terraform_review_agent.config import settings
from terraform_review_agent.utils.lenses import _annotate
from terraform_review_agent.utils.lenses import cost as cost_mod
from terraform_review_agent.utils.lenses import security as security_mod
from terraform_review_agent.utils.lenses import style as style_mod
from terraform_review_agent.utils.state import (
    ChangedFile,
    Finding,
    PRContext,
    ReviewState,
    SpecialistAnnotations,
)


def _by_agent(final: ReviewState, agent: str) -> list[Finding]:
    """The final findings produced by one lens (now merged into one list)."""

    return [f for f in final.all_findings() if f.agent == agent]


# ---------------------------------------------------------------------------
# recorded scanner output (the bytes a real scanner would print on stdout)
# ---------------------------------------------------------------------------

_TFSEC_JSON = {
    "results": [
        {
            "long_id": "aws-s3-enable-bucket-encryption",
            "severity": "CRITICAL",
            "description": "Bucket does not have encryption enabled",
            "resolution": "Enable server-side encryption",
            "location": {"filename": "main.tf", "start_line": 1},
        }
    ]
}

_CHECKOV_JSON = {
    "results": {
        "failed_checks": [
            {
                "check_id": "CKV_AWS_18",
                "severity": "HIGH",
                "check_name": "Ensure the S3 bucket has access logging enabled",
                "guideline": "https://docs.bridgecrew.io/docs/s3-access-logging",
                "file_path": "main.tf",
                "file_line_range": [3, 6],
            }
        ]
    }
}

_TFLINT_JSON = {
    "issues": [
        {
            "rule": {
                "name": "terraform_unused_declarations",
                "severity": "warning",
                "link": "https://github.com/terraform-linters/tflint",
            },
            "message": 'variable "unused" is declared but not used',
            "range": {"filename": "main.tf", "start": {"line": 9}},
        }
    ]
}

# `terraform fmt -check` prints one path per unformatted file (exit code 3).
_TERRAFORM_FMT_STDOUT = "main.tf\n"

_INFRACOST_JSON = {
    "totalMonthlyCost": "520.50",
    "diffTotalMonthlyCost": "120.00",
    "projects": [
        {
            "metadata": {"path": "."},
            "diff": {
                "resources": [
                    {"name": "aws_instance.web", "monthlyCost": "120.00"},
                ]
            },
        }
    ],
}

_MAIN_TF = """\
resource "aws_s3_bucket" "data" {
  bucket = "example-data"
}

resource "aws_instance" "web" {
  instance_type = "m5.4xlarge"
}

variable "unused" {
  type = string
}
"""


def _completed(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


def _fake_scanner_run(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
    """Stand in for ``subprocess.run``: dispatch on the scanner binary name.

    Returns the recorded stdout (and the exit code the real scanner would use)
    so the wrappers' own parsers run against realistic payloads.
    """

    binary = Path(cmd[0]).name
    if binary == "tfsec":
        return _completed(json.dumps(_TFSEC_JSON))
    if binary == "checkov":
        return _completed(json.dumps(_CHECKOV_JSON))
    if binary == "tflint":
        # tflint exits non-zero (here 2) when it finds issues; the wrapper
        # treats 0/1/2 as a successful run.
        return _completed(json.dumps(_TFLINT_JSON), returncode=2)
    if binary == "terraform":
        # `fmt -check` exits 3 when files differ from canonical style.
        return _completed(_TERRAFORM_FMT_STDOUT, returncode=3)
    if binary == "infracost":
        return _completed(json.dumps(_INFRACOST_JSON))
    raise AssertionError(f"unexpected scanner invocation: {cmd!r}")


class _FakeBackend:
    """No-op AI backend: records the (system, human) prompts, returns a canned result."""

    def __init__(self, result: SpecialistAnnotations, calls: list[tuple[str, str]]) -> None:
        self._result = result
        self._calls = calls

    def available(self) -> bool:
        return True

    def annotate(self, system: str, human: str) -> SpecialistAnnotations:
        self._calls.append((system, human))
        return self._result


@pytest.fixture
def llm_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Patch the AI backend with a no-op annotator; return the recorded prompts.

    Annotations are empty, so every scanner finding keeps its own wording — the
    finding *set* and severities under test come entirely from the (recorded)
    scanners, which is the contract these tests assert.
    """

    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        _annotate, "get_ai_backend", lambda: _FakeBackend(SpecialistAnnotations(), calls)
    )
    return calls


@pytest.fixture
def recorded_scanners(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the real scanner wrappers run against recorded output, not binaries."""

    monkeypatch.setattr(cost_mod, "build_synced_usage_file", lambda _wd: None)
    # Every scanner binary "exists"; subprocess returns recorded stdout.
    import terraform_review_agent.utils.tools as tools

    monkeypatch.setattr(tools.shutil, "which", lambda binary: f"/usr/bin/{binary}")
    monkeypatch.setattr(tools.subprocess, "run", _fake_scanner_run)


def _pr(files: list[ChangedFile]) -> PRContext:
    return PRContext(
        repository="acme/infra",
        pr_number=42,
        base_sha="a" * 40,
        head_sha="b" * 40,
        base_ref="main",
        head_ref="feature/add-bucket",
        title="Add data bucket + web instance",
        author="octocat",
        changed_files=files,
    )


def _write_workspace(tmp_path: Path) -> Path:
    (tmp_path / "main.tf").write_text(_MAIN_TF)
    baseline = tmp_path / "infracost-base.json"
    baseline.write_text("{}")
    return tmp_path


# ---------------------------------------------------------------------------
# full fan-out: all three specialists produce findings
# ---------------------------------------------------------------------------


def test_full_review_renders_every_severity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    llm_calls: list[list[Any]],
    recorded_scanners: None,
) -> None:
    monkeypatch.setattr(settings, "infracost_api_key", SecretStr("ico-key"))
    monkeypatch.setattr(settings, "enable_llm_findings", False)
    workspace = _write_workspace(tmp_path)

    state = ReviewState(
        pr=_pr([ChangedFile(path="main.tf", patch="@@ -0,0 +1 @@\n+resource {}")]),
        workspace=str(workspace),
        cost_baseline_path=str(workspace / "infracost-base.json"),
    )

    final = ReviewState.model_validate(agent.invoke(state))

    assert final.skipped is False

    # Each lens surfaced its recorded findings (all on main.tf, so the result is
    # the same under full or diff scan mode).
    assert [f.rule for f in _by_agent(final, "security")] == [
        "tfsec:aws-s3-enable-bucket-encryption",
        "checkov:CKV_AWS_18",
    ]
    assert [(f.rule, f.severity) for f in _by_agent(final, "cost")] == [
        ("infracost:resource-delta", "high"),  # +$120/mo crosses the high floor
    ]
    assert {f.rule for f in _by_agent(final, "style")} == {
        "tflint:terraform_unused_declarations",
        "terraform-fmt:unformatted",
    }
    # The absolute total + delta survived into the summary.
    assert final.cost_summary is not None
    assert final.cost_summary.total_monthly == pytest.approx(520.50)
    assert final.cost_summary.delta_monthly == pytest.approx(120.00)

    # The LLM annotator was consulted once per specialist that had findings.
    assert len(llm_calls) == 3
    # The file content reached the LLM prompt (real prepare_file_payloads ran).
    assert any("aws_s3_bucket" in human for _system, human in llm_calls)

    md = final.comment_markdown
    assert md is not None
    # Headline + cost callout.
    assert md.startswith("## Terraform Review Agent")
    assert "**5 findings**" in md
    assert "Infracost estimate:" in md and "$520.50/mo" in md
    # One collapsible section per severity: critical/high open, the rest collapsed.
    assert "<details open>\n<summary>🔴 Critical (1)</summary>" in md
    assert "<details open>\n<summary>🟠 High (2)</summary>" in md  # checkov HIGH + infracost high
    assert "<details>\n<summary>🟡 Medium (1)</summary>" in md  # tflint warning -> medium
    assert "<details>\n<summary>🔵 Low (1)</summary>" in md  # terraform-fmt low
    # Scanner-owned wording survived (annotations were empty).
    assert "Bucket does not have encryption enabled" in md


def test_no_findings_renders_all_clear(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    llm_calls: list[list[Any]],
    monkeypatched_clean_scanners: None,
) -> None:
    monkeypatch.setattr(settings, "infracost_api_key", None)  # cost agent skips
    workspace = _write_workspace(tmp_path)

    state = ReviewState(
        pr=_pr([ChangedFile(path="main.tf")]),
        workspace=str(workspace),
    )

    final = ReviewState.model_validate(agent.invoke(state))

    assert final.skipped is False
    assert final.all_findings() == []
    assert final.comment_markdown == (
        "## Terraform Review Agent\n\nNo issues found in the changed Terraform files.\n"
    )
    # Nothing for the LLM to reword, and discovery is off -> never called.
    assert llm_calls == []


# ---------------------------------------------------------------------------
# LLM rewording threads through the whole graph into the rendered comment
# ---------------------------------------------------------------------------


def test_llm_rewording_reaches_comment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    recorded_scanners: None,
) -> None:
    # Only tfsec reports, so the security finding is id 0 deterministically.
    monkeypatch.setattr(settings, "infracost_api_key", None)
    monkeypatch.setattr(settings, "enable_llm_findings", False)

    from terraform_review_agent.utils.state import FindingAnnotation

    reworded = SpecialistAnnotations(
        annotations=[
            FindingAnnotation(
                id=0,
                message="Public bucket has no server-side encryption.",
                suggestion="Add an aws_s3_bucket_server_side_encryption_configuration block.",
            )
        ]
    )
    monkeypatch.setattr(_annotate, "get_ai_backend", lambda: _FakeBackend(reworded, []))

    # Style scanners report nothing here so the security rewording is unambiguous.
    monkeypatch.setattr(style_mod, "run_tflint", _StubTool([]))
    monkeypatch.setattr(style_mod, "run_terraform_fmt", _StubTool([]))

    workspace = _write_workspace(tmp_path)
    state = ReviewState(
        pr=_pr([ChangedFile(path="main.tf")]),
        workspace=str(workspace),
    )

    final = ReviewState.model_validate(agent.invoke(state))

    f = _by_agent(final, "security")[0]
    # Scanner still owns severity/rule; only the prose was rewritten.
    assert f.severity == "critical"
    assert f.rule == "tfsec:aws-s3-enable-bucket-encryption"
    assert f.message == "Public bucket has no server-side encryption."
    assert final.comment_markdown is not None
    assert "Public bucket has no server-side encryption." in final.comment_markdown


# ---------------------------------------------------------------------------
# early exit: a PR with no Terraform changes never touches scanners or the LLM
# ---------------------------------------------------------------------------


def test_non_terraform_pr_short_circuits(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def _boom_backend(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("AI backend must not run when no terraform changed")

    def _boom_run(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("scanners must not run when no terraform changed")

    import terraform_review_agent.utils.tools as tools

    monkeypatch.setattr(_annotate, "get_ai_backend", _boom_backend)
    monkeypatch.setattr(tools.subprocess, "run", _boom_run)

    state = ReviewState(
        pr=_pr([ChangedFile(path="README.md")]),
        workspace=str(tmp_path),
    )

    final = ReviewState.model_validate(agent.invoke(state))

    assert final.skipped is True
    assert final.skip_reason is not None and "no terraform" in final.skip_reason
    assert final.all_findings() == []


# ---------------------------------------------------------------------------
# entrypoint.run(): fetch PR -> graph -> upsert sticky comment
# (covers the post-comment path the in-graph node only stubs)
# ---------------------------------------------------------------------------


class _FakeGitHubClient:
    """Records the sticky-comment upsert and serves a canned PRContext."""

    def __init__(self, pr: PRContext) -> None:
        self._pr = pr
        self.upserts: list[tuple[str, int, str]] = []
        self.review_comments: list[Any] = []

    def fetch_pr_context(self, repository: str, pr_number: int) -> PRContext:
        return self._pr

    def upsert_sticky_comment(self, repository: str, pr_number: int, body: str) -> int:
        self.upserts.append((repository, pr_number, body))
        return 9999

    def post_review_comments(self, repository: str, pr_number: int, comments: list[Any]) -> int:
        self.review_comments.append(comments)
        return len(comments)


def test_entrypoint_run_posts_sticky_comment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    llm_calls: list[list[Any]],
    recorded_scanners: None,
) -> None:
    # Cost off so this test stays focused on fetch -> graph -> upsert.
    monkeypatch.setattr(settings, "infracost_api_key", None)
    monkeypatch.setattr(settings, "enable_llm_findings", False)

    # Existing git checkout => entrypoint uses it as-is (no clone).
    workspace = _write_workspace(tmp_path)
    (workspace / ".git").mkdir()
    monkeypatch.setattr(entrypoint.settings, "workspace_dir", str(workspace))
    monkeypatch.setattr(entrypoint.settings, "infracost_baseline_path", None)
    # Redirect every output surface into the tmp dir (don't write to the repo).
    out = tmp_path / "out"
    monkeypatch.setattr(entrypoint.settings, "findings_output_path", str(out / "findings.json"))
    monkeypatch.setattr(entrypoint.settings, "sarif_output_path", str(out / "findings.sarif"))
    monkeypatch.setattr(entrypoint.settings, "evidence_html_path", str(out / "evidence.html"))
    monkeypatch.setattr(entrypoint.settings, "evidence_csv_path", str(out / "findings.csv"))

    pr = _pr([ChangedFile(path="main.tf")])
    client = _FakeGitHubClient(pr)

    final = entrypoint.run("acme/infra", 42, client=client)  # type: ignore[arg-type]

    # All four output surfaces are written from the one report (Phase 8).
    assert (out / "findings.json").is_file()
    assert (out / "findings.sarif").is_file()
    assert (out / "evidence.html").is_file()
    assert (out / "findings.csv").is_file()
    assert final.posted_comment_id == 9999
    assert len(client.upserts) == 1
    repo, pr_number, body = client.upserts[0]
    assert (repo, pr_number) == ("acme/infra", 42)
    # The rendered findings (security + style; cost skipped) made it into the body.
    assert "## Terraform Review Agent" in body
    assert "tfsec:aws-s3-enable-bucket-encryption" in body
    assert "Infracost estimate:" not in body  # cost agent was disabled
    # The changed file carries no patch, so no finding sits on a diff line -> no
    # inline review comments are posted (they stay in the sticky summary).
    assert client.review_comments == []


# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------


class _StubTool:
    """A scanner ``@tool`` stand-in returning a fixed finding list."""

    def __init__(self, result: list[Any]) -> None:
        self._result = result

    def invoke(self, _payload: dict[str, Any]) -> list[Any]:
        return self._result


@pytest.fixture
def monkeypatched_clean_scanners(monkeypatch: pytest.MonkeyPatch) -> None:
    """All scanners present but reporting nothing — exercises the all-clear path."""

    monkeypatch.setattr(cost_mod, "build_synced_usage_file", lambda _wd: None)
    monkeypatch.setattr(security_mod, "run_tfsec", _StubTool([]))
    monkeypatch.setattr(security_mod, "run_checkov", _StubTool([]))
    monkeypatch.setattr(style_mod, "run_tflint", _StubTool([]))
    monkeypatch.setattr(style_mod, "run_terraform_fmt", _StubTool([]))
