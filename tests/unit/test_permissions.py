"""
tests/unit/test_permissions.py — PR10 权限边界测试

Covers PermissionChecker:
  A. normalize_path (8 tests)
  B. is_within_allowed_scope (6 tests)
  C. is_protected (6 tests)
  D. is_ci_path (7 tests)
  E. extract_affected_files (5 tests)
  F. validate_diff (8 tests)
  G. validate_subtask_files (4 tests)
  H. find_first_violation (2 tests)
"""
from __future__ import annotations

import pytest


# ═══════════════════════════════════════════════════════════════════════════════
# A. normalize_path
# ═══════════════════════════════════════════════════════════════════════════════

def test_normalize_path_strips_git_a_prefix():
    """'a/features/x.py' → 'features/x.py'"""
    from libs.permissions import PermissionChecker
    assert PermissionChecker.normalize_path("a/features/x.py") == "features/x.py"


def test_normalize_path_strips_git_b_prefix():
    """'b/features/x.py' → 'features/x.py'"""
    from libs.permissions import PermissionChecker
    assert PermissionChecker.normalize_path("b/features/x.py") == "features/x.py"


def test_normalize_path_no_prefix_unchanged():
    """No git prefix → unchanged"""
    from libs.permissions import PermissionChecker
    assert PermissionChecker.normalize_path("features/x.py") == "features/x.py"


def test_normalize_path_resolves_parent_traversal():
    """'features/../core/secrets.py' → 'core/secrets.py' (single .. cancels features/)"""
    from libs.permissions import PermissionChecker
    assert PermissionChecker.normalize_path("features/../core/secrets.py") == "core/secrets.py"


def test_normalize_path_resolves_dot():
    """'features/./auth/login.py' → 'features/auth/login.py'"""
    from libs.permissions import PermissionChecker
    assert PermissionChecker.normalize_path("features/./auth/login.py") == "features/auth/login.py"


def test_normalize_path_resolves_multiple_traversals():
    """'a/features/../infra/../core/x.py' → 'core/x.py'"""
    from libs.permissions import PermissionChecker
    result = PermissionChecker.normalize_path("a/features/../infra/../core/x.py")
    assert result == "core/x.py"


def test_normalize_path_windows_backslash_to_forward():
    r"""Windows backslashes → forward slashes."""
    from libs.permissions import PermissionChecker
    result = PermissionChecker.normalize_path(r"features\auth\login.py")
    assert "\\" not in result
    assert result == "features/auth/login.py"


def test_normalize_path_empty_string():
    """Empty string returns '.' (os.path.normpath default)."""
    from libs.permissions import PermissionChecker
    result = PermissionChecker.normalize_path("")
    assert result == "."


# ═══════════════════════════════════════════════════════════════════════════════
# B. is_within_allowed_scope
# ═══════════════════════════════════════════════════════════════════════════════

def test_scope_allows_features_prefix():
    from libs.permissions import PermissionChecker
    assert PermissionChecker.is_within_allowed_scope("features/auth/login.py") is True


def test_scope_allows_nested_features():
    from libs.permissions import PermissionChecker
    assert PermissionChecker.is_within_allowed_scope("features/auth/sub/nested.py") is True


def test_scope_allows_root_readme():
    from libs.permissions import PermissionChecker
    assert PermissionChecker.is_within_allowed_scope("README.md") is True


def test_scope_allows_docs_prefix():
    from libs.permissions import PermissionChecker
    assert PermissionChecker.is_within_allowed_scope("docs/architecture.md") is True


def test_scope_rejects_core():
    from libs.permissions import PermissionChecker
    assert PermissionChecker.is_within_allowed_scope("core/auth.py") is False


def test_scope_traversal_lands_back_in_features():
    """'features/../features/x.py' normalizes back to 'features/x.py' → True."""
    from libs.permissions import PermissionChecker
    result = PermissionChecker.is_within_allowed_scope("features/../features/x.py")
    assert result is True


def test_scope_traversal_escapes():
    """'features/../../etc/passwd' → escapes allowed scope."""
    from libs.permissions import PermissionChecker
    result = PermissionChecker.is_within_allowed_scope("features/../../etc/passwd")
    assert result is False


def test_scope_rejects_empty():
    from libs.permissions import PermissionChecker
    assert PermissionChecker.is_within_allowed_scope("") is False


# ═══════════════════════════════════════════════════════════════════════════════
# C. is_protected
# ═══════════════════════════════════════════════════════════════════════════════

def test_protected_core_dir():
    from libs.permissions import PermissionChecker
    assert PermissionChecker.is_protected("core/auth.py") is True


def test_protected_infra_dir():
    from libs.permissions import PermissionChecker
    assert PermissionChecker.is_protected("infra/config.py") is True


def test_protected_features_is_not():
    from libs.permissions import PermissionChecker
    assert PermissionChecker.is_protected("features/auth.py") is False


