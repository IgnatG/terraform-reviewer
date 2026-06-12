"""LangSmith dataset sync + offline-evaluation runner (opt-in).

Pushes the golden cases to a LangSmith dataset and runs ``client.evaluate`` with
the same evaluators the local runner uses. Entirely opt-in: it needs
``LANGSMITH_API_KEY`` (and a provider key when ``--judge`` is set), so it never
runs in default CI.

Usage::

    python -m evals.langsmith_run --sync          # create/update the dataset
    python -m evals.langsmith_run                  # deterministic experiment
    python -m evals.langsmith_run --judge          # + live-model judges
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

from evals._env import bridge_env_from_settings
from evals.evaluators import (
    DETERMINISTIC_EVALUATORS,
    make_llm_judges,
    summary_json_match,
)
from evals.golden import CASES
from evals.target import langsmith_target

DATASET_NAME = "terraform-review-agent · golden PRs"


def _client() -> Any:
    from langsmith import Client

    bridge_env_from_settings(include_langsmith=True)
    if not os.environ.get("LANGSMITH_API_KEY"):
        raise SystemExit(
            "LANGSMITH_API_KEY is not set. Add it to .env (the app loads .env) or export it, "
            "then re-run with the venv python: ./.venv/Scripts/python -m evals.langsmith_run --sync"
        )
    return Client()


def sync_dataset(client: Any) -> Any:
    """Create the dataset (idempotent) and (re)load the golden examples."""

    if client.has_dataset(dataset_name=DATASET_NAME):
        dataset = client.read_dataset(dataset_name=DATASET_NAME)
    else:
        dataset = client.create_dataset(
            dataset_name=DATASET_NAME,
            description="Recorded Terraform PRs + the deterministic findings they must produce.",
        )
    client.create_examples(
        dataset_id=dataset.id,
        examples=[{"inputs": {"case": case.name}, "outputs": case.reference()} for case in CASES],
    )
    return dataset


def run(*, judge: bool, model: str) -> Any:
    """Run a LangSmith experiment over the golden dataset."""

    client = _client()
    evaluators: list[Any] = [*DETERMINISTIC_EVALUATORS, summary_json_match]
    if judge:
        evaluators += make_llm_judges(model)

    def target(inputs: dict[str, Any]) -> dict[str, Any]:
        return langsmith_target({**inputs, "use_llm": judge})

    return client.evaluate(
        target,
        data=DATASET_NAME,
        evaluators=evaluators,
        experiment_prefix="quality-judge" if judge else "quality-deterministic",
        # MUST stay 1: the target runs the graph behind process-global monkeypatches
        # (recorded scanners + lens selection in evals/_offline.py). Concurrent
        # targets would clobber each other's patches and cross-contaminate findings.
        max_concurrency=1,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="LangSmith quality eval for terraform-review-agent"
    )
    parser.add_argument("--sync", action="store_true", help="create/update the dataset, then exit")
    parser.add_argument("--judge", action="store_true", help="include live-model LLM judges")
    parser.add_argument("--model", default="openai:gpt-4.1", help="judge model (with --judge)")
    args = parser.parse_args(argv)

    if args.sync:
        dataset = sync_dataset(_client())
        print(f"Synced dataset {dataset.id} ({DATASET_NAME}) with {len(CASES)} examples.")
        return 0

    results = run(judge=args.judge, model=args.model)
    print(results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
