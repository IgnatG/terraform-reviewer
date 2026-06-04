"""Unit tests for the Phase 5 wedge lenses (A1 Terraform + A2 CI/CD)."""

from __future__ import annotations

from pathlib import Path

import pytest

from terraform_review_agent.config import settings
from terraform_review_agent.utils.standardisers import (
    CICDBaseline,
    TerraformStandard,
    check_modules,
    check_workflows,
    load_cicd_baseline,
    load_definition,
    load_terraform_standard,
)


def _std(**kw: object) -> TerraformStandard:
    base: dict[str, object] = {
        "id": "terraform-house",
        "name": "House",
        "version": "1.0.0",
        "required_files": ["main.tf", "variables.tf", "outputs.tf"],
        "require_required_version": True,
        "require_required_providers": True,
        "require_backend": False,
    }
    base.update(kw)
    return TerraformStandard.model_validate(base)


def _baseline(**kw: object) -> CICDBaseline:
    base: dict[str, object] = {"id": "ci-baseline", "name": "Baseline", "version": "1.0.0"}
    base.update(kw)
    return CICDBaseline.model_validate(base)


def _by_rule_prefix(findings: list, prefix: str) -> list:  # type: ignore[type-arg]
    return [f for f in findings if f.rule.startswith(prefix)]


# ---------------------------------------------------------------------------
# definition loader
# ---------------------------------------------------------------------------


def test_load_definition_off_when_empty() -> None:
    assert load_definition("", "terraform-house.json", TerraformStandard) is None


def test_load_definition_uses_builtin_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "terraform_standard", "default")
    std = load_terraform_standard()
    assert std is not None and std.id == "terraform-house"
    monkeypatch.setattr(settings, "cicd_standard", "default")
    base = load_cicd_baseline()
    assert base is not None and base.id == "ci-baseline"


def test_load_definition_reads_custom_path(tmp_path: Path) -> None:
    custom = tmp_path / "house.json"
    custom.write_text('{"id":"my-house","name":"Mine","version":"9","required_files":["main.tf"]}')
    std = load_definition(str(custom), "terraform-house.json", TerraformStandard)
    assert std is not None and std.id == "my-house"


def test_load_definition_malformed_is_off_not_fatal(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert load_definition(str(bad), "terraform-house.json", TerraformStandard) is None
    # A path that doesn't exist is also treated as off, not a crash.
    assert load_definition(str(tmp_path / "nope.json"), "x.json", TerraformStandard) is None


# ---------------------------------------------------------------------------
# A1 — terraform module structure
# ---------------------------------------------------------------------------


def test_a1_flags_missing_files_and_blocks(tmp_path: Path) -> None:
    (tmp_path / "main.tf").write_text('resource "aws_s3_bucket" "b" {}')  # no version/providers
    findings = check_modules(tmp_path, {"main.tf"}, _std())

    missing_files = _by_rule_prefix(findings, "terraform-house:missing-file")
    assert {f.file for f in missing_files} == {"variables.tf", "outputs.tf"}
    assert _by_rule_prefix(findings, "terraform-house:missing-required-version")
    assert _by_rule_prefix(findings, "terraform-house:missing-required-providers")
    # Every deviation is A1 / terraform-standard.
    assert all(f.lens == "A1" and f.agent == "terraform-standard" for f in findings)


def test_a1_clean_module_only_emits_score(tmp_path: Path) -> None:
    (tmp_path / "main.tf").write_text(
        'terraform {\n  required_version = ">= 1.5"\n  required_providers {}\n}'
    )
    (tmp_path / "variables.tf").write_text("")
    (tmp_path / "outputs.tf").write_text("")
    findings = check_modules(tmp_path, {"main.tf"}, _std())

    assert len(findings) == 1
    score = findings[0]
    assert score.rule == "terraform-house:score"
    assert score.severity == "info"
    assert "100%" in score.message


def test_a1_score_reflects_partial_compliance(tmp_path: Path) -> None:
    # 3 required files + required_version + required_providers = 5 checks.
    # Only main.tf present, no blocks -> 1/5 passed (main.tf) = 20%.
    (tmp_path / "main.tf").write_text("locals {}")
    findings = check_modules(tmp_path, {"main.tf"}, _std())
    score = next(f for f in findings if f.rule == "terraform-house:score")
    assert "1/5 checks" in score.message and "20%" in score.message


def test_a1_skips_module_with_no_tf_on_disk(tmp_path: Path) -> None:
    # The PR removed the only .tf in modules/gone/ -> nothing on disk to score.
    findings = check_modules(tmp_path, {"modules/gone/main.tf"}, _std())
    assert findings == []


def test_a1_ignores_tfvars_only_directory(tmp_path: Path) -> None:
    # A `.tfvars`-only dir (e.g. environments/prod/) is config, not a module, so
    # it must not be flagged for a missing main.tf / terraform{} block.
    env = tmp_path / "environments" / "prod"
    env.mkdir(parents=True)
    (env / "prod.tfvars").write_text('region = "eu-west-2"\n')
    assert check_modules(tmp_path, {"environments/prod/prod.tfvars"}, _std()) == []


def test_a1_nested_module_paths_are_relative(tmp_path: Path) -> None:
    mod = tmp_path / "modules" / "vpc"
    mod.mkdir(parents=True)
    (mod / "main.tf").write_text("terraform { required_version = 1\n required_providers {} }")
    findings = check_modules(tmp_path, {"modules/vpc/main.tf"}, _std())
    missing = _by_rule_prefix(findings, "terraform-house:missing-file")
    assert {f.file for f in missing} == {"modules/vpc/variables.tf", "modules/vpc/outputs.tf"}


# ---------------------------------------------------------------------------
# A2 — CI/CD workflow posture
# ---------------------------------------------------------------------------


def _write_workflow(tmp_path: Path, name: str, body: str) -> None:
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True, exist_ok=True)
    (wf / name).write_text(body)


