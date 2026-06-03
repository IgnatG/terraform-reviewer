"""Unit tests for the review lenses in :mod:`utils.lenses`.

Both sides of each lens are stubbed: scanner ``@tool`` objects are replaced with
fakes returning canned ``Finding``/raising ``ScannerError``, and ``get_llm`` is
replaced with a fake chat model whose structured-output runnable returns a
canned :class:`SpecialistAnnotations`. No subprocesses or network calls run.

The contract under test: scanner findings are canonical — their severity, file,
line, and rule survive verbatim and the finding *set* is fixed by the scanners.
The LLM may only reword ``message``/``suggestion`` (matched back by ``id``).
Speculative LLM-discovered findings are gated on ``settings.enable_llm_findings``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from terraform_review_agent.config import settings
from terraform_review_agent.utils.lenses import _annotate
from terraform_review_agent.utils.lenses import cost as cost_mod
from terraform_review_agent.utils.lenses import security as security_mod
from terraform_review_agent.utils.lenses import style as style_mod
from terraform_review_agent.utils.lenses.cost import CostLens
from terraform_review_agent.utils.lenses.security import SecurityLens
from terraform_review_agent.utils.lenses.style import StyleLens
from terraform_review_agent.utils.state import (
    ChangedFile,
    CostReport,
    CostSummary,
    Finding,
    FindingAnnotation,
    LLMFinding,
    PRContext,
    ReviewState,
    SpecialistAnnotations,
)
from terraform_review_agent.utils.tools import FilePayload, ScannerError

# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class _FakeTool:
    """Stand-in for a scanner ``@tool``: returns a canned result or raises on invoke."""

    def __init__(self, result: Any) -> None:
        self._result = result
        self.calls: list[dict[str, Any]] = []

    def invoke(self, payload: dict[str, Any]) -> Any:
        self.calls.append(payload)
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class _FakeBackend:
    """Stand-in AI backend: records the (system, human) prompts, returns a canned result."""

    def __init__(self, result: SpecialistAnnotations, *, available: bool = True) -> None:
        self._result = result
        self._available = available
        self.calls: list[tuple[str, str]] = []

    def available(self) -> bool:
        return self._available

    def annotate(self, system: str, human: str) -> SpecialistAnnotations:
        self.calls.append((system, human))
        return self._result

    @property
    def human(self) -> str:
        """The human prompt of the last annotate call (test convenience)."""

        return self.calls[-1][1]


def _patch_llm(monkeypatch: pytest.MonkeyPatch, result: SpecialistAnnotations) -> _FakeBackend:
    backend = _FakeBackend(result)
    monkeypatch.setattr(_annotate, "get_ai_backend", lambda: backend)
    return backend


def _forbid_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Boom:
        def available(self) -> bool:
            return True

        def annotate(self, system: str, human: str) -> Any:
            raise AssertionError("AI backend must not be invoked for this state")

    monkeypatch.setattr(_annotate, "get_ai_backend", lambda: _Boom())


def _pr(files: list[ChangedFile]) -> PRContext:
    return PRContext(
        repository="acme/example",
        pr_number=7,
        base_sha="a" * 7,
        head_sha="b" * 7,
        base_ref="main",
        head_ref="feature/x",
        changed_files=files,
    )


def _state(
    workspace: Path, *, files: list[ChangedFile], baseline: str | None = None
) -> ReviewState:
    return ReviewState(
        pr=_pr(files),
        workspace=str(workspace),
        cost_baseline_path=baseline,
    )


@pytest.fixture(autouse=True)
def _no_usage_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    # The cost lens auto-syncs an infracost usage file, which shells out to
    # infracost. Default it off so unit tests don't; the cost tests that care
    # about usage-file threading override this.
    monkeypatch.setattr(cost_mod, "build_synced_usage_file", lambda _wd: None)


# ---------------------------------------------------------------------------
# security
# ---------------------------------------------------------------------------


def test_security_lens_runs_scanners_then_llm(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "main.tf").write_text('resource "aws_s3_bucket" "b" {}\n')
    state = _state(tmp_path, files=[ChangedFile(path="main.tf")])

    tfsec = _FakeTool(
        [Finding(agent="security", severity="high", file="main.tf", rule="tfsec:x", message="raw")]
    )
    checkov = _FakeTool([])
    monkeypatch.setattr(security_mod, "run_tfsec", tfsec)
    monkeypatch.setattr(security_mod, "run_checkov", checkov)
    llm = _patch_llm(
        monkeypatch,
        SpecialistAnnotations(
            annotations=[
                FindingAnnotation(
                    id=0, message="Public S3 bucket", suggestion="Add a bucket policy"
                )
            ]
        ),
    )

    out = SecurityLens().run(state)

    assert tfsec.calls == [{"working_dir": str(tmp_path)}]
    assert checkov.calls == [{"working_dir": str(tmp_path)}]
    findings = out.findings
    assert len(findings) == 1
    f = findings[0]
    assert f.agent == "security"
    # Scanner owns severity/rule; LLM only reworded the message/suggestion.
    assert f.severity == "high"
    assert f.rule == "tfsec:x"
    assert f.message == "Public S3 bucket"
    assert f.suggestion == "Add a bucket policy"
    assert len(llm.calls) == 1
    # The raw scanner finding and the file content are both handed to the LLM.
    human = llm.human
    assert "tfsec:x" in human
    assert "aws_s3_bucket" in human


def test_security_lens_keeps_scanner_text_when_unannotated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A finding the LLM returns no annotation for keeps the scanner's own
    # message/suggestion verbatim — it is never dropped.
    (tmp_path / "main.tf").write_text("resource {}\n")
    state = _state(tmp_path, files=[ChangedFile(path="main.tf")])

    monkeypatch.setattr(
        security_mod,
        "run_tfsec",
        _FakeTool(
            [
                Finding(
                    agent="security",
                    severity="high",
                    file="main.tf",
                    rule="tfsec:x",
                    message="scanner message",
                    suggestion="scanner fix",
                )
            ]
        ),
    )
    monkeypatch.setattr(security_mod, "run_checkov", _FakeTool([]))
    _patch_llm(monkeypatch, SpecialistAnnotations(annotations=[]))

    out = SecurityLens().run(state)

    f = out.findings[0]
    assert f.message == "scanner message"
    assert f.suggestion == "scanner fix"
    assert f.severity == "high"


def test_security_lens_blank_annotation_preserves_scanner_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A blank/whitespace message or suggestion from the LLM means "nothing to
    # add" — it must not erase the scanner's own message/remediation.
    (tmp_path / "main.tf").write_text("resource {}\n")
    state = _state(tmp_path, files=[ChangedFile(path="main.tf")])

    monkeypatch.setattr(
        security_mod,
        "run_tfsec",
        _FakeTool(
            [
                Finding(
                    agent="security",
                    severity="high",
                    file="main.tf",
                    rule="tfsec:x",
                    message="scanner message",
                    suggestion="scanner remediation",
                )
            ]
        ),
    )
    monkeypatch.setattr(security_mod, "run_checkov", _FakeTool([]))
    _patch_llm(
        monkeypatch,
        SpecialistAnnotations(annotations=[FindingAnnotation(id=0, message="   ", suggestion="")]),
    )

    out = SecurityLens().run(state)

    f = out.findings[0]
    assert f.message == "scanner message"
    assert f.suggestion == "scanner remediation"


def test_security_lens_filters_unchanged_file_findings_from_llm_input(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Scanners run repo-wide; in diff mode a finding in an unchanged file must
    # not be fed to the LLM (deterministic pre-filter), only the changed-file one.
    monkeypatch.setattr(settings, "scan_mode", "diff")
    (tmp_path / "main.tf").write_text('resource "aws_s3_bucket" "b" {}\n')
    state = _state(tmp_path, files=[ChangedFile(path="main.tf")])

    tfsec = _FakeTool(
        [
            Finding(
                agent="security", severity="high", file="main.tf", rule="tfsec:changed", message="r"
            ),
            Finding(
                agent="security",
                severity="high",
                file="legacy/old.tf",
                rule="tfsec:unchanged",
                message="r",
            ),
        ]
    )
    monkeypatch.setattr(security_mod, "run_tfsec", tfsec)
    monkeypatch.setattr(security_mod, "run_checkov", _FakeTool([]))
    llm = _patch_llm(
        monkeypatch,
        SpecialistAnnotations(annotations=[FindingAnnotation(id=0, message="ok")]),
    )

    out = SecurityLens().run(state)

    human = llm.human
    assert "tfsec:changed" in human
    assert "tfsec:unchanged" not in human
    assert "legacy/old.tf" not in human
    assert [f.rule for f in out.findings] == ["tfsec:changed"]


def test_security_lens_full_scan_keeps_unchanged_file_findings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # In full mode (the default), a repo-wide finding in an unchanged file is
    # kept — the posture scan reports the whole repo, not just the diff.
    monkeypatch.setattr(settings, "scan_mode", "full")
    (tmp_path / "main.tf").write_text('resource "aws_s3_bucket" "b" {}\n')
    state = _state(tmp_path, files=[ChangedFile(path="main.tf")])

    tfsec = _FakeTool(
        [
            Finding(agent="security", severity="high", file="main.tf", rule="tfsec:a", message="r"),
            Finding(
                agent="security", severity="high", file="legacy/old.tf", rule="tfsec:b", message="r"
            ),
        ]
    )
    monkeypatch.setattr(security_mod, "run_tfsec", tfsec)
    monkeypatch.setattr(security_mod, "run_checkov", _FakeTool([]))
    _patch_llm(monkeypatch, SpecialistAnnotations())

    out = SecurityLens().run(state)

    assert sorted(f.rule for f in out.findings) == ["tfsec:a", "tfsec:b"]


def test_security_lens_discovery_off_ignores_llm_findings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # With discovery disabled (default) the scanners reported nothing, so the
    # LLM is never consulted and no speculative findings leak through.
    (tmp_path / "main.tf").write_text("resource {}\n")
    monkeypatch.setattr(settings, "enable_llm_findings", False)
    _forbid_llm(monkeypatch)
    state = _state(tmp_path, files=[ChangedFile(path="main.tf")])

    monkeypatch.setattr(security_mod, "run_tfsec", _FakeTool([]))
    monkeypatch.setattr(security_mod, "run_checkov", _FakeTool([]))

    assert SecurityLens().run(state).findings == []


def test_security_lens_discovery_on_post_filters_to_changed_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # With discovery enabled the LLM may add `discovered` findings; any that
    # land outside the changed files are stripped by the post-filter (diff mode).
    monkeypatch.setattr(settings, "scan_mode", "diff")
    (tmp_path / "main.tf").write_text("resource {}\n")
    monkeypatch.setattr(settings, "enable_llm_findings", True)
    state = _state(tmp_path, files=[ChangedFile(path="main.tf")])

    monkeypatch.setattr(security_mod, "run_tfsec", _FakeTool([]))
    monkeypatch.setattr(security_mod, "run_checkov", _FakeTool([]))
    _patch_llm(
        monkeypatch,
        SpecialistAnnotations(
            discovered=[
                LLMFinding(severity="high", file="main.tf", rule="security:llm-1", message="real"),
                LLMFinding(
                    severity="high", file="other/untouched.tf", rule="security:llm-2", message="x"
                ),
            ]
        ),
    )

    out = SecurityLens().run(state)

    assert [f.file for f in out.findings] == ["main.tf"]
    assert [f.rule for f in out.findings] == ["security:llm-1"]


def test_security_lens_discovery_namespaces_rule_ids(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A discovered finding cannot masquerade as scanner output: its rule is
    # coerced into the `security:llm-` namespace regardless of what the LLM
    # returned (here a scanner-looking id and a bare slug).
    (tmp_path / "main.tf").write_text("resource {}\n")
    monkeypatch.setattr(settings, "enable_llm_findings", True)
    state = _state(tmp_path, files=[ChangedFile(path="main.tf")])

    monkeypatch.setattr(security_mod, "run_tfsec", _FakeTool([]))
    monkeypatch.setattr(security_mod, "run_checkov", _FakeTool([]))
    _patch_llm(
        monkeypatch,
        SpecialistAnnotations(
            discovered=[
                LLMFinding(severity="high", file="main.tf", rule="tfsec:fake", message="spoof"),
                LLMFinding(severity="low", file="main.tf", rule="public-bucket", message="bare"),
                LLMFinding(
                    severity="low", file="main.tf", rule="security:llm-ok", message="already ok"
                ),
            ]
        ),
    )

    out = SecurityLens().run(state)

    rules = [f.rule for f in out.findings]
    assert rules == ["security:llm-fake", "security:llm-public-bucket", "security:llm-ok"]
    # Nothing leaked through with a scanner namespace.
    assert not any(r.startswith(("tfsec:", "checkov:")) for r in rules)


def test_security_lens_skips_when_no_terraform_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _forbid_llm(monkeypatch)
    tfsec, checkov = _FakeTool([]), _FakeTool([])
    monkeypatch.setattr(security_mod, "run_tfsec", tfsec)
    monkeypatch.setattr(security_mod, "run_checkov", checkov)
    # No Terraform file changed -> nothing to scan or review.
    state = _state(tmp_path, files=[ChangedFile(path="README.md")])

    lens = SecurityLens()
    assert lens.applies_to(state) is False
    assert lens.run(state).findings == []
    assert tfsec.calls == [] and checkov.calls == []


def test_security_lens_runs_scanners_when_payloads_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regression: a large PR whose patches GitHub omitted yields empty payloads,
    but the Terraform files still need scanning. The lens must run scanners and
    surface their findings rather than skipping the whole review."""

    # Terraform file changed but absent on disk with no patch -> payloads empty.
    state = _state(tmp_path, files=[ChangedFile(path="main.tf")])

    tfsec = _FakeTool(
        [Finding(agent="security", severity="high", file="main.tf", rule="tfsec:x", message="raw")]
    )
    monkeypatch.setattr(security_mod, "run_tfsec", tfsec)
    monkeypatch.setattr(security_mod, "run_checkov", _FakeTool([]))
    _patch_llm(
        monkeypatch,
        SpecialistAnnotations(annotations=[FindingAnnotation(id=0, message="ok")]),
    )

    out = SecurityLens().run(state)

    assert tfsec.calls == [{"working_dir": str(tmp_path)}]
    assert [f.rule for f in out.findings] == ["tfsec:x"]


