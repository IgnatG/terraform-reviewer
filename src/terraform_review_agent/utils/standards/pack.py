"""Rule-pack data model — the versioned, cited unit of the standard-mapping layer.

A rule pack ties raw scanner findings to a **standard** (CIS, GDS, an internal
framework …): it maps a finding's ``{source}:{rule}`` to a **control id**, and
declares **expected artefacts** whose absence is itself a finding (gap
detection). Every pack carries a ``standard_version`` + ``rule_pack_version`` and
a ``source_url`` so each control is cited and re-validatable against the live
standard (§9.1).

Packs are JSON, shipped in ``terraform_review_agent/rule_packs/`` (plus any
external dir set via ``RULE_PACKS_DIR``), and selected per-run by
``ENABLED_RULE_PACKS``.
"""

from __future__ import annotations

from pathlib import PurePosixPath, PureWindowsPath
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from terraform_review_agent.utils.state import Severity

# Three-state classification for a control (mirrors FindingRecord.state):
#   verified  ✅ machine-checked by a deterministic scanner
#   evidence  ◐ surfaced, but a human should confirm
#   human_only ○ not machine-decidable (e.g. "is the accessibility statement accurate")
ControlState = Literal["verified", "evidence", "human_only"]


def gap_rule(pack_id: str, artifact_id: str) -> str:
    """The synthetic rule id a missing-artefact (gap) finding carries.

    Shared by the gap detector (which emits it) and the mapper (which maps it
    back to the artefact's control), so both stay in lock-step.
    """

    return f"gap:{pack_id}-{artifact_id}"


class Control(BaseModel):
    """One control/point within a standard."""

    id: str
    title: str
    source_url: str | None = None
    # Default classification applied to findings mapped to this control.
    state: ControlState = "verified"


class RuleMapping(BaseModel):
    """Maps a scanner finding to a control by exact rule or rule prefix.

    Exactly one of ``rule`` / ``rule_prefix`` should be set; ``rule`` (exact)
    takes precedence over ``rule_prefix`` when both could match a finding.
    """

    control_id: str
    rule: str | None = None
    rule_prefix: str | None = None


class ExpectedArtifact(BaseModel):
    """An artefact whose absence is a (human_only) finding.

    Present if *any* of ``any_of`` exists in the workspace; otherwise the gap
    detector emits a finding mapped to ``control_id``.
    """

    id: str
    control_id: str
    any_of: list[str] = Field(min_length=1)
    severity: Severity = "medium"
    message: str
    suggestion: str | None = None

    @field_validator("any_of")
    @classmethod
    def _workspace_relative(cls, paths: list[str]) -> list[str]:
        """Reject absolute or parent-traversing candidates so gap checks can't
        probe outside the workspace (``base / "/etc/x"`` would escape)."""

        for p in paths:
            if (
                PurePosixPath(p).is_absolute()
                or PureWindowsPath(p).is_absolute()
                or ".." in PurePosixPath(p).parts
            ):
                raise ValueError(f"expected-artefact path must be workspace-relative: {p!r}")
        return paths


class RulePack(BaseModel):
    """A versioned, cited mapping of findings → controls for one standard."""

    id: str
    standard: str
    standard_version: str
    rule_pack_version: str
    source_url: str | None = None
    controls: list[Control] = Field(default_factory=list)
    mappings: list[RuleMapping] = Field(default_factory=list)
    expected_artifacts: list[ExpectedArtifact] = Field(default_factory=list)

    @model_validator(mode="after")
    def _control_refs_resolve(self) -> RulePack:
        """Every mapping/artefact must point at a declared control — otherwise the
        finding's three-state class would silently default to ``verified``."""

        known = {c.id for c in self.controls}
        for m in self.mappings:
            if m.control_id not in known:
                raise ValueError(f"mapping references unknown control_id {m.control_id!r}")
        for artefact in self.expected_artifacts:
            if artefact.control_id not in known:
                raise ValueError(
                    f"expected artefact {artefact.id!r} references unknown "
                    f"control_id {artefact.control_id!r}"
                )
        return self

    def control(self, control_id: str) -> Control | None:
        return next((c for c in self.controls if c.id == control_id), None)
