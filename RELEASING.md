# Releasing

Releases are **automated** by [release-please](https://github.com/googleapis/release-please)
from [Conventional Commits](https://www.conventionalcommits.org). You don't tag
or bump versions by hand.

## How it works

1. Land changes on `main` with Conventional Commit messages. The bump level
   follows the Conventional Commits spec and is **not** configurable per type:
   - `feat:` → **minor** · `fix:` and `deps:` → **patch** · `feat!:` or a
     `BREAKING CHANGE:` footer → **major**.
   - `chore:`, `docs:`, `refactor:`, `build:`, `ci:`, `test:`, `perf:` **do not
     bump the version** on their own — but they *do* show up in the CHANGELOG
     (under their `changelog-sections` heading) when a release is cut by a
     releasable commit. So docs land in the changelog without forcing a release.
   - **Maintenance that should ship a patch** (a Dockerfile/scanner-version bump,
     a dependency update): commit it as `fix:` or `deps:` so it bumps a patch and
     rebuilds the image. (release-please cannot map `chore:` → a bump — `chore` is
     a non-releasable type by design.)
2. `release-please.yml` keeps an open **"chore(main): release X.Y.Z" PR** that
   accumulates the CHANGELOG + version bumps. It updates `pyproject.toml`,
   `src/terraform_review_agent/__init__.py` (`release-type: python`), and the
   manifest. It does **not** touch `.github/workflows/` — release-please can't
   commit to workflow files (the token has no `workflow` scope), so the reusable
   workflow pins the **`:v1` image float** instead of an exact version (see below).
3. **Merge that PR** to cut the release. On the merge, the workflow:
   - creates the GitHub release + the exact **`vX.Y.Z`** git tag;
   - moves the **`vX`** (major) and **`vX.Y`** (minor) float git tags to the new
     release (`tag-floats` job) — GitHub's recommendation is to keep both current;
   - builds + pushes the image as **`:vX.Y.Z` + `:vX.Y` + `:vX` + `:latest`**
     (`build-image` called with the new version), mirroring the git tags.

So consumers can pin at any level — `@v1` (any 1.x.y), `@v1.2` (any 1.2.x), or
`@v1.2.3` (exact) — and the matching image tag exists for each. A float only
crosses a boundary on the matching bump: `v1` jumps to `v2` **only** on a breaking
(`feat!`) release; `v1.2` advances to `v1.3` on a `feat`; patches stay within.

## Why the image build is chained (not a tag trigger)

A tag created with the default `GITHUB_TOKEN` **does not trigger other
workflows**, so `build-image.yml` can't watch for the release tag. Instead
`release-please.yml` calls `build-image.yml` directly when `release_created ==
true`. `build-image.yml` is also `workflow_dispatch`-able with a `version` input
for manual rebuilds.

## Keeping uv.lock in sync (the `sync-lock` job)

release-please bumps `version` in `pyproject.toml` but **not** in `uv.lock`, so on
the release PR the lockfile's own package version drifts and CI's `uv lock
--check` (via `make lint`) fails. release-please **can't** fix this natively — it's
the open [release-please#2561](https://github.com/googleapis/release-please/issues/2561),
and the `extra-files` generic-updater workaround doesn't hold because `uv lock`
strips the `x-release-please-version` annotation on any real rewrite (e.g. a
Dependabot bump), so the marker never survives.

So `release-please.yml` carries a **`sync-lock` job** (no separate workflow file):
gated on `prs_created == 'true'`, it checks out the release PR branch (from the
`pr` output's `headBranchName`), runs `uv lock` — which writes the bumped pyproject
version into `uv.lock` — and pushes the result back **with `RELEASE_PAT`** so the
commit re-triggers CI. It runs *after* the `release-please` job has (re)written the
branch, and the push lands on the release branch (not `main`), so it doesn't
re-trigger the workflow — no loop. (This is the second consumer of `RELEASE_PAT`,
alongside release-please itself.)

## One-time setup on GitHub

- **Settings → Actions → General →** enable *"Allow GitHub Actions to create and
  approve pull requests"* (so the release PR can open).
- **Package visibility:** the GHCR package must be **public** (its own package
  settings page) for other repos to pull it.

## Hardening (best practices applied)

- **Major-tag action pins.** Every `uses:` in our workflows pins a **major tag**
  (`actions/checkout@v6`, `docker/build-push-action@v7`, …). A major tag already
  floats across its own minor/patch releases and only jumps on a breaking major,
  which is the maintenance/safety trade-off we want here. (Full SHA pins are
  GitHub's strictest [hardening](https://docs.github.com/en/actions/security-for-github-actions/security-guiding-principles/security-hardening-for-github-actions)
  option and what our own A2 lens prefers — we accept the looser major-tag pin
  deliberately; revisit if supply-chain risk tolerance changes.)
- **Dependabot, major-only for actions** (`.github/dependabot.yml`). Three
  ecosystems, all weekly, all `deps:`-prefixed (so a bump cuts a patch release):
  `github-actions` **ignores minor + patch** (a major tag already covers those —
  only a new major opens a PR), `docker` (base-image security patches), and `uv`
  (Python deps in `uv.lock`).
- **Job hardening.** Workflows set top-level least-privilege `permissions`,
  `concurrency` groups (release-please serialises on `main` without cancelling
  in-flight; the others cancel superseded runs), and `timeout-minutes` so a hung
  job can't run to the 6-hour default.
- **Tag protection (do this in the UI).** Protect the *exact* release tags so
  they're immutable, while leaving the floats movable:
  **Settings → Rules → Rulesets → New tag ruleset** → target tags matching
  `v[0-9]*.[0-9]*.[0-9]*` → enable *Restrict deletions* + *Restrict updates*
  (block force-moves). Do **not** match `v*` broadly — that would block the
  `tag-floats` job from moving `v1` / `v1.2`. (Three-part `vX.Y.Z` tags match;
  the one/two-part floats don't.)

## Running CI on the release PR, and pinning the exact image — the PAT route (#4)

The default `GITHUB_TOKEN` has two limits here: PRs it opens **don't trigger CI**,
and it **can't write to `.github/workflows/`** (so it can't bump an exact image
pin — that's why the workflow uses the `:v1` float). A Personal Access Token
fixes both.

**This is now configured:** `release-please.yml` reads `token: ${{ secrets.RELEASE_PAT }}`,
so as long as the `RELEASE_PAT` secret exists the release PR runs CI and opens
without the org/repo "Allow GitHub Actions to create and approve pull requests"
toggle. For reference, the secret was set up as:

1. **Create the PAT.** GitHub → your **Settings** (not the repo) →
   *Developer settings*:
   - **Fine-grained** (preferred): *Personal access tokens → Fine-grained tokens →
     Generate*. Scope it to the **`IgnatG/terraform-reviewer`** repo with
     **Repository permissions**: *Contents: Read and write*, *Pull requests: Read
     and write*, *Workflows: Read and write*. Set an expiry + a calendar reminder
     to rotate it (a fine-grained PAT expires — when it lapses, releases stop
     until you regenerate it and update the secret).
   - **Classic** (simpler): *Tokens (classic) → Generate* with the **`repo`** and
     **`workflow`** scopes.
2. **Add it as a repo secret.** Repo → *Settings → Secrets and variables →
   Actions → New repository secret* → name **`RELEASE_PAT`**, paste the token.
3. **Already wired in.** `.github/workflows/release-please.yml` passes
   `token: ${{ secrets.RELEASE_PAT }}` on the `release-please-action` step. (If you
   ever remove the PAT, drop that line and enable the repo toggle instead.)

4. **(Only if you want the exact image pinned per release.)** Put the workflow
   back under `extra-files` in `release-please-config.json`:

   ```jsonc
   "extra-files": [{ "type": "generic", "path": ".github/workflows/terraform-review.yml" }]
   ```

   and annotate the pin in `terraform-review.yml`:

   ```yaml
   image: ghcr.io/ignatg/terraform-reviewer:v1.0.0 # x-release-please-version
   ```

   With the PAT's `workflow` scope, release-please can now commit that file, so
   each release bumps the pin to the exact version. (Skip this step to keep the
   `:v1` float — both are valid.)

Trade-offs: a PAT is a long-lived credential you must rotate, and it carries
broader scope than `GITHUB_TOKEN`. If you don't need CI-on-release-PRs or exact
image pinning, the default token is the lower-maintenance choice.

## Image pinning (why `:v1`, not `:vX.Y.Z`)

release-please **cannot commit to `.github/workflows/`** — the default token has
no `workflow` scope, and trying (via `extra-files`) throws the ambiguous
`Error adding to tree`
([release-please-action#938](https://github.com/googleapis/release-please-action/issues/938)).
So `terraform-review.yml` pins the **`:v1` major float**, which `build-image`
keeps pointing at the newest `v1.x.y` image — no per-release edit needed.
Consumers still get reproducible *workflow logic* by pinning the git ref
(`uses: …@v1.2.3`); the bundled scanners track the major.

> Want the exact image pinned per release too? Give release-please a PAT with the
> `workflow` scope (`token: ${{ secrets.RELEASE_PAT }}` on the action) and add the
> workflow back to `extra-files` with an `# x-release-please-version` annotation.
> The same PAT also makes the release PR run CI.

## Files

- `release-please-config.json` — release-type, changelog-sections, plain-tag
  settings (`include-component-in-tag: false`).
- `.release-please-manifest.json` — the current released version (source of
  truth; release-please updates it on each release).
- `.github/workflows/release-please.yml` — the release + sync-lock + tag-floats +
  build jobs (sync-lock re-locks `uv.lock` on the release branch; see above).
- README/examples use the `@v1` float so they never need a manual bump.

## Bootstrapping note

The manifest starts at `1.0.0`. The first release PR will include everything
landed since then — with the Phase 10 work being `feat`s, that first release is
**`1.1.0`**, which publishes the `v1` git tag + `:v1` image for the first time.
