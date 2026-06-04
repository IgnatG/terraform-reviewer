# syntax=docker/dockerfile:1

# Single image used for both local `docker compose` dev and CI. It bundles the
# Python venv plus pinned scanner binaries so the reusable workflow needs no
# per-run installs.
#
# Two pinning sources, by necessity:
#   * Binary scanners below — pinned via these ARGs (not pip-installable).
#   * Python stack incl. checkov — pinned via pyproject.toml + uv.lock and
#     installed with `uv sync --frozen`, so rebuilds are deterministic.
# Bumping either is a rebuild-image PR.

# Pinned scanner binary versions (single source of truth for the binaries).
ARG TERRAFORM_VERSION=1.15.3
ARG TFSEC_VERSION=1.28.14
ARG TFLINT_VERSION=0.62.1
ARG INFRACOST_VERSION=0.10.44
ARG TRIVY_VERSION=0.71.0
# GitHub Copilot CLI — the standalone binary the github-copilot-sdk drives when
# AI_BACKEND=copilot. Self-contained (no Node runtime). Bump = rebuild-image PR.
ARG COPILOT_CLI_VERSION=1.0.59

# ---------------------------------------------------------------------------
# Stage 1 — fetch pinned scanner binaries (arch-aware via buildx TARGETARCH)
# ---------------------------------------------------------------------------
FROM debian:bookworm-slim AS scanners

ARG TERRAFORM_VERSION
ARG TFSEC_VERSION
ARG TFLINT_VERSION
ARG INFRACOST_VERSION
ARG TRIVY_VERSION
ARG COPILOT_CLI_VERSION
# TARGETARCH is auto-populated by buildx ("amd64" / "arm64"); matches the
# terraform/tfsec/tflint/infracost asset naming directly. trivy uses its own arch
# labels, mapped below.
ARG TARGETARCH

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl unzip \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /tmp/dl
RUN mkdir -p /out

# terraform — verified against HashiCorp's published SHA256SUMS (download named
# canonically so `sha256sum -c` matches the checksum line; empty grep => no
# matching line => sha256sum fails the build, so verification is fail-closed).
RUN curl -fsSL -o "terraform_${TERRAFORM_VERSION}_linux_${TARGETARCH}.zip" \
      "https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_linux_${TARGETARCH}.zip" \
    && curl -fsSL -o terraform_SHA256SUMS \
      "https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_SHA256SUMS" \
    && grep "  terraform_${TERRAFORM_VERSION}_linux_${TARGETARCH}.zip$" terraform_SHA256SUMS \
       | sha256sum -c - \
    && unzip -q "terraform_${TERRAFORM_VERSION}_linux_${TARGETARCH}.zip" terraform -d /out

# tfsec (release asset is the raw binary). tfsec is EOL (folded into trivy) and
# its release checksum-file naming isn't pinned here, so it relies on TLS + the
# execute-check below rather than a sha256 verify (unlike terraform/tflint/trivy).
RUN curl -fsSL -o /out/tfsec \
      "https://github.com/aquasecurity/tfsec/releases/download/v${TFSEC_VERSION}/tfsec-linux-${TARGETARCH}" \
    && chmod +x /out/tfsec

# tflint — verified against the release's checksums.txt.
RUN curl -fsSL -o "tflint_linux_${TARGETARCH}.zip" \
      "https://github.com/terraform-linters/tflint/releases/download/v${TFLINT_VERSION}/tflint_linux_${TARGETARCH}.zip" \
    && curl -fsSL -o tflint_checksums.txt \
      "https://github.com/terraform-linters/tflint/releases/download/v${TFLINT_VERSION}/checksums.txt" \
    && grep "  tflint_linux_${TARGETARCH}.zip$" tflint_checksums.txt | sha256sum -c - \
    && unzip -q "tflint_linux_${TARGETARCH}.zip" tflint -d /out

# infracost (tarball contains infracost-linux-<arch>). Relies on TLS + the
# execute-check below; its checksum-asset naming isn't pinned here.
RUN curl -fsSL -o infracost.tar.gz \
      "https://github.com/infracost/infracost/releases/download/v${INFRACOST_VERSION}/infracost-linux-${TARGETARCH}.tar.gz" \
    && tar -xzf infracost.tar.gz \
    && mv "infracost-linux-${TARGETARCH}" /out/infracost