def test_protected_via_traversal():
    """'features/../core/secrets.py' normalizes to 'core/secrets.py' → True."""
    from libs.permissions import PermissionChecker
    assert PermissionChecker.is_protected("features/../core/secrets.py") is True


def test_protected_partial_match_false():
    """'my_core_module/x.py' should not match — prefix must be exact directory."""
    from libs.permissions import PermissionChecker
    assert PermissionChecker.is_protected("my_core_module/x.py") is False


def test_protected_nested_core():
    from libs.permissions import PermissionChecker
    assert PermissionChecker.is_protected("core/sub/module.py") is True


# ═══════════════════════════════════════════════════════════════════════════════
# D. is_ci_path
# ═══════════════════════════════════════════════════════════════════════════════

def test_ci_detects_github_workflows():
    from libs.permissions import PermissionChecker
    assert PermissionChecker.is_ci_path(".github/workflows/ci.yml") is True


def test_ci_detects_gitlab_ci():
    from libs.permissions import PermissionChecker
    assert PermissionChecker.is_ci_path(".gitlab-ci.yml") is True


def test_ci_detects_jenkinsfile():
    from libs.permissions import PermissionChecker
    assert PermissionChecker.is_ci_path("Jenkinsfile") is True


def test_ci_detects_circleci():
    from libs.permissions import PermissionChecker
    assert PermissionChecker.is_ci_path(".circleci/config.yml") is True


def test_ci_not_features_file():
    """'features/ci.py' is not a CI path."""
    from libs.permissions import PermissionChecker
    assert PermissionChecker.is_ci_path("features/ci.py") is False


def test_ci_via_traversal():
    """'features/../.github/workflows/deploy.yml' → True after normalization."""
    from libs.permissions import PermissionChecker
    assert PermissionChecker.is_ci_path("features/../.github/workflows/deploy.yml") is True


@pytest.mark.parametrize("ci_pattern", [
    ".github/workflows/ci.yml",
    ".github/actions/build/action.yml",
    ".github/dependabot.yml",
    ".github/codeql/config.yml",
    ".gitlab-ci.yml",
    ".gitlab/ci/test.yml",
    "Jenkinsfile",
    ".circleci/config.yml",
    ".travis.yml",
    "azure-pipelines.yml",
    ".drone.yml",
    "bitbucket-pipelines.yml",
    ".tekton/pipeline.yaml",
    "buildspec.yml",
    "cloudbuild.yaml",
])
def test_ci_all_patterns_covered(ci_pattern):
    """Every CI_PATTERN entry should be detected."""
    from libs.permissions import PermissionChecker
    assert PermissionChecker.is_ci_path(ci_pattern) is True, f"Missed: {ci_pattern}"


# ═══════════════════════════════════════════════════════════════════════════════
# E. extract_affected_files
# ═══════════════════════════════════════════════════════════════════════════════

def test_extract_single_file():
    from libs.permissions import PermissionChecker
    diff = "diff --git a/features/auth.py b/features/auth.py\n+code\n"
    files = PermissionChecker.extract_affected_files(diff)
    assert files == ["features/auth.py"]


def test_extract_multiple_files():
    from libs.permissions import PermissionChecker
    diff = (
        "diff --git a/features/auth.py b/features/auth.py\n"
        "+code\n"
        "diff --git a/features/login.py b/features/login.py\n"
        "+more code\n"
    )
    files = PermissionChecker.extract_affected_files(diff)
    assert files == ["features/auth.py", "features/login.py"]


def test_extract_returns_normalized_paths():
    """Extracted paths are normalized (a/ prefix stripped)."""
    from libs.permissions import PermissionChecker
    diff = "diff --git a/features/./auth.py b/features/./auth.py\n"
    files = PermissionChecker.extract_affected_files(diff)
    assert "a/" not in files[0]
    assert files[0] == "features/auth.py"


def test_extract_empty_diff():
    from libs.permissions import PermissionChecker
    assert PermissionChecker.extract_affected_files("") == []


def test_extract_ignores_non_diff_lines():
    """Only 'diff --git' lines are parsed; other lines ignored."""
    from libs.permissions import PermissionChecker
    diff = (
        "some random log line\n"
        "diff --git a/features/x.py b/features/x.py\n"
        "--- a/features/x.py\n"
        "+++ b/features/x.py\n"
    )
    files = PermissionChecker.extract_affected_files(diff)
    assert files == ["features/x.py"]


# ═══════════════════════════════════════════════════════════════════════════════
# F. validate_diff
# ═══════════════════════════════════════════════════════════════════════════════

def test_validate_diff_all_features_passes():
    from libs.permissions import PermissionChecker
    diff = "diff --git a/features/auth.py b/features/auth.py\n+code\n"
    is_valid, violations = PermissionChecker.validate_diff(diff)
    assert is_valid is True
    assert violations == []


def test_validate_diff_readme_passes():
    from libs.permissions import PermissionChecker
    diff = "diff --git a/README.md b/README.md\n+project notes\n"
    is_valid, violations = PermissionChecker.validate_diff(diff)
    assert is_valid is True
    assert violations == []


