# OpenClaw AI Software Engineering System

## Project Overview

The OpenClaw AI Software Engineering System is a multi-stage feature-to-PR pipeline that converts natural language feature requirements into GitHub Pull Requests through intelligent patch-based code generation. This system leverages autonomous AI agents working in concert to analyze, decompose, implement, and validate software changes at scale.

## System Overview

OpenClaw bridges the gap between natural language feature requests and executable code by orchestrating multiple specialized AI agents through a custom pipeline architecture. The system accepts requirements from platforms like Feishu, intelligently breaks down complex features into manageable tasks, generates patches through parallel worker agents, validates changes through automated testing and CI integration, and creates polished pull requests ready for human review.

The platform is designed to handle real-world software engineering workflows with built-in error recovery, dependency management, and quality assurance mechanisms that ensure reliable code generation while maintaining human oversight for final review and merge decisions.

## Architecture

### Pipeline Architecture

```
Feishu Requirement Input
        ↓
OpenClaw Bridge (TypeScript long connection)
        ↓
Custom Pipeline (Python)
        ↓
Context Agent (repository analysis)
        ↓
Orchestrator Agent (task decomposition + scheduling)
        ↓
TaskGraph (parallel groups + dependencies)
        ↓
Test Agent (test generation)
        ↓
Parallel Claude/Codex Workers
        ↓
Aggregator Agent (merge patches)
        ↓
Integrator Agent (apply and validate patches)
        ↓
Review Agent (quality assessment)
        ↓
GitHub Agent (create PR)
        ↓
GitHub CI (test execution)
        ↓
Debug Agent (auto-fix loop ≤ 5 retries)
        ↓
Human Review → Merge
```

### Component Architecture

**Ingress Layer:**
- **Feishu Long Connection Bridge** - TypeScript-based entrypoint that maintains persistent connections with Feishu Open Platform, eliminating need for public webhooks on private networks
- **Direct Webhook Support** - Optional webhook endpoint for local/internal testing scenarios

**Pipeline Layer:**
- **Context Agent** - Analyzes repository structure, gathers code context, and identifies relevant files
- **Orchestrator Agent** - Decomposes requirements into a dependency-aware `TaskGraph` with parallel execution groups
- **Test Agent** - Generates comprehensive test coverage for feature requirements
- **Worker Agents** - Parallel execution of feature-level patch generation using Claude Code or Codex backends
- **Aggregator Agent** - Intelligently merges parallel worker outputs into cohesive patches
- **Integrator Agent** - Applies patches using 3-way merge (`git apply --3way`) and validates application
- **Review Agent** - Evaluates patch quality, checks for regressions, and assesses test coverage
- **GitHub Agent** - Creates feature branches, commits patches, and submits Pull Requests
- **Debug Agent** - Automatically diagnoses CI failures and iterates fixes with configurable retry limits

**State Management:**
- **TaskGraph** - Dependency-aware task scheduling with parallel group execution
- **StageExecutor** - Protocol for pipeline stage execution with observability hooks
- **SupervisorLoop** - State machine orchestration with `CONTINUE`, `RETRY`, and `ABORT` decision states
- **AgentEvent Protocol** - Structured event streaming for debugging and observability
- **Run-State Persistence** - Checkpointing and recovery for long-running pipeline executions

**Infrastructure Layer:**
- **Telemetry** - Lightweight spans and metrics collection for pipeline monitoring
- **Permission Boundary Checks** - Centralized validation to prevent unauthorized system modifications
- **Failure Classification** - Distinguishes transient vs. permanent failures for appropriate recovery strategies

