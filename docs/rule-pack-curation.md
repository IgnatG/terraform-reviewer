# Rule-pack & standard-definition curation

> Phase 9 of the [assessor build plan](../assessor-build-plan.md). The engine is
> only as good as the standards it cites. This is the **content workstream**:
> who owns the packs, how they stay current, and the bar a rule must clear before
> it ships. Engineering builds the mechanism; this keeps the moat honest.

## The two kinds of definition we ship

Both live inside the package so they ship in the wheel and are loadable by id.

| Kind | Where | Backs | Schema owner |
|------|-------|-------|--------------|
| **Rule packs** | `src/terraform_review_agent/rule_packs/*.json` | the standard-mapping + gap layer (Phase 4) — map a scanner finding → a standard control, detect absent artefacts, assign ✅/◐/○ | `utils/standards/pack.py` (`RulePack`) |
| **Golden definitions** | `src/terraform_review_agent/standards_defs/*.json` | the A1/A2/A5 lenses (Phase 5/7) — the house Terraform structure, the CI baseline, the GDS readiness points | `utils/standardisers/` models (`TerraformStandard`, `CICDBaseline`, `GDSDefinition`) |

Both carry the same provenance fields so every finding traces back to a source:

- a `version` / `standard_version` **and** a `rule_pack_version` (rule packs),
- a pack-level `source_url`, **and** a `source_url` on each control/point.

## The first packs (shipped)

| Definition | id | Cites | Status |
|------------|-----|-------|--------|
| Terraform house standard (A1) | `terraform-house` | [HashiCorp module structure](https://developer.hashicorp.com/terraform/language/modules/develop/structure) | shipped, v1.0.0 |
| CI/CD baseline (A2) | `ci-baseline` | [GitHub Actions hardening](https://docs.github.com/actions/security-guides/security-hardening-for-github-actions) | shipped, v1.0.0 |
| GDS readiness (A5) | `gds-readiness` | [Technology Code of Practice](https://www.gov.uk/guidance/the-technology-code-of-practice) | shipped, v1.0.0 |
| CIS AWS mapping pack (Phase 4) | `terraform-cis-aws` | [CIS AWS Foundations 3.0.0](https://www.cisecurity.org/benchmark/amazon_web_services) | shipped, 2026.06.0 |

> The **DSPT** pack belongs to the separate DSPT product, **not** this fork —
> this fork only *feeds it* code signals (secrets / TLS / dep-vulns / IaC) via the
> findings JSON. Don't add a DSPT pack here.

## The bar a rule must clear (before it ships)

A rule is only allowed in a pack when **all four** hold. This is the live-standard
validation gate (§9.1) — it is a human judgement, not something the engine asserts.

1. **It cites a live source.** A resolvable `source_url` to the *current* version
   of the standard, checked the day it lands. Dead links or "internal wiki" with
   no version = not shippable.
2. **The version is pinned.** Bump `rule_pack_version` (calendar `YYYY.MM.N`) on
   every change; bump `standard_version` when the upstream standard revs. Never
   edit a published rule's meaning in place without a version bump.
3. **The three-state is honest.** A rule is `verified` (✅) **only** if a
   deterministic scanner actually proves it. If it needs a human read it is
   `evidence` (◐); if the engine genuinely can't see it (rendered a11y, content
   design, secrets-in-history) it is `human_only` (○) and **excluded from any
   score** — never faked into a pass. This is the whole credibility of the tool.
4. **No dangling references.** Every `mappings[].control_id` resolves to a
   declared `control`, and every `expected_artifacts[].path` is repo-relative.
   `RulePack` load-time validation rejects both, so a bad pack fails fast in CI.

## Refresh cadence (the ongoing workstream — needs a human owner)

- **Quarterly review** of every shipped pack: re-resolve each `source_url`,
  diff against the upstream standard's current version, open a PR with the
  version bump + a changelog line. Owner signs off that each rule still reflects
  the live text.
- **Event-driven review** when an upstream standard publishes a new version
  (CIS benchmark, TCoP, GitHub Actions guidance) — don't wait for the quarter.
- **Citation/version trail.** Each pack edit is a reviewed PR; the
  `rule_pack_version` + git history *is* the audit trail. Keep the per-control
  `source_url` current so an auditor can click through from any finding.

> **Open human action** — standing up the owner + the quarterly cadence, and the
> first real validation pass against the live standards, is tracked in
> [`HUMAN-TODO.md`](../HUMAN-TODO.md). The engine, schema validation, and the
> first packs are done; the recurring curation is a people process.

## Authoring a new pack (mechanics)

1. Copy the closest existing pack/def as a starting point; give it a unique `id`.
2. Fill the provenance fields (versions + every `source_url`).
3. Drop it in `rule_packs/` (mapping packs) or `standards_defs/` (A-lens defs) —
   or, for org-private packs, point `RULE_PACKS_DIR` at a directory of extras.
4. Enable it: `ENABLED_RULE_PACKS=<id>` (packs) or the lens input
   (`terraform-standard` / `cicd-standard` / `gds-standard`).
5. `make test` — the schema + load-time validation run in CI; a malformed pack
   fails the build before it can emit a misleading finding.
