"""agentevals graph-trajectory eval runner.

Builds the review graph with an in-memory checkpointer (the production ``agent``
stays checkpointer-free per CLAUDE.md §6), runs each fixture through it offline,
extracts the LangGraph trajectory with **agentevals**, and checks that the right
lens nodes fired.

Two modes:

* **structural** (default) — deterministic, offline, no API key. Compares the
  multiset of executed node names to each fixture's expected counts.
* **judge** (``--judge``) — additionally runs ``create_graph_trajectory_llm_as_judge``
  over the extracted trajectory. Needs a provider key (``OPENAI_API_KEY`` by
  default); off in default CI.

Usage::

    python -m evals.run            # structural only (CI-safe)
    python -m evals.run --judge    # + LLM-as-judge (needs a key)
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver

from evals._offline import offline_review
from evals.trajectories import CASES, TrajectoryCase
from terraform_review_agent.agent import build_graph

# agentevals is an `eval`-extra dependency, not a runtime one — fail loudly with
# the install hint rather than a bare ImportError.
try:
    from agentevals.graph_trajectory.utils import extract_langgraph_trajectory_from_thread
except ImportError as exc:  # pragma: no cover - exercised only without the extra
    raise SystemExit(
        "agentevals is not installed. Install the eval extra:\n"
        "    uv sync --inexact --extra dev --extra eval\n"
        f"(original error: {exc})"
    ) from exc


def build_checkpointed_graph() -> Any:
    """Compile the review graph with an in-memory checkpointer for trajectory capture."""

    return build_graph().compile(checkpointer=InMemorySaver())


def _real_node_counts(trajectory: dict[str, Any]) -> Counter[str]:
    """Counter of executed graph nodes, dropping framework sentinels (``__start__`` …).

    ``trajectory["outputs"]["steps"]`` is a ``list[list[str]]`` (one inner list per
    invocation segment). We flatten it and keep only real node names, so the result
    is independent of how parallel ``Send`` branches are serialised within a step.
    """

    steps: list[list[str]] = trajectory["outputs"]["steps"]
    return Counter(
        node
        for segment in steps
        for node in segment
        if not (node.startswith("__") and node.endswith("__"))
    )


@dataclass
class CaseResult:
    """Outcome of evaluating one trajectory case."""

    name: str
    expected: dict[str, int]
    actual: dict[str, int]
    passed: bool
    judge_score: float | bool | None = None
    judge_reasoning: str | None = None


def evaluate_case(graph: Any, case: TrajectoryCase, workspace: Path) -> CaseResult:
    """Run one case offline, extract its trajectory, and check the node multiset."""

    case.write_workspace(workspace)
    config = {"configurable": {"thread_id": case.name}}
    with offline_review(
        enabled_lenses=case.enabled_lenses, infracost_api_key=case.infracost_api_key
    ):
        graph.invoke(case.state(workspace), config)
    trajectory = extract_langgraph_trajectory_from_thread(graph, config)
    actual = dict(_real_node_counts(trajectory))
    return CaseResult(
        name=case.name,
        expected=dict(case.expected_nodes),
        actual=actual,
        passed=actual == dict(case.expected_nodes),
    )


def _judge_case(graph: Any, case: TrajectoryCase, workspace: Path, model: str) -> CaseResult:
    """Structural check + an LLM-as-judge pass over the same extracted trajectory."""

    from agentevals.graph_trajectory.llm import create_graph_trajectory_llm_as_judge

    result = evaluate_case(graph, case, workspace)
    config = {"configurable": {"thread_id": case.name}}
    trajectory = extract_langgraph_trajectory_from_thread(graph, config)
    judge = create_graph_trajectory_llm_as_judge(model=model)
    verdict = judge(inputs=trajectory["inputs"], outputs=trajectory["outputs"])
    result.judge_score = verdict.get("score")
    result.judge_reasoning = verdict.get("reasoning") or verdict.get("comment")
    return result


def run_all(*, judge: bool = False, model: str = "openai:gpt-4.1") -> list[CaseResult]:
    """Evaluate every fixture; returns one :class:`CaseResult` per case."""

    graph = build_checkpointed_graph()
    results: list[CaseResult] = []
    with tempfile.TemporaryDirectory(prefix="tra-eval-") as tmp:
        for case in CASES:
            workspace = Path(tmp) / case.name
            workspace.mkdir(parents=True, exist_ok=True)
            if judge:
                results.append(_judge_case(graph, case, workspace, model))
            else:
                results.append(evaluate_case(graph, case, workspace))
    return results


def _print_report(results: list[CaseResult]) -> None:
    for r in results:
        status = "PASS" if r.passed else "FAIL"
        print(f"[{status}] {r.name}")
        if not r.passed:
            print(f"        expected nodes: {r.expected}")
            print(f"        actual nodes:   {r.actual}")
        if r.judge_score is not None:
            print(f"        judge: score={r.judge_score} — {r.judge_reasoning}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="agentevals graph-trajectory eval")
    parser.add_argument(
        "--judge",
        action="store_true",
        help="also run the LLM-as-judge trajectory evaluator (needs a provider key)",
    )
    parser.add_argument(
        "--model",
        default="openai:gpt-4.1",
        help="judge model (only used with --judge; default: openai:gpt-4.1)",
    )
    args = parser.parse_args(argv)

    results = run_all(judge=args.judge, model=args.model)
    _print_report(results)
    failed = [r.name for r in results if not r.passed]
    print(f"\n{len(results) - len(failed)}/{len(results)} cases passed.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
