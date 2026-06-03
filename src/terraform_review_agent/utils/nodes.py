"""Graph nodes for the review fan-out.

The topology is registry-driven:

    start ──► [lens ∥ lens ∥ …] ──► aggregator ──► post_comment

``start`` flags whether the PR touches Terraform at all; ``agent.py`` then fans
out one :func:`lens_node` task per *enabled* lens (via the ``Send`` API). Each
task runs one :class:`~terraform_review_agent.utils.lenses.base.Lens` and appends
its findings to the shared ``findings`` reducer; the cost lens also sets
``cost_summary``. ``aggregator`` (a deferred node, so it waits for every lens
branch) renders the comment + the findings-JSON contract. ``post_comment`` is a
no-op stub — the real sticky-comment upsert happens in ``entrypoint.run``.

The per-lens scanner + LLM logic lives in ``utils.lenses``; this module only
wires lenses into the graph.
"""

from __future__ import annotations

from typing import Any, TypedDict

from terraform_review_agent.config import settings
from terraform_review_agent.utils.findings_report import (
    build_findings_report,
    render_findings_json,
)
from terraform_review_agent.utils.lenses import LENSES_BY_ID
from terraform_review_agent.utils.render import render_comment
from terraform_review_agent.utils.standards import build_active_mapper
from terraform_review_agent.utils.state import Finding, ReviewState


class LensInvocation(TypedDict):
    """Payload carried by a ``Send`` to one :func:`lens_node` task."""

    lens_id: str
    state: ReviewState


def start_node(state: ReviewState) -> dict[str, object]:
    """Branch on whether the PR touches terraform files at all."""

    if not state.pr.has_terraform_changes:
        return {"skipped": True, "skip_reason": "no terraform files changed"}
    return {"skipped": False}


def lens_node(task: LensInvocation) -> dict[str, Any]:
    """Run one registered lens and merge its findings into shared state.

    Invoked once per enabled lens via the ``Send`` fan-out in ``agent.py``. The
    returned ``findings`` concatenate through the ``operator.add`` reducer;
    ``cost_summary`` is set only by the cost lens (the sole writer).
    """

    lens = LENSES_BY_ID[task["lens_id"]]
    result = lens.run(task["state"])
    update: dict[str, Any] = {"findings": result.findings}
    if result.cost_summary is not None:
        update["cost_summary"] = result.cost_summary
    if result.ai_errors:
        update["ai_errors"] = result.ai_errors
    return update


def aggregator_node(state: ReviewState) -> dict[str, str]:
    """Merge the lens findings into the rendered comment + findings report.

    Dedupe/severity-rank/render all live in :mod:`utils.render`; this node feeds
    it the joined findings and the PR context for file:line links. It also builds
    the versioned ``findings.json`` contract (the spine) — a pure serialization;
    the entrypoint writes it to disk.
    """

    findings: list[Finding] = state.all_findings()
    # Map findings to standard controls + three-state classes when rule packs
    # are active (ENABLED_RULE_PACKS); a no-pack mapper leaves the fields null.
    report = build_findings_report(
        pr=state.pr,
        findings=findings,
        cost_summary=state.cost_summary,
        mode=settings.scan_mode,
        mapper=build_active_mapper(),
    )
    # The report's records drive the ✅/◐/○ readiness section in the comment.
    markdown = render_comment(findings, state.pr, state.cost_summary, records=report.findings)
    return {
        "comment_markdown": markdown,
        "findings_report_json": render_findings_json(report),
    }


def post_comment_node(state: ReviewState) -> dict[str, object]:
    """Placeholder — the GitHub sticky-comment upsert happens in ``entrypoint.run``."""

    return {"posted_comment_id": None}
