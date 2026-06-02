# Examples — using the Assessor

How to wire up the reviewer and turn on the wedge lenses (A1 / A2).

## Files here

- [`example-caller.yml`](example-caller.yml) — a consumer `pull_request` workflow
  that calls the reusable reviewer. Copy it into your repo's `.github/workflows/`
  and pin `uses:` to a released tag.
- [`terraform-house.json`](terraform-house.json) — a sample **A1** golden
  definition you can copy, edit, and commit to enforce your own house standard.
- [`ci-baseline.json`](ci-baseline.json) — a sample **A2** golden baseline.

## The wedge lenses (A1 + A2)

Both are **deterministic** — no AI key needed — and **off by default**. Each is
enabled by naming a *golden-standard definition*: either `"default"` (a curated
definition shipped with the engine) or a path to a JSON file committed in your
repo.

| Lens | What it checks | Enable with |
|------|----------------|-------------|
| **A1 Terraform Standardiser** | Each touched module has the standard files (`main.tf`, `variables.tf`, …) and declares the expected `terraform { required_version / required_providers }` blocks. Emits a deviation per gap + a per-repo consistency score. | `terraform-standard:` input → `TERRAFORM_STANDARD` |
| **A2 CI/CD Standardiser** | Every `.github/workflows/*.yml` avoids `pull_request_target`, pins third-party actions to a full commit SHA, and sets a least-privilege top-level `permissions:` block. Emits a deviation per issue + a posture score. | `cicd-standard:` input → `CICD_STANDARD` |
| **A3 Coverage** | Changed source files below the line-coverage threshold + a repo coverage score (ingests a coverage report your CI already produces). | `coverage-report-path:` input → `COVERAGE_REPORT_PATH` |
| **A4 Tech-Debt** | Code duplication (jscpd) + Sonar issues on changed files + a tech-debt scorecard. | `jscpd-report-path:` / `sonarqube-sarif-path:` inputs |
| **A5 GDS Readiness** | Per-point GDS/TCoP readiness (✅/◐/○): govuk-frontend, open licence, accessibility statement; rendered/judgement points are reported honestly as out of scope, never faked. | `gds-standard:` input → `GDS_STANDARD` |

> **Outputs.** Every run writes `findings.json`, a SARIF export (uploaded to the
> Security → Code scanning tab), and an evidence pack (`evidence-pack.html`,
> prints to PDF, + `findings.csv`) — all in the `terraform-review-findings`
> artefact. A3/A4 ingest reports your own CI steps produce, so add those steps
> (and a path) before enabling them.

### Enable with the built-in standards

In your caller (see `example-caller.yml`):

```yaml
with:
  terraform-standard: default
  cicd-standard: default
  gds-standard: default # A5 GDS readiness (built-in points)
  # A3/A4 need a report your CI produced first:
  # coverage-report-path: coverage/lcov.info
  # jscpd-report-path: report/jscpd.json
```

### Enable with your own house standard

Commit a definition JSON to your repo (e.g. under `.assessor/`) and point the
input at the **repo-relative path** — the reviewer runs inside your checked-out
PR, so the path resolves against the repo root:

```yaml
with:
  terraform-standard: .assessor/terraform-house.json
  cicd-standard: .assessor/ci-baseline.json
```

Start from the sample files in this folder. The A1 schema:

```json
{
  "id": "terraform-house",
  "name": "House Terraform module standard",
  "version": "1.0.0",
  "source_url": "https://your-internal-standards-page",
  "required_files": ["main.tf", "variables.tf", "outputs.tf", "versions.tf"],
  "require_required_version": true,
  "require_required_providers": true,
  "require_backend": false,
  "deviation_severity": "low"
}
```

The A2 schema:

```json
{
  "id": "ci-baseline",
  "name": "CI/CD pipeline baseline",
  "version": "1.0.0",
  "source_url": "https://your-internal-standards-page",
  "forbid_pull_request_target": true,
  "require_pinned_action_shas": true,
  "require_top_level_permissions": true,
  "pull_request_target_severity": "high",
  "unpinned_action_severity": "medium",
  "missing_permissions_severity": "low"
}
```

## Running locally (CLI)

The engine is a plain CLI; the Action just wraps it. To dry-run a lens against a
checkout, set the env var and invoke the entrypoint:

```bash
export TERRAFORM_STANDARD=default
export CICD_STANDARD=default
python -m terraform_review_agent.entrypoint \
  --repository owner/repo --pr-number 123
# writes ./findings.json — each A1/A2 finding carries "lens": "A1" | "A2"
```

See [`../.env.example`](../.env.example) for the full list of settings.
