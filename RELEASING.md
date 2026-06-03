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
   accumulates the CHANGELOG + version bumps. It updates `pyproject.toml`
   (`release-type: python`) and — via `extra-files` — the image pin in
   `.github/workflows/terraform-review.yml`.
3. **Merge that PR** to cut the release. On the merge, the workflow:
   - creates the GitHub release + the exact **`vX.Y.Z`** git tag;
   - moves the **`vX`** major-float git tag to the new release (`tag-major` job);
   - builds + pushes the image as **`:vX.Y.Z` + `:vX` + `:latest`** (`build-image`
     called with the new version).

So both `uses: …@v1` (auto-updates within the major) and `…@v1.2.3` (pinned) work
for consumers, and the matching image tags exist for each.

## Why the image build is chained (not a tag trigger)

A tag created with the default `GITHUB_TOKEN` **does not trigger other
workflows**, so `build-image.yml` can't watch for the release tag. Instead
`release-please.yml` calls `build-image.yml` directly when `release_created ==
true`. `build-image.yml` is also `workflow_dispatch`-able with a `version` input
for manual rebuilds.

## One-time setup on GitHub

- **Settings → Actions → General →** enable *"Allow GitHub Actions to create and
  approve pull requests"* (so the release PR can open).
- **Package visibility:** the GHCR package must be **public** (its own package
  settings page) for other repos to pull it.
- *(Optional)* To run CI on the release PR itself, create a PAT secret and set
  `token: ${{ secrets.RELEASE_PAT }}` on the `release-please-action` step — PRs
  opened by the default token don't trigger downstream CI.

## Files

- `release-please-config.json` — release-type + `extra-files` (the image pin).
- `.release-please-manifest.json` — the current released version (source of
  truth; release-please updates it on each release).
- `.github/workflows/release-please.yml` — the release + tag-major + build jobs.
- The image pin in `terraform-review.yml` carries `# x-release-please-version`;
  the README/examples use the `@v1` float so they never need a manual bump.

## Bootstrapping note

The manifest starts at `1.0.0`. The first release PR will include everything
landed since then — with the Phase 10 work being `feat`s, that first release is
**`1.1.0`**, which publishes the `v1` git tag + `:v1` image for the first time.
