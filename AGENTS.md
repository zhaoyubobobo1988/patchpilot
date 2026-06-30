# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## Project Overview

This is the **PatchPilot / OpenClaw-style AI Software Engineering System** — a custom multi-stage pipeline that converts natural language feature requirements into GitHub Pull Requests via patch-based code generation.

Important current direction:

- Use this project's TypeScript bridge as the Feishu long-connection entrypoint.
- Keep the official OpenClaw gateway service optional behind the `official-gateway` Docker Compose profile for later experiments.
- Treat the sibling `myopenclaw` repository as a deployment reference only, not as this project's production runtime.
- Keep this repository's custom pipeline as backend execution infrastructure.
- Do not delete or abandon the custom pipeline unless a replacement exists for task decomposition, patch generation, PR creation, CI polling, and debug retries.
- See `docs/project-development-status.md` and `docs/decisions/ADR-001-gateway-entrypoint-custom-pipeline-backend.md`.

## Deployment Notes

This project is deployed on a server reachable over SSH:

- Host: `10.48.0.81`
- SSH user: `zy`
- SSH port: `22`

Do not store SSH passwords or other secrets in this repository. Use an SSH key or an external secret manager when possible.

## Current Integration Route

```
Feishu Requirement Input
        ↓
Project TypeScript bridge long connection
        ↓
Custom pipeline in this repo
        ↓
GitHub Pull Request
```

The direct Feishu developer-server webhook in this repository is not the preferred production entrypoint because `10.48.0.81` is a private address. It can remain useful for local/internal testing.

## Custom Pipeline Architecture

```
Requirement Input
        ↓
Context Agent (repository analysis)
        ↓
Orchestrator Agent (task decomposition + scheduling)
        ↓
TaskGraph (parallel groups + dependencies)
        ↓
Parallel Claude/Codex Workers
        ↓
Aggregator Agent (merge patches)
        ↓
Review Agent
        ↓
GitHub Agent (create PR)
        ↓
GitHub CI (test execution)
        ↓
Debug Agent (auto-fix loop ≤ 5 retries)
        ↓
Human Review → Merge
```

## Codex Worker Role

Workers operate as **feature-level patch generation agents**. They are NOT system architects, CI/CD managers, or autonomous agents.

### Input format

```json
{
  "feature": "user-login",
  "goal": "Add login rate limiting and logging",
  "files": ["features/auth/login.py", "features/auth/service.py"],
  "constraints": ["must be thread-safe", "must not modify core system"]
}
```

### Output format

Output is **unified diff patches only**. No prose, no markdown, no JSON, no full file rewrites.

```diff
diff --git a/features/... b/features/...
...
```

## Hard Constraints

**Allowed:**
- Create/modify files under `/features/**`
- Add unit tests within feature scope

**Forbidden:**
- Modify `/core/**` or `/infra/**`
- Modify CI/CD configuration
- Modify files outside the assigned feature scope
- Output anything other than unified diff patches
- Merge PRs

## Execution Strategy

1. Identify affected modules and minimal change set
2. Design the smallest possible diff (no over-engineering)
3. Generate a syntactically correct unified diff that applies cleanly
4. Verify no broken imports, missing dependencies, or invalid syntax

## Multi-Agent Coordination

Multiple workers run in parallel and an Aggregator merges outputs. Keep changes **localized and additive** — avoid overlapping file regions and global refactoring. CI failures trigger a Debug Agent retry loop (max 5 retries).

This repository has partial state-machine infrastructure:

- `models/task.py` defines `TaskGraph` for parallel groups and dependencies.
- `pipeline_stages.py` defines `StageExecutor` and early concrete stages.
- `supervisor.py` defines `SupervisorLoop` with `CONTINUE`, `RETRY`, and `ABORT`.
- `pipeline.py` has not yet been fully migrated to `SupervisorLoop`; that is a future hardening step.
