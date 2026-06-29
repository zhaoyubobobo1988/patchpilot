"""
libs/permissions.py — Centralized permission boundary checker.

PermissionChecker provides a single source of truth for path validation
across all agents. All methods are @staticmethod — no instantiation needed.

Key security: normalize_path() resolves ".." and "." via os.path.normpath,
so traversal attacks like "features/../../core/secrets.py" are caught by
all downstream checks.

Usage:
    from libs.permissions import PermissionChecker

    is_valid, violations = PermissionChecker.validate_diff(diff)
    is_valid, violations = PermissionChecker.validate_diff(diff, subtask.files)
"""
from __future__ import annotations

import os

# ── constants ────────────────────────────────────────────────────────────────

ALLOWED_PREFIX: str = "features/"

PROTECTED_PREFIXES: tuple[str, ...] = ("core/", "infra/")

CI_PATTERNS: tuple[str, ...] = (
    ".github/workflows/",
    ".github/actions/",
    ".github/dependabot.yml",
    ".github/codeql/",
    ".gitlab-ci.yml",
    ".gitlab/ci/",
    "Jenkinsfile",
    ".circleci/",
    ".travis.yml",
    "azure-pipelines.yml",
    ".drone.yml",
    "bitbucket-pipelines.yml",
    ".tekton/",
    "buildspec.yml",
    "cloudbuild.yaml",
)


# ── PermissionChecker ────────────────────────────────────────────────────────

class PermissionChecker:
    """Centralized file-path permission validation for the OpenClaw pipeline.

    All methods are @staticmethod — no state, no instantiation required.
    """

    ALLOWED_PREFIX = ALLOWED_PREFIX
    PROTECTED_PREFIXES = PROTECTED_PREFIXES
    CI_PATTERNS = CI_PATTERNS

    # ── path helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def normalize_path(path: str) -> str:
        """Strip git a/ / b/ prefixes, resolve .. and ., normalize slashes.

        >>> PermissionChecker.normalize_path("a/features/../../core/x.py")
        'core/x.py'
        """
        clean = path
        for prefix in ("a/", "b/"):
            if clean.startswith(prefix):
                clean = clean[len(prefix):]
                break

        norm = os.path.normpath(clean)
        # Normalize Windows backslashes to POSIX forward slashes
        norm = norm.replace("\\", "/")
        return norm

    @staticmethod
    def is_within_allowed_scope(path: str) -> bool:
        """Check whether *path* (after normalization) falls under features/."""
        normalized = PermissionChecker.normalize_path(path)
        if not normalized or normalized == ".":
            return False
        return normalized.startswith(PermissionChecker.ALLOWED_PREFIX)

    @staticmethod
    def is_protected(path: str) -> bool:
        """Check whether *path* (after normalization) falls under core/ or infra/."""
        normalized = PermissionChecker.normalize_path(path)
        for prefix in PermissionChecker.PROTECTED_PREFIXES:
            if normalized.startswith(prefix):
                return True
        return False

    @staticmethod
    def is_ci_path(path: str) -> bool:
        """Check whether *path* (after normalization) matches a CI/CD pattern."""
        normalized = PermissionChecker.normalize_path(path)
        for pattern in PermissionChecker.CI_PATTERNS:
            if normalized == pattern or normalized.startswith(pattern):
                return True
        return False

    # ── diff parsing ─────────────────────────────────────────────────────────

    @staticmethod
    def extract_affected_files(diff: str) -> list[str]:
        """Parse ``diff --git`` lines from a unified diff and return normalized paths."""
        if not diff:
            return []

        files: list[str] = []
        for line in diff.splitlines():
            if line.startswith("diff --git"):
                parts = line.split(" ")
                if len(parts) >= 4:
                    # parts[3] is "b/<path>" — strip the "b/" prefix
                    raw = parts[3]
                    if raw.startswith("b/"):
                        raw = raw[2:]
                    files.append(PermissionChecker.normalize_path(raw))
        return files

    # ── validation ───────────────────────────────────────────────────────────

    @staticmethod
    def validate_diff(
        diff: str,
        allowed_files: list[str] | None = None,
    ) -> tuple[bool, list[str]]:
        """Comprehensive diff validation.

        Returns (is_valid, violations) where violations lists the file paths
        that failed at least one check.  When *allowed_files* is None every
        file under features/ is accepted (backward compat).
        """
        if not diff:
            return True, []

        affected = PermissionChecker.extract_affected_files(diff)
        if not affected:
            return True, []

        violations: list[str] = []
        for f in affected:
            # Check 1 — within allowed scope
            if not PermissionChecker.is_within_allowed_scope(f):
                violations.append(f)
                continue

            # Check 2 — not a protected prefix
            if PermissionChecker.is_protected(f):
                violations.append(f)
                continue

            # Check 3 — not a CI/CD path
            if PermissionChecker.is_ci_path(f):
                violations.append(f)
                continue

            # Check 4 — within assigned files (scope restriction)
            if allowed_files is not None:
                # Normalize allowed files for comparison
                normalized_allowed = {
                    PermissionChecker.normalize_path(af) for af in allowed_files
                }
                if f not in normalized_allowed:
                    violations.append(f)

        return len(violations) == 0, violations

    @staticmethod
    def validate_subtask_files(files: list[str]) -> tuple[bool, list[str]]:
        """Validate that every file in a SubTask is within allowed scope
        and not protected or CI.

        Returns (is_valid, violations).
        """
        violations: list[str] = []
        for f in files:
            normalized = PermissionChecker.normalize_path(f)
            if not PermissionChecker.is_within_allowed_scope(normalized):
                violations.append(f)
                continue
            if PermissionChecker.is_protected(normalized):
                violations.append(f)
                continue
            if PermissionChecker.is_ci_path(normalized):
                violations.append(f)

        return len(violations) == 0, violations

    @staticmethod
    def find_first_violation(diff: str) -> str | None:
        """Return the first violating path in *diff*, or None if clean."""
        is_valid, violations = PermissionChecker.validate_diff(diff)
        if not is_valid and violations:
            return violations[0]
        return None
