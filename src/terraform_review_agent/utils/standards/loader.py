"""Discover + select rule packs.

Built-in packs ship in ``terraform_review_agent/rule_packs/`` (packaged with the
engine); an optional external directory (``RULE_PACKS_DIR``) adds custom/org
packs. ``ENABLED_RULE_PACKS`` selects which are active for a run:

* empty (default) → none active → the standard-mapping layer is inert and
  findings carry no ``control_id`` (behaviour identical to pre-Phase-4).
* ``*`` → every discovered pack.
* a CSV of pack ids → just those.

A malformed pack is logged and skipped rather than failing the whole run.
"""

from __future__ import annotations

import importlib.resources
from pathlib import Path
from typing import Protocol

import structlog
from pydantic import ValidationError

from terraform_review_agent.config import settings
from terraform_review_agent.utils.standards.pack import RulePack

log = structlog.get_logger(__name__)

_BUILTIN_ANCHOR = "terraform_review_agent"
_BUILTIN_DIR = "rule_packs"


class _Readable(Protocol):
    """Anything with a ``read_text`` — a ``Path`` or an importlib Traversable."""

    def read_text(self, encoding: str = ...) -> str: ...


def _safe_load(src: _Readable, origin: str) -> RulePack | None:
    """Read + validate one pack, skipping (with a warning) on any failure.

    The read is inside the guard too, so an unreadable / non-UTF-8 file is
    skipped rather than crashing the run (honours the "malformed → skip" contract).
    """

    try:
        return RulePack.model_validate_json(src.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValidationError) as exc:
        log.warning("rule_pack.invalid", origin=origin, error=str(exc))
        return None


def _builtin_packs() -> list[RulePack]:
    packs: list[RulePack] = []
    root = importlib.resources.files(_BUILTIN_ANCHOR).joinpath(_BUILTIN_DIR)
    if not root.is_dir():
        return packs
    for entry in sorted(root.iterdir(), key=lambda e: e.name):
        if entry.name.endswith(".json"):
            pack = _safe_load(entry, entry.name)
            if pack is not None:
                packs.append(pack)
    return packs


def _external_packs() -> list[RulePack]:
    raw = (settings.rule_packs_dir or "").strip()
    if not raw:
        return []
    base = Path(raw)
    if not base.is_dir():
        log.warning("rule_packs_dir.missing", dir=raw)
        return []
    packs: list[RulePack] = []
    for f in sorted(base.glob("*.json")):
        pack = _safe_load(f, str(f))
        if pack is not None:
            packs.append(pack)
    return packs


def all_packs() -> list[RulePack]:
    """Every discoverable pack (built-in + external dir), regardless of selection."""

    return [*_builtin_packs(), *_external_packs()]


def load_active_packs() -> list[RulePack]:
    """The packs selected by ``ENABLED_RULE_PACKS`` for this run."""

    raw = settings.enabled_rule_packs.strip()
    if not raw:
        return []
    packs = all_packs()
    if raw == "*":
        return packs
    ids = {part.strip() for part in raw.split(",") if part.strip()}
    active = [p for p in packs if p.id in ids]
    # A requested id that matches no discovered pack is almost always a typo or a
    # missing custom pack — and silently resolves to "inert", indistinguishable
    # from the off state. Warn so the misconfiguration is visible.
    unmatched = sorted(ids - {p.id for p in active})
    if unmatched:
        log.warning(
            "rule_pack.unknown_id",
            requested=unmatched,
            available=sorted(p.id for p in packs),
        )
    return active
