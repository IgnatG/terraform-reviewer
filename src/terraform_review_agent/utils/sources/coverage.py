"""Coverage-report parsers (lcov / cobertura / jacoco) → a normalized model.

Built for the A3 "Test Coverage & Gap Analyser" lens (Phase 7): it consumes a
:class:`CoverageReport` to rank uncovered critical paths. This module is pure
parsing — no findings are emitted here; the lens decides how to score.

XML inputs are coverage reports produced by the reviewed repo's own CI, which a
fork PR can influence — so they're parsed with :mod:`defusedxml`, which blocks
entity-expansion ("billion laughs") and external-entity/XXE attacks the stdlib
parser is vulnerable to. A blocked document raises a ``ValueError`` subclass,
which the A3 lens already catches and degrades to "no coverage findings".
"""

from __future__ import annotations

from pathlib import Path

import defusedxml.ElementTree as ET
from pydantic import BaseModel, Field


class FileCoverage(BaseModel):
    """Line coverage for a single source file."""

    path: str
    covered_lines: int = 0
    total_lines: int = 0
    uncovered_lines: list[int] = Field(default_factory=list)

    @property
    def percent(self) -> float:
        """Line-coverage percentage (0-100); 100.0 for a file with no measured lines."""

        if self.total_lines <= 0:
            return 100.0
        return round(100.0 * self.covered_lines / self.total_lines, 2)


class CoverageReport(BaseModel):
    """Parsed coverage across files, with a roll-up percentage."""

    files: list[FileCoverage] = Field(default_factory=list)

    @property
    def total_lines(self) -> int:
        return sum(f.total_lines for f in self.files)

    @property
    def covered_lines(self) -> int:
        return sum(f.covered_lines for f in self.files)

    @property
    def percent(self) -> float:
        total = self.total_lines
        if total <= 0:
            return 100.0
        return round(100.0 * self.covered_lines / total, 2)


def parse_lcov(text: str) -> CoverageReport:
    """Parse lcov ``.info`` text (``SF:`` / ``DA:line,hits`` / ``end_of_record``)."""

    files: list[FileCoverage] = []
    path: str | None = None
    covered = 0
    total = 0
    uncovered: list[int] = []

    def _flush() -> None:
        nonlocal path, covered, total, uncovered
        if path is not None:
            files.append(
                FileCoverage(
                    path=Path(path).as_posix(),
                    covered_lines=covered,
                    total_lines=total,
                    uncovered_lines=uncovered,
                )
            )
        path, covered, total, uncovered = None, 0, 0, []

    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("SF:"):
            _flush()
            path = line[3:].strip()
        elif line.startswith("DA:") and path is not None:
            body = line[3:]
            parts = body.split(",")
            if len(parts) < 2:
                continue
            try:
                lineno = int(parts[0])
                hits = int(parts[1])
            except ValueError:
                continue
            total += 1
            if hits > 0:
                covered += 1
            else:
                uncovered.append(lineno)
        elif line == "end_of_record":
            _flush()
    _flush()
    return CoverageReport(files=files)


def _strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _coverage_from_line_hits(path: str, hits_by_line: dict[int, int]) -> FileCoverage:
    """Build a :class:`FileCoverage` from a ``{line_number: max_hits}`` map.

    Each line is counted once (the map is keyed by line number), so a line that
    appeared in several ``<class>`` blocks doesn't inflate the totals.
    """

    covered = sum(1 for hits in hits_by_line.values() if hits > 0)
    uncovered = sorted(n for n, hits in hits_by_line.items() if hits <= 0)
    return FileCoverage(
        path=path,
        covered_lines=covered,
        total_lines=len(hits_by_line),
        uncovered_lines=uncovered,
    )


def parse_cobertura(xml_text: str) -> CoverageReport:
    """Parse Cobertura XML (``<class filename=…><lines><line number= hits=/>``).

    A file may appear as several ``<class>`` elements (e.g. per Python class), and
    a single line number may even repeat across them. Lines are merged *by number*
    — counted once per path and treated as covered if any occurrence recorded a
    hit — so the percentage isn't inflated by double-counting a shared line.
    """

    root = ET.fromstring(xml_text)
    # path -> {line_number: max hit count seen across every <class> for that path}
    by_path: dict[str, dict[int, int]] = {}
    for class_el in root.iter():
        if _strip_ns(class_el.tag) != "class":
            continue
        filename = class_el.get("filename")
        if not filename:
            continue
        lines = by_path.setdefault(Path(filename).as_posix(), {})
        for lines_el in class_el:
            if _strip_ns(lines_el.tag) != "lines":
                continue
            for line_el in lines_el:
                if _strip_ns(line_el.tag) != "line":
                    continue
                try:
                    lineno = int(line_el.get("number", ""))
                    hits = int(line_el.get("hits", "0"))
                except ValueError:
                    continue
                lines[lineno] = max(lines.get(lineno, 0), hits)
    return CoverageReport(
        files=[_coverage_from_line_hits(path, lines) for path, lines in by_path.items()]
    )


def parse_jacoco(xml_text: str) -> CoverageReport:
    """Parse JaCoCo XML (``<sourcefile><counter type="LINE" missed= covered=/>``).

    JaCoCo reports per-file LINE counters but not per-line hit data, so
    ``uncovered_lines`` is left empty. A path that appears in more than one
    ``<sourcefile>`` (multi-module aggregate reports) has its counters summed so
    each path is reported once rather than as duplicate rows.
    """

    root = ET.fromstring(xml_text)
    # path -> [covered, total] summed across every <sourcefile> for that path.
    by_path: dict[str, list[int]] = {}
    for package in root.iter():
        if _strip_ns(package.tag) != "package":
            continue
        pkg_name = package.get("name", "")
        for sourcefile in package:
            if _strip_ns(sourcefile.tag) != "sourcefile":
                continue
            name = sourcefile.get("name")
            if not name:
                continue
            path = Path(pkg_name, name).as_posix() if pkg_name else Path(name).as_posix()
            for counter in sourcefile:
                if _strip_ns(counter.tag) != "counter" or counter.get("type") != "LINE":
                    continue
                try:
                    missed = int(counter.get("missed", "0"))
                    covered = int(counter.get("covered", "0"))
                except ValueError:
                    continue
                acc = by_path.setdefault(path, [0, 0])
                acc[0] += covered
                acc[1] += missed + covered
                break
    return CoverageReport(
        files=[
            FileCoverage(path=path, covered_lines=cov, total_lines=tot)
            for path, (cov, tot) in by_path.items()
        ]
    )


def parse_coverage_file(path: str | Path) -> CoverageReport:
    """Parse a coverage report, auto-detecting lcov / cobertura / jacoco.

    Dispatch is by extension first (``.info``/``.lcov`` → lcov), then by XML root
    element (``<coverage>`` → cobertura, ``<report>`` → jacoco).
    """

    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if p.suffix.lower() in {".info", ".lcov"} or text.lstrip().startswith(("SF:", "TN:")):
        return parse_lcov(text)

    root = ET.fromstring(text)
    root_tag = _strip_ns(root.tag)
    if root_tag == "coverage":
        return parse_cobertura(text)
    if root_tag == "report":
        return parse_jacoco(text)
    raise ValueError(f"unrecognized coverage report format (root <{root_tag}>) at {p}")
