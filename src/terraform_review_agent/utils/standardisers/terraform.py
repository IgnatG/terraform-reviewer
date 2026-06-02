"""A1 Terraform Standardiser — diff each touched module against the house standard.

Deterministic, no LLM: for every Terraform module the PR touches (a directory
that still contains at least one ``.tf`` file on disk), check the golden
structure — required files present, and the expected ``terraform {}`` settings
declared — and emit a deviation finding per gap plus one repo-level consistency
score.

Block presence (``required_version`` / ``required_providers`` / ``backend``) is
detected by scanning the module's ``.tf`` text for the token, not by parsing
HCL — enough for a presence check. Deeper module-composition analysis (prefer
modules over raw resources) is its own design spike (build plan Phase 11).
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, Field

from terraform_review_agent.utils.state import Finding, Severity

# A directory is a *module* only if it holds module-defining HCL — files that
# can carry resources and the `terraform {}` block. Value files (`.tfvars` /
# `.tfvars.json`) don't make a directory a module (an `environments/prod/` dir
# of only `.tfvars` is config, not a module), so they're excluded here.
_MODULE_SUFFIXES = (".tf", ".tf.json")

_REQUIRED_VERSION_RE = re.compile(r"\brequired_version\b")
_REQUIRED_PROVIDERS_RE = re.compile(r"\brequired_providers\b")
_BACKEND_RE = re.compile(r'\bbackend\s+"')


class TerraformStandard(BaseModel):
    """Golden house-module structure A1 diffs each module against."""

    id: str
    name: str
    version: str
    source_url: str | None = None
    required_files: list[str] = Field(default_factory=list)
    require_required_version: bool = True
    require_required_providers: bool = True
    require_backend: bool = False
    deviation_severity: Severity = "low"


def _touched_module_dirs(changed_terraform_paths: set[str]) -> list[str]:
    """The distinct parent directories of the PR's changed Terraform files."""

    return sorted({PurePosixPath(p).parent.as_posix() for p in changed_terraform_paths})


def _tf_files_in(dir_path: Path) -> list[Path]:
    """Top-level (non-recursive) module-defining Terraform files in ``dir_path``, sorted."""

    if not dir_path.is_dir():
        return []
    return sorted(
        p for p in dir_path.iterdir() if p.is_file() and p.name.endswith(_MODULE_SUFFIXES)
    )


def _join(module_dir: str, name: str) -> str:
    """Repo-relative posix path of ``name`` within ``module_dir`` ('.' = root)."""

    return name if module_dir == "." else f"{module_dir}/{name}"


def _module_label(module_dir: str) -> str:
    return "(root)" if module_dir == "." else module_dir


def check_modules(
    workspace: str | Path,
    changed_terraform_paths: set[str],
    std: TerraformStandard,
) -> list[Finding]:
    """Emit deviation findings + a consistency score for the touched modules."""

    base = Path(workspace)
    findings: list[Finding] = []
    total_checks = 0
    module_count = 0
    first_representative: str | None = None

    for module_dir in _touched_module_dirs(changed_terraform_paths):
        tf_files = _tf_files_in(base / ("" if module_dir == "." else module_dir))
        # A module the PR only *removed* leaves no .tf on disk; skip it rather
        # than flag every required file as "missing" on a deleted directory.
        if not tf_files:
            continue
        module_count += 1
        label = _module_label(module_dir)
        representative = _join(module_dir, tf_files[0].name)
        if first_representative is None:
            first_representative = representative

        for name in std.required_files:
            total_checks += 1
            if (base / _join(module_dir, name)).is_file():
                continue
            findings.append(
                Finding(
                    agent="terraform-standard",
                    lens="A1",
                    severity=std.deviation_severity,
                    file=_join(module_dir, name),
                    rule=f"{std.id}:missing-file",
                    message=f"Module `{label}` is missing the standard file `{name}`.",
                    suggestion=f"Add `{name}` to bring the module in line with the house standard.",
                )
            )

        text = "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in tf_files)
        for enabled, pattern, rule, what, hint in (
            (
                std.require_required_version,
                _REQUIRED_VERSION_RE,
                "missing-required-version",
                "a `terraform { required_version = ... }` constraint",
                "Pin the Terraform CLI version with a `required_version` constraint.",
            ),
            (
                std.require_required_providers,
                _REQUIRED_PROVIDERS_RE,
                "missing-required-providers",
                "a `terraform { required_providers { ... } }` block",
                "Declare and version-pin providers in a `required_providers` block.",
            ),
            (
                std.require_backend,
                _BACKEND_RE,
                "missing-backend",
                "a `terraform { backend ... }` block",
                "Configure a remote backend with state locking.",
            ),
        ):
            if not enabled:
                continue
            total_checks += 1
            if pattern.search(text):
                continue
            findings.append(
                Finding(
                    agent="terraform-standard",
                    lens="A1",
                    severity=std.deviation_severity,
                    file=representative,
                    rule=f"{std.id}:{rule}",
                    message=f"Module `{label}` does not declare {what}.",
                    suggestion=hint,
                )
            )

    # No on-disk modules touched (e.g. a pure deletion) → nothing to score.
    if module_count == 0 or first_representative is None:
        return []

    passed = total_checks - len(findings)
    pct = round(100 * passed / total_checks) if total_checks else 100
    noun = "module" if module_count == 1 else "modules"
    findings.append(
        Finding(
            agent="terraform-standard",
            lens="A1",
            severity="info",
            file=first_representative,
            rule=f"{std.id}:score",
            message=(
                f"Terraform house-standard ({std.name}): {passed}/{total_checks} checks "
                f"passed across {module_count} {noun} ({pct}%)."
            ),
        )
    )
    return findings
