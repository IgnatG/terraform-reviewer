# CLAUDE.md

> Project contract for Claude Code / AI coding assistants. Kept in sync with [`AGENTS.md`](AGENTS.md) — same contract, different assistant.

---

## 1. Project

- **Name:** `terraform-review-agent`
- **Goal:** A reusable GitHub Actions workflow that uses a LangGraph multi-agent system to review Terraform pull requests for security, cost, and style issues, posting a single severity-ranked comment.
- **Trigger:** GitHub `pull_request` events (`opened`, `synchronize`, `reopened`, `ready_for_review`) on Terraform-file changes, via a reusable workflow consumed by other repos. Users: PR authors and reviewers. Secondary: `workflow_dispatch` for manual re-runs.
- **Framework:** LangGraph

---

## 2. Stack (fixed — do not change without explicit instruction)

- **Python 3.14** · **uv** package manager · **`.venv` only** (never system Python)
- **Pydantic v2** for all state, tool I/O, and config schemas
- **LangGraph** or **LangChain**
- **SQLite** checkpointer (`langgraph-checkpoint-sqlite`) if keeping state is required
- **LLM providers: OpenAI, Anthropic, Google, Azure OpenAI** — selectable via config; plus an optional **GitHub Copilot** AI backend (reword-only)
- **Docker** (compose) for reproducibility · `structlog` logging · `pytest` · `ruff` · `mypy --strict`

---

## 3. Layout

