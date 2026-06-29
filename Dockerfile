# =============================================================================
# OpenClaw AI Software Engineering System — Docker Image
# =============================================================================
# Multi-stage build combining Python (uv), Node.js (pnpm), and Claude Code CLI.
#
# Build:
#   docker build -t openclaw .
#
# Run:
#   docker run --rm \
#     -e DEEPSEEK_API_KEY=sk-xxx \
#     -e GITHUB_TOKEN=ghp_xxx \
#     openclaw \
#     --requirement "添加用户登录限流功能" \
#     --repo "org/repo"
#
# Or mount .env:
#   docker run --rm -v $(pwd)/.env:/opt/openclaw/.env openclaw ...
# =============================================================================

# ── Stage 1: Python dependencies (uv) ───────────────────────────────────────
FROM python:3.11-slim AS python-builder

WORKDIR /build

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1

# Layer cache: deps change less often than code
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-editable --python-preference only-system


# ── Stage 2: TypeScript bridge (pnpm + tsc) ─────────────────────────────────
FROM node:20-slim AS node-builder

WORKDIR /build

# Install all dependencies (including devDependencies for tsc)
COPY integrations/package.json integrations/pnpm-lock.yaml ./
RUN corepack enable && pnpm install --frozen-lockfile

# Compile TypeScript → dist/
COPY integrations/tsconfig.json ./
COPY integrations/src/ ./src/
RUN pnpm build

# Strip devDependencies, keep only production node_modules
RUN pnpm prune --prod


# ── Stage 3: Runtime ────────────────────────────────────────────────────────
FROM python:3.11-slim

LABEL org.opencontainers.image.title="OpenClaw AI Software Engineering System"
LABEL org.opencontainers.image.description="Multi-agent pipeline: natural language → GitHub Pull Request"
LABEL org.opencontainers.image.source="https://github.com/zhaoyubobobo1988/myownagent"

# ── System packages ─────────────────────────────────────────────────────────
# git              — gitpython, workspace clone, Worker worktree isolation
# openssh-client   — git clone over SSH
# ca-certificates  — HTTPS for API calls
# curl             — health checks, NodeSource setup
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    openssh-client \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

# ── Node.js 20 LTS ──────────────────────────────────────────────────────────
# Required by: Claude Code CLI (Worker / Planner / Reviewer) and TS Bridge
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# ── Claude Code CLI ─────────────────────────────────────────────────────────
# The core agent backend — Worker, Orchestrator/Planner, Reviewer all use
# `claude --print` as a subprocess.  Without it, the pipeline falls back to
# litellm direct API calls (no workspace file-editing capability).
RUN npm install -g @anthropic-ai/claude-code \
    && which claude

# ── Non-root user ───────────────────────────────────────────────────────────
RUN useradd --create-home --shell /bin/bash openclaw

# ── Copy artifacts from builders ────────────────────────────────────────────
COPY --from=python-builder --chown=openclaw:openclaw \
    /build/.venv /opt/openclaw/.venv

COPY --from=node-builder --chown=openclaw:openclaw \
    /build/dist /opt/openclaw/integrations/dist

COPY --from=node-builder --chown=openclaw:openclaw \
    /build/node_modules /opt/openclaw/integrations/node_modules

# ── Copy application code ───────────────────────────────────────────────────
COPY --chown=openclaw:openclaw . /opt/openclaw/

# ── Environment ─────────────────────────────────────────────────────────────
ENV PATH="/opt/openclaw/.venv/bin:$PATH" \
    PYTHONPATH="/opt/openclaw:$PYTHONPATH" \
    GIT_AUTHOR_NAME="OpenClaw" \
    GIT_AUTHOR_EMAIL="openclaw@noreply.github.com" \
    GIT_COMMITTER_NAME="OpenClaw" \
    GIT_COMMITTER_EMAIL="openclaw@noreply.github.com" \
    WORKSPACE_BASE_PATH="/tmp/openclaw-workspaces" \
    LOG_LEVEL="INFO" \
    TS_BRIDGE_PORT="3001" \
    TS_BRIDGE_URL="http://localhost:3001"

# ── External port: Feishu webhook + GitHub REST API ─────────────────────────
EXPOSE 3001

# ── Writable directories ────────────────────────────────────────────────────
RUN mkdir -p /tmp/openclaw-workspaces \
    && chown openclaw:openclaw /tmp/openclaw-workspaces

USER openclaw
WORKDIR /opt/openclaw

# ── Entry point ─────────────────────────────────────────────────────────────
# Server mode (default): TS Bridge listens for Feishu webhooks, triggers pipeline on demand.
# CLI mode: override with `docker run --entrypoint python openclaw pipeline.py --requirement "..." --repo "..."`
ENTRYPOINT ["node", "integrations/dist/index.js"]
