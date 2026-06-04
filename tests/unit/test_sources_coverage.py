"""Unit tests for the coverage-report parsers (lcov / cobertura / jacoco)."""

from __future__ import annotations

from pathlib import Path

import pytest

from terraform_review_agent.utils.sources.coverage import (
    parse_cobertura,
    parse_coverage_file,
    parse_jacoco,
    parse_lcov,
)

_LCOV = """\
TN:
SF:src/app.py
DA:1,3
DA:2,0
DA:3,5
LF:3
LH:2
end_of_record
SF:src/util.py
DA:1,1
DA:2,1
end_of_record
"""

_COBERTURA = """\
<?xml version="1.0"?>
<coverage line-rate="0.75">
  <packages><package name="app"><classes>
    <class filename="src/app.py">
      <lines>
        <line number="1" hits="3"/>
        <line number="2" hits="0"/>
      </lines>
    </class>
    <class filename="src/app.py">
      <lines>
        <line number="10" hits="0"/>
      </lines>
    </class>
  </classes></package></packages>
</coverage>
"""

_JACOCO = """\
<?xml version="1.0"?>
<report name="app">
  <package name="com/example">
    <sourcefile name="App.java">
      <counter type="BRANCH" missed="2" covered="4"/>
      <counter type="LINE" missed="2" covered="8"/>
    </sourcefile>
  </package>
</report>
"""


def test_parse_lcov_counts_and_uncovered() -> None:
    report = parse_lcov(_LCOV)
    by_path = {f.path: f for f in report.files}
    assert set(by_path) == {"src/app.py", "src/util.py"}
    app = by_path["src/app.py"]
    assert (app.covered_lines, app.total_lines) == (2, 3)
    assert app.uncovered_lines == [2]
    assert app.percent == pytest.approx(66.67)
    # Roll-up across files: 4 covered of 5.
    assert report.covered_lines == 4
    assert report.total_lines == 5
    assert report.percent == pytest.approx(80.0)


def test_parse_cobertura_merges_classes_per_file() -> None:
    report = parse_cobertura(_COBERTURA)
    assert len(report.files) == 1
    f = report.files[0]
    assert f.path == "src/app.py"
    # Two classes on the same file merge: 3 lines total, 1 covered.
    assert (f.covered_lines, f.total_lines) == (1, 3)
    assert sorted(f.uncovered_lines) == [2, 10]


_COBERTURA_REPEATED_LINE = """\
<?xml version="1.0"?>
<coverage>
  <packages><package name="app"><classes>
    <class filename="src/app.py">
      <lines>
        <line number="1" hits="0"/>
        <line number="2" hits="5"/>
      </lines>
    </class>
    <class filename="src/app.py">
      <lines>
        <line number="1" hits="4"/>
        <line number="3" hits="0"/>
      </lines>
    </class>
  </classes></package></packages>
</coverage>
"""

_JACOCO_MULTI_MODULE = """\
<?xml version="1.0"?>
<report name="app">
  <package name="com/example">
    <sourcefile name="App.java">
      <counter type="LINE" missed="2" covered="8"/>
    </sourcefile>
  </package>
  <package name="com/example">
    <sourcefile name="App.java">
      <counter type="LINE" missed="0" covered="5"/>
    </sourcefile>
  </package>
</report>
"""


def test_parse_cobertura_counts_repeated_line_once() -> None:
    # Regression: a line number repeated across <class> blocks must be counted
    # once (not double-counted), and covered if *any* occurrence recorded a hit.
    report = parse_cobertura(_COBERTURA_REPEATED_LINE)
    assert len(report.files) == 1
    f = report.files[0]
    # Distinct lines: 1 (hit in the 2nd block), 2 (hit), 3 (missed) -> 3 total.
    assert (f.covered_lines, f.total_lines) == (2, 3)
    assert f.uncovered_lines == [3]


def test_parse_jacoco_merges_sourcefile_per_path() -> None:
    # Regression: a path appearing in two <sourcefile> entries (multi-module
    # aggregate) is reported once, with its LINE counters summed.
    report = parse_jacoco(_JACOCO_MULTI_MODULE)
    assert len(report.files) == 1
    f = report.files[0]
    assert f.path == "com/example/App.java"
    # 8+5 covered of (2+8)+(0+5) total = 13/15.
    assert (f.covered_lines, f.total_lines) == (13, 15)


def test_parse_jacoco_uses_line_counter() -> None:
    report = parse_jacoco(_JACOCO)
    assert len(report.files) == 1
    f = report.files[0]
    assert f.path == "com/example/App.java"
    assert (f.covered_lines, f.total_lines) == (8, 10)
    # JaCoCo has no per-line data.
    assert f.uncovered_lines == []
    assert f.percent == pytest.approx(80.0)


def test_parse_coverage_file_autodetects(tmp_path: Path) -> None:
    lcov = tmp_path / "coverage.info"
    lcov.write_text(_LCOV)
    assert parse_coverage_file(lcov).percent == pytest.approx(80.0)

    cob = tmp_path / "cobertura.xml"
    cob.write_text(_COBERTURA)
    assert parse_coverage_file(cob).files[0].path == "src/app.py"

    jac = tmp_path / "jacoco.xml"
    jac.write_text(_JACOCO)
    assert parse_coverage_file(jac).files[0].path == "com/example/App.java"


def test_parse_coverage_file_rejects_unknown_xml(tmp_path: Path) -> None:
    bad = tmp_path / "weird.xml"
    bad.write_text("<nonsense/>")
    with pytest.raises(ValueError, match="unrecognized coverage report"):
        parse_coverage_file(bad)


def test_empty_report_is_fully_covered() -> None:
    assert parse_lcov("").percent == 100.0
