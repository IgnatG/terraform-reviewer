"""Coverage-report parsers (lcov / cobertura / jacoco) → a normalized model.

Built for the A3 "Test Coverage & Gap Analyser" lens (Phase 7): it consumes a
:class:`CoverageReport` to rank uncovered critical paths. This module is pure
parsing — no findings are emitted here; the lens decides how to score.

XML inputs are coverage reports produced by the repo's own CI (semi-trusted).
They are parsed with the stdlib XML parser, which does not resolve external
entities; we do not parse arbitrary third-party XML here.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

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


def parse_cobertura(xml_text: str) -> CoverageReport:
    """Parse Cobertura XML (``<class filename=…><lines><line number= hits=/>``).

    A file may appear as several ``<class>`` elements (e.g. per Python class);
    their lines are merged so each path is reported once.
    """

    root = ET.fromstring(xml_text)
    by_path: dict[str, FileCoverage] = {}
    for class_el in root.iter():
        if _strip_ns(class_el.tag) != "class":
            continue
        filename = class_el.get("filename")
        if not filename:
            continue
        fc = by_path.setdefault(
            Path(filename).as_posix(), FileCoverage(path=Path(filename).as_posix())
        )
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
                fc.total_lines += 1
                if hits > 0:
                    fc.covered_lines += 1
                else:
                    fc.uncovered_lines.append(lineno)
    return CoverageReport(files=list(by_path.values()))


def parse_jacoco(xml_text: str) -> CoverageReport:
    """Parse JaCoCo XML (``<sourcefile><counter type="LINE" missed= covered=/>``).

    JaCoCo reports per-file LINE counters but not per-line hit data, so
    ``uncovered_lines`` is left empty.
    """

    root = ET.fromstring(xml_text)
    files: list[FileCoverage] = []
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
                files.append(
                    FileCoverage(
                        path=path,
                        covered_lines=covered,
                        total_lines=missed + covered,
                    )
                )
                break
    return CoverageReport(files=files)


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