def test_validate_diff_docs_passes():
    from libs.permissions import PermissionChecker
    diff = "diff --git a/docs/usage.md b/docs/usage.md\n+usage notes\n"
    is_valid, violations = PermissionChecker.validate_diff(diff)
    assert is_valid is True
    assert violations == []


def test_validate_diff_core_rejected():
    from libs.permissions import PermissionChecker
    diff = "diff --git a/core/auth.py b/core/auth.py\n+code\n"
    is_valid, violations = PermissionChecker.validate_diff(diff)
    assert is_valid is False
    assert len(violations) >= 1
    assert "core/auth.py" in violations[0]


def test_validate_diff_infra_rejected():
    from libs.permissions import PermissionChecker
    diff = "diff --git a/infra/config.py b/infra/config.py\n+code\n"
    is_valid, violations = PermissionChecker.validate_diff(diff)
    assert is_valid is False
    assert len(violations) >= 1


def test_validate_diff_ci_rejected():
    from libs.permissions import PermissionChecker
    diff = "diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml\n+code\n"
    is_valid, violations = PermissionChecker.validate_diff(diff)
    assert is_valid is False
    assert len(violations) >= 1


def test_validate_diff_traversal_rejected():
    """'features/../../core/x.py' should be rejected after normalization."""
    from libs.permissions import PermissionChecker
    diff = "diff --git a/features/../../core/x.py b/features/../../core/x.py\n+code\n"
    is_valid, violations = PermissionChecker.validate_diff(diff)
    assert is_valid is False


def test_validate_diff_with_allowed_files_restriction():
    """Worker can only touch assigned files — out-of-scope features/ file rejected."""
    from libs.permissions import PermissionChecker
    diff = "diff --git a/features/other.py b/features/other.py\n+code\n"
    allowed = ["features/auth.py", "features/login.py"]
    is_valid, violations = PermissionChecker.validate_diff(diff, allowed_files=allowed)
    assert is_valid is False
    assert "features/other.py" in violations[0]


def test_validate_diff_with_allowed_files_pass():
    """Worker touches only assigned files → valid."""
    from libs.permissions import PermissionChecker
    diff = "diff --git a/features/auth.py b/features/auth.py\n+code\n"
    allowed = ["features/auth.py", "features/login.py"]
    is_valid, violations = PermissionChecker.validate_diff(diff, allowed_files=allowed)
    assert is_valid is True
    assert violations == []


def test_validate_diff_allowed_files_none_backward_compat():
    """allowed_files=None → any features/ file is fine (backward compat)."""
    from libs.permissions import PermissionChecker
    diff = (
        "diff --git a/features/auth.py b/features/auth.py\n+code\n"
        "diff --git a/features/login.py b/features/login.py\n+code\n"
    )
    is_valid, violations = PermissionChecker.validate_diff(diff, allowed_files=None)
    assert is_valid is True


# ═══════════════════════════════════════════════════════════════════════════════
# G. validate_subtask_files
# ═══════════════════════════════════════════════════════════════════════════════

def test_validate_subtask_all_valid():
    from libs.permissions import PermissionChecker
    is_valid, violations = PermissionChecker.validate_subtask_files(
        ["features/auth.py", "features/login.py"]
    )
    assert is_valid is True
    assert violations == []


def test_validate_subtask_core_rejected():
    from libs.permissions import PermissionChecker
    is_valid, violations = PermissionChecker.validate_subtask_files(
        ["features/auth.py", "core/secrets.py"]
    )
    assert is_valid is False
    assert len(violations) >= 1
    assert any("core/secrets.py" in v for v in violations)


def test_validate_subtask_ci_rejected():
    from libs.permissions import PermissionChecker
    is_valid, violations = PermissionChecker.validate_subtask_files(
        [".github/workflows/deploy.yml"]
    )
    assert is_valid is False
    assert len(violations) >= 1


def test_validate_subtask_mixed_valid_and_invalid():
    from libs.permissions import PermissionChecker
    is_valid, violations = PermissionChecker.validate_subtask_files(
        ["features/auth.py", "core/db.py", "features/login.py"]
    )
    assert is_valid is False
    assert len(violations) >= 1
    # Only the core/ file should be in violations
    assert any("core/db.py" in v for v in violations)


# ═══════════════════════════════════════════════════════════════════════════════
# H. find_first_violation
# ═══════════════════════════════════════════════════════════════════════════════

def test_find_first_violation_returns_none_when_clean():
    from libs.permissions import PermissionChecker
    diff = "diff --git a/features/auth.py b/features/auth.py\n+code\n"
    assert PermissionChecker.find_first_violation(diff) is None


def test_find_first_violation_returns_first_bad_path():
    from libs.permissions import PermissionChecker
    diff = "diff --git a/core/auth.py b/core/auth.py\n+code\n"
    result = PermissionChecker.find_first_violation(diff)
    assert result is not None
    assert "core/auth.py" in result
