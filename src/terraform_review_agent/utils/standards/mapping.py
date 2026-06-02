"""Map a finding's rule to a standard control, using the active rule packs."""

from __future__ import annotations

from pydantic import BaseModel

from terraform_review_agent.utils.standards.loader import load_active_packs
from terraform_review_agent.utils.standards.pack import (
    ControlState,
    RuleMapping,
    RulePack,
    gap_rule,
)


class StandardMapping(BaseModel):
    """The standard/control a finding maps to, plus its three-state class."""

    standard: str
    standard_version: str
    control_id: str
    rule_pack_version: str
    state: ControlState
    source_url: str | None = None


class StandardMapper:
    """Indexes the active packs' mappings for fast ``rule -> control`` lookup.

    Exact-rule mappings win over prefix mappings; longer prefixes win over
    shorter ones. Expected-artefact (gap) findings are mapped via synthetic
    ``gap:<pack>-<artefact>`` rules so gaps and scanner findings classify the
    same way.
    """

    def __init__(self, packs: list[RulePack]):
        self._exact: dict[str, tuple[RulePack, str]] = {}
        self._prefix: list[tuple[str, RulePack, str]] = []
        for pack in packs:
            for m in pack.mappings:
                self._register(pack, m)
            for artefact in pack.expected_artifacts:
                self._register(
                    pack,
                    RuleMapping(
                        control_id=artefact.control_id, rule=gap_rule(pack.id, artefact.id)
                    ),
                )
        # Longest prefix first so the most specific mapping wins deterministically.
        self._prefix.sort(key=lambda t: len(t[0]), reverse=True)

    def _register(self, pack: RulePack, m: RuleMapping) -> None:
        if m.rule:
            self._exact.setdefault(m.rule, (pack, m.control_id))
        elif m.rule_prefix:
            self._prefix.append((m.rule_prefix, pack, m.control_id))

    def map_rule(self, rule: str) -> StandardMapping | None:
        """Return the control mapping for ``rule`` (e.g. ``checkov:CKV_AWS_18``), or None."""

        hit = self._exact.get(rule)
        if hit is None:
            for prefix, pack, control_id in self._prefix:
                if rule.startswith(prefix):
                    hit = (pack, control_id)
                    break
        if hit is None:
            return None
        pack, control_id = hit
        control = pack.control(control_id)
        return StandardMapping(
            standard=pack.standard,
            standard_version=pack.standard_version,
            control_id=control_id,
            rule_pack_version=pack.rule_pack_version,
            state=control.state if control else "verified",
            source_url=(control.source_url if control else None) or pack.source_url,
        )


def build_active_mapper() -> StandardMapper:
    """A mapper over the packs selected by ``ENABLED_RULE_PACKS`` (empty → maps nothing)."""

    return StandardMapper(load_active_packs())
