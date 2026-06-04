"""Hosted-dashboard ingest client — pushes the findings report after each scan.

Surface 3 of the Assessor is the hosted dashboard: per-standard readiness over
time, cross-repo rollups. This client POSTs the same versioned ``findings.json``
contract (Phase 1) to its ingest endpoint so each scan lands in history.

Two deliberate properties:

* **Opt-in.** :meth:`from_settings` returns ``None`` when ``DASHBOARD_INGEST_URL``
  is unset, so the engine's behaviour is unchanged until a dashboard is wired up.
* **Best-effort.** A failed POST is logged and swallowed (:meth:`post_report`
  returns ``False``) — it never raises, so dashboard downtime can't fail a CI run
  or block the PR comment. This mirrors the AI-backend graceful degradation: the
  report always posts; history is a bonus, not a gate.

Uses ``httpx`` directly (like :mod:`github_client`) to keep the dependency
surface narrow and request shapes obvious in tests.
"""

from __future__ import annotations

from typing import Any, Protocol

import httpx
import structlog

from terraform_review_agent.config import Settings, settings
from terraform_review_agent.utils.findings_report import FindingsReport

log = structlog.get_logger(__name__)


class _HTTPTransport(Protocol):
    """Subset of ``httpx.Client`` we depend on — eases unit testing."""

    def request(self, method: str, url: str, *, json: Any | None = ...) -> httpx.Response: ...


class DashboardClient:
    """POSTs a :class:`FindingsReport` to the hosted dashboard ingest endpoint."""

    def __init__(
        self,
        *,
        ingest_url: str,
        api_key: str | None = None,
        transport: _HTTPTransport | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._ingest_url = ingest_url
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "terraform-review-agent/1.0",
        }
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = transport or httpx.Client(headers=headers, timeout=timeout)

    @classmethod
    def from_settings(
        cls,
        settings_obj: Settings | None = None,
        *,
        transport: _HTTPTransport | None = None,
    ) -> DashboardClient | None:
        """Build a client from config, or ``None`` when no ingest URL is set.

        Returning ``None`` (rather than raising) is what makes the dashboard
        opt-in: callers skip the POST entirely when no dashboard is configured.
        """

        cfg = settings_obj or settings
        if not cfg.dashboard_ingest_url:
            return None
        api_key = (
            cfg.dashboard_api_key.get_secret_value() if cfg.dashboard_api_key is not None else None
        )
        return cls(
            ingest_url=cfg.dashboard_ingest_url,
            api_key=api_key,
            transport=transport,
            timeout=float(cfg.dashboard_timeout_seconds),
        )

    def post_report(self, report: FindingsReport) -> bool:
        """POST ``report`` to the ingest endpoint. Returns success; never raises.

        Any failure is logged and swallowed so a dashboard outage can't fail the
        scan or block the PR comment. The broad ``Exception`` catch is deliberate:
        besides ``httpx`` transport errors (connection, timeout, non-2xx), the
        ``model_dump`` serialization or a malformed ingest URL can raise other
        exception types, and the contract here is *never raises* — so everything
        degrades to a logged ``False`` rather than propagating.
        """

        scan = report.scan
        try:
            response = self._client.request(
                "POST", self._ingest_url, json=report.model_dump(mode="json")
            )
            response.raise_for_status()
        except Exception as exc:
            log.warning(
                "dashboard ingest failed; continuing",
                repo=scan.repository,
                pr=scan.pr_number,
                error=str(exc),
            )
            return False
        log.info(
            "posted findings to dashboard",
            repo=scan.repository,
            pr=scan.pr_number,
            findings=report.summary.total,
        )
        return True
