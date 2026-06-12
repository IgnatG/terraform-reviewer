"""CI gate for the agentevals graph-trajectory eval.

Runs the structural (offline, no-API-key) trajectory check over every fixture so a
routing regression — a lens that stops firing, or fans out the wrong number of
times — fails the build. Self-skips when the ``eval`` extra (``agentevals``) is
not installed, so the default ``dev`` install stays lean.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("agentevals")

from evals.run import build_checkpointed_graph, evaluate_case
from evals.trajectories import CASES, TrajectoryCase


@pytest.fixture(scope="module")
def graph() -> object:
    return build_checkpointed_graph()


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_trajectory_routing(case: TrajectoryCase, graph: object, tmp_path: Path) -> None:
    result = evaluate_case(graph, case, tmp_path)
    assert result.passed, f"{case.name}: expected {result.expected}, got {result.actual}"
