"""Application configuration — loaded from environment / `.env`.

All settings are read once at process start via :class:`Settings`. Secrets are
never hardcoded; downstream modules (``llm.py``, ``github_client.py``, …) read
from :data:`settings` rather than from ``os.environ`` directly.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LLMProvider = Literal["openai", "anthropic", "google", "azure"]
# Scan scope: "full" reviews the whole repo (posture scan — the default for an
# assessor); "diff" scopes scanner findings to the files this PR changed.
ScanMode = Literal["full", "diff"]
# Which AI backend rewords findings: BYOK (your own provider key, the default)
# or the bundled GitHub Copilot CLI. Both only reword — never change a verdict.
AIBackendName = Literal["byok", "copilot"]
Environment = Literal["development", "staging", "production"]
# "none" disables CI gating; otherwise the run fails when a finding's severity
# meets or exceeds this floor. Mirrors `Severity` in utils.state plus "none".
FailOnSeverity = Literal["critical", "high", "medium", "low", "info", "none"]


class Settings(BaseSettings):
    """Process-wide configuration sourced from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        # Treat empty env values (`KEY=`) as unset so copying `.env.example`
        # doesn't crash int parsing or turn blank keys into SecretStr("").
        env_ignore_empty=True,
    )

    openai_api_key: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None
    google_api_key: SecretStr | None = None
    # Azure OpenAI (BYOK provider "azure"). Endpoint + deployment are required to
    # use it; the API version defaults to a current GA value.
    azure_api_key: SecretStr | None = None
    azure_openai_endpoint: str | None = None
    azure_openai_api_version: str = "2024-10-21"
    azure_openai_deployment: str | None = None

    # Which AI backend rewords findings (Phase 6). "byok" uses the provider below;
    # "copilot" shells out to the GitHub Copilot CLI. The deterministic finding
    # set is identical either way — the backend only rewrites message/suggestion.
    ai_backend: AIBackendName = "byok"

    default_llm_provider: LLMProvider = "anthropic"
    default_llm_model: str = "claude-sonnet-4-6"
    default_llm_temperature: float = 0.0
    # Best-effort determinism for providers that honor it (OpenAI). Reasoning
    # models still vary somewhat; pair with a pinned model snapshot. Set to
    # `none`/`off` to disable (a blank env value is treated as unset and keeps
    # the default).
    default_llm_seed: int | None = 7
    # When false (default) the specialist LLMs may only reword/clarify findings
    # the scanners produced — they cannot invent new `*:llm-` findings. Scanners
    # own detection and severity, so the finding *set* is deterministic across
    # runs. Flip to true to re-enable speculative LLM-discovered findings (more
    # coverage, less run-to-run consistency).
    enable_llm_findings: bool = False

    # Whole-codebase LLM review. When true, the security/style LLM is fed *every*
    # Terraform file in the repo (not just the PR diff) and discovery is forced on
    # — regardless of `enable_llm_findings`. Costs more tokens and is less
    # reproducible than the diff-scoped default, so it's opt-in.
    llm_full_review: bool = False

    # GitHub Copilot backend (Phase 6; used when ai_backend == "copilot"). The CLI
    # is invoked as a subprocess with COPILOT_GITHUB_TOKEN in its env. Both the
    # command and a per-call timeout are configurable so the wiring can adapt to
    # the installed CLI without code changes.
    copilot_github_token: SecretStr | None = None
    copilot_cli_command: str = "copilot"
    copilot_timeout_seconds: int = 120

    # Which lenses the registry runs, as a comma-separated list of ids (e.g.
    # "security,style"). Empty (default) runs every registered lens that applies
    # to the repo. Unknown ids are ignored. See `utils.lenses.registry`.
    enabled_lenses: str = ""

    # Scan scope (Phase 10). "full" (default) reports findings across the whole
    # repo — the assessor's posture scan; "diff" scopes scanner findings to the
    # files this PR changed. Repo-level lenses (A1/A2, gap detection) are
    # always whole-repo; this only governs the diff-scoped scanner lenses.
    scan_mode: ScanMode = "full"

    # Post a PR review with a comment on each finding that sits on a changed line
    # (Phase 10). On by default; findings off the diff stay in the sticky summary.
    # Re-runs are idempotent (a hidden per-finding marker dedupes).
    inline_comments: bool = True

    # Run `tflint --init` when the repo ships a `.tflint.hcl`. OFF by default
    # because `--init` downloads and executes the plugins that file declares — a
    # malicious PR could point it at an attacker-controlled plugin (arbitrary code
    # execution). Enable only for repos whose `.tflint.hcl` you trust; with it off,
    # tflint still runs its built-in rules (and skips cleanly if a plugin block
    # makes it error).
    tflint_init: bool = False

    # External check sources (Phase 3): each runs as its own CI step and writes a
    # SARIF report; set the path to ingest it. Empty (default) skips the source,
    # so the engine's behaviour is unchanged unless a report is supplied.
    #   prowler/trivy -> security lens · megalinter -> style lens
    prowler_sarif_path: str | None = None
    trivy_sarif_path: str | None = None
    megalinter_sarif_path: str | None = None
    # Coverage report (lcov/cobertura/jacoco) for the A3 lens (Phase 7).
    coverage_report_path: str | None = None
    # A3 flags a changed file whose line coverage is below this percentage.
    coverage_min_percent: float = 80.0

    # A4 Tech-Debt Scorecard sources (Phase 7). Each runs as its own CI step and
    # writes a report; set the path to ingest it. Empty = that signal is skipped.
    jscpd_report_path: str | None = None  # jscpd duplication JSON
    sonarqube_sarif_path: str | None = None  # SonarQube issues exported as SARIF

    # Standard-mapping layer (Phase 4). Which rule packs are active:
    #   empty (default) -> none (mapping inert; findings carry no control_id)
    #   "*"             -> every discovered pack
    #   CSV of ids      -> just those (e.g. "terraform-cis-aws")
    enabled_rule_packs: str = ""
    # Optional directory of extra/custom rule-pack JSON (built-in packs ship with
    # the engine and are always discoverable).
    rule_packs_dir: str | None = None

    # Wedge lenses (Phase 5). Each names the golden-standard definition to enforce:
    #   empty (default) -> the lens is off (behaviour unchanged)
    #   "default"       -> the built-in definition shipped with the engine
    #   a path          -> a custom definition JSON (your house standard)
    # A1 Terraform Standardiser: golden module structure (required files/blocks).
    terraform_standard: str = ""
    # A2 CI/CD Standardiser: golden .github/workflows posture baseline.
    cicd_standard: str = ""

    sqlite_path: str = "./data/state.sqlite"

    github_token: SecretStr | None = None
    github_repository: str | None = None
    github_pr_number: int | None = None
    github_api_url: str = "https://api.github.com"

    infracost_api_key: SecretStr | None = None
    infracost_baseline_path: str | None = None

    fail_on_severity: FailOnSeverity = "none"

    # When true, the run exits non-zero (failing the CI check) if a *configured*
    # AI backend call failed this run — e.g. a bad key, exhausted credits, or an
    # unsupported model. The deterministic scanner report still posts either way
    # (graceful degradation, §9.2); this only controls whether such a failure is
    # surfaced as a red check. Off by default so a transient LLM blip can't block
    # PRs; AI failures are always emitted as a GitHub annotation regardless.
    fail_on_ai_error: bool = False

    workspace_dir: str = "."

    # Where the aggregator's findings.json (the versioned output contract) is
    # written each run. Uploaded as a CI artefact and POSTed to the dashboard.
    findings_output_path: str = "./findings.json"
    # Phase 8 output surfaces (all written from the same report; CI uploads them).
    # SARIF lands in GitHub code-scanning; the evidence pack is the downloadable
    # ✅/◐/○ artefact (HTML prints to PDF) + a CSV export.
    sarif_output_path: str = "./findings.sarif"
    evidence_html_path: str = "./evidence-pack.html"
    evidence_csv_path: str = "./findings.csv"

    # Hosted dashboard ingest (Phase 9). After each scan the findings.json is
    # POSTed here for per-standard readiness history + cross-repo rollups.
    #   empty (default) -> off; no POST, behaviour unchanged
    #   a URL           -> POST the report there (Bearer-authed with the key below)
    # The POST is best-effort: a failure is logged and never fails the scan.
    dashboard_ingest_url: str | None = None
    dashboard_api_key: SecretStr | None = None
    dashboard_timeout_seconds: int = 30

    langsmith_api_key: SecretStr | None = None
    langsmith_tracing: bool = False
    langsmith_project: str = "terraform-review-agent"
    # Region endpoint; blank = US default. Set to https://eu.api.smith.langchain.com
    # for an EU-region workspace (a US/EU mismatch returns 403 Forbidden).
    langsmith_endpoint: str | None = None

    log_level: str = "INFO"
    environment: Environment = "development"

    sticky_comment_marker: str = Field(
        default="<!-- terraform-review-agent:v1 -->",
        description="Hidden HTML marker used to find/upsert the bot's PR comment.",
    )

    @field_validator("default_llm_seed", mode="before")
    @classmethod
    def _seed_sentinel(cls, value: object) -> object:
        """Map a ``none``/``off`` env string to ``None`` so the seed can be disabled.

        Pydantic can't coerce ``"none"`` to ``int | None`` on its own (it errors),
        so the explicit off-switch is a sentinel string. A blank value never
        reaches here — ``env_ignore_empty`` drops it and the default applies.
        """

        if isinstance(value, str) and value.strip().lower() in {"none", "off", "disabled"}:
            return None
        return value

    def provider_key(self, provider: LLMProvider | None = None) -> SecretStr | None:
        """Return the API key for ``provider`` (defaults to the configured provider)."""

        target = provider or self.default_llm_provider
        if target == "openai":
            return self.openai_api_key
        if target == "anthropic":
            return self.anthropic_api_key
        if target == "google":
            return self.google_api_key
        if target == "azure":
            return self.azure_api_key
        raise ValueError(f"Unsupported LLM provider: {target!r}")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached :class:`Settings` singleton."""

    return Settings()


settings = get_settings()
