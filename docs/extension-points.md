<!-- markdownlint-disable MD060 -->

# Architecture & extension points (Assessor enhancement)

> Phase 0 note for the [assessor build plan](../assessor-build-plan.md). Where the current `terraform-review-agent` flow runs, and where each later phase plugs in. Written after reading the codebase on 2026-06-01.

## The flow as it stands

```text
GitHub PR event
  └─► .github/workflows/terraform-review.yml  (reusable workflow, runs the GHCR container)
        └─► python -m terraform_review_agent.entrypoint        # entrypoint.py — the I/O boundary
              ├─ _parse_args → repository + pr_number
              ├─ GitHubClient.fetch_pr_context()               # github_client.py
              ├─ _ensure_workspace()  (clone PR merge ref if not already a checkout)
              ├─ agent.invoke(ReviewState(pr, workspace, …))   # agent.py — compiled LangGraph
              │     start ──► [lens ∥ lens ∥ …] ──► aggregator ──► post_comment   # registry-driven (Phase 2)
              ├─ write/post outputs (sticky comment upsert)
              └─ exit code: 0, or 2 when a finding trips `fail_on_severity`
```

**Node responsibilities** (`agent.py` wiring; nodes in `utils/nodes.py`):

- `start_node` — sets `skipped` when the PR touches no `*.tf`.
- `fan_out_to_lenses` (conditional edge) — emits one `Send("lens", …)` per `enabled_lenses(state)`, or routes straight to the aggregator when none apply.
- `lens_node` — runs one `Lens` (`utils/lenses/`) and appends to the `findings` reducer; the cost lens also sets `cost_summary`.
- `lens_node` (standards) — `StandardsLens` runs gap detection (`utils/standards/gaps.py`) over the active rule packs, emitting `human_only` findings for missing expected artefacts.
- `lens_node` (A1/A2 wedge) — `TerraformStandardLens` + `CICDLens` (`utils/standardisers/`) diff the repo against a golden definition (`standards_defs/`) and emit deviation findings + a consistency/posture score, stamping `lens="A1"|"A2"`. Deterministic, no LLM; inert unless `TERRAFORM_STANDARD` / `CICD_STANDARD` is set.
- `lens_node` (A3/A4/A5 repo lenses) — `CoverageLens` (coverage report → under-covered changed files + score), `TechDebtLens` (jscpd + Sonar SARIF → duplication/issue findings + scorecard), `GDSLens` (`utils/standardisers/gds.py` → ✅/◐/○ per GDS point, out-of-scope ones honest). Deterministic; each inert unless its report/definition is configured.
- `aggregator_node` — builds the `FindingsReport` (mapper) **first**, then renders the comment with the report's records so it can show the ✅/◐/○ "Standards readiness" section; `entrypoint._write_outputs` writes findings.json + the SARIF export (`sarif_export.py`) + the HTML/CSV evidence pack (`evidence_pack.py`).
- `aggregator_node` — deferred (`defer=True`); joins `state.all_findings()`, calls `render_comment(...)` → `comment_markdown`, and builds the findings-JSON contract passing `build_active_mapper()` so findings are mapped to standard controls + ✅/◐/○ states.
- `post_comment_node` — **no-op stub**; the real sticky-comment upsert happens in `entrypoint.run`, not the node.

## Asset we inherit (don't rebuild): the determinism guardrail is already here

`utils/lenses/_annotate.annotate_with_llm` + the `state.py` models already enforce our core thesis: **scanners own the finding set + severity/file/line/rule; the LLM may only reword `message`/`suggestion`** (matched back by id). Speculative LLM findings are off by default (`settings.enable_llm_findings = False`). That is exactly the Phase 6 "LLM rewords only" guardrail — it exists, we extend rather than build it.

## Where each phase plugs in

