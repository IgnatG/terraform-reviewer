# terraform review
![Python 3.13](https://img.shields.io/badge/Python-3.13-3776AB?logo=python&logoColor=white)
![LangGraph](https://img.shields.io/badge/LangGraph-1C3C3C?logo=langchain&logoColor=white)
![Pydantic v2](https://img.shields.io/badge/Pydantic-v2-E92063?logo=pydantic&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-2496ED?logo=docker&logoColor=white)
![Terraform](https://img.shields.io/badge/Terraform-844FBA?logo=terraform&logoColor=white)
![uv](https://img.shields.io/badge/uv-DE5FE9?logo=uv&logoColor=white)
![Ruff](https://img.shields.io/badge/Ruff-D7FF64?logo=ruff&logoColor=black)
![mypy: strict](https://img.shields.io/badge/mypy-strict-2A6DB2?logo=python&logoColor=white)

**LLM providers:**
![OpenAI](https://img.shields.io/badge/OpenAI-412991?logo=openai&logoColor=white)
![Anthropic](https://img.shields.io/badge/Anthropic-191919?logo=anthropic&logoColor=white)
![Google Gemini](https://img.shields.io/badge/Google%20Gemini-8E75B2?logo=googlegemini&logoColor=white)

A reusable GitHub Actions workflow that reviews Terraform pull requests with a
LangGraph multi-agent system and posts a single, severity-ranked sticky comment.

Pluggable **lenses** run in parallel over the PR's changed Terraform files. Three
run by default:

| Lens | Scanners | Looks for |
|:--|:--|:--|
| 🔒 **Security** | `tfsec` + `checkov` (+ optional Prowler/gitleaks/Trivy SARIF) | misconfigurations, insecure defaults, exposed resources, secrets |
| 💰 **Cost** | `infracost diff` | monthly cost deltas vs. the base branch |
| 🎨 **Style** | `tflint` + `terraform fmt -check` (+ optional MegaLinter SARIF) | lint findings and formatting drift |

Three more are **opt-in** (off by default, deterministic, no AI needed):

| Lens | Enable with | Looks for |
|:--|:--|:--|
| 📋 **Standards** | `ENABLED_RULE_PACKS` | maps findings to standard controls (✅/◐/○) + flags missing README/LICENSE/… via versioned rule packs |
| 🏗️ **Terraform Std** (A1) | `terraform-standard` | golden module structure: required files + `terraform{}` `required_version`/`required_providers` blocks, with a consistency score |
| ⚙️ **CI/CD** (A2) | `cicd-standard` | `.github/workflows` posture: no `pull_request_target`, SHA-pinned actions, least-privilege `permissions`, with a posture score |
| 🧪 **Coverage** (A3) | `coverage-report-path` | changed files below the line-coverage threshold + a repo coverage score |
| 🧹 **Tech Debt** (A4) | `jscpd-report-path` / `sonarqube-sarif-path` | code duplication + Sonar issues on changed files + a tech-debt scorecard |
| 🇬🇧 **GDS** (A5) | `gds-standard` | per-point GDS/TCoP readiness (✅/◐/○) — govuk-frontend, open licence, accessibility statement; rendered points honestly out of scope |

Scanners own *detection and severity*; an LLM only rewords each finding into a
concise, actionable sentence — so the set of findings is deterministic run to
run. Results are merged, de-duplicated, severity-ranked, and upserted as one
comment (edited in place on every push) rather than stacking up. Every finding is
also emitted in a versioned `findings.json` artefact. See
[`examples/README.md`](examples/README.md) for enabling A1/A2.


---

## Quick start

Add a workflow to your repo that calls the reusable workflow. A complete,
commented sample lives in [`examples/example-caller.yml`](examples/example-caller.yml);
the minimal version:

```yaml
# .github/workflows/terraform-review.yml
name: terraform-review

on:
  pull_request:
    types: [opened, synchronize, reopened, ready_for_review]
    paths:
      - "**/*.tf"
      - "**/*.tfvars"
      - "**/*.tf.json"
      - "**/*.tfvars.json"

jobs:
  terraform-review:
    uses: IgnatG/terraform-reviewer/.github/workflows/terraform-review.yml@v1  # or @v1.2 / @v1.2.3
    permissions:
      contents: read          # checkout
      pull-requests: write    # post/edit the sticky comment
      security-events: write  # upload SARIF to the code-scanning tab
    with:
      llm-provider: anthropic
      llm-model: claude-sonnet-4-6
      fail-on-severity: high  # fail the check on any high/critical finding
    secrets:
      anthropic-api-key: ${{ secrets.ANTHROPIC_API_KEY }}
      infracost-api-key: ${{ secrets.INFRACOST_API_KEY }}   # optional; enables cost agent
```

Pin `@v1` (the major float) or a specific release tag such as `@v1.2.3`. The
`paths` filter on the trigger decides whether the job spins up at all; the agent
additionally early-exits if no Terraform files actually changed.

> **Manual re-runs:** `workflow_dispatch` events carry no PR context, so pass a
> `pr-number` input when triggering manually. See the example caller for the
> `github.event.pull_request.number || inputs.pr-number` pattern.

---

## Inputs

| Input | Default | Description |
|:--|:--|:--|
| `llm-provider` | `openai` | `openai` \| `anthropic` \| `google`. |
| `llm-model` | `gpt-4o` | Model id — **must match the provider**. The default suits `openai`; set this when choosing another provider (e.g. `claude-sonnet-4-6`). |
| `fail-on-severity` | `none` | Gate CI when a finding meets/exceeds this floor: `critical` \| `high` \| `medium` \| `low` \| `info` \| `none`. The comment is always posted first; `none` never fails the check. |
| `pr-number` | `""` | PR to review. Defaults to the triggering `pull_request` event; required for `workflow_dispatch` runs. |

## Secrets

| Secret | Required | Description |
|:--|:--|:--|
| `openai-api-key` / `anthropic-api-key` / `google-api-key` | one, matching `llm-provider` | LLM credentials. |
| `infracost-api-key` | optional | Enables the 💰 cost agent. When unset, cost review is skipped (security + style still run). Get a free key at [infracost.io](https://www.infracost.io/). |
| `github-token` | optional | Defaults to the caller's `${{ github.token }}`. Override only if you need broader scope. |

## Permissions

The calling job needs:

```yaml
permissions:
  contents: read          # checkout the PR merge ref
  pull-requests: write     # create/edit the sticky comment
```

---

## Sample comment

> ## Terraform Review Agent
>
> **5 findings** in 3 files — 1 critical, 2 high, 1 medium, 1 low
>
> _By agent:_ 🔒 Security 2 · 💰 Cost 1 · 🎨 Style 2
>
> 💰 **Infracost estimate:** **$520.50/mo** total · **+$120.00/mo** from this PR
>
> ### 🔴 Critical (1)
>
> | Severity | Issue | Location |
> |:--|:--|:--|
> | 🔴 🔒 | **S3 bucket has no server-side encryption configured.** <br> 💡 Add an `aws_s3_bucket_server_side_encryption_configuration` block. <br> <sub>`tfsec:aws-s3-enable-bucket-encryption`</sub> | `modules/s3/main.tf:12` |
>
> ### 🟠 High (2)
>
> | Severity | Issue | Location |
> |:--|:--|:--|
> | 🟠 💰 | **Estimated monthly cost change for `aws_instance.web`: +$120.00** <br> 💡 Consider a smaller instance type or autoscaling. <br> <sub>`infracost:resource-delta`</sub> | `.` |
> | 🟠 🔒 | **S3 bucket access logging is not enabled.** <br> 💡 Enable access logging to an audit bucket. <br> <sub>`checkov:CKV_AWS_18`</sub> | `modules/s3/main.tf:12` |
>
> ### 🟡 Medium (1)
>
> | Severity | Issue | Location |
> |:--|:--|:--|
> | 🟡 🎨 | **variable "region" is declared but never used.** <br> 💡 Remove the unused variable. <br> <sub>`tflint:terraform_unused_declarations`</sub> | `main.tf:9` |
>
> <details><summary>Low &amp; info (1)</summary>
>
> #### 🔵 Low (1)
>
> | Severity | Issue | Location |
> |:--|:--|:--|
> | 🔵 🎨 | **File does not match `terraform fmt` canonical style.** <br> 💡 Run `terraform fmt` locally and commit the result. <br> <sub>`terraform-fmt:unformatted`</sub> | `main.tf` |
>
> </details>

Critical / high / medium findings show inline; `low` and `info` collapse into a
`<details>` block so the comment stays scannable. Each location links to the
exact file and line at the PR head. On the next push, this same comment is
edited in place.

---

## How it works

```
GitHub PR event
  └─► reusable workflow (terraform-review.yml)
        └─► container: ghcr.io/ignatg/terraform-reviewer:v1   (also tagged :v1.x.y · :latest)
              └─► python -m terraform_review_agent.entrypoint
                    └─► LangGraph:
                          start ─► [lens ∥ lens ∥ …] ─► aggregator ─► post_comment
```

- **start** filters the PR to Terraform files and early-exits if none changed.
- **lenses** — the registry fans out one parallel task per enabled lens (the
  default security/cost/style plus any opt-in standards/A1/A2). Scanner lenses
  then have an LLM reword the findings (it cannot change severity, file, line, or
  rule); the deterministic lenses (standards/A1/A2) skip the LLM entirely.
- **aggregator** dedupes by `(file, rule, line)`, severity-ranks, and renders
  the markdown.
- **post_comment** upserts the sticky comment via a hidden HTML marker.

Alongside the comment, every run writes a versioned **`findings.json`** — the
machine-readable output contract (schema:
[`schemas/findings.schema.json`](schemas/findings.schema.json)) — plus a
**SARIF** export (uploaded to the repo's Security → Code scanning tab + inline
PR annotations) and an **evidence pack** (`evidence-pack.html`, prints to PDF, +
`findings.csv`) showing ✅/◐/○ readiness per standard. The reusable workflow
uploads them all as the `terraform-review-findings` artefact. Set
`DASHBOARD_INGEST_URL` (+ `DASHBOARD_API_KEY`) to also POST the `findings.json`
to a hosted dashboard for per-standard readiness history — opt-in and
best-effort (a dashboard outage never fails the scan).

By default the reviewer scans the **whole repo** (`scan-mode: full`; use `diff`
to scope to changed files) and posts an **inline review comment** on each finding
that sits on a changed line (`inline-comments: true`; re-runs are idempotent).
Findings off the diff stay in the sticky comment, which groups repeated rules and
collapses Medium so large result sets stay readable.

Scanner versions are pinned in the container image — bumping one is a
rebuild-image PR in this repo, not an edit to your workflow file.

---

## Local development

Requires Python 3.13 + [uv](https://docs.astral.sh/uv/). Scanners only run
inside the container; the host test suite mocks them.

```bash
make install              # create .venv and sync pinned deps
make fmt lint type test    # format, lint, mypy --strict, pytest
```

See [`CLAUDE.md`](CLAUDE.md) for the full project contract and layout.

### Run the agent against a real PR locally

`make run` executes the CLI **inside the container**, which bundles every
scanner (`terraform` / `tfsec` / `tflint` / `infracost` / `checkov`) — your host
`.venv` does not. For an external repo the entrypoint clones the PR's merge ref
into a scratch dir, scans it, and upserts the sticky comment on that PR, exactly
as the reusable workflow does in CI.

1. **Configure `.env`** (copy `.env.example`). For an end-to-end run you need:

   ```env
   GITHUB_TOKEN=ghp_...            # read access to the repo + write access to its PRs
   DEFAULT_LLM_PROVIDER=anthropic   # openai | anthropic | google
   DEFAULT_LLM_MODEL=claude-sonnet-4-6
   ANTHROPIC_API_KEY=sk-ant-...     # the key matching DEFAULT_LLM_PROVIDER
   INFRACOST_API_KEY=ico-...        # optional — enables the cost agent
   ```

2. **Build the image** (bundles the pinned scanners). Re-run only after a
   dependency or scanner-version bump; `./src` is bind-mounted, so code edits
   need no rebuild:

   ```bash
   make docker-build
   ```

3. **Review a PR.** Point `--repository`/`--pr-number` at any repo your token
   can reach. Using the sample Cloud Run service repo
   [`ignatg/gcp-test-cloudrun-service`](https://github.com/ignatg/gcp-test-cloudrun-service)
   — open (or reuse) a PR there that touches a `.tf` file, then:

   ```bash
   make run ARGS="--repository ignatg/gcp-test-cloudrun-service --pr-number 2"
   ```

   The agent fetches the PR, runs the enabled lenses, and posts/edits the
   sticky comment on PR #2. (You can also set `GITHUB_REPOSITORY` /
   `GITHUB_PR_NUMBER` in `.env` and run `make run` with no `ARGS`.)

> **Notes**
> - The token needs `pull-requests: write` to post the comment and read access
>   to clone the PR; without `INFRACOST_API_KEY` the 💰 cost agent is skipped.
> - To inspect output without posting, point at a PR in a throwaway repo, or
>   review the structured logs the run prints to stderr.

---

## License

[AGPL-3.0-or-later](LICENSE). This fork descends from the MIT-licensed
[`infiniumtek/terraform-review-agent`](https://github.com/infiniumtek/terraform-review-agent);
that upstream copyright notice is preserved in [`NOTICE`](NOTICE).