### Deployment Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    Feishu Open Platform                     │
└─────────────────────────────┬───────────────────────────────┘
                              │
                              │ Long Connection (WebSocket)
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│              OpenClaw Bridge (TypeScript)                    │
│              - Feishu client                                 │
│              - Message parsing                               │
│              - Requirement normalization                     │
└─────────────────────────────┬───────────────────────────────┘
                              │
                              │ ParsedRequirement
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│              Custom Pipeline (Python)                        │
│              - Multi-agent orchestration                     │
│              - Task graph execution                          │
│              - Patch generation & aggregation                │
└─────────────────────────────┬───────────────────────────────┘
                              │
                              │ Pull Request
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│              GitHub                                          │
│              - Feature branch                                │
│              - Pull request                                  │
│              - CI/CD execution                               │
└─────────────────────────────────────────────────────────────┘
```

## Core Principles

### 1. **Test-Driven Development (TDD) First**

All feature work follows strict TDD methodology:

- **Red Phase** - Write tests first and confirm they FAIL before implementation
- **Green Phase** - Write minimal implementation to make tests pass
- **Refactor Phase** - Clean up without changing behavior while tests stay green

This is non-negotiable. Never write implementation before tests exist and are confirmed red.

### 2. **Patch-Based Code Generation**

Workers generate unified diff patches only - no prose, no markdown, no JSON, no full file rewrites. This ensures:

- Precise change tracking
- Easy conflict resolution through 3-way merging
- Clear audit trails for code changes
- Seamless integration with existing Git workflows

### 3. **Permission Boundary Safety**

The system enforces strict boundaries on what agents can modify:

**Allowed:**
- Create/modify files under `/features/**`
- Add unit tests within feature scope
- Generate feature-specific documentation

**Forbidden:**
- Modify `/core/**` or `/infra/**` directories
- Change CI/CD configuration
- Modify files outside assigned feature scope
- Merge PRs autonomously
- Output anything other than unified diff patches

### 4. **Task Dependency Awareness**

The `TaskGraph` system understands and respects dependencies between subtasks:

- **Parallel Groups** - Independent subtasks execute concurrently
- **Dependency Edges** - Dependent subtasks wait for prerequisite completion
- **Dependency Enforcement** - `_run_workers()` ensures tasks execute in valid topological order

### 5. **Graceful Failure Recovery**

The system distinguishes between failure types and responds appropriately:

- **Transient Failures** - Automatic retry with exponential backoff (Debug Agent, max 5 retries)
- **Permanent Failures** - Immediate abort with clear error reporting
- **Configuration Failures** - Early validation before pipeline execution
- **External Resource Failures** - Retry with proper timeout and circuit breaking

### 6. **Observability and Debugging**

Every stage produces structured events through the `AgentEvent` protocol:

- **Lightweight Spans** - Track execution timing across pipeline stages
- **Metrics Collection** - Monitor success rates, retry counts, and performance
- **Event Streaming** - Real-time pipeline state inspection
- **Run-State Persistence** - Recover from interruption without losing progress

### 7. **Multi-Agent Coordination**

Workers operate as feature-level patch generation agents, not autonomous agents:

- **Specialized Roles** - Each agent has a well-defined responsibility
- **Parallel Execution** - Multiple workers handle different aspects simultaneously
- **Centralized Orchestration** - Orchestrator manages task scheduling and dependency resolution
- **Aggregated Output** - Aggregator combines parallel outputs into cohesive patches

### 8. **Human-in-the-Loop**

Despite autonomous capabilities, the system maintains human oversight:

- **Final Review Required** - PRs require human approval before merge
- **Clear Diff Presentation** - Patches presented in standard unified diff format
- **Quality Gates** - CI tests must pass before merge consideration
- **Audit Trail** - Complete history of decisions and actions

### 9. **Infrastructure Portability**

The system is designed for deployment flexibility:

- **Container-Based** - Docker Compose deployment with service isolation
- **Private Network Support** - Long connection mode avoids public webhook requirements
- **Optional Components** - Official gateway available behind `official-gateway` profile for experimentation
- **Stateless Pipeline** - Pipeline execution doesn't depend on container state

### 10. **Incremental Evolution**

The architecture supports gradual enhancement without wholesale replacement:

- **Custom Pipeline Preservation** - Existing pipeline remains functional while improvements are made
- **Stage-Based Migration** - Sequential migration of pipeline stages to `StageExecutor` protocol
- **Decision Framework** - ADRs document architectural decisions for historical context
- **Experimentation Safety** - Optional components can be tested without disrupting production

## Documentation

- [AGENTS.md](./AGENTS.md) - Guidance for Codex agents working in this repository
- [CLAUDE.md](./CLAUDE.md) - Guidance for Claude Code agents working in this repository
- [docs/project-development-status.md](./docs/project-development-status.md) - Current development status and roadmap
- [docs/decisions/ADR-001-gateway-entrypoint-custom-pipeline-backend.md](./docs/decisions/ADR-001-gateway-entrypoint-custom-pipeline-backend.md) - Architecture decision on entrypoint strategy

## License

[Specify your license here]

