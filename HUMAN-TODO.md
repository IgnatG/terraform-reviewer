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

- [~] **GitHub Copilot backend — PARKED, blocked on an org/enterprise policy
  (2026-06-03).** Verified live end-to-end through every layer: the SDK over the
  image-bundled standalone CLI round-trips, delta/event handling works, and a
  **user-owned fine-grained PAT with the "Copilot Requests" permission**
  authenticates (classic `ghp_` and PATs without that permission are rejected).
  The only remaining blocker is an **account entitlement**: the agentic Copilot
  CLI returns *"You are not authorized to use this Copilot feature, it requires
  an enterprise or organization policy to be enabled"* — an org/enterprise owner
  must enable the CLI feature under **Org → Settings → Copilot → Policies**. No
  code change unblocks it. The `terraform-aws-repo-examples` caller is reverted
  to `ai-backend: byok` (which works) so its check is green; flip it back once
  the policy is on. BYOK remains the tested default. *(Phase 6 "Done when")*

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
