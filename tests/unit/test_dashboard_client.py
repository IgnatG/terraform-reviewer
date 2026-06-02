"""Unit tests for :mod:`terraform_review_agent.dashboard_client` (Phase 9).

The client is driven through a fake transport so no real HTTP traffic happens.
We assert the opt-in gate (no URL → no client), the POSTed body matches the
findings contract, the Bearer header is set from the API key, and that a failed
POST is swallowed (returns ``False``, never raises) so dashboard downtime can't
fail a scan.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from terraform_review_agent.config import Settings
from terraform_review_agent.dashboard_client import DashboardClient
from terraform_review_agent.utils.findings_report import FindingsReport, build_findings_report
from terraform_review_agent.utils.state import Finding, PRContext

INGEST = "https://dashboard.example/api/ingest"
FIXED_TIME = datetime(2026, 6, 2, 12, 0, 0, tzinfo=UTC)


@dataclass
class _Call:
    method: str
    url: str
    json: Any | None


@dataclass
class FakeTransport:
    """Records requests and returns a scripted response (or raises)."""

    status: int = 200
    raise_exc: Exception | None = None
    calls: list[_Call] = field(default_factory=list)

    def request(self, method: str, url: str, *, json: Any | None = None) -> httpx.Response:
        self.calls.append(_Call(method=method, url=url, json=json))
        if self.raise_exc is not None:
            raise self.raise_exc
        return httpx.Response(status_code=self.status, request=httpx.Request(method, url))


def _report(findings: list[Finding] | None = None) -> FindingsReport:
    pr = PRContext(
        repository="acme/widgets",
        pr_number=42,
        base_sha="b" * 40,
        head_sha="h" * 40,
        base_ref="main",
        head_ref="feature",
    )
    return build_findings_report(
        pr=pr,
        findings=findings or [],
        cost_summary=None,
        scan_time=FIXED_TIME,
    )


def test_from_settings_returns_none_when_no_ingest_url() -> None:
    # Opt-in: with no dashboard configured, callers skip the POST entirely.
    assert DashboardClient.from_settings(Settings(dashboard_ingest_url=None)) is None


def test_from_settings_builds_client_when_url_set() -> None:
    client = DashboardClient.from_settings(Settings(dashboard_ingest_url=INGEST))
    assert isinstance(client, DashboardClient)


def test_api_key_sets_bearer_header() -> None:
    client = DashboardClient(ingest_url=INGEST, api_key="s3cret")
    assert client._client.headers["Authorization"] == "Bearer s3cret"


def test_no_api_key_omits_authorization_header() -> None:
    client = DashboardClient(ingest_url=INGEST)
    assert "Authorization" not in client._client.headers


def test_post_report_posts_contract_body_and_returns_true() -> None:
    tx = FakeTransport(status=202)
    finding = Finding(
        agent="security", severity="high", file="main.tf", rule="tfsec:AWS017", message="m"
    )
    report = _report([finding])

    ok = DashboardClient(ingest_url=INGEST, transport=tx).post_report(report)

    assert ok is True
    assert len(tx.calls) == 1
    call = tx.calls[0]
    assert call.method == "POST"
    assert call.url == INGEST
    # The body is exactly the findings contract (Phase 1), not a re-shaped payload.
    assert call.json == report.model_dump(mode="json")
    assert call.json["scan"]["repository"] == "acme/widgets"
    assert call.json["summary"]["total"] == 1


def test_post_report_swallows_http_status_error() -> None:
    tx = FakeTransport(status=500)
    ok = DashboardClient(ingest_url=INGEST, transport=tx).post_report(_report())
    assert ok is False  # non-2xx logged, not raised


def test_post_report_swallows_connection_error() -> None:
    tx = FakeTransport(raise_exc=httpx.ConnectError("boom"))
    ok = DashboardClient(ingest_url=INGEST, transport=tx).post_report(_report())
    assert ok is False  # transport failure logged, not raised


def test_post_report_propagates_non_http_errors() -> None:
    # Only httpx failures are best-effort; a programming bug must surface.
    tx = FakeTransport(raise_exc=ValueError("unexpected"))
    with pytest.raises(ValueError):
        DashboardClient(ingest_url=INGEST, transport=tx).post_report(_report())