# trivy (IaC misconfiguration scanning). Asset arch is 64bit/ARM64.
RUN case "${TARGETARCH}" in \
      amd64) TV_ARCH=64bit ;; \
      arm64) TV_ARCH=ARM64 ;; \
      *) echo "unsupported TARGETARCH for trivy: ${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && curl -fsSL -o "trivy_${TRIVY_VERSION}_Linux-${TV_ARCH}.tar.gz" \
      "https://github.com/aquasecurity/trivy/releases/download/v${TRIVY_VERSION}/trivy_${TRIVY_VERSION}_Linux-${TV_ARCH}.tar.gz" \
    && curl -fsSL -o trivy_checksums.txt \
      "https://github.com/aquasecurity/trivy/releases/download/v${TRIVY_VERSION}/trivy_${TRIVY_VERSION}_checksums.txt" \
    && grep "  trivy_${TRIVY_VERSION}_Linux-${TV_ARCH}.tar.gz$" trivy_checksums.txt | sha256sum -c - \
    && tar -xzf "trivy_${TRIVY_VERSION}_Linux-${TV_ARCH}.tar.gz" trivy \
    && mv trivy /out/trivy

# Fail the build immediately if any downloaded binary can't execute on the
# target arch (catches wrong-arch / truncated / corrupt downloads here, before
# they ship and silently degrade reviews).
RUN chmod +x /out/* \
    && for b in /out/*; do \
         "$b" --version >/dev/null 2>&1 || { echo "binary failed to execute: $b" >&2; exit 1; }; \
       done

# GitHub Copilot CLI (standalone self-contained binary — no Node runtime). Kept
# out of /out because the tarball is a tree, not a lone binary; the loop above
# verifies single binaries only. We locate the `copilot` executable wherever the
# tarball nests it, pin it at a stable path, and `--version` it so a wrong-arch
# or moved-layout download fails the build here rather than shipping broken.
RUN case "${TARGETARCH}" in \
      amd64) CP_ARCH=x64 ;; \
      arm64) CP_ARCH=arm64 ;; \
      *) echo "unsupported TARGETARCH for copilot: ${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && mkdir -p /opt/copilot/bin \
    && curl -fsSL -o copilot.tar.gz \
      "https://github.com/github/copilot-cli/releases/download/v${COPILOT_CLI_VERSION}/copilot-linux-${CP_ARCH}.tar.gz" \
    && tar -xzf copilot.tar.gz -C /opt/copilot \
    && CP_BIN="$(find /opt/copilot -type f -name copilot | head -n1)" \
    && if [ -z "$CP_BIN" ]; then echo "copilot binary not found in tarball" >&2; exit 1; fi \
    && chmod +x "$CP_BIN" \
    && ln -sf "$CP_BIN" /opt/copilot/bin/copilot \
    && /opt/copilot/bin/copilot --version

# ---------------------------------------------------------------------------
# Stage 2 — build the Python venv (build tools confined to this stage)
# ---------------------------------------------------------------------------
FROM python:3.14-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_PYTHON=python3.14 \
    UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1 \
    PATH="/app/.venv/bin:${PATH}"

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

# Pinned uv for a reproducible resolver/installer.
RUN pip install --no-cache-dir uv==0.11.15

WORKDIR /app

COPY pyproject.toml uv.lock ./
COPY src ./src

# Deterministic install straight from the committed lock: the app (editable, so
# compose can hot-reload ./src) plus the `container` extra (checkov). Dev tools
# are excluded; --frozen forbids any re-resolution at build time.
RUN uv sync --frozen --no-dev --extra container

# ---------------------------------------------------------------------------
# Stage 3 — final runtime image
# ---------------------------------------------------------------------------
FROM python:3.14-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/app/.venv \
    PATH="/opt/copilot/bin:/app/.venv/bin:${PATH}"

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 app

WORKDIR /app

# Scanner binaries on PATH for every user.
COPY --from=scanners /out/ /usr/local/bin/

# Copilot CLI tree (its bin/ is on PATH via the ENV above). Bundled for the
# optional AI_BACKEND=copilot path; BYOK never invokes it.
COPY --from=scanners /opt/copilot /opt/copilot

# Python venv (identical /app/.venv path keeps the editable install valid).
COPY --from=builder /app/.venv /app/.venv

COPY pyproject.toml ./
COPY src ./src
COPY scripts ./scripts

# Set the exec bit explicitly: Git on Windows checkouts doesn't preserve the
# Unix +x mode, so the COPY'd script can arrive as 0644 and fail with exit 126.
RUN mkdir -p /app/data && chmod +x ./scripts/*.sh && chown -R app:app /app

USER app

# Fail the build if any bundled binary fails to resolve on PATH.
RUN ./scripts/smoke_test.sh

CMD ["python", "-m", "terraform_review_agent.entrypoint"]
