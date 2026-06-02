"""Unit tests for the SARIF → Finding normalizer."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from terraform_review_agent.utils.sources.sarif import parse_sarif


def _sarif(
    driver: str, results: list[dict[str, Any]], rules: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    return {
        "version": "2.1.0",
        "runs": [
            {
                "tool": {"driver": {"name": driver, "rules": rules or []}},
                "results": results,
            }
        ],
    }


def test_security_severity_takes_precedence() -> None:
    data = _sarif(
        "Trivy",
        [
            {
                "ruleId": "CVE-2024-1",
                "level": "note",  # would be "low", but security-severity wins
                "message": {"text": "Vulnerable dependency"},
                "properties": {"security-severity": "9.1"},
                "locations": [
                    {
                        "physicalLocation": {
                            "artifactLocation": {"uri": "go.mod"},
                            "region": {"startLine": 3},
                        }
                    }
                ],
            }
        ],
    )
    findings = parse_sarif(data, ".")
    assert len(findings) == 1
    f = findings[0]
    assert f.severity == "critical"  # 9.1 -> critical
    assert f.rule == "trivy:CVE-2024-1"  # driver name slugged + rule id preserved
    assert f.file == "go.mod"
    assert f.line == 3
    assert f.message == "Vulnerable dependency"


def test_level_mapping_when_no_cvss() -> None:
    levels = {"error": "high", "warning": "medium", "note": "low", "none": "info"}
    for level, expected in levels.items():
        data = _sarif("gitleaks", [{"ruleId": "r", "level": level, "message": {"text": "m"}}])
        assert parse_sarif(data, ".")[0].severity == expected


def test_cvss_band_mapping() -> None:
    bands = {"9.0": "critical", "7.0": "high", "4.0": "medium", "0.1": "low"}
    for score, expected in bands.items():
        data = _sarif(
            "trivy",
            [{"ruleId": "r", "properties": {"security-severity": score}, "message": {"text": "m"}}],
        )
        assert parse_sarif(data, ".")[0].severity == expected


def test_zero_cvss_falls_through_to_level() -> None:
    # A 0.0 security-severity is "no CVSS signal" and must not suppress a real
    # result level (regression: it used to pin the finding to info).
    data = _sarif(
        "trivy",
        [
            {
                "ruleId": "r",
                "level": "error",
                "properties": {"security-severity": "0.0"},
                "message": {"text": "m"},
            }
        ],
    )
    assert parse_sarif(data, ".")[0].severity == "high"


def test_rule_default_configuration_level_fallback() -> None:
    data = _sarif(
        "prowler",
        [{"ruleId": "check_1", "message": {"text": "m"}}],
        rules=[{"id": "check_1", "defaultConfiguration": {"level": "error"}}],
    )
    assert parse_sarif(data, ".")[0].severity == "high"


def test_default_is_medium_when_no_signal() -> None:
    # No security-severity, no result level, no rule default -> SARIF default
    # "warning" -> medium.
    data = _sarif("checkov", [{"ruleId": "CKV_X", "message": {"text": "m"}}])
    assert parse_sarif(data, ".")[0].severity == "medium"


def test_suppressed_results_are_dropped() -> None:
    data = _sarif(
        "gitleaks",
        [
            {"ruleId": "leak", "level": "error", "message": {"text": "real"}},
            {
                "ruleId": "leak",
                "level": "error",
                "message": {"text": "suppressed"},
                "suppressions": [{"kind": "inSource"}],
            },
        ],
    )
    findings = parse_sarif(data, ".")
    assert [f.message for f in findings] == ["real"]


def test_help_uri_becomes_suggestion() -> None:
    data = _sarif(
        "prowler",
        [{"ruleId": "c1", "level": "warning", "message": {"text": "m"}}],
        rules=[{"id": "c1", "helpUri": "https://docs.example/c1"}],
    )
    assert parse_sarif(data, ".")[0].suggestion == "https://docs.example/c1"


def test_missing_rule_id_becomes_unknown() -> None:
    data = _sarif("trivy", [{"level": "error", "message": {"text": "m"}}])
    assert parse_sarif(data, ".")[0].rule == "trivy:unknown"


def test_message_falls_back_to_rule_description_then_id() -> None:
    data = _sarif(
        "tool",
        [{"ruleId": "r1"}],  # no message
        rules=[{"id": "r1", "shortDescription": {"text": "from rule"}}],
    )
    assert parse_sarif(data, ".")[0].message == "from rule"

    data2 = _sarif("tool", [{"ruleId": "r2"}])  # no message, no rule meta
    assert parse_sarif(data2, ".")[0].message == "r2"


def test_file_uri_resolved_relative_to_workspace(tmp_path: Path) -> None:
    target = tmp_path / "modules" / "s3" / "main.tf"
    target.parent.mkdir(parents=True)
    target.write_text("x")
    data = _sarif(
        "trivy",
        [
            {
                "ruleId": "r",
                "level": "error",
                "message": {"text": "m"},
                "locations": [{"physicalLocation": {"artifactLocation": {"uri": target.as_uri()}}}],
            }
        ],
    )
    assert parse_sarif(data, tmp_path)[0].file == "modules/s3/main.tf"


def test_percent_encoded_uri_is_decoded() -> None:
    data = _sarif(
        "tool",
        [
            {
                "ruleId": "r",
                "level": "error",
                "message": {"text": "m"},
                "locations": [
                    {"physicalLocation": {"artifactLocation": {"uri": "modules/my%20dir/main.tf"}}}
                ],
            }
        ],
    )
    assert parse_sarif(data, ".")[0].file == "modules/my dir/main.tf"


def test_multi_run_preserves_each_sub_tool_source() -> None:
    # MegaLinter-style aggregated SARIF: one run per sub-linter; each finding's
    # source must reflect the real linter, not a flat label.
    data = {
        "runs": [
            {
                "tool": {"driver": {"name": "checkov"}},
                "results": [{"ruleId": "CKV_AWS_1", "level": "error", "message": {"text": "a"}}],
            },
            {
                "tool": {"driver": {"name": "yamllint"}},
                "results": [
                    {"ruleId": "indentation", "level": "warning", "message": {"text": "b"}}
                ],
            },
        ]
    }
    findings = parse_sarif(data, ".", category="style")
    assert {f.rule for f in findings} == {"checkov:CKV_AWS_1", "yamllint:indentation"}
    assert all(f.agent == "style" for f in findings)


def test_empty_log_yields_no_findings() -> None:
    assert parse_sarif({}, ".") == []
    assert parse_sarif({"runs": []}, ".") == []
    assert parse_sarif({"runs": [{"tool": {"driver": {"name": "t"}}, "results": []}]}, ".") == []
