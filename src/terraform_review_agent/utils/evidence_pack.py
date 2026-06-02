"""Evidence pack — a downloadable ✅/◐/○ report from a :class:`FindingsReport`.

Two renderers, both pure + deterministic, built from the same report the PR
comment and SARIF export use:

* :func:`render_evidence_html` — a self-contained HTML page (inline CSS, no
  assets), grouped per standard, with the three-state class + per-finding
  confidence. Prints to PDF from any browser, so it covers the "PDF evidence
  pack" need without a binary dependency.
* :func:`render_evidence_csv` — one row per finding for spreadsheets / ingest.
"""

from __future__ import annotations

import csv
import html
import io

from terraform_review_agent.utils.findings_report import FindingRecord, FindingsReport

_STATE_BADGE: dict[str, str] = {"verified": "✅", "evidence": "◐", "human_only": "○"}
_STATE_LABEL: dict[str, str] = {
    "verified": "Verified",
    "evidence": "Evidence",
    "human_only": "Human only",
}

_CSV_COLUMNS = [
    "id",
    "lens",
    "category",
    "standard",
    "standard_version",
    "control_id",
    "state",
    "severity",
    "confidence",
    "source",
    "rule_id",
    "file",
    "line",
    "evidence",
    "remediation_hint",
]

_CSS = """
body { font-family: -apple-system, Segoe UI, Roboto, sans-serif; margin: 2rem; color: #1a1a1a; }
h1 { margin-bottom: 0.25rem; } .meta { color: #555; font-size: 0.9rem; }
table { border-collapse: collapse; width: 100%; margin: 0.5rem 0 1.5rem; }
th, td { border: 1px solid #ddd; padding: 6px 8px; text-align: left; vertical-align: top;
  font-size: 0.88rem; } th { background: #f5f5f5; }
.sev-critical { color: #b00020; font-weight: 600; } .sev-high { color: #d35400; font-weight: 600; }
.sev-medium { color: #b7950b; } .sev-low, .sev-info { color: #555; }
code { background: #f0f0f0; padding: 0 3px; border-radius: 3px; }
""".strip()


def _group_label(record: FindingRecord) -> str:
    if record.standard:
        version = f" {record.standard_version}" if record.standard_version else ""
        return f"{record.standard}{version}"
    return "Ungrouped findings"


def _grouped(report: FindingsReport) -> dict[str, list[FindingRecord]]:
    """Findings grouped by standard (insertion order preserved for determinism)."""

    groups: dict[str, list[FindingRecord]] = {}
    for f in report.findings:
        groups.setdefault(_group_label(f), []).append(f)
    return groups


def _state_counts(records: list[FindingRecord]) -> str:
    counts = {"verified": 0, "evidence": 0, "human_only": 0}
    for r in records:
        counts[r.state] += 1
    return " · ".join(
        f"{_STATE_BADGE[s]} {counts[s]}" for s in ("verified", "evidence", "human_only")
    )


def render_evidence_html(report: FindingsReport) -> str:
    """Render the self-contained HTML evidence pack."""

    def esc(value: object) -> str:
        return html.escape(str(value), quote=True)

    scan = report.scan
    parts: list[str] = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        f"<title>Assessor evidence pack — {esc(scan.repository)} PR #{scan.pr_number}</title>",
        f"<style>{_CSS}</style></head><body>",
        "<h1>Assessor evidence pack</h1>",
        f'<p class="meta">{esc(scan.repository)} · PR #{scan.pr_number} · '
        f"commit {esc(scan.commit_sha[:12])} · {esc(scan.scan_time)} · "
        f"engine {esc(scan.engine_version)}</p>",
        f"<p>{report.summary.total} finding(s) — {_state_counts(report.findings)}</p>",
    ]

    for label, records in _grouped(report).items():
        parts.append(f"<h2>{esc(label)} — {_state_counts(records)}</h2>")
        parts.append(
            "<table><thead><tr><th>State</th><th>Severity</th><th>Control</th>"
            "<th>Rule</th><th>Location</th><th>Confidence</th><th>Evidence</th>"
            "<th>Remediation</th></tr></thead><tbody>"
        )
        for r in records:
            loc = esc(r.location.file)
            if r.location.line is not None:
                loc += f":{r.location.line}"
            confidence = "—" if r.confidence is None else f"{r.confidence:.2f}"
            parts.append(
                "<tr>"
                f"<td>{_STATE_BADGE[r.state]} {esc(_STATE_LABEL[r.state])}</td>"
                f'<td class="sev-{r.severity}">{esc(r.severity)}</td>'
                f"<td>{esc(r.control_id or '—')}</td>"
                f"<td><code>{esc(r.rule_id)}</code></td>"
                f"<td><code>{loc}</code></td>"
                f"<td>{confidence}</td>"
                f"<td>{esc(r.evidence)}</td>"
                f"<td>{esc(r.remediation_hint or '—')}</td>"
                "</tr>"
            )
        parts.append("</tbody></table>")

    parts.append("</body></html>")
    return "\n".join(parts) + "\n"


def _csv_safe(value: str) -> str:
    """Neutralize spreadsheet formula injection in free-text cells.

    csv quoting handles delimiters, but a cell beginning with ``= + - @`` (or a
    control char) is executed as a formula by Excel/Sheets. Prefix a ``'`` so
    the value is shown literally. (Scanner/LLM text is only semi-trusted.)
    """

    return "'" + value if value[:1] in ("=", "+", "-", "@", "\t", "\r") else value


def render_evidence_csv(report: FindingsReport) -> str:
    """Render one CSV row per finding (RFC-4180 quoting via the csv module)."""

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(_CSV_COLUMNS)
    for f in report.findings:
        writer.writerow(
            [
                f.id,
                f.lens or "",
                f.category,
                _csv_safe(f.standard or ""),
                f.standard_version or "",
                _csv_safe(f.control_id or ""),
                f.state,
                f.severity,
                "" if f.confidence is None else f.confidence,
                f.source,
                _csv_safe(f.rule_id),
                _csv_safe(f.location.file),
                "" if f.location.line is None else f.location.line,
                _csv_safe(f.evidence),
                _csv_safe(f.remediation_hint or ""),
            ]
        )
    return buf.getvalue()
