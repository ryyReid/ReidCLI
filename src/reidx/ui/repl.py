"""Interactive REPL entry point.

Thin wrapper around the full-screen chat TUI in ui.app — kept as a separate
module so app/commands.py's `from reidx.ui.repl import repl` doesn't need
to change.
"""
from __future__ import annotations

from reidx.runtime.orchestrator import Orchestrator
from reidx.ui import app


def repl(orchestrator: Orchestrator, initial_prompt: str | None = None) -> int:
    return app.run(orchestrator, initial_prompt=initial_prompt)