| Phase | What | Touch points |
|-------|------|--------------|
| **1 — findings JSON (the spine)** | emit a versioned `findings.json` | `utils/findings_report.py` (new model + builder), `utils/state.py` (`ReviewState.findings_report_json`), `utils/nodes.aggregator_node` (build it — pure), `entrypoint.run` (write the file — I/O), `config.py` (`findings_output_path`), `schemas/findings.schema.json` (new), `terraform-review.yml` (upload artefact) |
| **2 — lens registry** ✅ | replaced the 3 fixed agents with a pluggable `Lens` interface | **Done:** `utils/lenses/` (`base`, `security`/`cost`/`style`, `registry`, `_annotate`); `agent.py` (`Send` fan-out + deferred aggregator); `utils/nodes.py` (`lens_node`); `utils/state.py` (`findings` `operator.add` reducer); `ENABLED_LENSES` config. A-coded lenses (A1–A5) still to come (Phase 5). |
| **3 — check sources** ✅ | consume MegaLinter + Prowler-IaC (SARIF), coverage parsers, gitleaks/Trivy | **Done:** `utils/sources/sarif.py` (SARIF→Finding, source+rule preserved) + `utils/sources/coverage.py` (lcov/cobertura/jacoco); ingestion `@tool`s in `utils/tools.py` (`run_prowler_iac`/`run_gitleaks`/`run_trivy`→security, `run_megalinter`→style) gated on `*_SARIF_PATH`. Coverage parser awaits the A3 lens (Phase 7). |
| **4 — standard-mapping + gap (moat)** ✅ | rule packs, control mapping, absence checks, ✅/◐/○ states | **Done:** `utils/standards/` (`pack`, `loader`, `mapping`, `gaps`) + `rule_packs/*.json`; `findings_report` maps `standard`/`control_id`/`state`; `StandardsLens` does gap detection; `ENABLED_RULE_PACKS` config. (`lens`=A-codes still Phase 5.) |
| **5 — A1 + A2 lenses** ✅ | Terraform-house + CI/CD baseline lenses | **Done:** `utils/standardisers/` (`terraform`/`cicd` logic + `load_definition`) + thin `utils/lenses/{terraform_standard,cicd}.py`; golden defs `standards_defs/*.json` (`TerraformStandard`/`CICDBaseline`); `Finding.lens` (A-code) wired through `findings_report`; `TERRAFORM_STANDARD`/`CICD_STANDARD` config + reusable-workflow inputs. (Real-repo dogfooding is the one open item — needs your repos.) |
| **6 — AI backend** ✅ | swappable reword-only backend (BYOK + Copilot), graceful degradation | **Done:** `ai/` (`base` interface, `langchain_backend` BYOK incl. Azure, `copilot_backend` CLI, `get_ai_backend` factory); `_annotate.annotate_with_llm` routes through it (availability check + try/except degradation); `llm.py` Azure branch; `config.py` (`AI_BACKEND`, `azure_*`, `copilot_*`). Guardrail is structural (the `SpecialistAnnotations` return type). Live Copilot-with-PAT verify is a human-todo. |
| **7 — repo lenses A3-A5** ✅ | coverage, tech-debt, GDS readiness | **Done:** `utils/lenses/{coverage,tech_debt,gds}.py` + `utils/sources/jscpd.py` + `utils/standardisers/gds.py` + `standards_defs/gds-readiness.json`; `Finding.state` for intrinsic ✅/◐/○. Gated on `COVERAGE_REPORT_PATH` / `JSCPD_REPORT_PATH` / `SONARQUBE_SARIF_PATH` / `GDS_STANDARD`. |
| **8 — reporting** ✅ | 3-state report, SARIF export, evidence pack, confidence | **Done:** `render._readiness_section` (✅/◐/○ in the comment); `utils/sarif_export.py` (→ code-scanning via the workflow's `upload-sarif`); `utils/evidence_pack.py` (HTML+CSV); confidence derived from state. `entrypoint._write_outputs` writes all four surfaces. |
| **9 — dashboard & rule-pack rollout** ✅ | POST findings JSON to the hosted ingest; ship + curate the first packs | **Done:** `dashboard_client.py` (`DashboardClient`, opt-in + best-effort POST of the Phase-1 contract) + `entrypoint._post_to_dashboard`; `DASHBOARD_INGEST_URL`/`DASHBOARD_API_KEY` config. First packs (`terraform-house`/`ci-baseline`/`gds-readiness` + `terraform-cis-aws`) shipped, versioned, cited; curation workstream in [`rule-pack-curation.md`](rule-pack-curation.md). Live-dashboard + live-standard validation are human-todos. |

## Notes / gotchas observed

- **Provenance:** authored by `ignatg`, distributed as `ghcr.io/ignatg/terraform-review-agent` — not the `infiniumtek` repo the plan named. Same architecture; treat ignatg as the upstream to credit.
- **Licence:** AGPL-3.0-or-later across `LICENSE`, `pyproject.toml`, and the `README.md` footer; the upstream MIT notice is preserved in `NOTICE` (resolved 2026-06-01, was previously inconsistent).
- **Local tests:** 250/250 pass on Windows + Linux. (The earlier 2 Windows-only checkov leading-slash failures were fixed in `_relpath` — it now strips a POSIX leading slash on both platforms.)
- **Engine is CLI-first already** (`python -m …entrypoint`); the Action just wraps it — good, keep it that way (no GitHub lock-in).
