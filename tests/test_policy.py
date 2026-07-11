"""Policy engine tests: mode matrix, path confinement, command gating."""
from __future__ import annotations

from pathlib import Path

from reidx.config.models import PolicyConfig, default_config
from reidx.policy.engine import PolicyEngine
from reidx.policy.models import ActionKind, PermissionDecision, PermissionMode


def _engine(mode: PermissionMode, tmp_path: Path, **policy_kwargs) -> PolicyEngine:
    cfg = default_config()
    cfg.workspace_root = tmp_path
    cfg.policy = PolicyConfig(default_mode=mode, **policy_kwargs)
    return PolicyEngine(cfg)


def test_strict_mode_denies_high_risk(tmp_path: Path) -> None:
    eng = _engine(PermissionMode.STRICT, tmp_path)
    assert eng.evaluate(ActionKind.SHELL_EXEC) is PermissionDecision.DENY
    assert eng.evaluate(ActionKind.FILE_WRITE) is PermissionDecision.PROMPT
    assert eng.evaluate(ActionKind.FILE_READ) is PermissionDecision.ALLOW


def test_balanced_mode_prompts_medium_and_high(tmp_path: Path) -> None:
    eng = _engine(PermissionMode.BALANCED, tmp_path)
    assert eng.evaluate(ActionKind.FILE_READ) is PermissionDecision.ALLOW
    assert eng.evaluate(ActionKind.FILE_WRITE) is PermissionDecision.PROMPT
    assert eng.evaluate(ActionKind.SHELL_EXEC) is PermissionDecision.PROMPT


def test_autonomous_mode_allows_low_and_medium(tmp_path: Path) -> None:
    eng = _engine(PermissionMode.AUTONOMOUS, tmp_path)
    assert eng.evaluate(ActionKind.FILE_READ) is PermissionDecision.ALLOW
    assert eng.evaluate(ActionKind.FILE_WRITE) is PermissionDecision.ALLOW
    assert eng.evaluate(ActionKind.SHELL_EXEC) is PermissionDecision.PROMPT


def test_path_confined_to_workspace(tmp_path: Path) -> None:
    """Paths inside the workspace go through the mode's normal risk gate;
    paths outside prompt for approval (yes/no) rather than hard-denying, so
    one-off cross-project reads / writes can be allowed without editing the
    config. Explicit read_only_paths still hard-deny (covered elsewhere)."""
    eng = _engine(PermissionMode.AUTONOMOUS, tmp_path)
    assert eng.check_path(tmp_path / "file.txt", write=True) is PermissionDecision.ALLOW
    assert eng.check_path(Path("/etc/passwd"), write=True) is PermissionDecision.PROMPT
    assert eng.check_path(tmp_path.parent / "outside.txt", write=True) is PermissionDecision.PROMPT


def test_blocked_commands_always_denied(tmp_path: Path) -> None:
    eng = _engine(PermissionMode.AUTONOMOUS, tmp_path)
    assert eng.check_command("rm -rf /") is PermissionDecision.DENY
    assert eng.check_command("format C:") is PermissionDecision.DENY


def test_allowed_commands_pass(tmp_path: Path) -> None:
    eng = _engine(PermissionMode.STRICT, tmp_path, allowed_commands=["git", "ls"])
    assert eng.check_command("git status") is PermissionDecision.ALLOW
    assert eng.check_command("ls -la") is PermissionDecision.ALLOW
    # Non-allowlisted in strict -> denied (high risk)
    assert eng.check_command("python script.py") is PermissionDecision.DENY