Standard LangGraph src-layout (<https://docs.langchain.com/oss/python/langgraph/application-structure>):

```text
terraform-review-agent/
├── CLAUDE.md, AGENTS.md, README.md, PLAN.md, .env.example, .gitignore, .python-version
├── pyproject.toml, uv.lock, langgraph.json
├── Dockerfile, docker-compose.yml, Makefile
├── src/terraform_review_agent/
│   ├── agent.py              # exposes compiled `agent` for langgraph.json
│   ├── config.py             # Pydantic Settings (env-driven)
│   ├── entrypoint.py         # CLI invoked by the GitHub Action (I/O boundary)
│   ├── github_client.py      # fetch PR + diff; sticky-comment upsert
│   ├── dashboard_client.py   # opt-in best-effort POST of findings.json to the hosted ingest
│   ├── llm.py                # provider factory (OpenAI/Anthropic/Google)
│   ├── ai/                   # swappable reword-only AI backend (BYOK + Copilot)
│   ├── utils/lenses/         # pluggable review lenses + registry (security/cost/style/standards + A1-A4)
│   ├── utils/sources/        # check-source normalizers: SARIF + coverage (lcov/cobertura/jacoco) + jscpd
│   ├── utils/standards/      # rule packs: finding→control mapping + gap detection (the moat)
│   ├── utils/standardisers/  # golden-standard lenses A1 (terraform) + A2 (cicd): diff + score
│   ├── rule_packs/*.json     # versioned, cited rule packs (shipped with the engine)
│   ├── standards_defs/*.json # golden A1/A2 definitions (house module + CI/CD baseline; shipped)
│   └── utils/{state,nodes,tools,prompts,render,findings_report,sarif_export,evidence_pack}.py
├── schemas/findings.schema.json   # versioned findings-JSON output contract
├── docs/                     # architecture & extension-point notes
├── tests/{unit,integration}/
└── scripts/                  # thin wrappers only — no business logic
```

All importable code lives under `src/terraform_review_agent/`. `agent.py` exposes the compiled graph as a module-level `agent` variable.

---

## 4. Setup & hard rules

```bash
python3.14 -m venv .venv && source .venv/bin/activate
pip install uv
uv sync --inexact --extra dev   # exact versions from uv.lock (--inexact keeps uv/pip)
cp .env.example .env            # fill in keys
```

- ❌ Never `pip install` outside `.venv`
- ❌ Never invoke a bare `python` — always activate `.venv` first
- ❌ Never commit `.env`, `data/*.sqlite`, `.venv/`, `__pycache__`
- 📌 Deps are pinned in `uv.lock` (committed). After editing `pyproject.toml`, run `uv lock` (or `make lock`); `make lint` runs `uv lock --check` and fails on drift.

---

## 5. Required env vars

[`.env.example`](.env.example) is the canonical, commented list — copy it to `.env` and fill in keys. Summary:

```env
AI_BACKEND=byok                      # byok (default) | copilot — reword-only either way
# BYOK: at least one provider key (for the provider selected below)
OPENAI_API_KEY=
ANTHROPIC_API_KEY=
GOOGLE_API_KEY=
AZURE_API_KEY=                       # azure also needs endpoint + deployment
AZURE_OPENAI_ENDPOINT=
AZURE_OPENAI_DEPLOYMENT=
# Copilot backend (only when AI_BACKEND=copilot): needs the Copilot CLI + token
COPILOT_GITHUB_TOKEN=
COPILOT_CLI_COMMAND=copilot

DEFAULT_LLM_PROVIDER=anthropic        # openai | anthropic | google | azure
DEFAULT_LLM_MODEL=claude-sonnet-4-6   # pin a dated snapshot for reproducibility
DEFAULT_LLM_TEMPERATURE=0.0
DEFAULT_LLM_SEED=7                    # best-effort determinism (OpenAI); `none` to disable
ENABLE_LLM_FINDINGS=false            # true lets the LLM discover findings on changed files (less deterministic)
LLM_FULL_REVIEW=false                # true = LLM reviews the whole codebase (every .tf), discovery forced on
ENABLED_LENSES=                      # CSV of lens ids to run; empty = all (security,cost,style)
# External SARIF check sources (empty = skip): prowler/trivy -> security, megalinter -> style
PROWLER_SARIF_PATH=
TRIVY_SARIF_PATH=
MEGALINTER_SARIF_PATH=
ENABLED_RULE_PACKS=                   # empty=none · "*"=all · CSV of pack ids (e.g. terraform-cis-aws)
RULE_PACKS_DIR=                       # optional dir of extra/custom rule packs
# Wedge lenses (A1/A2): empty=off · "default"=built-in golden def · a path=custom JSON
TERRAFORM_STANDARD=                   # A1 Terraform Standardiser (golden module structure)
CICD_STANDARD=                        # A2 CI/CD Standardiser (.github/workflows posture)
# A3/A4 repo-lens signals (each off when unset)
COVERAGE_REPORT_PATH=                 # A3 lcov/cobertura/jacoco (COVERAGE_MIN_PERCENT defaults to 80)
JSCPD_REPORT_PATH=                    # A4 duplication · SONARQUBE_SARIF_PATH for Sonar issues
# Phase 8 outputs (all written each run; CI uploads them)
SARIF_OUTPUT_PATH=./findings.sarif    # also EVIDENCE_HTML_PATH / EVIDENCE_CSV_PATH
# Phase 9 dashboard ingest (opt-in; empty=off). POST is best-effort, never fails the scan.
DASHBOARD_INGEST_URL=                  # also DASHBOARD_API_KEY (Bearer) / DASHBOARD_TIMEOUT_SECONDS
# Phase 10 scope + inline comments.
SCAN_MODE=full                         # full (whole-repo posture) | diff (changed files only)
INLINE_COMMENTS=true                   # post a review comment per finding on a changed line
TFLINT_INIT=false                      # run `tflint --init` (executes repo .tflint.hcl plugins) — off for safety

SQLITE_PATH=./data/state.sqlite

# GitHub access (read PRs, post the sticky comment)
GITHUB_TOKEN=
GITHUB_REPOSITORY=
GITHUB_PR_NUMBER=

# Infracost cost agent (set the key to enable; leave blank to skip)
INFRACOST_API_KEY=
INFRACOST_BASELINE_PATH=

FAIL_ON_SEVERITY=none                # critical | high | medium | low | info | none
FAIL_ON_AI_ERROR=false               # true = fail the check when a configured AI call errored (still posts the report)
WORKSPACE_DIR=.                      # where the PR head is checked out

# Optional observability
LANGSMITH_API_KEY=
LANGSMITH_TRACING=false
LANGSMITH_PROJECT=terraform-review-agent

LOG_LEVEL=INFO
ENVIRONMENT=development               # development | staging | production
```

---

## 6. Patterns (where things live)

- **LLM factory** (`llm.py`): single `get_llm(provider, model, temperature)` switching on `openai|anthropic|google|azure` using `langchain-openai` (OpenAI + `AzureChatOpenAI`), `langchain-anthropic`, `langchain-google-genai`. Defaults from `config.settings`.
- **AI backend** (`ai/`): the swappable reword-only layer (Phase 6). `get_ai_backend()` returns the backend named by `AI_BACKEND` — `LangChainBackend` (BYOK, the default, over `get_llm`) or `CopilotBackend` (the GitHub Copilot CLI as a subprocess). The interface (`AIBackend.annotate(system, human) -> SpecialistAnnotations`) is the **guardrail**: the AI can only reword `message`/`suggestion`, never `severity`/`state`/`control_id`/`location` (enforced by the narrow return type, not by trusting the model). `annotate_with_llm` skips it when unavailable and catches any failure → the deterministic report still posts (graceful degradation, §9.2). The finding *set* is identical AI-on vs AI-off.
- **State** (`utils/state.py`): Pydantic models. For LangGraph messages: `Annotated[list[AnyMessage], add_messages]`. No untyped dicts.
- **Checkpointer**: off for the MVP (one-shot CI run — no state kept between runs). If persistence is ever needed, add `src/terraform_review_agent/persistence/checkpointer.py` with `SqliteSaver.from_conn_string(settings.sqlite_path)` and the `langgraph-checkpoint-sqlite` dep. Postgres only on explicit request.
- **Lenses** (`utils/lenses/`): each check is a `Lens` (`id` / `applies_to(state)` / `run(state) -> LensResult`); `registry.enabled_lenses(state)` picks which run (config `ENABLED_LENSES` ∩ applicable). `agent.py` fans out one `Send` per enabled lens into the `findings` reducer; the aggregator is deferred. Add a check = a new `Lens` subclass + a registry entry, no graph change.
- **Standards** (`utils/standards/` + `rule_packs/`): versioned, cited rule packs map a finding's `{source}:{rule}` to a standard **control** and a three-state class (✅ verified / ◐ evidence / ○ human_only), and declare **expected artefacts** whose absence is a `human_only` finding (gap detection, via `StandardsLens`). The mapper runs at report-build time (`build_findings_report(mapper=…)`); active packs are chosen by `ENABLED_RULE_PACKS` (empty = inert). Add a standard = a new pack JSON, no code.
- **Check sources** (`utils/sources/`): normalize external-tool output into `Finding`s. `sarif.py` parses any SARIF (MegaLinter, Prowler-IaC, Trivy) preserving the producing tool + rule id as `{source}:{rule}`; `coverage.py` parses lcov/cobertura/jacoco. **trivy is bundled in the image and runs directly** (`run_trivy` shells out when no `TRIVY_SARIF_PATH` is set, else ingests the report). **MegaLinter + Prowler-IaC stay ingest-only** — MegaLinter overlaps tflint/checkov + is a heavy Docker action, and Prowler audits live cloud accounts, not Terraform files. **Secret scanning (gitleaks) is deliberately excluded**: it surfaces credential *values* as findings, which would then reach the LLM rewording step. A source with nothing to do raises `ScannerNotConfigured` (logged at info, not warning).
- **Wedge lenses** (`utils/standardisers/` + `standards_defs/`): the A-coded lenses (A1 Terraform, A2 CI/CD). Deterministic, no LLM — they diff a repo against a *golden definition* (`TerraformStandard` / `CICDBaseline`, versioned + cited JSON) and emit deviation findings + a consistency/posture score, stamping `lens="A1"|"A2"` (the `LensCode` on `Finding`). A2 parses workflow YAML with **PyYAML** (handle the `on:` → `True` boolean-key quirk). Each is inert unless `TERRAFORM_STANDARD` / `CICD_STANDARD` names a def (empty=off · `"default"`=built-in · path=custom); thin `Lens` wrappers in `utils/lenses/` gate on terraform changes like the standards lens. Add a wedge standard = a new def JSON, no code.
- **Repo lenses A3-A4** (`utils/lenses/{coverage,tech_debt}.py`): deterministic, gated-off-by-default. **A3** ingests a coverage report (`COVERAGE_REPORT_PATH`) → under-covered changed files + score; **A4** ingests jscpd JSON (`JSCPD_REPORT_PATH`) + an optional Sonar SARIF (`SONARQUBE_SARIF_PATH`) → duplication/issue findings + a scorecard. A finding may assert its three-state class directly via `Finding.state` (gap checks); else `findings_report` derives it.
- **Output surfaces** (Phase 8): from the one `FindingsReport`, the aggregator/entrypoint emit findings.json (`findings_report.py`), a **SARIF** export (`sarif_export.py` → code-scanning), and an HTML+CSV **evidence pack** (`evidence_pack.py`). The comment gains a ✅/◐/○ "Standards readiness" section (`render._readiness_section`) only when there's a three-state story. Per-finding `confidence` is derived from state (verified 1.0 / evidence 0.5 / human_only none).
- **Dashboard ingest** (Phase 9): `dashboard_client.DashboardClient` POSTs the `FindingsReport` to `DASHBOARD_INGEST_URL` (`entrypoint._post_to_dashboard`). **Opt-in** (`from_settings` → `None` when no URL) + **best-effort** (`post_report` swallows `httpx` errors → `False`, never raises) so a dashboard outage can't fail a scan — same rule as the AI backend. Rule-pack/standard-def curation is a content workstream: [`docs/rule-pack-curation.md`](docs/rule-pack-curation.md).
- **Scan scope + inline comments** (Phase 10): `SCAN_MODE` (`full` default = whole-repo posture; `diff` = changed files only) gates `_annotate.filter_to_changed` + the coverage lens; repo-level lenses (A1/A2, gaps) are always whole-repo. `INLINE_COMMENTS` (on by default) → `entrypoint._post_inline_comments` posts one PR review comment per finding on a changed line (`utils/diff.commentable_lines` parses hunks; `github_client.post_review_comments` is idempotent via a `tra-inline:<key>` marker, best-effort on httpx errors). The sticky comment renders one collapsible `<details>` section per severity (critical/high open, the rest collapsed + grouped by rule; `render._findings_sections`/`_grouped_table`), with the headline summary always visible.
- **Releases** are automated via release-please + Conventional Commits (`feat`→minor, `fix`→patch, `feat!`→major): merging the release PR cuts `vX.Y.Z` + `vX.Y` + `vX` git tags (`tag-floats` moves the major+minor floats) and pushes matching `:vX.Y.Z`/`:vX.Y`/`:vX`/`:latest` (build chained in `release-please.yml`, since a GITHUB_TOKEN tag can't trigger `build-image.yml`). release-please updates pyproject/`__init__`/CHANGELOG only — **not** `.github/workflows/` (the token has no `workflow` scope; committing there throws "Error adding to tree"), so `terraform-review.yml` pins the `:v1` image float (build-image keeps it current). Don't hand-tag — see `RELEASING.md`.
- **Tools** (`utils/tools.py`): `@tool` from `langchain_core.tools` with Pydantic input schemas.
- **Prompts** (`utils/prompts.py`): never inlined in node code.
- **Config** (`config.py`): `pydantic_settings.BaseSettings` reading env — secrets never hardcoded.

---

## 7. Docker

- Base: `python:3.14-slim`, `.venv` at `/app/.venv` (identical to host), non-root user
- Compose: mount `./data` (SQLite persists across restarts) and `./src` (dev hot-reload — remove for prod)
- Run via `docker compose up`

---

## 8. `make` targets

`venv` · `install` · `fmt` · `lint` · `type` · `test` · `run` · `docker-build` · `docker-up` · `clean`

Python targets invoke `./.venv/bin/...` — never bare `python`. `run` is the exception: it executes inside the container (`docker compose run agent …`) so the bundled scanners are on `PATH`. Use these instead of re-inventing commands.

---

## 9. `langgraph.json`

```json
{
  "dependencies": ["."],
  "graphs": { "agent": "./src/terraform_review_agent/agent.py:agent" },
  "env": "./.env",
  "python_version": "3.14"
}
```

---

## 10. Conventions

- Type hints everywhere; `mypy --strict` must pass on `src/`
- Pydantic for all data shapes — no bare dicts across module boundaries
- One node = one small, pure-ish function
- No `print` in `src/` — structured logging only
- Raise typed exceptions; let the graph handle retry/branching, not try/except spaghetti

---

## 11. Testing

- `tests/unit/` — pure function and node tests; mock LLM calls
- `tests/integration/` — compiled graph end-to-end with in-memory SQLite checkpointer
- Every node gets at least one unit test
- Use `pytest.fixture` for graph construction to stay DRY

---

## 12. Workflow when editing this project

1. Read this file, then `pyproject.toml`, then `src/terraform_review_agent/agent.py`
2. Honor framework choice from §1
3. Add Pydantic schemas to `utils/state.py` before writing node code
4. New nodes in `utils/nodes.py`, wired in `agent.py`
5. Add at least one unit test
6. Run `make fmt lint type test` before declaring done

---

## 13. Out of scope (stop and ask first)

Cloud-managed DBs, vector stores, message queues · Kubernetes / Helm / Terraform · LLM providers other than OpenAI / Anthropic / Google / Azure OpenAI (+ the GitHub Copilot reword backend) · Frontend frameworks · Paid third-party APIs beyond LLMs.

---

## 14. References

- <https://docs.langchain.com/>
- <https://docs.langchain.com/oss/python/langgraph/application-structure>
- <https://docs.langchain.com/oss/python/langgraph/persistence>
- <https://docs.langchain.com/oss/python/langgraph/workflows-agents>
- <https://docs.pydantic.dev/latest/>
- <https://docs.astral.sh/uv/>
