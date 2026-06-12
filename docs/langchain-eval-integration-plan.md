# Eval & agent integration plan (Tier 1 + Tier 2)

Follow-up to [`langchain-ecosystem-opportunities.md`](langchain-ecosystem-opportunities.md).
This is the concrete integration plan for the Tier 1 (evaluation) and Tier 2 (agent
ergonomics) items, with file touch-points, dependencies, and the documentation needed
for each.

> **Local constraint:** this repo runs on Python 3.14, which segfaults on SSL init on
> the maintainer's Windows box, so `uv`/`pytest` can't run locally — everything here is
> verified by **ruff/mypy static checks locally + CI (Linux) for the runtime suite**.
> After any `pyproject.toml` change, `uv lock` (a.k.a. `make lock`) must be run on
> Linux/CI to refresh `uv.lock`, or `make lint`'s `uv lock --check` will fail.

---

## Guiding principles for this codebase

1. **Eval deps stay out of the runtime.** The agent that runs in CI on every PR must
   not gain `agentevals`/`openevals`/`langsmith` as install deps. They go in an
   **`eval` optional-dependency group**; eval code lives in a top-level `evals/`
   package, never imported by `src/`.
2. **Deterministic-by-default, LLM-judge opt-in.** Structural checks (which lenses
   fired, schema shape) run offline in CI with no API key. LLM-as-judge checks
   (qualitative review quality) are opt-in and need a provider key.
3. **Reuse the offline fakes.** The integration tests already prove the graph runs
   fully offline with recorded scanners + a fake AI backend. Evals reuse that pattern
   so an eval run is hermetic.

---

## Tier 1 — Evaluation

### 1.1 agentevals — graph-trajectory evaluation  ✅ implementing first

**Goal:** regression-test the graph's *routing* — for a given PR, the right set of
lens nodes fire (skip on non-Terraform, single lens, parallel fan-out) — and,
opt-in, LLM-judge whether the trajectory was reasonable.

**Why graph-trajectory, not message-trajectory:** this agent has **no LLM
tool-calling loop**; lenses call scanners directly, so there are no `tool_calls` in
any message stream. agentevals' message-format evaluators
(`create_trajectory_match_evaluator`) would have nothing to match. The right
primitive is the **graph trajectory** family, which works on the sequence of graph
*nodes* (`steps`).

**Approach:**
- Compile a dedicated graph instance with an `InMemorySaver` checkpointer (the prod
  `agent` stays checkpointer-free per CLAUDE.md §6) so
  `extract_langgraph_trajectory_from_thread(graph, config)` can read the thread's
  step history.
- Run each PR fixture through it offline (stubbed scanners + AI-off).
- **Structural assertion (authoritative, deterministic):** flatten the extracted
  `steps`, drop framework sentinels (`__start__`, `__interrupt__`), and compare the
  **multiset (Counter) of real node names** to the fixture's expected counts.
  - *We deliberately do NOT use `graph_trajectory_strict_match` for the multi-lens
    case:* strict match is order-sensitive, and parallel `Send` fan-out emits the
    `lens` node N times within one superstep with no guaranteed intra-step order. A
    multiset check is the correct tolerance for parallelism.
- **Qualitative check (opt-in, `--judge`):** `create_graph_trajectory_llm_as_judge`
  over the same extracted trajectory; needs a provider key, so off in default CI.

**Files:**
- `evals/__init__.py`, `evals/_offline.py` (hermetic fakes context-manager),
  `evals/trajectories.py` (fixtures + expected node counts), `evals/run.py`
  (extract → evaluate → report + CLI).
