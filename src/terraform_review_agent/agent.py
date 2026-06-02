"""LangGraph wiring for the Terraform review agent.

Registry-driven topology — one parallel task per enabled lens, then aggregate:

    start ──► [lens ∥ lens ∥ …] ──► aggregator ──► post_comment

``start`` flags whether the PR touches Terraform; :func:`fan_out_to_lenses` then
emits one ``Send`` per enabled lens (from the registry), so adding a lens needs
no graph change. The lens tasks append to the shared ``findings`` reducer; the
aggregator is a **deferred** node, so it runs only once every lens branch has
finished. The nodes live in :mod:`utils.nodes`; ``post_comment`` is a no-op stub
(the real upsert is in ``entrypoint.run``).
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from terraform_review_agent.utils.lenses import enabled_lenses
from terraform_review_agent.utils.nodes import (
    LensInvocation,
    aggregator_node,
    lens_node,
    post_comment_node,
    start_node,
)
from terraform_review_agent.utils.state import ReviewState


def fan_out_to_lenses(state: ReviewState) -> list[Send] | str:
    """Conditional edge: one ``Send`` per enabled lens, or straight to aggregate.

    When no lens applies (e.g. the PR touched no Terraform) there's nothing to
    fan out, so route directly to the deferred aggregator — it still renders the
    (empty) comment + findings report.
    """

    lenses = enabled_lenses(state)
    if not lenses:
        return "aggregator"
    return [Send("lens", LensInvocation(lens_id=lens.id, state=state)) for lens in lenses]


def build_graph() -> StateGraph[ReviewState]:
    graph: StateGraph[ReviewState] = StateGraph(ReviewState)

    graph.add_node("start", start_node)
    # `lens` is reached only via Send with a LensInvocation payload (not the graph
    # ReviewState), which the StateGraph[ReviewState] node typing can't express.
    graph.add_node("lens", lens_node)  # type: ignore[arg-type]
    # Deferred: wait for every fanned-out lens task before aggregating, even when
    # branches differ in length (future multi-node lenses).
    graph.add_node("aggregator", aggregator_node, defer=True)
    graph.add_node("post_comment", post_comment_node)

    graph.add_edge(START, "start")
    graph.add_conditional_edges("start", fan_out_to_lenses, ["lens", "aggregator"])
    graph.add_edge("lens", "aggregator")
    graph.add_edge("aggregator", "post_comment")
    graph.add_edge("post_comment", END)

    return graph


agent = build_graph().compile()
