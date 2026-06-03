"""Tests for the SARIF-ingestion runners and their wiring into the lenses."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from terraform_review_agent.config import settings
from terraform_review_agent.utils import tools
from terraform_review_agent.utils.lenses import _annotate
from terraform_review_agent.utils.lenses import security as security_mod
from terraform_review_agent.utils.lenses import style as style_mod
from terraform_review_agent.utils.lenses.security import SecurityLens
from terraform_review_agent.utils.lenses.style import StyleLens
from terraform_review_agent.utils.state import (
    ChangedFile,
    Finding,
    PRContext,
    ReviewState,
    SpecialistAnnotations,
)
from terraform_review_agent.utils.tools import (
    ScannerError,
    ScannerNotConfigured,
    run_megalinter,
    run_prowler_iac,
)


class _Empty:
    """A scanner stand-in that reports nothing (isolates the source under test)."""

    def invoke(self, _payload: dict[str, Any]) -> list[Finding]:
        return []


def _sarif(driver: str, file: str, rule: str = "r", level: str = "error") -> dict[str, Any]:
    return {
        "runs": [
            {
                "tool": {"driver": {"name": driver}},
                "results": [
                    {
                        "ruleId": rule,
                        "level": level,
                        "message": {"text": f"{driver} finding"},
                        "locations": [{"physicalLocation": {"artifactLocation": {"uri": file}}}],
                    }
                ],
            }
        ]
    }


def _pr(files: list[ChangedFile]) -> PRContext:
    return PRContext(
        repository="acme/example",
        pr_number=1,
        base_sha="a" * 7,
        head_sha="b" * 7,
        base_ref="main",
        head_ref="feature/x",
        changed_files=files,
    )


def _no_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    # No-op AI backend: keeps every scanner finding with its own wording.
    class _Backend:
        def available(self) -> bool:
            return True

        def annotate(self, system: str, human: str) -> SpecialistAnnotations:
            return SpecialistAnnotations()

    monkeypatch.setattr(_annotate, "get_ai_backend", lambda: _Backend())


# ---------------------------------------------------------------------------
# runners
# ---------------------------------------------------------------------------


def test_runner_skips_when_report_path_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    # An unset path is "not opted in", not a failure — it raises the
    # ScannerNotConfigured subclass so the runner can log it at info, not warning.
    monkeypatch.setattr(settings, "prowler_sarif_path", None)
    with pytest.raises(ScannerNotConfigured, match="PROWLER_SARIF_PATH not set"):
        run_prowler_iac.invoke({"working_dir": "."})


def test_runner_raises_when_report_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # A path that IS set but points nowhere is a real problem (a plain
    # ScannerError, not the not-configured subclass) — worth a warning.
    monkeypatch.setattr(settings, "trivy_sarif_path", str(tmp_path / "nope.sarif"))
    with pytest.raises(ScannerError) as exc_info:
        tools.run_trivy.invoke({"working_dir": str(tmp_path)})
    assert "not found" in str(exc_info.value)
    assert not isinstance(exc_info.value, ScannerNotConfigured)


def test_runner_raises_on_invalid_json(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    bad = tmp_path / "bad.sarif"
    bad.write_text("{not json")
    monkeypatch.setattr(settings, "trivy_sarif_path", str(bad))
    with pytest.raises(ScannerError, match="invalid SARIF JSON"):
        tools.run_trivy.invoke({"working_dir": str(tmp_path)})


def test_trivy_runs_bundled_binary_when_no_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # No TRIVY_SARIF_PATH but the bundled binary is present → run `trivy config`
    # directly and parse its SARIF stdout (the new bundled behaviour).
    monkeypatch.setattr(settings, "trivy_sarif_path", None)
    monkeypatch.setattr(tools.shutil, "which", lambda _n: "/usr/local/bin/trivy")
    captured: dict[str, Any] = {}

    def fake_run(cmd: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        captured["cmd"] = cmd
        sarif = _sarif("Trivy", "main.tf", rule="AVD-AWS-0089")
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(sarif), stderr="")

    monkeypatch.setattr(tools.subprocess, "run", fake_run)

    findings = tools.run_trivy.invoke({"working_dir": str(tmp_path)})

    assert [f.rule for f in findings] == ["trivy:AVD-AWS-0089"]
    assert captured["cmd"][1] == "config" and "sarif" in captured["cmd"]


def test_trivy_skips_when_no_report_and_no_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "trivy_sarif_path", None)
    monkeypatch.setattr(tools.shutil, "which", lambda _n: None)
    with pytest.raises(ScannerNotConfigured, match="trivy"):
        tools.run_trivy.invoke({"working_dir": "."})


def test_prowler_runner_ingests_report(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    report = tmp_path / "prowler.sarif"
    report.write_text(json.dumps(_sarif("prowler", "main.tf", rule="aws_s3_encryption")))
    monkeypatch.setattr(settings, "prowler_sarif_path", str(report))

    findings = run_prowler_iac.invoke({"working_dir": str(tmp_path)})

    assert [f.rule for f in findings] == ["prowler:aws_s3_encryption"]
    assert findings[0].agent == "security"


def test_megalinter_runner_is_style(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    report = tmp_path / "ml.sarif"
    report.write_text(json.dumps(_sarif("yamllint", "ci.yml", rule="indentation", level="warning")))
    monkeypatch.setattr(settings, "megalinter_sarif_path", str(report))

    findings = run_megalinter.invoke({"working_dir": str(tmp_path)})

    assert findings[0].agent == "style"
    assert findings[0].rule == "yamllint:indentation"


# ---------------------------------------------------------------------------
# lens wiring
# ---------------------------------------------------------------------------


def test_security_lens_scopes_ingested_source_to_changed_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Isolate one source (an ingested Trivy report); the rest report nothing.
    # The security lens scopes findings to ALL changed files (not just .tf), so a
    # finding in a changed non-.tf file is kept and an unchanged-file one dropped.
    monkeypatch.setattr(settings, "scan_mode", "diff")  # this test asserts diff-scoping
    for name in ("run_tfsec", "run_checkov", "run_prowler_iac"):
        monkeypatch.setattr(security_mod, name, _Empty())
    _no_llm(monkeypatch)

    report = tmp_path / "trivy.sarif"
    report.write_text(
        json.dumps(
            {
                "runs": [
                    {
                        "tool": {"driver": {"name": "Trivy"}},
                        "results": [
                            {  # in a changed non-.tf file -> kept (changed_paths widening)
                                "ruleId": "AVD-AWS-0001",
                                "level": "error",
                                "message": {"text": "misconfig in app.yaml"},
                                "locations": [
                                    {"physicalLocation": {"artifactLocation": {"uri": "app.yaml"}}}
                                ],
                            },
                            {  # in an unchanged file -> filtered out
                                "ruleId": "AVD-AWS-0001",
                                "level": "error",
                                "message": {"text": "misconfig in untouched file"},
                                "locations": [
                                    {
                                        "physicalLocation": {
                                            "artifactLocation": {"uri": "legacy/old.yaml"}
                                        }
                                    }
                                ],
                            },
                        ],
                    }
                ]
            }
        )
    )
    monkeypatch.setattr(settings, "trivy_sarif_path", str(report))

    # PR changes a .tf (so the lens applies) plus app.yaml (where the finding is).
    state = ReviewState(
        pr=_pr([ChangedFile(path="main.tf"), ChangedFile(path="app.yaml")]),
        workspace=str(tmp_path),
    )

    out = SecurityLens().run(state)

    assert [(f.rule, f.file) for f in out.findings] == [("trivy:AVD-AWS-0001", "app.yaml")]
    assert out.findings[0].agent == "security"


def test_style_lens_ingests_megalinter(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in ("run_tflint", "run_terraform_fmt"):
        monkeypatch.setattr(style_mod, name, _Empty())
    _no_llm(monkeypatch)

    report = tmp_path / "ml.sarif"
    report.write_text(
        json.dumps(_sarif("markdownlint", "README.md", rule="MD013", level="warning"))
    )
    monkeypatch.setattr(settings, "megalinter_sarif_path", str(report))

    state = ReviewState(
        pr=_pr([ChangedFile(path="main.tf"), ChangedFile(path="README.md")]),
        workspace=str(tmp_path),
    )

    out = StyleLens().run(state)

    assert [(f.rule, f.file) for f in out.findings] == [("markdownlint:MD013", "README.md")]
    assert out.findings[0].agent == "style"
