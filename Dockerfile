FROM python:3.14.6-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy

ARG TARGETARCH
# Changing a scanner version means updating its pinned hashes below too.
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
# Hashes are pinned from each release's checksum file, so a swapped
# download fails the build instead of landing in every run container.
RUN case "$TARGETARCH" in \
      amd64) gl=x64; osv=amd64; \
             gl_sha=551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb; \
             osv_sha=fb7dab19a8bd9bbe5c3189f09d4b7ee1820c9553a23bac202648a56efd2f6ee8 ;; \
      arm64) gl=arm64; osv=arm64; \
             gl_sha=e4a487ee7ccd7d3a7f7ec08657610aa3606637dab924210b3aee62570fb4b080; \
             osv_sha=983d22fbad9724514e3fcc6acc83b5fa64c5ffa6b0d9f510028f22c1b223ff81 ;; \
      *) echo "unsupported arch $TARGETARCH" && exit 1 ;; \
    esac \
    && curl -fsSL -o /tmp/gitleaks.tar.gz \
       "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_linux_${gl}.tar.gz" \
    && echo "${gl_sha}  /tmp/gitleaks.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/gitleaks.tar.gz -C /usr/local/bin gitleaks \
    && curl -fsSL -o /usr/local/bin/osv-scanner \
       "https://github.com/google/osv-scanner/releases/download/v${OSV_SCANNER_VERSION}/osv-scanner_linux_${osv}" \
    && echo "${osv_sha}  /usr/local/bin/osv-scanner" | sha256sum -c - \
    && chmod +x /usr/local/bin/osv-scanner \
    && rm /tmp/gitleaks.tar.gz

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
