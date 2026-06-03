"""Wedge lenses (Phase 5) — A1 Terraform + A2 CI/CD standardisers.

Deterministic, no-LLM lenses that diff a repo against a *golden* definition (a
versioned, cited JSON shipped with the engine or supplied per-repo) and emit
deviation findings plus a consistency score. Unlike the scanner lenses they
flag structure/posture (what *should* be there), so they're repo-level, not
diff-scoped — and inert unless their definition is configured.

This package holds the definition models + check logic; the thin ``Lens``
wrappers live in ``utils/lenses/`` (``terraform_standard.py`` / ``cicd.py``).
"""

from __future__ import annotations

import importlib.resources
from pathlib import Path

import structlog
from pydantic import BaseModel, ValidationError

from terraform_review_agent.config import settings
from terraform_review_agent.utils.standardisers.cicd import CICDBaseline, check_workflows
from terraform_review_agent.utils.standardisers.terraform import (
    TerraformStandard,
    check_modules,
)

log = structlog.get_logger(__name__)

_ANCHOR = "terraform_review_agent"
_DEFS_DIR = "standards_defs"


def load_definition[T: BaseModel](raw: str, builtin_filename: str, model: type[T]) -> T | None:
    """Resolve a golden-standard setting to a validated definition, or ``None``.

    ``raw`` is the config value: empty → off (``None``); ``"default"`` → the
    built-in definition packaged with the engine; anything else → a path to a
    custom definition JSON. A missing/malformed file is logged and treated as
    off rather than crashing the run (mirrors the rule-pack loader's contract).
    """

    value = raw.strip()
    if not value:
        return None
    try:
        if value == "default":
            text = (
                importlib.resources.files(_ANCHOR)
                .joinpath(_DEFS_DIR, builtin_filename)
                .read_text(encoding="utf-8")
            )
        else:
            text = Path(value).read_text(encoding="utf-8")
        return model.model_validate_json(text)
    except (OSError, UnicodeDecodeError, ValidationError) as exc:
        log.warning("standard_def.invalid", setting=raw, error=str(exc))
        return None


def load_terraform_standard() -> TerraformStandard | None:
    """The active A1 house-standard definition (``TERRAFORM_STANDARD``), or None."""

    return load_definition(settings.terraform_standard, "terraform-house.json", TerraformStandard)


def load_cicd_baseline() -> CICDBaseline | None:
    """The active A2 CI/CD baseline definition (``CICD_STANDARD``), or None."""

    return load_definition(settings.cicd_standard, "ci-baseline.json", CICDBaseline)


__all__ = [
    "CICDBaseline",
    "TerraformStandard",
    "check_modules",
    "check_workflows",
    "load_cicd_baseline",
    "load_definition",
    "load_terraform_standard",
]
