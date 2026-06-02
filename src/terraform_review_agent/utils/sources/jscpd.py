"""jscpd duplication-report parser (JSON) → a normalized model.

Built for the A4 "Tech-Debt Scorecard" lens (Phase 7). Pure parsing — no
findings are emitted here; the lens decides how to score and scope.

jscpd's ``--reporters json`` output carries a ``statistics.total.percentage``
(duplicated-line %) and a ``duplicates`` array of clone pairs.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class Clone(BaseModel):
    """One duplicated block and the location it is cloned from."""

    file: str
    start_line: int | None = None
    lines: int = 0
    other_file: str
    other_start_line: int | None = None


class JscpdReport(BaseModel):
    """Parsed jscpd output: the overall duplication % + the clone list."""

    duplication_percent: float = 0.0
    clones: list[Clone] = Field(default_factory=list)


def _to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def parse_jscpd(data: dict[str, Any]) -> JscpdReport:
    """Normalize a parsed jscpd JSON document into a :class:`JscpdReport`."""

    total = (data.get("statistics") or {}).get("total") or {}
    clones: list[Clone] = []
    for dup in data.get("duplicates") or []:
        first = dup.get("firstFile") or {}
        second = dup.get("secondFile") or {}
        name = first.get("name")
        if not name:
            continue
        clones.append(
            Clone(
                file=Path(str(name)).as_posix(),
                start_line=_to_int(first.get("start")),
                lines=_to_int(dup.get("lines")) or 0,
                other_file=Path(str(second.get("name") or "")).as_posix(),
                other_start_line=_to_int(second.get("start")),
            )
        )
    return JscpdReport(duplication_percent=_to_float(total.get("percentage")), clones=clones)


def parse_jscpd_file(path: str | Path) -> JscpdReport:
    """Read and parse a jscpd JSON report. Raises on missing file / invalid JSON."""

    return parse_jscpd(json.loads(Path(path).read_text(encoding="utf-8")))