def test_security_lens_tolerates_missing_scanner_binary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "main.tf").write_text("resource {}\n")
    state = _state(tmp_path, files=[ChangedFile(path="main.tf")])

    monkeypatch.setattr(security_mod, "run_tfsec", _FakeTool(ScannerError("tfsec missing")))
    checkov = _FakeTool(
        [Finding(agent="security", severity="low", file="main.tf", rule="checkov:y", message="raw")]
    )
    monkeypatch.setattr(security_mod, "run_checkov", checkov)
    _patch_llm(
        monkeypatch,
        SpecialistAnnotations(annotations=[FindingAnnotation(id=0, message="ok")]),
    )

    out = SecurityLens().run(state)

    # tfsec blew up but the lens still produced checkov-derived findings.
    assert [f.rule for f in out.findings] == ["checkov:y"]


# ---------------------------------------------------------------------------
# cost
# ---------------------------------------------------------------------------


def test_cost_lens_skips_without_api_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Gate is the infracost key: even with a baseline present, no key => skip.
    monkeypatch.setattr(settings, "infracost_api_key", None)
    _forbid_llm(monkeypatch)
    (tmp_path / "main.tf").write_text('resource "aws_instance" "w" {}\n')
    state = _state(tmp_path, files=[ChangedFile(path="main.tf")], baseline=str(tmp_path / "b.json"))

    lens = CostLens()
    assert lens.applies_to(state) is False
    out = lens.run(state)
    assert out.findings == []
    assert out.cost_summary is None


