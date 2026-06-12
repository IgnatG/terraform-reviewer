"""Golden dataset for the quality eval.

Each :class:`GoldenCase` pairs a PR + *recorded* scanner output with the
deterministic finding identities that the pipeline must emit (the contract the
scanners own: severity/file/line/rule never change run-to-run). This is the data
both the local quality runner (:mod:`evals.run_quality`) and the LangSmith runner
(:mod:`evals.langsmith_run`) evaluate against.

Recorded payloads mirror ``tests/integration/test_graph_end_to_end.py`` so the
golden expectations track the real parsers. Cost is intentionally excluded (it
needs an infracost key + git worktree) — security + style cover the LLM-reword
path that the quality judges care about.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from evals._offline import ScannerOutputs
from terraform_review_agent.utils.state import ChangedFile, PRContext

# --- recorded scanner stdout (the bytes a real scanner prints) ----------------

_TFSEC_JSON = json.dumps(
    {
        "results": [
            {
                "long_id": "aws-s3-enable-bucket-encryption",
                "severity": "CRITICAL",
                "description": "Bucket does not have encryption enabled",
                "resolution": "Enable server-side encryption",
                "location": {"filename": "main.tf", "start_line": 1},
            }
        ]
    }
)

_CHECKOV_JSON = json.dumps(
    {
        "results": {
            "failed_checks": [
                {
                    "check_id": "CKV_AWS_18",
                    "severity": "HIGH",
                    "check_name": "Ensure the S3 bucket has access logging enabled",
                    "guideline": "https://docs.bridgecrew.io/docs/s3-access-logging",
                    "file_path": "main.tf",
                    "file_line_range": [3, 6],
                }
            ]
        }
    }
)

_TFLINT_JSON = json.dumps(
    {
        "issues": [
            {
                "rule": {
                    "name": "terraform_unused_declarations",
                    "severity": "warning",
                    "link": "https://github.com/terraform-linters/tflint",
                },
                "message": 'variable "unused" is declared but not used',
                "range": {"filename": "main.tf", "start": {"line": 9}},
            }
        ]
    }
)

# `terraform fmt -check` prints one path per unformatted file (exit 3); `tflint`
# exits 2 when it finds issues. Both are "ran fine" for the wrappers.
_SECURITY_STYLE_OUTPUTS: ScannerOutputs = {
    "tfsec": (_TFSEC_JSON, 0),
    "checkov": (_CHECKOV_JSON, 0),
    "tflint": (_TFLINT_JSON, 2),
    "terraform": ("main.tf\n", 3),
    "trivy": ('{"runs": []}', 0),
}

_CLEAN_OUTPUTS: ScannerOutputs = {
    "tfsec": ("{}", 0),
    "checkov": ("", 0),
    "tflint": ("{}", 0),
    "terraform": ("", 0),
    "trivy": ('{"runs": []}', 0),
}


@dataclass(frozen=True)
class FindingIdentity:
    """The scanner-owned identity of a finding (stable across reruns + AI on/off)."""

    source: str
    rule_id: str
    severity: str
    file: str
    line: int | None

    @classmethod
    def from_record(cls, record: dict[str, object]) -> FindingIdentity:
        loc = record["location"]
        assert isinstance(loc, dict)
        return cls(
            source=str(record["source"]),
            rule_id=str(record["rule_id"]),
            severity=str(record["severity"]),
            file=str(loc["file"]),
            line=loc.get("line"),  # type: ignore[arg-type]
        )


@dataclass(frozen=True)
class GoldenCase:
    """A PR + recorded scanner output + the finding identities it must produce."""

    name: str
    description: str
    changed_files: tuple[ChangedFile, ...]
    scanner_outputs: ScannerOutputs
    expected_findings: frozenset[FindingIdentity]
    enabled_lenses: str = ""
    expected_severities: dict[str, int] = field(default_factory=dict)

    def pr(self) -> PRContext:
        return PRContext(
            repository="acme/infra",
            pr_number=7,
            base_sha="a" * 40,
            head_sha="b" * 40,
            base_ref="main",
            head_ref=f"eval/{self.name}",
            title=self.description,
            author="eval-harness",
            changed_files=list(self.changed_files),
        )

    def reference(self) -> dict[str, object]:
        """The expected output the evaluators compare against (JSON-serialisable)."""

        identities = sorted(
            ([i.source, i.rule_id, i.severity, i.file, i.line] for i in self.expected_findings),
            key=lambda row: (row[3], row[1]),
        )
        return {
            "identities": identities,
            "summary": {
                "total": sum(self.expected_severities.values()),
                "by_severity": dict(self.expected_severities),
            },
        }


def _tf(path: str) -> ChangedFile:
    return ChangedFile(path=path, patch="@@ -0,0 +1 @@\n+resource {}")


CASES: tuple[GoldenCase, ...] = (
    GoldenCase(
        name="s3_bucket_findings",
        description="S3 bucket PR — tfsec/checkov/tflint/fmt all fire",
        changed_files=(_tf("main.tf"),),
        scanner_outputs=_SECURITY_STYLE_OUTPUTS,
        enabled_lenses="security,style",
        expected_findings=frozenset(
            {
                FindingIdentity(
                    "tfsec", "tfsec:aws-s3-enable-bucket-encryption", "critical", "main.tf", 1
                ),
                FindingIdentity("checkov", "checkov:CKV_AWS_18", "high", "main.tf", 3),
                FindingIdentity(
                    "tflint", "tflint:terraform_unused_declarations", "medium", "main.tf", 9
                ),
                FindingIdentity(
                    "terraform-fmt", "terraform-fmt:unformatted", "low", "main.tf", None
                ),
            }
        ),
        expected_severities={"critical": 1, "high": 1, "medium": 1, "low": 1},
    ),
    GoldenCase(
        name="clean_pr",
        description="Terraform PR with nothing to report",
        changed_files=(_tf("main.tf"),),
        scanner_outputs=_CLEAN_OUTPUTS,
        enabled_lenses="security,style",
        expected_findings=frozenset(),
        expected_severities={},
    ),
)