def test_a2_flags_pull_request_target_and_missing_permissions(tmp_path: Path) -> None:
    # `on:` parses as the YAML-1.1 boolean True — the check must still see it.
    _write_workflow(
        tmp_path,
        "ci.yml",
        "on:\n  pull_request_target:\njobs:\n  build:\n    steps:\n      - run: echo hi\n",
    )
    findings = check_workflows(tmp_path, _baseline())

    assert _by_rule_prefix(findings, "ci-baseline:pull-request-target")
    assert _by_rule_prefix(findings, "ci-baseline:missing-permissions")
    assert all(f.lens == "A2" and f.agent == "cicd" for f in findings)


def test_a2_flags_unpinned_actions_but_not_sha_pinned(tmp_path: Path) -> None:
    sha = "a" * 40
    _write_workflow(
        tmp_path,
        "ci.yml",
        "on: push\n"
        "permissions:\n  contents: read\n"
        "jobs:\n  build:\n    steps:\n"
        "      - uses: actions/checkout@v4\n"
        f"      - uses: actions/setup-node@{sha}\n"
        "      - uses: ./.github/actions/local\n",
    )
    findings = check_workflows(tmp_path, _baseline())
    unpinned = _by_rule_prefix(findings, "ci-baseline:unpinned-action")

    # Only the @v4 tag is flagged; the SHA-pinned and local actions are fine.
    assert len(unpinned) == 1
    assert "actions/checkout" in unpinned[0].message
    # No PRT / permissions findings here -> score should be high.
    score = next(f for f in findings if f.rule == "ci-baseline:score")
    assert score.severity == "info"


def test_a2_multiple_unpinned_actions_do_not_collapse(tmp_path: Path) -> None:
    _write_workflow(
        tmp_path,
        "ci.yml",
        "on: push\npermissions:\n  contents: read\n"
        "jobs:\n  build:\n    steps:\n"
        "      - uses: actions/checkout@v4\n"
        "      - uses: actions/setup-python@v5\n",
    )
    findings = check_workflows(tmp_path, _baseline())
    unpinned = _by_rule_prefix(findings, "ci-baseline:unpinned-action")
    # Distinct rules (action embedded) so the (file, rule, line) dedupe keeps both.
    assert len({f.rule for f in unpinned}) == 2


def test_a2_flags_unpinned_job_level_reusable_workflow(tmp_path: Path) -> None:
    # Regression: a reusable-workflow call lives at `jobs.<job>.uses` (no steps),
    # not under steps[]. An unpinned ref there must still be flagged.
    _write_workflow(
        tmp_path,
        "deploy.yml",
        "on: push\npermissions:\n  contents: read\n"
        "jobs:\n"
        "  call-pinned:\n    uses: org/repo/.github/workflows/x.yml@" + "a" * 40 + "\n"
        "  call-unpinned:\n    uses: org/repo/.github/workflows/y.yml@v2\n",
    )
    findings = check_workflows(tmp_path, _baseline())
    unpinned = _by_rule_prefix(findings, "ci-baseline:unpinned-action")

    # Only the @v2 reusable-workflow call is flagged; the SHA-pinned one is fine.
    assert len(unpinned) == 1
    assert "org/repo/.github/workflows/y.yml" in unpinned[0].message


def test_a2_malformed_workflow_skipped_not_fatal(tmp_path: Path) -> None:
    _write_workflow(tmp_path, "broken.yml", "this: : : not valid yaml\n  - [")
    # No parseable workflow -> no findings, no crash.
    assert check_workflows(tmp_path, _baseline()) == []


def test_a2_no_workflows_returns_empty(tmp_path: Path) -> None:
    assert check_workflows(tmp_path, _baseline()) == []