def test_cost_lens_runs_infracost_when_payloads_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regression: empty payloads (large PR, omitted patches) must not suppress
    infracost, which prices the workspace on disk without LLM payloads."""

    monkeypatch.setattr(settings, "infracost_api_key", SecretStr("k"))
    # Terraform file changed but absent on disk with no patch -> payloads empty.
    baseline = tmp_path / "baseline.json"
    baseline.write_text("{}")
    state = _state(tmp_path, files=[ChangedFile(path="main.tf")], baseline=str(baseline))

    infracost = _FakeTool(
        CostReport(
            findings=[
                Finding(
                    agent="cost",
                    severity="medium",
                    file="main.tf",
                    rule="infracost:resource-delta",
                    message="raw delta",
                )
            ],
            summary=CostSummary(total_monthly=26.0, delta_monthly=25.0),
        )
    )
    monkeypatch.setattr(cost_mod, "run_infracost_diff", infracost)
    _patch_llm(
        monkeypatch,
        SpecialistAnnotations(annotations=[FindingAnnotation(id=0, message="+$25/mo")]),
    )

    out = CostLens().run(state)

    assert infracost.calls and infracost.calls[0]["working_dir"] == str(tmp_path)
    assert [f.rule for f in out.findings] == ["infracost:resource-delta"]


def test_cost_lens_runs_infracost_then_llm(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(settings, "infracost_api_key", SecretStr("k"))
    (tmp_path / "main.tf").write_text('resource "aws_instance" "w" {}\n')
    baseline = tmp_path / "baseline.json"
    baseline.write_text("{}")
    state = _state(tmp_path, files=[ChangedFile(path="main.tf")], baseline=str(baseline))

    infracost = _FakeTool(
        CostReport(
            findings=[
                Finding(
                    agent="cost",
                    severity="medium",
                    file="main.tf",
                    rule="infracost:resource-delta",
                    message="raw delta",
                )
            ],
            summary=CostSummary(total_monthly=26.0, delta_monthly=25.0),
        )
    )
    monkeypatch.setattr(cost_mod, "run_infracost_diff", infracost)
    _patch_llm(
        monkeypatch,
        SpecialistAnnotations(
            annotations=[
                FindingAnnotation(
                    id=0,
                    message="+$25/mo for aws_instance.w",
                    suggestion="Use a smaller instance type",
                )
            ]
        ),
    )

    out = CostLens().run(state)

    assert infracost.calls == [
        {
            "working_dir": str(tmp_path),
            "baseline_path": str(baseline),
            "usage_file_path": None,
        }
    ]
    assert [f.agent for f in out.findings] == ["cost"]
    # Scanner owns the severity; the LLM only reworded the message.
    assert out.findings[0].severity == "medium"
    assert out.findings[0].message.startswith("+$25")
    # The absolute total + delta are surfaced via cost_summary.
    assert out.cost_summary == CostSummary(total_monthly=26.0, delta_monthly=25.0)


def test_cost_lens_auto_generates_baseline_when_unset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # No pre-built baseline => the lens builds one from the workspace git history.
    monkeypatch.setattr(settings, "infracost_api_key", SecretStr("k"))
    (tmp_path / "main.tf").write_text('resource "aws_instance" "w" {}\n')
    state = _state(tmp_path, files=[ChangedFile(path="main.tf")], baseline=None)

    monkeypatch.setattr(
        cost_mod,
        "build_infracost_baseline",
        lambda wd, name, usage_file_path=None: "/tmp/generated.json",
    )
    infracost = _FakeTool(
        CostReport(
            findings=[
                Finding(
                    agent="cost",
                    severity="medium",
                    file="main.tf",
                    rule="infracost:resource-delta",
                    message="raw",
                )
            ],
            summary=CostSummary(total_monthly=10.0, delta_monthly=5.0),
        )
    )
    monkeypatch.setattr(cost_mod, "run_infracost_diff", infracost)
    _patch_llm(
        monkeypatch,
        SpecialistAnnotations(annotations=[FindingAnnotation(id=0, message="+$5/mo")]),
    )

    out = CostLens().run(state)

    assert infracost.calls == [
        {
            "working_dir": str(tmp_path),
            "baseline_path": "/tmp/generated.json",
            "usage_file_path": None,
        }
    ]
    assert [f.agent for f in out.findings] == ["cost"]


def test_cost_lens_applies_synced_usage_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The auto-synced usage file is applied to BOTH the base breakdown and the
    # head diff, so usage-based resources are priced and the delta stays
    # apples-to-apples.
    monkeypatch.setattr(settings, "infracost_api_key", SecretStr("k"))
    _forbid_llm(monkeypatch)
    (tmp_path / "main.tf").write_text('resource "aws_instance" "w" {}\n')
    monkeypatch.setattr(cost_mod, "build_synced_usage_file", lambda _wd: "/tmp/usage.yml")
    state = _state(tmp_path, files=[ChangedFile(path="main.tf")], baseline=None)

    baseline_calls: list[dict[str, Any]] = []

    def _fake_baseline(wd: str, name: str, usage_file_path: str | None = None) -> str:
        baseline_calls.append({"working_dir": wd, "usage_file_path": usage_file_path})
        return "/tmp/generated.json"

    monkeypatch.setattr(cost_mod, "build_infracost_baseline", _fake_baseline)
    infracost = _FakeTool(
        CostReport(findings=[], summary=CostSummary(total_monthly=42.0, delta_monthly=0.0))
    )
    monkeypatch.setattr(cost_mod, "run_infracost_diff", infracost)

    CostLens().run(state)

    assert baseline_calls == [{"working_dir": str(tmp_path), "usage_file_path": "/tmp/usage.yml"}]
    assert infracost.calls == [
        {
            "working_dir": str(tmp_path),
            "baseline_path": "/tmp/generated.json",
            "usage_file_path": "/tmp/usage.yml",
        }
    ]


def test_cost_lens_reports_summary_with_no_resource_change(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Cost-neutral PR: no per-resource findings (so no LLM call), but the
    # absolute total is still surfaced via cost_summary.
    monkeypatch.setattr(settings, "infracost_api_key", SecretStr("k"))
    _forbid_llm(monkeypatch)
    (tmp_path / "main.tf").write_text('resource "aws_instance" "w" {}\n')
    baseline = tmp_path / "baseline.json"
    baseline.write_text("{}")
    state = _state(tmp_path, files=[ChangedFile(path="main.tf")], baseline=str(baseline))

    summary = CostSummary(total_monthly=21.90, delta_monthly=0.0)
    monkeypatch.setattr(
        cost_mod, "run_infracost_diff", _FakeTool(CostReport(findings=[], summary=summary))
    )

    out = CostLens().run(state)

    assert out.findings == []
    assert out.cost_summary == summary


def test_cost_lens_discovery_flag_never_invents_cost_findings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Even with enable_llm_findings on, cost has no source of truth for invented
    # dollar amounts, so `discovered` is ignored for the cost lens.
    monkeypatch.setattr(settings, "infracost_api_key", SecretStr("k"))
    monkeypatch.setattr(settings, "enable_llm_findings", True)
    (tmp_path / "main.tf").write_text('resource "aws_instance" "w" {}\n')
    baseline = tmp_path / "baseline.json"
    baseline.write_text("{}")
    state = _state(tmp_path, files=[ChangedFile(path="main.tf")], baseline=str(baseline))

    infracost = _FakeTool(
        CostReport(
            findings=[
                Finding(
                    agent="cost",
                    severity="medium",
                    file="main.tf",
                    rule="infracost:resource-delta",
                    message="raw",
                )
            ],
            summary=CostSummary(total_monthly=26.0, delta_monthly=25.0),
        )
    )
    monkeypatch.setattr(cost_mod, "run_infracost_diff", infracost)
    _patch_llm(
        monkeypatch,
        SpecialistAnnotations(
            annotations=[FindingAnnotation(id=0, message="+$25/mo")],
            discovered=[
                LLMFinding(severity="high", file="main.tf", rule="cost:llm-1", message="invented")
            ],
        ),
    )

    out = CostLens().run(state)

    assert [f.rule for f in out.findings] == ["infracost:resource-delta"]


def test_cost_lens_tolerates_infracost_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(settings, "infracost_api_key", SecretStr("k"))
    _forbid_llm(monkeypatch)
    (tmp_path / "main.tf").write_text('resource "aws_instance" "w" {}\n')
    baseline = tmp_path / "baseline.json"
    baseline.write_text("{}")
    state = _state(tmp_path, files=[ChangedFile(path="main.tf")], baseline=str(baseline))

    monkeypatch.setattr(cost_mod, "run_infracost_diff", _FakeTool(ScannerError("infracost boom")))

    out = CostLens().run(state)
    assert out.findings == []
    assert out.cost_summary is None


# ---------------------------------------------------------------------------
# style
# ---------------------------------------------------------------------------


def test_style_lens_runs_scanners_then_llm(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / "main.tf").write_text("variable x {}\n")
    state = _state(tmp_path, files=[ChangedFile(path="main.tf")])

    tflint = _FakeTool(
        [Finding(agent="style", severity="medium", file="main.tf", rule="tflint:z", message="raw")]
    )
    fmt = _FakeTool([])
    monkeypatch.setattr(style_mod, "run_tflint", tflint)
    monkeypatch.setattr(style_mod, "run_terraform_fmt", fmt)
    _patch_llm(
        monkeypatch,
        SpecialistAnnotations(annotations=[FindingAnnotation(id=0, message="Add type")]),
    )

    out = StyleLens().run(state)

    assert tflint.calls and fmt.calls
    assert [f.agent for f in out.findings] == ["style"]
    # Scanner owns the severity; the LLM cannot downgrade it.
    assert out.findings[0].severity == "medium"
    assert out.findings[0].message == "Add type"


def test_style_lens_skips_when_no_terraform_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _forbid_llm(monkeypatch)
    tflint, fmt = _FakeTool([]), _FakeTool([])
    monkeypatch.setattr(style_mod, "run_tflint", tflint)
    monkeypatch.setattr(style_mod, "run_terraform_fmt", fmt)
    state = _state(tmp_path, files=[ChangedFile(path="README.md")])

    lens = StyleLens()
    assert lens.applies_to(state) is False
    assert lens.run(state).findings == []
    assert tflint.calls == [] and fmt.calls == []


def test_style_lens_runs_scanners_when_payloads_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regression: empty payloads (large PR, omitted patches) must not suppress
    the style scanners, which read the workspace from disk."""

    state = _state(tmp_path, files=[ChangedFile(path="main.tf")])

    tflint = _FakeTool(
        [Finding(agent="style", severity="low", file="main.tf", rule="tflint:z", message="raw")]
    )
    monkeypatch.setattr(style_mod, "run_tflint", tflint)
    monkeypatch.setattr(style_mod, "run_terraform_fmt", _FakeTool([]))
    _patch_llm(
        monkeypatch,
        SpecialistAnnotations(annotations=[FindingAnnotation(id=0, message="ok")]),
    )

    out = StyleLens().run(state)

    assert tflint.calls == [{"working_dir": str(tmp_path)}]
    assert [f.rule for f in out.findings] == ["tflint:z"]


