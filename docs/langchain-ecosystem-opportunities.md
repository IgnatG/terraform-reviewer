# LangGraph usage & LangChain/LangSmith ecosystem opportunities

Research note (June 2026) on how `terraform-review-agent` uses LangGraph today, and
which LangChain/LangSmith ecosystem tools could be worth adopting. Sources are
linked per item.

---

## 1. How LangGraph is used today

The project is a **stateless, one-shot CI job**: a GitHub Action invokes
[`entrypoint.run`](../src/terraform_review_agent/entrypoint.py), which drives a
compiled LangGraph and posts a single sticky PR comment. No state is kept between
runs. That framing matters — several headline ecosystem features (checkpointers,
human-in-the-loop, prompt hub) solve problems a stateless CI job doesn't have.

What's actually wired in:

| Area | Usage | Where |
|---|---|---|
| **Graph topology** | `StateGraph[ReviewState]` with fan-out/fan-in: `start → [lens ∥ lens ∥ …] → aggregator → post_comment`. One `Send` per enabled lens (registry-driven, so adding a lens needs no graph change); the aggregator is a **deferred** node so it runs once after every branch finishes. | [`agent.py`](../src/terraform_review_agent/agent.py) |
| **Send API + conditional edges** | `fan_out_to_lenses` returns `list[Send]` (or routes straight to the aggregator when no lens applies). | [`agent.py:31`](../src/terraform_review_agent/agent.py#L31) |
| **Provider factory** | `get_llm()` switches OpenAI / Anthropic / Google / Azure via the `langchain-*` integrations; reasoning-model carve-out drops `temperature`/`seed`. | [`llm.py`](../src/terraform_review_agent/llm.py) |
| **Structured output** | `get_llm().with_structured_output(SpecialistAnnotations)` forces the reword-only AI backend's schema. | [`ai/langchain_backend.py:32`](../src/terraform_review_agent/ai/langchain_backend.py#L32) |
| **Tools** | `@tool` from `langchain_core.tools` wraps the scanner subprocesses (tfsec/checkov/tflint/infracost/trivy/…). Currently invoked directly (`.invoke({...})`), **not** bound to an LLM. | [`utils/tools.py`](../src/terraform_review_agent/utils/tools.py) |
| **State** | Pydantic `ReviewState`; `Annotated[list[AnyMessage], add_messages]` pattern per CLAUDE.md. | `utils/state.py` |
| **Observability** | LangSmith **tracing only**, opt-in via `LANGSMITH_API_KEY` / `LANGSMITH_TRACING` / `LANGSMITH_PROJECT`. | [`config.py:186`](../src/terraform_review_agent/config.py#L186) |

**Not used today:** deepagents, checkpointers/durable execution, human-in-the-loop
interrupts, LangSmith evaluations/datasets, the Prompt Hub, `create_agent`,
supervisor/swarm multi-agent libs, streaming.

---

## 2. Ecosystem opportunities (ranked by fit for *this* CI reviewer)

### Tier 1 — strong fit, worth piloting

- **LangSmith Datasets + offline Evaluations (LLM-as-judge)** — Build a golden set
  of Terraform PRs with known issues, run experiments with `client.evaluate(...)`,
  and catch review-quality regressions when you swap models/prompts. Reuses the
  `LANGSMITH_API_KEY` already plumbed in. Highest-value LangSmith feature for a
  reviewer. → <https://docs.langchain.com/langsmith/evaluation-concepts> ·
  <https://www.langchain.com/langsmith/evaluation>

- **openevals** (`pip install openevals`) — Ready-made evaluators; core is
  `create_llm_as_judge(prompt, model)`. Ships **code evaluators** (Pyright/Mypy/tsc)
  and **sandboxed execution via E2B** (`e2b-code-interpreter`). Strong fit: grade
  reviewer output *and* actually execute/validate suggested fixes in a sandbox —
  and the wider stack already uses E2B. → <https://github.com/langchain-ai/openevals>
  · <https://www.langchain.com/blog/evaluating-llms-with-openevals>

- **agentevals** (`pip install agentevals`) — Evaluators for agent **trajectories**
  via `create_trajectory_match_evaluator`. Verifies the fan-out reviewer inspected
  the right resources / took the right tool steps, not just that the final comment
  looks plausible. → <https://github.com/langchain-ai/agentevals>

### Tier 2 — useful if simplifying nodes or hitting schema flakiness

- **`create_agent` + `response_format` + middleware** (`pip install langchain`, 1.x) —
  The 1.0 prebuilt tool-calling agent. `response_format=<finding schema>` folds
  structured output *into* the model↔tools loop (no extra `with_structured_output`
  round-trip). **Middleware** (`before_model`/`after_model`/`wrap_tool_call`) gives
  central PII redaction of plan secrets + summarization of huge plan JSON. Would
  replace an individual lens *node*, **not** the fan-out/fan-in graph (that's exactly
  what raw LangGraph is for). → <https://www.langchain.com/blog/langchain-langgraph-1dot0>
  · <https://docs.langchain.com/oss/python/releases/langchain-v1>

- **trustcall** (`pip install trustcall`) — Reliable structured extraction via
  JSON-patch ops with resilient retry on validation errors; shines on large/nested
  schemas. Adopt only if `with_structured_output` starts failing validation on the
  findings schema. → <https://github.com/hinthornw/trustcall>

- **deepagents — Rubrics** (`pip install deepagents`, 0.6.8; needs `langchain>=1.3.4`,
  Py≥3.11) — The full harness (planning tool, subagents, virtual filesystem) is
  **overkill** vs the existing explicit Send-API fan-out, but the **Rubrics** feature
  (agents self-evaluate and correct their output against a rubric) is the one piece
  that maps onto improving review quality. → <https://www.langchain.com/blog/introducing-rubrics-for-deepagents>
  · <https://docs.langchain.com/oss/python/deepagents/overview>

### Tier 3 — situational / lower value for a stateless CI job

- **LangSmith Annotation Queues + online evaluation** — Route real review comments
  to engineers for thumbs-up/down, feed labels back into datasets / judge calibration;
  attach evaluators to live traces to flag low-confidence findings. Valuable once a
  golden eval set exists. → <https://docs.langchain.com/langsmith/evaluation-concepts>

- **LangSmith Prompt Hub** (`client.pull_prompt("name:prod")`) — Versioned central
  prompt store. Lower value here: prompts live in CI code, and pinning a `:prod` tag
  adds a network dependency to every run. → <https://docs.langchain.com/langsmith/manage-prompts>

- **Streaming** (`stream_mode="messages"|"updates"|"custom"`) — Stream per-lens
  findings into the CI log as branches complete instead of waiting for fan-in.
  Cosmetic for a batch job. → <https://www.langchain.com/blog/token-streams-to-agent-streams>

- **`Command`** — Return state update + next-edge routing in one object; could tidy
  the aggregator's conditional routing. Minor. → <https://docs.langchain.com/oss/python/langgraph/persistence>

- **Checkpointers / durable execution** (`langgraph-checkpoint-sqlite`/`-postgres`),
  **human-in-the-loop `interrupt()`**, **`langgraph-supervisor`** (0.0.31),
  **`langgraph-swarm`**, **LangGraph Platform/Studio** — General-purpose power, but
  they target persistence, approval gates, dynamic routing, and conversational/hosted
  apps. A one-shot CI run that re-runs cheaply, dispatches deterministically, and posts
  a comment doesn't currently need them. (Studio is handy for *locally* debugging the
  fan-out graph visually.) → <https://docs.langchain.com/oss/python/langgraph/persistence>

---

## 3. Recommendation

1. **Add a LangSmith eval harness** (Datasets + LLM-as-judge) — the single biggest win;
   turns "did the prompt/model change make reviews worse?" into a measurable experiment.
   There's already an `evals/govuk/` runner pattern elsewhere in the org to mirror.
2. **Layer in openevals + agentevals** for code/type-check evaluators, E2B-sandboxed
   fix validation, and trajectory checks.
3. **Defer** `create_agent`/middleware and trustcall until there's a concrete pain
   (node complexity or schema-validation flakiness).
4. **Skip** checkpointers, HITL, supervisor/swarm, and Prompt Hub unless the product
   gains state, approval steps, or auto-fix.

> Package versions current as of June 2026: deepagents 0.6.8, langgraph-supervisor
> 0.0.31 (needs langgraph ≥1.0.2), langchain 1.x / langgraph 1.x (GA Oct 2025).
