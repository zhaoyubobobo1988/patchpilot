from __future__ import annotations

import ast
from pathlib import Path

from config.logging import get_logger
from models.context import AgentContext, CodeContext, FileSnippet
from models.task import FeatureTask

logger = get_logger(__name__)

_MAX_FILE_BYTES = 32_768   # 单文件最大读取字节数，避免超长文件撑爆 context


class ContextAgent:
    def __init__(self, ctx: AgentContext) -> None:
        self.ctx = ctx

    async def gather(self, task: FeatureTask, workspace_path: str) -> CodeContext:
        logger.info(f"ContextAgent gathering context for task {task.id} in {workspace_path}")
        keywords = self._extract_keywords(task)
        snippets = self._scan_python_files(Path(workspace_path), keywords)
        dep_map = self._build_dependency_map(snippets)
        patterns = self._detect_patterns(snippets)
        return CodeContext(
            feature_task_id=task.id,
            relevant_files=snippets,
            dependency_map=dep_map,
            existing_patterns=patterns,
        )

    # ── 内部方法 ──────────────────────────────────────────────────────────────

    def _extract_keywords(self, task: FeatureTask) -> list[str]:
        words = (task.feature_name + " " + task.raw_requirement).lower().split()
        stopwords = {"the", "a", "an", "and", "or", "to", "of", "in", "for", "添加", "功能", "实现"}
        return [w for w in words if len(w) > 2 and w not in stopwords]

    def _scan_python_files(self, root: Path, keywords: list[str]) -> list[FileSnippet]:
        snippets: list[FileSnippet] = []
        if not root.exists():
            return snippets
        for py_file in root.rglob("*.py"):
            try:
                content = py_file.read_bytes()[:_MAX_FILE_BYTES].decode(errors="replace")
            except OSError:
                continue
            score = self._relevance_score(content, keywords)
            if score > 0:
                snippets.append(FileSnippet(
                    path=str(py_file.relative_to(root)),
                    content=content,
                    relevance_score=score,
                ))
        snippets.sort(key=lambda s: s.relevance_score, reverse=True)
        return snippets[:20]   # 最多返回 20 个最相关文件

    def _relevance_score(self, content: str, keywords: list[str]) -> float:
        lower = content.lower()
        return sum(1.0 for kw in keywords if kw in lower)

    def _build_dependency_map(self, snippets: list[FileSnippet]) -> dict[str, list[str]]:
        dep_map: dict[str, list[str]] = {}
        for snippet in snippets:
            imports = self._extract_imports(snippet.content)
            if imports:
                dep_map[snippet.path] = imports
        return dep_map

    def _extract_imports(self, source: str) -> list[str]:
        imports: list[str] = []
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return imports
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.append(alias.name)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.append(node.module)
        return imports

    def _detect_patterns(self, snippets: list[FileSnippet]) -> list[str]:
        patterns: list[str] = []
        combined = "\n".join(s.content for s in snippets)
        if "class " in combined and "def " in combined:
            patterns.append("OOP with classes")
        if "async def " in combined:
            patterns.append("async/await")
        if "pytest" in combined or "unittest" in combined:
            patterns.append("unit tests present")
        if "@dataclass" in combined or "BaseModel" in combined:
            patterns.append("dataclass/pydantic models")
        return patterns
