from __future__ import annotations

from config.llm import llm_complete
from config.logging import get_logger
from models.context import AgentContext
from models.patch import MergedPatch, PatchResult, PatchSet, PatchStatus

logger = get_logger(__name__)

_MERGE_SYSTEM_PROMPT = """You are a patch merge specialist. You receive two conflicting unified diff hunks for the same file and must produce a single merged unified diff hunk that correctly incorporates both changes.

Output ONLY the merged unified diff hunk, no explanations."""


class AggregatorAgent:
    def __init__(self, ctx: AgentContext) -> None:
        self.ctx = ctx

    async def merge(self, patch_set: PatchSet) -> MergedPatch:
        logger.info(f"Merging {len(patch_set.patches)} patches for task {patch_set.feature_task_id}")

        successful = [p for p in patch_set.patches if p.patch_content]
        if not successful:
            return MergedPatch(
                feature_task_id=patch_set.feature_task_id,
                merged_diff="",
                source_patch_ids=[],
                status=PatchStatus.FAILED,
                error_details="No successful patches to merge",
            )

        file_hunks: dict[str, list[str]] = self._group_by_file(successful)
        merged_sections: list[str] = []
        conflicts_resolved = 0

        for file_path, hunks in file_hunks.items():
            if len(hunks) == 1:
                merged_sections.append(hunks[0])
            else:
                logger.info(f"Resolving conflict for {file_path} ({len(hunks)} hunks)")
                merged = await self._resolve_conflict(file_path, hunks)
                merged_sections.append(merged)
                conflicts_resolved += 1

        return MergedPatch(
            feature_task_id=patch_set.feature_task_id,
            merged_diff="\n".join(merged_sections),
            source_patch_ids=[p.subtask_id for p in successful],
            conflicts_resolved=conflicts_resolved,
            status=PatchStatus.SUCCESS,
        )

    def _group_by_file(self, patches: list[PatchResult]) -> dict[str, list[str]]:
        file_hunks: dict[str, list[str]] = {}
        for patch in patches:
            current_file: str | None = None
            current_hunk_lines: list[str] = []

            for line in patch.patch_content.splitlines(keepends=True):
                if line.startswith("diff --git"):
                    if current_file and current_hunk_lines:
                        file_hunks.setdefault(current_file, []).append("".join(current_hunk_lines))
                    current_file = line.split(" b/")[-1].strip()
                    current_hunk_lines = [line]
                elif current_file is not None:
                    current_hunk_lines.append(line)

            if current_file and current_hunk_lines:
                file_hunks.setdefault(current_file, []).append("".join(current_hunk_lines))

        return file_hunks

    async def _resolve_conflict(self, file_path: str, conflicting_hunks: list[str]) -> str:
        hunks_text = "\n\n---HUNK SEPARATOR---\n\n".join(conflicting_hunks)
        prompt = f"File: {file_path}\n\nConflicting hunks:\n\n{hunks_text}\n\nMerge these into a single unified diff."
        try:
            return (await llm_complete(
                model=self.ctx.model,
                max_tokens=4096,
                messages=[
                    {"role": "system", "content": _MERGE_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
            )).strip()
        except Exception as exc:
            # LLM 不可用时取最长的 hunk 作为 fallback（内容最完整）
            logger.warning(f"LLM conflict resolution failed for {file_path}: {exc}, using longest hunk as fallback")
            return max(conflicting_hunks, key=len)