def test_style_lens_pre_filters_unchanged_and_post_filters_discovered(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Pre-filter: an unchanged-file scanner finding never reaches the LLM.
    # Post-filter (discovery on): a discovered finding outside the changed files
    # is stripped from the output. (Both are diff-mode behaviour.)
    monkeypatch.setattr(settings, "scan_mode", "diff")
    (tmp_path / "main.tf").write_text("variable x {}\n")
    monkeypatch.setattr(settings, "enable_llm_findings", True)
    state = _state(tmp_path, files=[ChangedFile(path="main.tf")])

    monkeypatch.setattr(
        style_mod,
        "run_tflint",
        _FakeTool(
            [
                Finding(
                    agent="style", severity="low", file="main.tf", rule="tflint:z", message="r"
                ),
                Finding(
                    agent="style",
                    severity="low",
                    file="legacy/old.tf",
                    rule="tflint:unchanged",
                    message="r",
                ),
            ]
        ),
    )
    monkeypatch.setattr(style_mod, "run_terraform_fmt", _FakeTool([]))
    llm = _patch_llm(
        monkeypatch,
        SpecialistAnnotations(
            annotations=[FindingAnnotation(id=0, message="ok")],
            discovered=[
                LLMFinding(
                    severity="low", file="legacy/old.tf", rule="style:llm-leak", message="leak"
                ),
                LLMFinding(severity="low", file="main.tf", rule="style:llm-1", message="real"),
            ],
        ),
    )

    out = StyleLens().run(state)

    human = llm.human
    assert "tflint:unchanged" not in human
    assert "legacy/old.tf" not in human
    assert sorted(f.file for f in out.findings) == ["main.tf", "main.tf"]
    assert "style:llm-leak" not in [f.rule for f in out.findings]


# ---------------------------------------------------------------------------
# graceful degradation
# ---------------------------------------------------------------------------


def test_annotate_degrades_when_backend_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # AI off (no key / no CLI): the scanner findings pass through un-reworded —
    # same finding set, scanner wording preserved.
    class _Unavailable:
        def available(self) -> bool:
            return False

        def annotate(self, system: str, human: str) -> Any:
            raise AssertionError("must not be called when unavailable")

    monkeypatch.setattr(_annotate, "get_ai_backend", lambda: _Unavailable())

    raw = [Finding(agent="security", severity="high", file="a.tf", rule="r", message="scanner")]
    findings = _annotate.annotate_with_llm("security", raw, [])
    assert [f.message for f in findings] == ["scanner"]
    assert [f.severity for f in findings] == ["high"]


def test_annotate_degrades_when_backend_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A backend failure (network, CLI, parse) must never block the report — the
    # deterministic findings are returned unchanged.
    class _Flaky:
        def available(self) -> bool:
            return True

        def annotate(self, system: str, human: str) -> Any:
            raise RuntimeError("backend exploded")

    monkeypatch.setattr(_annotate, "get_ai_backend", lambda: _Flaky())

    raw = [Finding(agent="security", severity="high", file="a.tf", rule="r", message="scanner")]
    findings = _annotate.annotate_with_llm("security", raw, [])
    assert [(f.rule, f.message, f.severity) for f in findings] == [("r", "scanner", "high")]


def test_annotate_records_error_in_sink_on_backend_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A configured-but-failing backend must record the error so the entrypoint can
    # surface it — while findings still degrade to the scanner set.
    class _Flaky:
        def available(self) -> bool:
            return True

        def annotate(self, system: str, human: str) -> Any:
            raise RuntimeError("400 credit balance too low")

    monkeypatch.setattr(_annotate, "get_ai_backend", lambda: _Flaky())

    sink: list[str] = []
    raw = [Finding(agent="security", severity="high", file="a.tf", rule="r", message="scanner")]
    findings = _annotate.annotate_with_llm("security", raw, [], error_sink=sink)

    assert [f.message for f in findings] == ["scanner"]  # degraded, not lost
    assert len(sink) == 1
    assert "security" in sink[0] and "credit balance" in sink[0]


def test_annotate_no_sink_entry_when_backend_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No key / no CLI is a *choice*, not an error — the sink stays empty so an
    # unconfigured AI never fails the check.
    class _Unavailable:
        def available(self) -> bool:
            return False

        def annotate(self, system: str, human: str) -> Any:
            raise AssertionError("must not be called when unavailable")

    monkeypatch.setattr(_annotate, "get_ai_backend", lambda: _Unavailable())

    sink: list[str] = []
    raw = [Finding(agent="security", severity="high", file="a.tf", rule="r", message="scanner")]
    _annotate.annotate_with_llm("security", raw, [], error_sink=sink)

    assert sink == []


def test_security_lens_propagates_ai_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # The lens surfaces a backend failure via LensResult.ai_errors so the graph
    # can route it to the entrypoint.
    (tmp_path / "main.tf").write_text('resource "aws_s3_bucket" "b" {}\n')
    state = _state(tmp_path, files=[ChangedFile(path="main.tf")])

    monkeypatch.setattr(
        security_mod,
        "run_tfsec",
        _FakeTool(
            [
                Finding(
                    agent="security", severity="high", file="main.tf", rule="tfsec:x", message="r"
                )
            ]
        ),
    )
    monkeypatch.setattr(security_mod, "run_checkov", _FakeTool([]))

    class _Flaky:
        def available(self) -> bool:
            return True

        def annotate(self, system: str, human: str) -> Any:
            raise RuntimeError("boom")

    monkeypatch.setattr(_annotate, "get_ai_backend", lambda: _Flaky())

    out = SecurityLens().run(state)

    assert len(out.ai_errors) == 1 and "security" in out.ai_errors[0]
    # The finding still survives, un-reworded.
    assert [f.rule for f in out.findings] == ["tfsec:x"]


# ---------------------------------------------------------------------------
# whole-codebase LLM review (PR-label trigger)
# ---------------------------------------------------------------------------


def test_annotate_forces_discovery_for_full_review_without_enable_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # llm-full-review must surface discovered findings even with
    # enable_llm_findings off — that's the whole point of the whole-codebase pass.
    monkeypatch.setattr(settings, "enable_llm_findings", False)
    backend = _patch_llm(
        monkeypatch,
        SpecialistAnnotations(
            discovered=[
                LLMFinding(severity="high", file="legacy/old.tf", rule="x", message="real risk")
            ]
        ),
    )

    findings = _annotate.annotate_with_llm(
        "security",
        [],
        [FilePayload(path="legacy/old.tf", mode="full", content="resource {}")],
        full_review=True,
    )

    assert [f.rule for f in findings] == ["security:llm-x"]
    assert [f.file for f in findings] == ["legacy/old.tf"]
    # The system prompt switches to whole-repo wording.
    system = backend.calls[-1][0]
    assert "every Terraform file in the repository" in system


def test_security_lens_full_review_feeds_whole_repo_and_keeps_unchanged_findings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # diff mode + discovery off, but llm-full-review is on: the LLM is fed every
    # .tf in the repo (incl. the unchanged legacy/old.tf) and its discovered
    # finding in that unchanged file survives the (skipped) post-filter.
    monkeypatch.setattr(settings, "scan_mode", "diff")
    monkeypatch.setattr(settings, "enable_llm_findings", False)
    monkeypatch.setattr(settings, "llm_full_review", True)
    (tmp_path / "main.tf").write_text('resource "aws_s3_bucket" "b" {}\n')
    (tmp_path / "legacy").mkdir()
    (tmp_path / "legacy" / "old.tf").write_text('resource "aws_security_group" "open" {}\n')

    state = _state(tmp_path, files=[ChangedFile(path="main.tf")])

    monkeypatch.setattr(security_mod, "run_tfsec", _FakeTool([]))
    monkeypatch.setattr(security_mod, "run_checkov", _FakeTool([]))
    llm = _patch_llm(
        monkeypatch,
        SpecialistAnnotations(
            discovered=[
                LLMFinding(
                    severity="high",
                    file="legacy/old.tf",
                    rule="security:llm-open-sg",
                    message="0.0.0.0/0 ingress",
                )
            ]
        ),
    )

    out = SecurityLens().run(state)

    # The whole repo was fed — including the unchanged file's content.
    assert "legacy/old.tf" in llm.human
    assert "## Terraform files (whole repository)" in llm.human
    # The discovered finding in the unchanged file is kept despite diff mode.
    assert [(f.file, f.rule) for f in out.findings] == [("legacy/old.tf", "security:llm-open-sg")]