- `tests/integration/test_evals_trajectory.py` — runs the cases offline, gates in CI
  (self-skips via `pytest.importorskip("agentevals")` when the extra isn't installed).
- `pyproject.toml` — new `eval` optional-dependency group.
- `Makefile` — `eval` target.

**Deps:** `agentevals>=0.0.9` (pulls `openevals>=0.0.20`; LLM-judge uses the existing
`langchain-openai`). Python `>=3.9` — fine on 3.14.

**Docs:**
- agentevals README (graph trajectory): <https://github.com/langchain-ai/agentevals>
- `extract_langgraph_trajectory_from_thread`, `create_graph_trajectory_llm_as_judge`,
  `graph_trajectory_strict_match` — same README, "Graph trajectory" section.
- LangGraph persistence / checkpointers (why a checkpointer is needed for thread
  history): <https://docs.langchain.com/oss/python/langgraph/persistence>
- `InMemorySaver`: `from langgraph.checkpoint.memory import InMemorySaver`.

---

### 1.2 LangSmith Datasets + offline Evaluations (LLM-as-judge)  ✅ implemented

**Goal:** a golden set of real Terraform PRs with known issues; quantify review
quality and catch regressions when swapping model/prompt.

**Approach (as built):**
- Golden dataset of cases in [`evals/golden.py`](../evals/golden.py): each `GoldenCase`
  = a PR + *recorded* scanner output + the deterministic finding identities it must
  produce + the expected summary (`GoldenCase.reference()`).
- A **target function** ([`evals/target.py`](../evals/target.py)) that runs the real
  compiled `agent` over `recorded_review(...)` (realistic scanners) and returns the
  parsed `findings.json` + comment. `use_llm=False` → deterministic; `use_llm=True` →
  live model rewording.
- **Evaluators** ([`evals/evaluators.py`](../evals/evaluators.py)) in LangSmith's
  `(inputs, outputs, reference_outputs)` shape: deterministic predicates (finding-set
  identity, severity preservation, schema validity) + openevals.
- LangSmith runner ([`evals/langsmith_run.py`](../evals/langsmith_run.py)):
  `sync_dataset()` + `client.evaluate(target, data=..., evaluators=[...], experiment_prefix=...)`.
  Gated behind `LANGSMITH_API_KEY`; never part of the per-PR run.

**Files:** `evals/golden.py`, `evals/target.py`, `evals/evaluators.py`,
`evals/langsmith_run.py`, `evals/run_quality.py` (local runner),
`tests/integration/test_evals_quality.py` (deterministic CI gate).

**Deps:** `langsmith` (added to the `eval` group).

**Docs:**
- Evaluate an app (Python): <https://docs.langchain.com/langsmith/evaluate-llm-application>
- Evaluation concepts (evaluator types, LLM-as-judge, datasets):
  <https://docs.langchain.com/langsmith/evaluation-concepts>
- pytest integration (`@pytest.mark.langsmith`, `langsmith.testing`):
  <https://docs.langchain.com/langsmith/pytest>

---

### 1.3 openevals — finding-quality + structured-output judges  ✅ implemented

**Goal:** ready-made judges for the *content* of findings (correctness, no
hallucination, structured-output match against the findings schema).

**Approach (as built, in [`evals/evaluators.py`](../evals/evaluators.py)):**
- `summary_json_match` — `create_json_match_evaluator(aggregator="all", exclude_keys=[…])`
  over the `findings.json` summary block (model-free, deterministic). *Gotcha found &
  handled:* `aggregator="all"` fails on any extra key, so the null `cost_*` headline
  keys are excluded (these golden cases have no cost lens).
- `make_llm_judges(model)` — `create_llm_as_judge` with `CORRECTNESS_PROMPT` /
  `HALLUCINATION_PROMPT` grading the *reworded comment* against the scanner-owned
  facts. Opt-in (needs a key); never gates the finding set.
- These run alongside the bespoke deterministic evaluators in both the local
  (`run_quality.py`) and LangSmith (`langsmith_run.py`) runs.

**Deps:** `openevals` (pulled transitively by agentevals; listed explicitly).

**Docs:**
- openevals README (`create_llm_as_judge`, prebuilt prompts, `create_json_match_evaluator`,
  code/E2B evaluators): <https://github.com/langchain-ai/openevals>
- Prebuilt prompt names: `CORRECTNESS_PROMPT`, `HALLUCINATION_PROMPT`,
  `CODE_CORRECTNESS_PROMPT`, `PII_LEAKAGE_PROMPT` (relevant given secrets handling).

---

## Tier 2 — Agent ergonomics (adopt only on concrete need)

### 2.1 `create_agent` + `response_format` + middleware

**Goal:** fold the reword-only structured-output call into one model↔tools step and
move cross-cutting concerns (PII redaction of plan/tfvars content, oversize-payload
summarization) into reusable **middleware** instead of ad-hoc code.

**Where it fits:** replaces the *body* of the AI backend / a lens's annotate step —
**not** the fan-out/fan-in graph (that stays raw LangGraph). Specifically, the
`LangChainBackend.annotate` path ([`ai/langchain_backend.py`](../src/terraform_review_agent/ai/langchain_backend.py))
and `_annotate.annotate_with_llm`.

**Approach (phased, behind a flag):**
- Build a `create_agent(model, tools=[], response_format=SpecialistAnnotations)` and
  compare its output to today's `with_structured_output` on the eval set before
  switching. Keep the `AIBackend.annotate` interface (the guardrail) unchanged.
- Add `before_model` middleware for `.tfvars`/secret redaction (today this is handled
  by excluding `.tfvars` payloads in `prepare_file_payloads`; middleware would make it
  a single enforced policy) and `before_model` summarization for oversize plan JSON.

**Deps:** `langchain>=1.x` (the project already depends on the v1 line via
`langchain-core>=1.4`; `create_agent` lives in the `langchain` package).

**Docs:**
- LangChain v1 release (create_agent, middleware overview):
  <https://www.langchain.com/blog/langchain-langgraph-1dot0>
- `create_agent` reference:
  <https://reference.langchain.com/python/langchain/agents/factory/create_agent>
- Middleware + `response_format`: <https://docs.langchain.com/oss/python/releases/langchain-v1>

**Risk:** medium — changes the LLM call path. Gate behind a config flag, prove parity
on the 1.2 eval set first, keep the old path as fallback.

### 2.2 trustcall — resilient structured extraction

**Goal:** drop-in hardening for `with_structured_output` *if* the
`SpecialistAnnotations` schema starts failing validation on some providers (the
backend already has a dict-vs-model fallback in `langchain_backend.py`, a hint this
is a real edge).

**Approach:** swap `get_llm().with_structured_output(...)` for a trustcall extractor
in `LangChainBackend.annotate`; it retries via JSON-patch on validation errors. Adopt
**only** if the eval run shows structured-output failures; otherwise skip.

**Deps:** `trustcall`.
**Docs:** <https://github.com/hinthornw/trustcall>
**Risk:** low/isolated — single function swap behind the `AIBackend` interface.

### 2.3 deepagents — Rubrics (self-correcting review quality)

**Goal:** have the rewording step self-evaluate its output against a rubric
(clarity, no severity drift, actionable suggestion) and self-correct — quality lift
without changing the deterministic finding set.

**Approach:** evaluate the **Rubrics** feature in isolation against the 1.2 eval set;
adopt only if it beats plain `create_agent`. The full deepagents harness (planning
tool, subagents, virtual filesystem) is **out of scope** — the explicit `Send`
fan-out already covers orchestration.

**Deps:** `deepagents>=0.6.8` (needs `langchain>=1.3.4`, Python ≥3.11 — OK on 3.14).
**Docs:**
- Rubrics: <https://www.langchain.com/blog/introducing-rubrics-for-deepagents>
- Overview: <https://docs.langchain.com/oss/python/deepagents/overview>
**Risk:** medium — new heavyweight dep; keep behind the eval gate, runtime-flagged.

---

## Sequencing

1. ✅ **agentevals graph-trajectory** — cheapest, no API key, pure routing regression net.
2. ✅ **LangSmith dataset + openevals judges** (1.2 + 1.3) — the quality baseline that
   every Tier 2 change must beat. Deterministic checks gate CI; live-model judges are opt-in.
3. **`create_agent` + middleware** (2.1) — *next.* Only after the baseline exists, behind a flag.
4. **trustcall / deepagents Rubrics** (2.2 / 2.3) — adopt only if the eval set shows
   they help.

Each Tier 2 step must show **no finding-set regression** and a **quality win** on the
Tier 1 eval set before it ships.
