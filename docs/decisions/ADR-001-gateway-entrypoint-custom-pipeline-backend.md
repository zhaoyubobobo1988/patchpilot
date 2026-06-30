# ADR-001: Add Project OpenClaw Gateway For Feishu Entrypoint And Preserve Custom Pipeline

## Status

Accepted

## Date

2026-06-30

## Context

This repository contains a custom multi-stage feature-to-PR pipeline. It can clone a repository, gather context, decompose work into a `TaskGraph`, run workers, aggregate patches, review, create PRs, poll CI, and run debug retries.

The repository also includes a TypeScript Feishu webhook bridge. The bridge was deployed on the internal server at `10.48.0.81:3001`. It was fixed to return valid JSON for Feishu URL verification, including the newer Feishu verification shape. However, Feishu Open Platform could not reach the private `10.48.0.81` address from the public platform, so developer-server webhook mode is not a reliable ingress path without public HTTPS or tunneling.

A sibling project, `myopenclaw`, already demonstrates the official OpenClaw gateway deployment pattern. It supports Feishu long connection mode. That project is a reference implementation, not the target production entrypoint for this repository.

The custom pipeline also has useful state-machine work in progress:

- `TaskGraph` for dependency-aware subtasks
- `StageExecutor` and `PipelineState`
- `SupervisorLoop` with `CONTINUE`, `RETRY`, and `ABORT`
- failure classification
- spans, metrics, run-state persistence

It is not yet fully migrated to the supervisor loop. The main `pipeline.py` flow remains mostly hand-written sequential control flow.

## Decision

Add this project's own OpenClaw gateway service and use it as the Feishu entrypoint.

Preserve this repository's custom pipeline as backend execution infrastructure. Do not delete it or treat it as obsolete. It should remain available for feature-to-PR execution while the gateway handles chat ingress and session concerns.

The direct Feishu webhook in this repository is no longer the preferred production entrypoint. It can remain available for local/internal testing.

## Alternatives Considered

### Continue With Direct Feishu Webhook

Pros:

- Already implemented in this repository.
- Directly triggers the existing TypeScript bridge and Python pipeline.

Cons:

- Requires a public HTTPS endpoint or tunneling because Feishu cannot reach private `10.48.0.81`.
- Adds operational burden around certificates, public exposure, and callback diagnostics.

Rejected for now because long connection mode already solves the ingress problem.

### Use The Sibling myopenclaw Gateway Directly

Pros:

- Fastest way to use an already-running gateway.
- Proves Feishu long connection works.

Cons:

- Couples this project to another project's runtime state and user home directory.
- Creates confusion around ownership (`gaoyu` deployment vs. this repository's `zy` deployment).
- Makes it harder to later invoke this repository's custom pipeline cleanly.

Rejected. Use `myopenclaw` as a deployment reference, not as this project's production entrypoint.

### Replace Custom Pipeline With OpenClaw Gateway

Pros:

- Simpler conceptual model.
- Uses an existing gateway service and long connection.

Cons:

- Throws away existing work in task decomposition, patch aggregation, GitHub PR creation, CI polling, debug retries, permission checks, and telemetry.
- We do not yet have a complete replacement path for feature-to-PR execution.

Rejected for now. The custom pipeline stays.

### Migrate Immediately To LangGraph

Pros:

- Better fit if the workflow needs conditional graph edges, checkpointing, and richer stateful execution.

Cons:

- No current dependency or implementation exists.
- The project already has a lightweight supervisor loop and task graph.
- Migration before the gateway/pipeline boundary is clear would add complexity.

Deferred. Re-evaluate after the current pipeline is fully stage-based and the gateway integration boundary is known.

## Consequences

- Feishu should be configured with long connection mode through this project's OpenClaw gateway.
- The internal webhook URL `http://10.48.0.81:3001/feishu/webhook` should not be treated as the main route.
- The custom pipeline should be documented and maintained.
- Future work should define a clean gateway-to-pipeline invocation path.
- The next pipeline architecture step is to finish migrating the hand-written `pipeline.py` stages into `StageExecutor` and run them through `SupervisorLoop`.

## Follow-Up Tasks

- Deploy this project's `openclaw-gateway` service.
- Configure this project's gateway Feishu channel.
- Configure this project's gateway model/API credentials.
- Decide whether gateway invokes the custom pipeline through HTTP, CLI, or tool integration.
- Keep deployment notes current for both:
  - `myopenclaw` gateway deployment
  - this repository's custom pipeline deployment
- Revisit LangGraph only after the custom supervisor loop is either insufficient or too costly to maintain.
