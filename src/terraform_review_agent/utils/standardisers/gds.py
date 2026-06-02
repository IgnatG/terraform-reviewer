"""A5 GDS Readiness Scanner logic (deterministic, honesty-first).

Evaluates a repo against a GDS / Technology-Code-of-Practice points definition,
emitting one finding per point with its three-state class:

* ``dependency`` / ``file`` points are *code-evidenceable* — checked against
  ``package.json`` deps or the presence of an artefact, and reported ✅ verified
  (met → info, unmet → a real finding).
* ``out_of_scope`` points are reported honestly as ◐ evidence (a dedicated
  tool/step can confirm) or ○ human_only (rendered/judgement) — never faked as
  passing.

A score covers only the code-evidenceable points; the out-of-scope ones are
called out separately so the report stays honest about what it did and didn't
check.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from terraform_review_agent.utils.state import Finding, Severity, ThreeState

GDSCheck = Literal["dependency", "file", "out_of_scope"]


class GDSPoint(BaseModel):
    """One GDS/TCoP point and how (or whether) the engine can evidence it."""

    id: str
    title: str
    source_url: str | None = None
    check: GDSCheck
    # Dependency name (for `dependency`) or comma-separated candidate paths (for
    # `file`); unused for `out_of_scope`.
    target: str | None = None
    state: ThreeState
    # Severity when a code-evidenceable point is *not* met.
    severity: Severity = "medium"

    @model_validator(mode="after")
    def _target_required_for_checks(self) -> GDSPoint:
        if self.check != "out_of_scope" and not (self.target and self.target.strip()):
            raise ValueError(f"GDS point {self.id!r} with check {self.check!r} needs a target")
        return self


class GDSDefinition(BaseModel):
    """A versioned, cited set of GDS readiness points."""

    id: str
    name: str
    version: str
    source_url: str | None = None
    points: list[GDSPoint] = Field(default_factory=list)


def _package_deps(workspace: Path) -> set[str]:
    """All dependency names declared in ``package.json`` (deps + devDeps)."""

    pkg = workspace / "package.json"
    if not pkg.is_file():
        return set()
    try:
        data = json.loads(pkg.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    names: set[str] = set()
    for key in ("dependencies", "devDependencies", "peerDependencies"):
        section = data.get(key)
        if isinstance(section, dict):
            names.update(str(k) for k in section)
    return names


def _candidates(target: str) -> list[str]:
    return [c.strip() for c in target.split(",") if c.strip()]


def evaluate_gds(workspace: str | Path, definition: GDSDefinition) -> list[Finding]:
    """Emit a per-point finding (with its three-state class) + a readiness score."""

    base = Path(workspace)
    deps = _package_deps(base)
    findings: list[Finding] = []
    checkable = 0
    met = 0

    for point in definition.points:
        rule = f"gds:{point.id}"
        if point.check == "out_of_scope":
            need = "a dedicated tool/step" if point.state == "evidence" else "manual review"
            findings.append(
                Finding(
                    agent="gds",
                    lens="A5",
                    severity="info",
                    file=".",
                    rule=rule,
                    message=(f"{point.title} — not evaluated by static analysis; requires {need}."),
                    suggestion=point.source_url,
                    state=point.state,
                )
            )
            continue

        checkable += 1
        if point.check == "dependency":
            is_met = point.target in deps
            anchor = "package.json"
        else:  # file
            candidates = _candidates(point.target or "")
            present = [c for c in candidates if (base / c).exists()]
            is_met = bool(present)
            anchor = present[0] if present else (candidates[0] if candidates else ".")

        # A dependency/file point is machine-checked, so its result is always
        # `verified` (✅) regardless of what the definition declared `state` to be
        # — that field only governs how out_of_scope points are classified.
        if is_met:
            met += 1
            findings.append(
                Finding(
                    agent="gds",
                    lens="A5",
                    severity="info",
                    file=anchor,
                    rule=rule,
                    message=f"{point.title} — met.",
                    suggestion=point.source_url,
                    state="verified",
                )
            )
        else:
            findings.append(
                Finding(
                    agent="gds",
                    lens="A5",
                    severity=point.severity,
                    file=anchor,
                    rule=rule,
                    message=f"{point.title} — not met.",
                    suggestion=point.source_url,
                    state="verified",
                )
            )

    out_of_scope = len(definition.points) - checkable
    findings.append(
        Finding(
            agent="gds",
            lens="A5",
            severity="info",
            file=".",
            rule="gds:score",
            message=(
                f"GDS readiness ({definition.name}): {met}/{checkable} code-evidenceable "
                f"points met; {out_of_scope} point(s) need manual/rendered review."
            ),
            state="verified",
        )
    )
    return findings
