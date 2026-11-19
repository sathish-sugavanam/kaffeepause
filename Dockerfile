FROM python:3.14.6-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy

ARG TARGETARCH
ARG GITLEAKS_VERSION=8.30.1
ARG OSV_SCANNER_VERSION=2.7.1
ARG SEMGREP_VERSION=1.186.0

WORKDIR /app

# git: the worker clones and pushes, and the agents commit in run containers.
# curl: fetches the scanners below.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# Security scanners for the security agent. All language-agnostic.
RUN case "$TARGETARCH" in \
      amd64) gl=x64; osv=amd64 ;; \
      arm64) gl=arm64; osv=arm64 ;; \
      *) echo "unsupported arch $TARGETARCH" && exit 1 ;; \
    esac \
    && curl -fsSL "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_linux_${gl}.tar.gz" \
       | tar -xz -C /usr/local/bin gitleaks \
    && curl -fsSL -o /usr/local/bin/osv-scanner \
       "https://github.com/google/osv-scanner/releases/download/v${OSV_SCANNER_VERSION}/osv-scanner_linux_${osv}" \
    && chmod +x /usr/local/bin/osv-scanner

RUN python -m pip install --no-cache-dir uv==0.12.17
# semgrep in its own environment, so its dependencies never meet the harness's.
RUN uv venv /opt/semgrep \
    && uv pip install --python /opt/semgrep/bin/python --no-cache "semgrep==${SEMGREP_VERSION}" \
    && ln -s /opt/semgrep/bin/semgrep /usr/local/bin/semgrep

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app
RUN uv sync --frozen --no-dev

EXPOSE 5001
CMD [".venv/bin/python", "-m", "app.webhook"]
