from __future__ import annotations

import json
import uuid
from pathlib import Path

from agents.base import AgentTask
from agents.claude_code import ClaudeCodeAgent
from agents.result_utils import extract_agent_output
from config.llm import llm_complete
from config.logging import get_logger
from config.settings import settings
from models.context import AgentContext
from models.task import FeatureTask, SubTask, TaskGraph, TaskStatus
from .prompts import ORCHESTRATOR_SYSTEM_PROMPT, build_orchestrator_prompt

_claude_agent = ClaudeCodeAgent()

logger = get_logger(__name__)

# Extra instruction appended to the prompt so the Planner agent outputs only JSON
_JSON_ONLY_SUFFIX = """

---
IMPORTANT: Respond with ONLY the JSON object described above — no prose, no markdown fences, \
no explanation. The first character of your response must be `{` and the last must be `}`.
"""


class OrchestratorAgent:
    """
    Planner agent — decomposes a feature requirement into a TaskGraph.

    Primary backend (when ANTHROPIC_BASE_URL is set):
        Runs a `claude` CLI subprocess in --print mode.
        The subprocess can explore the workspace with read tools (LS, Read, Grep)
        to understand the repo structure before producing the task graph.

    Fallback: litellm direct API call (no tool access, text-only).
    """

    def __init__(self, ctx: AgentContext) -> None:
        self.ctx = ctx

    async def decompose(self, task: FeatureTask) -> TaskGraph:
        logger.info(f"[Planner] Decomposing: {task.feature_name}")
        prompt = self._build_planner_prompt(task)
        raw = await self._call_planner(prompt)
        task_graph = self._parse_task_graph(raw, task)
        logger.info(
            f"[Planner] {len(task.subtasks)} subtasks, "
            f"{len(task_graph.parallel_groups)} parallel groups"
        )
        return task_graph

    # ── Planner subprocess (primary) ──────────────────────────────────────────

    def _build_planner_prompt(self, task: FeatureTask) -> str:
        return (
            f"{ORCHESTRATOR_SYSTEM_PROMPT}"
            f"{_JSON_ONLY_SUFFIX}\n\n"
            "---\n"
            f"{build_orchestrator_prompt(task.raw_requirement, task.repository)}\n\n"
            "You may use read-only tools (LS, Read, Grep) to explore the repository "
            "structure before producing the JSON.\n"
            "DO NOT create or modify any files.\n"
            "Output ONLY the JSON object."
        )

    async def _call_planner(self, prompt: str) -> str:
        """Run ClaudeCodeAgent as Planner. Falls back to litellm if unavailable."""
        if settings.ANTHROPIC_BASE_URL:
            task = AgentTask(
                task_id=self.ctx.run_id,
                role="planner",
                prompt=prompt,
                workspace=Path(self.ctx.workspace_path),
                timeout_seconds=settings.CLAUDE_CODE_TIMEOUT or 300,
                output_format="json",
            )
            result = await _claude_agent.run(task)
            output = extract_agent_output(result, "Planner")
            if output:
                return output
            # agent failed → fall through to litellm

        # litellm fallback — prompt already contains system + user content
        return await llm_complete(
            model=self.ctx.model,
            max_tokens=self.ctx.max_tokens,
            messages=[
                {"role": "user", "content": prompt},
            ],
        )

    # ── JSON → TaskGraph ──────────────────────────────────────────────────────

    def _parse_task_graph(self, raw: str, task: FeatureTask) -> TaskGraph:
        raw = raw.strip()
        # Strip markdown code fences if present
        if raw.startswith("```"):
            lines = raw.split("\n")
            raw = "\n".join(lines[1:-1])
        # Find the first { ... } block in case the agent added prose before/after
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start != -1 and end > start:
            raw = raw[start:end]

        data = json.loads(raw)

        subtasks: list[SubTask] = []
        for st_data in data.get("subtasks", []):
            st = SubTask(
                id=st_data.get("id", str(uuid.uuid4())[:8]),
                feature=st_data["feature"],
                goal=st_data["goal"],
                files=st_data.get("files", []),
                constraints=st_data.get("constraints", []),
                status=TaskStatus.PENDING,
            )
            subtasks.append(st)

        task.subtasks = subtasks
        if data.get("feature_name"):
            task.feature_name = data["feature_name"]

        return TaskGraph(
            feature_task=task,
            parallel_groups=data.get("parallel_groups", [[st.id for st in subtasks]]),
            dependencies=data.get("dependencies", {}),
        )
