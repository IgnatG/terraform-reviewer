<!-- markdownlint-disable MD060 -->

# Assessor Build Plan — enhancing the `terraform-review-agent` fork

> A working checklist for turning a fork of [`infiniumtek/terraform-review-agent`](https://github.com/infiniumtek/terraform-review-agent) (MIT, Python 3.13 + LangGraph) into **the Assessor** described in [`delivery-assurance-plan.md`](../langgraph-server/delivery-assurance-plan.md). Tick items off as you go.
>
> **Started:** 2026-06-01 · **Target:** the repo/code-shaped Assessor lenses (A1–A5) on one pluggable engine, BYOK-first, deterministic, emitting the findings-JSON spine.

## Scope — what this fork covers (and what it doesn't)

This fork is the **CI/CD repo Assessor** — only the **deterministic, repo/code-shaped lenses** that run inside a GitHub Action.

**In scope (this fork):**

- **A1** Terraform Standardiser
- **A2** CI/CD Pipeline Standardiser
- **A3** Test Coverage & Gap Analyser
- **A4** Tech-Debt Scorecard
- **A5** GDS Readiness Scanner — the code-evidenceable points only; rendered checks (axe/content-design) are deferred to the rendered tier (§9.3)

**Not in this fork — separate products / surfaces:**

- **A8 SoW Standardiser** → a **web-app** document tool (pairs with the B5 SoW generator). Document input, not a repo — not a GitHub Action.
- **A6 DSPT Readiness** → a **separate compliance product** (mostly questionnaire/evidence; only a slice is code-evidenceable). This fork may *feed it* code signals (secrets/TLS/dep-vulns/IaC) via findings JSON, but DSPT itself is built elsewhere.
- **A7 Accessibility Auditor** → needs rendered pages (the flaky tier); **deferred / ignore for now.**

## How to use this

Status legend: `[ ]` todo · `[~]` in progress · `[x]` done · `[-]` deferred/skipped.

Work the phases top-to-bottom — each builds on the last. Every phase has a **Done when** check; don't move on until it's green. Section references (§) point to `delivery-assurance-plan.md`.

**Progress:** Phase 0 ▣ · 1 ▣ · 2 ▣ · 3 ▣ · 4 ▣ · 5 ▣ · 6 ▣ · 7 ▣ · 8 ▣ · 9 ▣ (update as phases complete)

**North star (what "done" looks like):** one Python engine with a pluggable **lens registry** running A1–A5; deterministic checks as the source of truth with the LLM **rewording only**; a **standard-mapping + gap layer** keyed to versioned **rule packs**; emitting **findings JSON** + a three-state (✅/◐/○) report; BYOK-first with an optional Copilot SDK backend; posting history to the dashboard.

---

## Phase 0 — Fork & baseline (prove it runs as-is)

Goal: a working fork you understand, before changing anything.

- [x] Fork the repo (`IgnatG/terraform-reviewer`; author set to `ignatg`).
- [x] **Licence — resolved.** `LICENSE` is AGPL-3.0; `pyproject.toml` (`license = { text = "AGPL-3.0-or-later" }`) and the `README.md` footer now match it. Upstream's MIT copyright is preserved in `NOTICE` (this fork derives from the MIT-licensed `infiniumtek/terraform-review-agent`).
- [x] Get it building locally — `uv sync --inexact --extra dev --native-tls` (the `--native-tls` flag is required on this Windows machine; cert-interception otherwise fails downloads). Docker image build not run locally (CI/GHCR builds it).
- [ ] Run the existing agent on a sample Terraform PR; confirm the sticky comment posts. *Needs a throwaway repo + token + BYOK key — requires you (can't drive a live PR without creds).*
- [x] Read and map the LangGraph flow → `terraform-reviewer/docs/extension-points.md`.
- [ ] Confirm the green CI workflow runs end-to-end on GitHub. *Local: 169/169 pass (the earlier 2 Windows-only checkov leading-slash failures were fixed in `_relpath` during the Phase 3 audit — now cross-platform). Needs a push to confirm Actions.*

**Done when:** the unmodified fork posts a review comment on a test PR, and you can describe the node graph from memory. *(Mapping ✓; the live-PR post needs your creds.)*

---

## Phase 1 — The findings-JSON contract (the spine)

Goal: lock the data contract first — everything downstream (report, dashboard, Remediator) integrates through it (§2.3).

- [x] Draft the **findings JSON schema** (versioned) — `terraform-reviewer/schemas/findings.schema.json` (`schema_version` 1.0, JSON Schema 2020-12). Fields per finding:
  - [x] `id` (stable content-hash), `lens` (A1–A5, nullable until Phase 2), `standard`/`standard_version`/`control_id` (nullable until Phase 4), `category` (current producer)
  - [x] `state` (`verified` ✅ / `evidence` ◐ / `human_only` ○— scanner findings default `verified`, LLM-discovered → `evidence`), `severity`, `confidence`
  - [x] `evidence`, `location` (file/line/url), `remediation_hint`
  - [x] `source` (derived from the rule prefix: tfsec/checkov/infracost/tflint/llm/…), `rule_id`, `rule_pack_version`
- [x] Top-level scan metadata: `repository`, `commit_sha`/`base_sha`, `scan_time` (ISO-UTC), `engine_version`, `pr_number`, `mode` (diff/full) + a `summary` (counts + cost headline).
- [x] Publish the schema file; validate emitted reports against it in CI (`tests/unit/test_findings_report.py`, runs under `make test` in `ci.yml`).
- [x] Aggregator **emits findings JSON** (`utils/findings_report.py` builder; `aggregator_node` serializes — pure; `entrypoint` writes `./findings.json` — I/O) + uploaded as a CI artefact (`terraform-review.yml`).

**Done when:** a scan produces a schema-valid `findings.json` artefact on every run. ✅ *(verified end-to-end: aggregator → schema-valid report; new tests + integration green.)*

---

## Phase 2 — Refactor to a pluggable lens registry

Goal: replace the three hard-coded agents (security/cost/style) with a generic **lens** interface so one engine runs A1–A5 (§2.2).

- [x] Define a `Lens` interface: `id`, `applies_to(state)`, `run() -> LensResult` — `utils/lenses/base.py` (`run` returns findings + the cost lens's `cost_summary`).
- [x] Port the existing security/cost/style agents to the new interface as the first lenses (no behaviour change) — `utils/lenses/{security,cost,style}.py`; shared scanner/LLM plumbing in `utils/lenses/_annotate.py`.
- [x] Build a **registry** that discovers/enables lenses via config — `utils/lenses/registry.py` (`ALL_LENSES`, `enabled_lenses(state)`), selected by the `ENABLED_LENSES` setting ∩ each lens's `applies_to`.
- [x] Generalise the LangGraph fan-out to iterate the registry (`start → [lens ∥ lens ∥ …] → aggregator`). Done via the **`Send` API** (`fan_out_to_lenses` conditional edge → one `lens` task per enabled lens) and a **deferred** aggregator (`add_node("aggregator", aggregator_node, defer=True)`); the three hard-coded `add_edge` branches are gone.
- [x] Keep lens execution **parallel and deterministic** — lenses are side-effect-free; results merge through `ReviewState.findings` (`Annotated[list[Finding], operator.add]`). Merge order is irrelevant (render + findings-report both re-sort; verified byte-identical across input orders).

**Done when:** the three original checks run as registered lenses through the new registry, output unchanged. ✅ *(139 unit/integration tests green; comment markdown + `findings.json` proven byte-identical — `lens` field stays null until A-coded lenses land in Phase 5. The 2 remaining failures are the pre-existing Windows-only checkov path quirk, green on Linux CI.)*

---

## Phase 3 — Check-source layer (consume tools, don't rewrite them)

Goal: a normalisation layer that turns external scanner output into findings (§2.1).

- [x] Add **MegaLinter** as a separate-process check source; parse its aggregated SARIF — ingested via `MEGALINTER_SARIF_PATH` into the style lens (`utils/sources/sarif.py` + `tools.run_megalinter`). Its per-sub-linter runs are preserved as distinct sources.
- [x] Add **Prowler IaC provider** (SARIF) as a check source — `PROWLER_SARIF_PATH` → security lens (`tools.run_prowler_iac`). The same SARIF parser covers CFN/Dockerfile/K8s output too.
- [x] Add **coverage parsers** (lcov/cobertura/jacoco) for A3 — `utils/sources/coverage.py` (`parse_coverage_file` auto-detects). Parser only; the A3 lens consumes it in Phase 7.
- [x] Add **gitleaks/Trivy** (secrets, dep/IaC vulns) for A5 — `GITLEAKS_SARIF_PATH` / `TRIVY_SARIF_PATH` → security lens. Findings scope to all changed files (not just `.tf`) so non-Terraform secrets survive.
- [-] **Skip rendered checks** (axe-core/pa11y) — deferred as planned (rendered/flaky tier, §9.3).
- [x] Normalise every source into the findings schema (Phase 1); preserve original `source` + rule IDs — SARIF `tool.driver.name` + `ruleId` become `{source}:{rule}`; `findings_report._source_of` recovers the tool.

**Done when:** MegaLinter and Prowler-IaC findings flow through the registry into valid findings JSON. ✅ *(verified end-to-end: a Prowler SARIF → security lens → reducer → schema-valid `findings.json` with `source=prowler`, and rendered in the comment. 169 tests green — incl. a new SARIF suite, coverage suite, and ingestion suite. Sources are ingestion-based: each tool runs as its own CI step and writes a report; the engine consumes it, self-skipping when no report is configured, so default behaviour is unchanged.)*

---

## Phase 4 — Standard-mapping + gap layer (the moat)

Goal: the part nobody gives us — tie raw findings to a standard and detect what's *absent* (§1, §2.2).

- [x] Design the **rule-pack format** — `utils/standards/pack.py` (`RulePack`: `controls` + `mappings` + `expected_artifacts`, each with `control_id`). JSON, shipped in `rule_packs/` + an optional `RULE_PACKS_DIR`. Load-time validation rejects dangling control refs + non-relative artefact paths.
- [x] Build the **mapping layer**: finding → `control_id` — `utils/standards/mapping.py` (`StandardMapper`: exact rule > longest prefix). Applied in `findings_report._to_record` (populates `standard`/`standard_version`/`control_id`/`rule_pack_version`); the aggregator passes `build_active_mapper()`.
- [x] Build **absence/gap detection** — `utils/standards/gaps.py` + `StandardsLens` (registered): expected-artefact presence check (README/LICENSE/… via `any_of`) → `human_only` findings. Gated on terraform changes + an active pack, so inert by default.
- [x] Implement **three-state classification** (✅ verified / ◐ evidence / ○ human-only) per control — the mapped control's `state` drives `FindingRecord.state` (validated `Literal`); unmapped falls back to scanner=verified / llm=evidence.
- [x] Make rule packs **versioned + cited** — every pack carries `standard_version` + `rule_pack_version` + `source_url`, and each control its own `source_url`. Example: `terraform-cis-aws` (CIS AWS 3.0.0).

**Done when:** a scan emits findings mapped to control IDs with correct ✅/◐/○ states for at least one rule pack. ✅ *(verified e2e: with `terraform-cis-aws` active, a Prowler finding → CIS control 2.1.1 ✅ verified; missing README/LICENSE → DOC.1/LIC.1 ○ human_only; ◐ evidence covered by unit test; schema-valid. 185 tests green; default (no pack) output byte-identical. Pack JSON confirmed packaged in the wheel.)*

---

## Phase 5 — Wedge lenses first: A1 + A2 (deterministic, dogfoolable)

Goal: ship the two lenses you can run on your own repos day one, no AI needed (§8 sequencing).

- [x] **A1 Terraform Standardiser** — golden house-module structure diff + per-repo consistency score + deviation list. `utils/standardisers/terraform.py` (`check_modules`) + `utils/lenses/terraform_standard.py` (`TerraformStandardLens`). Checks each *touched module* (a dir that still holds a `.tf`/`.tf.json` file): required files present + the expected `terraform { required_version / required_providers / backend }` blocks declared (text-presence, not full HCL — deeper module-composition is Phase 11). `fmt`/tflint/Checkov already run in the style/security lenses, so A1 doesn't re-run them (no duplicate findings); it adds the structure-diff + score they don't cover.
- [x] Define the **"golden" house-standard definition file** (A1's reference) — `TerraformStandard` model; built-in `standards_defs/terraform-house.json`; versioned + cited (`version`/`source_url`). Custom defs via a path.
- [x] **A2 CI/CD Standardiser** — parse `.github/workflows/*.yml`, diff vs the golden baseline; flag insecure patterns: `pull_request_target`, third-party actions not pinned to a full commit SHA, and a missing least-privilege top-level `permissions` block. `utils/standardisers/cicd.py` (`check_workflows`) + `utils/lenses/cicd.py` (`CICDLens`); built-in `standards_defs/ci-baseline.json`. (ADO/GitLab + OIDC/branch-protection are a later addition — scope note.)
- [ ] Dogfood A1+A2 on ≥3 of your own client repos; sanity-check scores against your manual read. *(Needs you — requires real repos + a human judgement call I can't make. Enable with `terraform-standard: default` / `cicd-standard: default` in the caller; see `examples/README.md`.)*

**Done when:** A1+A2 produce a consistency score + deviations on your real repos that match your judgement. ✅ *(Engine side done & verified e2e: with the built-in standards active, a repo with a bare `main.tf` + a `pull_request_target` workflow yields A1 missing-file/missing-block deviations + a `terraform-house:score`, and A2 `pull-request-target`/`unpinned-action`/`missing-permissions` + a `ci-baseline:score`, each carrying `lens: A1|A2`. 202 tests green; both lenses inert by default so existing output is byte-identical. The real-repo sanity-check is the one item left for you.)*

Both lenses are **gated off by default** (`TERRAFORM_STANDARD` / `CICD_STANDARD` empty) and **deterministic** (no LLM), so they're inert until enabled and never change a verdict. The reusable workflow exposes them as `terraform-standard` / `cicd-standard` inputs; the engine stamps `lens: A1|A2` on their findings (the field reserved since Phase 1).

---

## Phase 6 — AI backend (BYOK-first, Copilot optional)

Goal: a swappable AI layer that **only rewords** — never changes a verdict (§2.5).

- [x] Define an **AI-backend interface** (reword-only) — `ai/base.py` (`AIBackend.annotate(system, human) -> SpecialistAnnotations`). `get_ai_backend()` selects it via `AI_BACKEND`.
- [x] Implement **BYOK** adapter (OpenAI/Anthropic/Gemini/Azure) as the default — `ai/langchain_backend.py` over `get_llm`; Azure branch added to `llm.py` (endpoint + deployment-fallback). `DEFAULT_LLM_PROVIDER` now includes `azure`.
- [x] Implement the **Copilot** adapter — `ai/copilot_backend.py`: drives the Copilot CLI as a subprocess (configurable command), token via `COPILOT_GITHUB_TOKEN` in the env (never argv), JSON parsed out of stdout. The CLI invocation is isolated in `_invoke_cli` (the one seam to adapt). *Live verification against a real CLI + PAT is a human-todo (HUMAN-TODO.md) — it can't run on a machine without the Copilot CLI.*
- [x] Enforce the **guardrail** — structural: the backend returns `SpecialistAnnotations` (message/suggestion only, keyed by id), so it cannot touch `state`/`severity`/`control_id`/`location`; `state`/`control_id` are set later by the Phase-4 mapper, not the AI. Discovery stays opt-in + namespaced.
- [x] Graceful degradation (§9.2) — `annotate_with_llm` skips an unavailable backend and wraps `annotate` in try/except; any failure (network, CLI, timeout, bad JSON, validation) falls back to the un-reworded scanner findings, so the report always posts.

**Done when:** the same scan produces identical findings/verdicts with AI on (BYOK) and AI off; Copilot adapter works with a PAT. ✅ *(BYOK half done & verified: 215 tests green incl. guardrail + degradation + AI-on==AI-off; the finding set/severity/state are scanner+mapper-owned so they're identical with AI on/off — only prose differs. The "Copilot works with a PAT" half is the human verification step in HUMAN-TODO.md.)*

---

## Phase 7 — Remaining repo lenses A3–A5

Goal: complete the in-scope repo lenses. (A6 DSPT, A7 Accessibility, A8 SoW are separate products/surfaces — see Scope.)

- [x] **A3 Test Coverage & Gap Analyser** — `utils/lenses/coverage.py` (consumes `utils/sources/coverage.py`): flags changed files below `COVERAGE_MIN_PERCENT` + a repo coverage score, ordered lowest-coverage-first. Ranking is deterministic (severity by gap), not LLM-driven — that keeps the guardrail (the AI never moves a verdict). Gated on `COVERAGE_REPORT_PATH`.
- [x] **A4 Tech-Debt Scorecard** — `utils/lenses/tech_debt.py` + `utils/sources/jscpd.py`: ingests jscpd duplication JSON + an optional SonarQube SARIF (reuses the SARIF parser), scopes per-issue findings to the diff, emits a scorecard summarising the signals that ran. dep-age deferred (ecosystem-specific tool); the historical *trend* needs the dashboard (Phase 9).
- [x] **A5 GDS Readiness Scanner** — `utils/lenses/gds.py` + `utils/standardisers/gds.py` + `standards_defs/gds-readiness.json`: govuk-frontend (package.json) + artefact-presence checks → ✅ per code-evidenceable point; rendered/judgement points (axe, content design, secrets-in-history) are reported ○ human_only / ◐ evidence, **never faked**, and excluded from the score. Gated on `GDS_STANDARD`.

**Done when:** each lens emits valid, mapped findings; A5 honestly reports out-of-scope points rather than faking them. ✅ *(Verified e2e: A3/A4/A5 emit `lens=A3/A4/A5` findings with the right three-state class; A5's out-of-scope points surface as ○/◐ and can't inflate its score. All gated off by default. 239 tests green.)*

---

## Phase 8 — Reporting & output surfaces

Goal: make the findings usable where people look.

- [x] **Three-state report** rendered (✅/◐/○ by area) — a "Standards readiness" section in the sticky comment (`render._readiness_section`, driven by the mapped report records) + a "needs a human" list so ✅ is never mistaken for full coverage. Only shown when there's a three-state story (a standard/A-lens/non-verified finding), so plain PRs are unchanged.
- [x] **SARIF export** — `utils/sarif_export.py` (`FindingsReport` → SARIF 2.1.0); the workflow uploads it via `github/codeql-action/upload-sarif`, landing findings in the Security → Code-scanning tab + inline PR annotations.
- [x] **Evidence pack** — `utils/evidence_pack.py`: a self-contained **HTML** pack (✅/◐/○ per standard, prints to PDF — chosen over a PDF binary dep) + a **CSV** export (formula-injection-safe). *(Literal PDF deferred: HTML→PDF is a one-line browser/CI step.)*
- [x] **Per-finding confidence** surfaced — derived from the three-state class (verified 1.0 / evidence 0.5 / human_only n/a) and shown in the evidence pack (+ findings.json + SARIF properties).

**Done when:** one scan yields the PR comment, the Code-Scanning entries, and a downloadable evidence pack. ✅ *(Verified e2e: one scan writes findings.json + findings.sarif + evidence-pack.html + findings.csv, and the comment carries the readiness section. The Code-scanning entries themselves need a push to GitHub to confirm — see HUMAN-TODO.md.)*

---

## Phase 9 — Dashboard integration & rule-pack rollout

Goal: feed Surface 3 and stand up the moat content (§6, §1).

- [x] **History POST** — push findings JSON to the hosted dashboard ingest after each scan. `dashboard_client.py` (`DashboardClient`, httpx like `github_client`) POSTs the findings contract (Phase 1) to `DASHBOARD_INGEST_URL`, Bearer-authed with `DASHBOARD_API_KEY`; wired into `entrypoint.run` via `_post_to_dashboard`. **Opt-in** (no URL → no POST, behaviour unchanged) and **best-effort** (any `httpx` failure is logged and swallowed — dashboard downtime never fails the scan or blocks the comment, mirroring the AI graceful-degradation rule). Posted on every scan incl. skipped, so "scanned, 0 findings" is recorded too.
- [x] Author the first **rule packs**: `terraform-house` + `ci-baseline` (back A1/A2), then the **GDS** pack (A5) — versioned, cited. *(All shipped + versioned + cited: `standards_defs/{terraform-house,ci-baseline,gds-readiness}.json` (each `version` + `source_url`) and the `terraform-cis-aws` mapping pack. Catalogued in [`docs/rule-pack-curation.md`](docs/rule-pack-curation.md). The DSPT pack belongs to the separate DSPT product, not this fork — though it can consume the code signals this fork emits.)*
- [x] Stand up **rule-pack curation** as a first-class workstream: refresh cadence, citation/version trail (§1). *(Documented in [`docs/rule-pack-curation.md`](docs/rule-pack-curation.md): the two definition kinds + provenance fields, the four-point shipping bar, quarterly + event-driven refresh cadence, and the authoring mechanics. Standing up the owner + the recurring cadence is a people process — tracked in `HUMAN-TODO.md`.)*
- [ ] Validate every rule against the **live standard** before publishing (§9.1). *(The bar + process are written (`docs/rule-pack-curation.md` "the bar a rule must clear"); load-time validation rejects dangling/non-relative refs in CI. The actual judgement pass against the live standards needs a human reviewer — `HUMAN-TODO.md`.)*

**Done when:** scans appear in the dashboard with per-standard readiness, and the GDS pack cites a live source + version. ✅ *(Engine side done & verified: `DashboardClient` POSTs the schema-valid report (opt-in + best-effort, unit-tested incl. the swallowed-failure path); the GDS pack ships with `version` 1.0.0 + a live `source_url` per point. Confirming scans land in a **live** dashboard needs the hosted ingest endpoint + a key — `HUMAN-TODO.md`; the first human validation pass against live standards is the one open curation item.)*

---

## Phase 10 — Above-and-beyond (after the core is solid)

Pick up as needed; none block the MVP.

- [x] **Diff mode vs full-scan mode** — `SCAN_MODE` (`full` default = whole-repo posture scan; `diff` = changed-files-only). Gates `_annotate.filter_to_changed` (no-op in full) + the coverage lens; repo-level lenses (A1/A2/A5, gaps) are always whole-repo; the mode flows into the findings report. *(Node caching across modes is still a future optimization.)*
- [x] **Inline PR review comments + scannable comment** — `INLINE_COMMENTS` (on by default): one review comment per finding on a changed line (`utils/diff.commentable_lines` + idempotent `github_client.post_review_comments`); findings off the diff stay in the summary. The sticky comment renders one collapsible `<details>` section per severity (critical/high open, the rest collapsed + grouped by rule via `render._findings_sections`/`_grouped_table`), keeping the headline summary always visible. *(Reporting enhancement over Phase 8.)*
- [ ] **Waiver / risk-acceptance workflow** — accept a finding with justification + owner + expiry, audit-trailed (DefectDojo-modelled, §2.2).
- [ ] **Scheduled scans + drift/regression alerts** — flag when a green repo backslides.
- [ ] **Air-gapped / offline mode** — deterministic checks + a local/approved model, no external calls (sensitive gov estates).
- [ ] **Portfolio rollup** — cross-repo readiness view (feeds the paid dashboard).
- [ ] **Auto-fix handoff** — emit findings the **Remediator (Pullfrog fork)** can consume to open fix PRs (the assess→remediate loop, §8).

**Done when:** these are tracked separately; revisit once Phases 0–9 are green.

---

## Phase 11 — Standards-adherence lens (module-first IaC) — *needs its own design spike*

> Captured 2026-06-02 from the reviewer's note: *"one of the checks is adherence to standards — e.g. building AWS infra in Terraform should use AWS Terraform modules as much as possible."* This is **not** the same as A1 (Terraform Standardiser, which diffs against a golden **house** module structure and runs fmt/validate/tflint/checkov). This lens judges **how** infra is composed: prefer well-maintained **modules over hand-rolled resource soup**, and prefer **official/verified** modules where one exists. It's deterministic-leaning but genuinely fuzzy at the edges, so it gets its own thinking before any code.

**The thesis.** Hand-assembling primitives (dozens of `resource "aws_*"` blocks) when a mature module exists is a maintainability/security/consistency smell: you re-implement (often worse) what `terraform-aws-modules/vpc`, `terraform-aws-modules/eks`, the cloud's verified modules, or the org's internal module already solved. The lens should surface that and score it — *without* dogmatically punishing legitimate raw-resource use.

**What to detect (candidate signals):**

- **Module-adoption ratio** — count `module {}` blocks vs. `resource {}` blocks, weighted by resource family. A stack that is 90% raw `aws_*` resources for an area that has a canonical module scores low.
- **Source quality of the modules used** — is each `module.source` a registry module that is **official/partner/verified** (Terraform Registry API exposes this), an org-internal module, a raw git/local path, or unpinned? Prefer verified/pinned; flag unpinned (`?ref=` / `version` missing) and untrusted sources (overlaps A2's unpinned-SHA check but for modules).
- **"A module exists for this" gap detection** — for clusters of raw resources that map to a known module (e.g. a hand-built VPC: `aws_vpc` + subnets + route tables + NAT), emit an `evidence`/`human_only` finding suggesting the canonical module. This is the moat-y, hard part (pattern → recommended module).
- **Provider/well-architected best-practice patterns** — tagging standards, naming conventions, remote state + locking, provider version pinning. Some overlaps tflint rulesets; keep only what those don't cover.

**How (open design questions to resolve in the spike):**

- **Parsing** — need real HCL structure (module/resource blocks, `source`, `version`), not regex. Options: `python-hcl2`, or shell out to `terraform graph` / `terraform show -json` on a `plan`. Decide the dependency + whether a plan is feasible in CI (needs provider creds → probably parse HCL statically instead).
- **The "approved/recommended modules" rule pack** — this is the same **versioned, cited rule-pack** mechanism as Phase 4. A pack maps `(provider, resource-pattern) → recommended module + rationale + source URL`. Org-overridable (internal module catalog). Curation is a real workstream (like the GDS/Prowler packs).
- **Registry trust lookup** — call the Terraform Registry API to classify a module's `source` as official/partner/verified/community; cache it. Offline/air-gapped mode (Phase 10) needs a bundled snapshot.
- **Scoring + honesty** — a per-repo *module-adoption score* + per-deviation findings, with **waivers** (Phase 10): raw resources are sometimes correct (a one-off, a gap the module can't express). Default to `evidence` (◐) not `verified` (✅) for "should use a module" findings, since it's a judgement call — the LLM rewords, never decides.

**Where it slots in:** a new lens in `utils/lenses/` (e.g. `module_first.py`, likely the concrete shape of **A1's deeper half** or a sibling **A1b**), consuming a Phase-4 rule pack, emitting findings keyed to a `terraform-house`/`module-standards` control set. It depends on Phase 2 (registry — done) and Phase 4 (rule packs + ✅/◐/○ states).

**Done when (draft):** the spike produces (1) a decision on HCL parsing, (2) a v0 `module-standards` rule pack format with ≥1 cited entry (e.g. "prefer `terraform-aws-modules/vpc` over a hand-built VPC"), and (3) a lens that emits a module-adoption score + deviation findings on a real repo that match a human read. Until the spike lands, this stays a capture, not a commitment.

---

## Cross-cutting reminders (apply throughout)

- [x] Deterministic checks are the **source of truth**; the LLM never changes a verdict. *(Enforced in Phase 6 by the reword-only AI-backend interface — the AI can only rewrite prose.)*
- [ ] Keep the engine runnable as a **CLI/library** (the GitHub Action just wraps it) — no GitHub lock-in.
- [ ] Static checks before rendered ones (§9.3).
- [x] Preserve upstream MIT notices; new code AGPL (§2.6). *(Done: `NOTICE` preserves the upstream MIT notice; `LICENSE`/`pyproject.toml`/`README.md` are AGPL-3.0-or-later.)*
- [ ] Engine stays **standalone** (Python), separate from the NestJS hosted side (§2.4).
