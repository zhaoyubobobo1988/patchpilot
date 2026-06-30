# Project Development Status

## Current Decision

Use the existing OpenClaw gateway deployment as the Feishu entrypoint, and keep this repository's custom pipeline as a backend execution capability.

Do not delete or abandon the custom pipeline yet. It already contains useful work around task decomposition, worker isolation, patch aggregation, review, PR creation, CI polling, debug retries, permission checks, and telemetry. The gateway should solve ingress/session problems first; this pipeline can remain available for feature-to-PR execution.

## Why This Route

The direct Feishu webhook in this repository was deployed on `http://10.48.0.81:3001/feishu/webhook`, but Feishu could not reach that private network address from the public platform. The webhook implementation now returns valid JSON for URL verification, but the network topology still blocks developer-server callbacks.

The sibling `myopenclaw` deployment already runs the official OpenClaw gateway and supports Feishu long connection mode. In Feishu Open Platform, long connection verification now shows connected. That route avoids public webhook and HTTPS exposure work.

## System Boundary

### OpenClaw Gateway Deployment

Reference project: `D:\code\myopenclaw`

Server path: `/home/gaoyu/source_code/myopenclaw`

Important service:

- `openclaw-gateway`
- image: `ghcr.io/openclaw/openclaw:latest`
- port: `18789`
- health endpoint: `/healthz`
- Feishu mode: long connection

Gateway responsibility:

- Feishu long connection
- Chat/session entrypoint
- User-facing agent gateway behavior
- Avoiding public webhook callback setup

Known current issue:

- Logs show missing OpenAI provider auth for the default gateway agent:
  `No API key found for provider "openai"`.
- The gateway can connect, but model credentials still need to be configured before it can reliably handle tasks.

### Custom Pipeline In This Repository

Current repository: `C:\Users\Administrator\Desktop\myownagent`

Server path: `/home/zy/code/patchpilot`

Current service:

- Docker service name: `openclaw`
- port: `3001`
- role: custom TypeScript bridge plus Python feature-to-PR pipeline

Pipeline responsibility:

- Clone target repository
- Gather code context
- Decompose a requirement into a task graph
- Generate tests
- Run parallel workers
- Merge patches
- Apply and validate patches
- Review patches
- Create GitHub PRs
- Poll CI and run debug retries

This repository is not using official OpenClaw gateway internally. Its agent execution is driven by Python code and CLI subprocesses.

## Current Architecture In This Repository

The current pipeline is a custom fixed workflow with partial state-machine infrastructure.

Main path today:

```text
pipeline.py
  -> clone
  -> ContextAgent
  -> OrchestratorAgent
  -> TestAgent
  -> ClaudeCodeWorker workers
  -> AggregatorAgent
  -> IntegratorAgent
  -> ReviewAgent
  -> GitHubAgent
  -> DebugAgent
```

Agent backends:

- `agents/claude_code.py` runs `claude --print` as a subprocess.
- `agents/codex.py` runs `codex exec` as a subprocess.
- `agents/worker/agent.py` uses `ClaudeCodeAgent` for worker execution when `ANTHROPIC_BASE_URL` is configured.

Task graph:

- `models/task.py` defines `TaskGraph`.
- `parallel_groups` controls which subtasks can run together.
- `dependencies` records subtask dependency edges.
- `_run_workers()` in `pipeline.py` enforces dependency completion before running a subtask.

State machine / orchestration infrastructure:

- `pipeline_stages.py` defines `StageExecutor`, `PipelineState`, and early concrete stages.
- `supervisor.py` defines `SupervisorLoop`.
- `models/decision.py` defines `OrchestratorDecision` with `CONTINUE`, `RETRY`, and `ABORT`.
- `models/errors.py` classifies failures so the supervisor can retry transient failures and abort permanent/config/external/resource failures.

Important gap:

- `SupervisorLoop` and `StageExecutor` are implemented and tested, but `pipeline.py` still mostly runs the full pipeline as a hand-written sequential flow.
- Only early stages are extracted as `CloneStage`, `ContextStage`, and `OrchestrateStage`.
- The full pipeline has not yet been migrated to `SupervisorLoop`.

## LangGraph Status

LangGraph is not currently used.

Evidence:

- No `langgraph` or `langchain` dependency exists in `pyproject.toml` or `uv.lock`.
- No `StateGraph`, compiled graph, or LangGraph conditional edges appear in the code.
- Historical planning notes mentioned evaluating LangGraph after control-flow and state improvements, but the actual commits implemented a custom `StageExecutor` and `SupervisorLoop` instead.

Current route:

```text
Fixed pipeline
  -> TaskGraph DAG
  -> StageExecutor
  -> SupervisorLoop
  -> Failure classification and observability
  -> Later decision: keep custom loop or migrate to LangGraph
```

## Commit Milestones

Recent architectural history:

- `PR0`: replace file-copy worker merge with `git apply --3way`
- `PR1+PR2`: concurrency/dependency enforcement and pre-publish quality gate
- `PR3`: add `AgentEvent` protocol and `AgentResult.events`
- `PR4`: add `TaskBoard`
- `PR5`: add run-state persistence
- `PR6`: extract `StageExecutor` protocol plus early stages
- `PR7`: add `OrchestratorDecision` and `SupervisorLoop`
- `PR8`: add failure classification and recovery
- `PR9`: add lightweight spans and metrics
- `PR10`: add centralized permission boundary checks
- deployment work: Docker Compose deployment and Feishu webhook fixes

## Recommended Next Steps

1. Use OpenClaw gateway for Feishu ingress.
2. Configure gateway model/API credentials so the long-connection agent can execute tasks.
3. Keep the custom pipeline deployed but stop treating its Feishu webhook as the primary entrypoint.
4. Decide how gateway should invoke the custom pipeline:
   - direct HTTP call into the existing TypeScript bridge, or
   - a small CLI/job wrapper around `pipeline.py`, or
   - an OpenClaw/Hermes tool integration.
5. Continue custom pipeline hardening:
   - migrate remaining stages into `StageExecutor`,
   - make `SupervisorLoop` the main runner,
   - persist richer run state/checkpoints,
   - only then re-evaluate LangGraph if custom control flow becomes hard to maintain.

## Current Operating Rule

OpenClaw gateway is the preferred Feishu entrypoint.

The custom pipeline is preserved as backend execution infrastructure and should not be removed unless a replacement exists for feature decomposition, patch generation, PR creation, CI polling, and debug retries.
