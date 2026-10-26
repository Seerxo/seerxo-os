# Base images pinned by digest (multi-arch index, resolved 2026-10-06): the same commit
# builds the same base. Bump deliberately: change tag and digest together.
FROM python:3.12-slim@sha256:02108f5d322dd89f1c9e552442c25acb0543dfdbc455693a5599624f20d9155d AS api

# postgresql-client is needed by scripts/backup.sh (pg_dump/pg_restore) and by
# restore-test.sh, which restores into a scratch database to prove the dump is usable.
# age encrypts the secret-bearing backups before R2 (scripts/backup.sh ENCRYPT_TARGETS).
RUN apt-get update && apt-get install -y --no-install-recommends \
        postgresql-client curl ca-certificates age \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY alembic.ini .
COPY migrations ./migrations
COPY app ./app
COPY scripts ./scripts
COPY tests ./tests
COPY agents ./agents
COPY skills ./skills
COPY docs/PERMANENT_FACTS.md ./docs/PERMANENT_FACTS.md
RUN chmod +x scripts/*.sh

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
EXPOSE 8000

# No host port is published in compose — §109, internal only.
# --no-proxy-headers: the client address is the TCP peer, so X-Forwarded-For cannot pose as
# loopback, which app/api_auth.py treats as the host's own `docker exec` callers.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers"]

# --------------------------------------------------------------------------- worker
# The unattended worker (app/worker.py): control plane only. Same code plus git and gh for
# Stage C publishing. No model CLI: those run in company-reasoner (the `reasoner` stage), so
# the container holding DATABASE_URL and the GitHub token cannot start a model at all.
FROM api AS worker
RUN apt-get update && apt-get install -y --no-install-recommends git gh \
    && rm -rf /var/lib/apt/lists/*
# Non-root: nothing in this container may touch the image or other containers.
RUN useradd --create-home --uid 10001 worker
USER worker
ENV HOME=/home/worker
CMD ["python", "-m", "app.worker", "--interval", "60"]

# --------------------------------------------------------------------------- coder
# Stage C coder sandbox (docs/CODER_RUNTIME.md): the two pinned CLIs and nothing else, no
# app code, no credentials. Started only by scripts/company-coder-launch with a fixed
# profile; python stays so the sandbox proof (check-coder-sandbox.py) can probe this image.
FROM python:3.12-slim@sha256:02108f5d322dd89f1c9e552442c25acb0543dfdbc455693a5599624f20d9155d AS coder
RUN apt-get update && apt-get install -y --no-install-recommends nodejs npm ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g @anthropic-ai/claude-code@2.1.286 \
    && npm cache clean --force \
    && useradd --create-home --uid 10001 coder
USER coder
ENV HOME=/home/coder
# Coolify's nightly docker cleanup (00:00 UTC) removes every unused image without this label; the coder image runs
# only per job, so it was deleted each night and Stage C preflight lost coder_image/claude_auth.
LABEL coolify.managed=true

# --------------------------------------------------------------------------- reasoner
# Stage B model runtime (app/reasoner.py, company-reasoner). Only what a provider run needs:
# the two pinned CLIs (same versions as the worker), curl for the deploy's egress probe, and
# two stdlib-only modules. No git, gh, pip packages, database client, tests or other app code.
FROM python:3.12-slim@sha256:02108f5d322dd89f1c9e552442c25acb0543dfdbc455693a5599624f20d9155d AS reasoner
RUN apt-get update && apt-get install -y --no-install-recommends nodejs npm ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g @anthropic-ai/claude-code@2.1.286 @openai/codex@0.159.2 \
    && npm cache clean --force \
    && useradd --create-home --uid 10001 worker \
    && install -d -o worker -g worker -m 0700 /home/worker/.claude /home/worker/.codex
WORKDIR /app
COPY app/__init__.py app/liveness.py app/providers.py app/reasoner.py ./app/
USER worker
ENV HOME=/home/worker PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
CMD ["python", "-m", "app.reasoner"]

# --------------------------------------------------------------------------- review signer
# The only holder of the release reviewer key (docs/security/RELEASE_GATEWAY.md, F1): no model CLI, no npm, its own
# uid. It asks company-reasoner for the review itself and signs only a strictly valid answer for the gateway.
FROM python:3.12-slim@sha256:02108f5d322dd89f1c9e552442c25acb0543dfdbc455693a5599624f20d9155d AS review-signer
RUN apt-get update && apt-get install -y --no-install-recommends openssh-client \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --no-create-home --uid 10002 signer
WORKDIR /app
COPY app/__init__.py app/release_gateway.py app/review_signer.py ./app/
USER signer
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
CMD ["python", "-m", "app.review_signer"]

# --------------------------------------------------------------------------- egress
# The coder's only way out: tinyproxy with a default-deny host allowlist (CODER_RUNTIME.md D3).
FROM debian:bookworm-slim@sha256:3783cc01769c7b2b1b83a5c5ad96c815348e28ed7da68e2e3687004faa906251 AS egress
RUN apt-get update && apt-get install -y --no-install-recommends tinyproxy \
    && rm -rf /var/lib/apt/lists/*
COPY scripts/coder/tinyproxy.conf /etc/tinyproxy/tinyproxy.conf
COPY scripts/coder/allowlist /etc/tinyproxy/allowlist
USER nobody
CMD ["tinyproxy", "-d", "-c", "/etc/tinyproxy/tinyproxy.conf"]
