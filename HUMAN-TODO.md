# Human action items

Things only **you** can do — they need live credentials, real repositories, an
external runtime, or a human judgement call I can't make. Pulled out of
[`assessor-build-plan.md`](assessor-build-plan.md) so they don't get lost as the
engineering phases tick off. The engine-side work for each is already done; what
remains here is verification / dogfooding / curation.

> Legend: `[ ]` todo · `[x]` done. Add the date + a note when you close one.

## Verification (needs creds / a live PR / a push)

- [x] **Run the reviewer on a real PR and confirm the sticky comment posts.**
  *(Confirmed 2026-06-03 — verified live on the `terraform-aws-repo-examples`
  caller; sticky comment posts, evidence column populates with `gpt-5.4-mini`.)*
  Needs a throwaway repo + a `GITHUB_TOKEN` (with `pull-requests: write`) + at
  least one LLM key. Enable the wedge lenses too (`terraform-standard: default`,
  `cicd-standard: default`) to see A1/A2 in the comment. *(Phase 0 + Phase 5)*
- [x] **Confirm the CI workflow runs green end-to-end on GitHub Actions.**
  *(Confirmed 2026-06-03 — the reusable workflow + GHCR `:v1` image run green in
  Actions on a live PR.)*
  Local is green (202/202), but a push is needed to prove the `terraform-review.yml`
  reusable workflow + the GHCR image run in Actions. *(Phase 0)*

## Dogfooding (needs your real repos + judgement)

- [ ] **Dogfood A1 + A2 on ≥3 of your own client repos.** Enable with
  `terraform-standard: default` / `cicd-standard: default` (see
  [`examples/README.md`](examples/README.md)) and sanity-check the consistency /
  posture scores + deviation lists against your manual read. Tune the golden
  definitions (commit a custom JSON) where the defaults don't match your house
  standard. *(Phase 5 "Done when")*

## AI backend (needs the Copilot CLI + a PAT) — _added when Phase 6 lands_

- [ ] **Verify the GitHub Copilot backend against a live PAT.** Needs Node + the
  Copilot CLI installed alongside the engine and a `COPILOT_GITHUB_TOKEN`. BYOK
  (OpenAI/Anthropic/Gemini/Azure) is the tested default and needs nothing extra.
  *(Phase 6 "Done when" — Copilot half)*

## Dashboard (needs the hosted ingest endpoint + a key) — _added when Phase 9 lands_

- [ ] **Confirm scans land in the live dashboard.** Set `DASHBOARD_INGEST_URL`
  (+ `DASHBOARD_API_KEY`) and verify a scan appears with per-standard readiness.
  The engine side is done + unit-tested (opt-in + best-effort POST of the
  findings contract); this needs the hosted ingest endpoint stood up. *(Phase 9
  "Done when" — the live half.)*

## Rule-pack curation (ongoing, needs a human owner) — _Phase 9_

The process is written up in [`docs/rule-pack-curation.md`](docs/rule-pack-curation.md)
(definition kinds, the four-point shipping bar, refresh cadence, authoring steps).
What's left is the *people* part:

- [ ] **Name an owner** and stand up the **quarterly + event-driven refresh
  cadence** for the shipped packs.
- [ ] **Validate every rule against the live standard before publishing.** The
  bar + load-time checks exist; the actual judgement pass (re-resolve each
  `source_url`, confirm each rule still matches the current standard text, confirm
  the ✅/◐/○ classification is honest) is a human read. *(Phase 9 §9.1.)*
