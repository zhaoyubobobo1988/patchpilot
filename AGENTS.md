# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## Project Overview

This is the **OpenClaw AI Software Engineering System** — a multi-agent pipeline that converts natural language feature requirements into GitHub Pull Requests via patch-based code generation.

## Pipeline Architecture

```
Feishu Requirement Input
        ↓
OpenClaw Orchestrator (task decomposition + scheduling)
        ↓
Planner Agent (Codex)
        ↓
Parallel Codex Workers  ← this repo
        ↓
Aggregator Agent (merge patches)
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
