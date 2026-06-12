"""Graph-trajectory eval fixtures.

Each :class:`TrajectoryCase` is a PR shape plus the *expected* set of graph nodes
that should execute for it — the routing contract. We assert the **multiset**
(node name → count) of real nodes in the extracted trajectory, not their order:
parallel ``Send`` fan-out emits the ``lens`` node once per enabled lens within a
single superstep with no guaranteed intra-step ordering, so a multiset is the
correct tolerance (see ``docs/langchain-eval-integration-plan.md`` §1.1).

Node names: ``start`` → one ``lens`` per enabled lens → ``aggregator`` →
``post_comment``. Non-Terraform PRs route ``start`` straight to ``aggregator``
(no ``lens``).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from terraform_review_agent.utils.state import ChangedFile, PRContext, ReviewState

# A trivial but valid Terraform file written into each case's workspace so the
# scanner lenses have something on disk to read (content is irrelevant offline).
_STUB_TF = 'resource "null_resource" "noop" {}\n'


@dataclass(frozen=True)
class TrajectoryCase:
    """One PR fixture + the graph nodes expected to run for it."""

    name: str
    description: str
    changed_files: tuple[ChangedFile, ...]
    expected_nodes: dict[str, int]
    enabled_lenses: str = ""
    infracost_api_key: str | None = None

    def pr(self) -> PRContext:
        return PRContext(
            repository="acme/infra",
            pr_number=1,
            base_sha="a" * 40,
            head_sha="b" * 40,
            base_ref="main",
            head_ref=f"eval/{self.name}",
            title=self.description,
            author="eval-harness",
            changed_files=list(self.changed_files),
        )

    def write_workspace(self, root: Path) -> Path:
        """Materialise the changed Terraform files under ``root``; return the dir."""

        for f in self.changed_files:
            if f.path.endswith((".tf", ".tfvars", ".tf.json", ".tfvars.json")):
                target = root / f.path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(_STUB_TF, encoding="utf-8")
        return root

    def state(self, workspace: Path) -> ReviewState:
        return ReviewState(pr=self.pr(), workspace=str(workspace))


def _tf(path: str) -> ChangedFile:
    return ChangedFile(path=path, patch=f"@@ -0,0 +1 @@\n+{_STUB_TF.strip()}")


# The aggregator + post_comment always run; `start` always runs. The variable is
# how many `lens` nodes fan out.
_TAIL = {"start": 1, "aggregator": 1, "post_comment": 1}

CASES: tuple[TrajectoryCase, ...] = (
    TrajectoryCase(
        name="non_terraform_pr",
        description="PR touches no Terraform — start routes straight to the aggregator",
        changed_files=(ChangedFile(path="README.md"),),
        # No lens fans out; start -> aggregator -> post_comment.
        expected_nodes=dict(_TAIL),
    ),
    TrajectoryCase(
        name="security_only",
        description="Single enabled lens — one lens task fans out",
        changed_files=(_tf("main.tf"),),
        enabled_lenses="security",
        expected_nodes={**_TAIL, "lens": 1},
    ),
    TrajectoryCase(
        name="security_and_style",
        description="Two enabled lenses — parallel fan-out emits the lens node twice",
        changed_files=(_tf("main.tf"),),
        enabled_lenses="security,style",
        expected_nodes={**_TAIL, "lens": 2},
    ),
)
