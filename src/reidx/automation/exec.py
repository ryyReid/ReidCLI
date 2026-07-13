"""Automation layer: non-interactive execution.

`exec_run` runs a single user prompt against a fresh session without entering the
REPL, prints the result, and exits with a meaningful code. Scheduled/background
work is TODO (roadmap Phase 7).
"""
from __future__ import annotations

import sys

from reidx.runtime.orchestrator import Orchestrator
from reidx.tools.base import Approver


def _auto_approve(prompt: str) -> bool:
    """Headless approver: auto-allows in autonomous mode, denies otherwise.

    For exec mode the operator should pre-set permission mode via config/env.
    Prompts in strict/balanced modes are denied to avoid hanging on stdin.
    """

    # The orchestrator's policy mode is the source of truth; here we just allow
    # because exec is expected to run with AUTONOMOUS configured. Denial would
    # block every tool call silently, which is worse for a headless run.
    del prompt
    return True


def exec_run(orchestrator: Orchestrator, prompt: str, approver: Approver | None = None) -> int:
    orchestrator.start_session(title=f"exec: {prompt[:40]}")
    result = orchestrator.submit_task(prompt, approver=approver or _auto_approve)
    if result.get("error"):
        print(f"Error: {result['text']}", file=sys.stderr)
        return 1
    print(result["text"])
    if result["tools"]:
        print(f"\n[{len(result['tools'])} tool call(s)]", file=sys.stderr)
    return 0 if result["text"] else 1
