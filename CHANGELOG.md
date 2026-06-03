# Changelog

## [1.2.2](https://github.com/IgnatG/terraform-reviewer/compare/v1.2.1...v1.2.2) (2026-06-03)


### Bug Fixes

* **config:** :sparkles: add whole-codebase LLM review option ([d169b6e](https://github.com/IgnatG/terraform-reviewer/commit/d169b6e419a9b5f01850b8ccc4250eeda669c6d5))

## [1.2.1](https://github.com/IgnatG/terraform-reviewer/compare/v1.2.0...v1.2.1) (2026-06-03)


### Documentation

* **readme:** :sparkles: enhance configuration options for inline comments and scan modes ([de6627b](https://github.com/IgnatG/terraform-reviewer/commit/de6627bf75d458ed62572a11f50c7ab8a88a0b7d))


### Miscellaneous

* :fire: remove GDS readiness lens and related components ([ea75fd1](https://github.com/IgnatG/terraform-reviewer/commit/ea75fd16ed88ad872d2b5c30e54ba83233d21550))

## [1.2.0](https://github.com/IgnatG/terraform-reviewer/compare/v1.1.0...v1.2.0) (2026-06-03)


### Features

* migrate to Python 3.14 ([549a67f](https://github.com/IgnatG/terraform-reviewer/commit/549a67f8286dd3b56eabd19c88854de63c946d83))

## [1.1.0](https://github.com/IgnatG/terraform-reviewer/compare/v1.0.0...v1.1.0) (2026-06-03)


### Features

* **config:** :sparkles: update default LLM model to claude-sonnet-4-6 ([8e131c6](https://github.com/IgnatG/terraform-reviewer/commit/8e131c6cbef5b64c7978900e2d63b6ce4c9f0676))
* **dependabot:** :sparkles: add Dependabot configuration for GitHub Actions and Docker ([8e131c6](https://github.com/IgnatG/terraform-reviewer/commit/8e131c6cbef5b64c7978900e2d63b6ce4c9f0676))
* scan mode, inline comments, collapsible report, release automation ([4d9e033](https://github.com/IgnatG/terraform-reviewer/commit/4d9e033d728714ae3bca18f29741421d3ed04cfb))


### Bug Fixes

* :construction_worker: update run-name for workflows to include dynamic context ([759c22f](https://github.com/IgnatG/terraform-reviewer/commit/759c22fae10a063c1b0ff5af821ec04982e3db60))
* **pyproject:** :bug: update checkov version to 3.2.532 ([b8273fd](https://github.com/IgnatG/terraform-reviewer/commit/b8273fd8f9ea2e989200e2c70fcdf7c2b03f2070))
* **release:** :bug: update uv setup action to version 8.1.0 ([f025828](https://github.com/IgnatG/terraform-reviewer/commit/f025828d7873a258e3ef441e0a92f9f36c05934e))


### Documentation

* :memo: update documentation for LLM model changes and release automation ([8e131c6](https://github.com/IgnatG/terraform-reviewer/commit/8e131c6cbef5b64c7978900e2d63b6ce4c9f0676))


### Build System

* **docker:** :sparkles: upgrade base image to python:3.14-slim ([14ecd71](https://github.com/IgnatG/terraform-reviewer/commit/14ecd71ec9bcac260a4eaee05bc8f300b23bfdf6))
* **workflow:** :sparkles: upgrade actions and Docker setup in CI workflows ([8e131c6](https://github.com/IgnatG/terraform-reviewer/commit/8e131c6cbef5b64c7978900e2d63b6ce4c9f0676))


### Miscellaneous

* **dependabot:** :wrench: configure major-only updates for GitHub Actions and Docker ([14ecd71](https://github.com/IgnatG/terraform-reviewer/commit/14ecd71ec9bcac260a4eaee05bc8f300b23bfdf6))
* **release:** :bookmark: bump version to 1.0.0 and update release process details ([8e131c6](https://github.com/IgnatG/terraform-reviewer/commit/8e131c6cbef5b64c7978900e2d63b6ce4c9f0676))
* **releasing:** :memo: update hardening practices and PAT usage instructions ([14ecd71](https://github.com/IgnatG/terraform-reviewer/commit/14ecd71ec9bcac260a4eaee05bc8f300b23bfdf6))
* **workflows:** :wrench: set timeout for build, CI, and release jobs ([14ecd71](https://github.com/IgnatG/terraform-reviewer/commit/14ecd71ec9bcac260a4eaee05bc8f300b23bfdf6))
