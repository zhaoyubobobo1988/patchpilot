from __future__ import annotations

from config.llm import llm_complete
from config.logging import get_logger
from models.context import AgentContext, CodeContext
from models.task import SubTask, GeneratedTestSpec

logger = get_logger(__name__)

_SYSTEM_PROMPT = """You are the Test Agent in the OpenClaw system.
Given a coding subtask and relevant codebase context, generate ONLY pytest test code.
Do NOT generate the implementation. Output ONLY valid Python test code starting with imports.
File path for the test should be inside features/ directory."""


class TestAgent:
    def __init__(self, ctx: AgentContext) -> None:
        self.ctx = ctx

    async def generate(self, subtask: SubTask, context: CodeContext) -> GeneratedTestSpec:
        logger.info(f"TestAgent generating tests for subtask {subtask.id}")
        prompt = self._build_prompt(subtask, context)
        try:
            test_code = (await llm_complete(
                model=self.ctx.model,
                max_tokens=self.ctx.max_tokens,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
            )).strip()
            test_file_path = f"features/{subtask.feature}/test_{subtask.id}.py"
            return GeneratedTestSpec(
                subtask_id=subtask.id,
                test_file_path=test_file_path,
                test_code=test_code,
            )
        except Exception as exc:
            logger.error(f"TestAgent failed for subtask {subtask.id}: {exc}")
            return GeneratedTestSpec(
                subtask_id=subtask.id,
                test_file_path=f"features/{subtask.feature}/test_{subtask.id}.py",
                test_code="",
            )

    def _build_prompt(self, subtask: SubTask, context: CodeContext) -> str:
        relevant = "\n\n".join(
            f"# {s.path}\n{s.content[:1000]}" for s in context.relevant_files[:5]
        )
        return (
            f"## Feature: {subtask.feature}\n"
            f"## Goal: {subtask.goal}\n"
            f"## Target files: {', '.join(subtask.files)}\n"
            f"## Constraints: {', '.join(subtask.constraints)}\n\n"
            f"## Relevant codebase context:\n{relevant}\n\n"
            f"Generate pytest tests for this goal. Tests go in features/{subtask.feature}/. "
            f"Output ONLY Python code."
        )
