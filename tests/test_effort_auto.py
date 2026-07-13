"""Tests for /effort auto resolution."""
from __future__ import annotations

from pathlib import Path

from reidx.config.models import default_config
from reidx.policy.engine import PolicyEngine
from reidx.provider.stub import StubProvider
from reidx.runtime.agent import Agent
from reidx.runtime.effort_auto import auto_effort_for, classify_prompt, resolve_effort
from reidx.runtime.state import RuntimeState
from reidx.session.models import Session
from reidx.tools import default_registry


def test_classify_and_auto_map() -> None:
    assert classify_prompt("hello") == "simple"
    assert auto_effort_for("hello") == "low"
    assert auto_effort_for("plan a multi-file refactor of the auth system") == "high"
    assert auto_effort_for("fix the typo in app.py") in ("medium", "high")


def test_resolve_effort_manual_passthrough() -> None:
    assert resolve_effort("high", "hello") == "high"
    assert resolve_effort("low", "plan everything") == "low"


def test_resolve_effort_auto() -> None:
    assert resolve_effort("auto", "thanks") == "low"
    assert resolve_effort("auto", "design an architecture for the runtime") == "high"


def test_agent_run_turn_resolves_auto(tmp_path: Path) -> None:
    cfg = default_config()
    cfg.workspace_root = tmp_path
    agent = Agent(StubProvider(), default_registry(), PolicyEngine(cfg))
    state = RuntimeState(session=Session(title="t", workspace=tmp_path, reasoning_effort="auto"))
    text, _ = agent.run_turn(state, "hello")
    assert text
    assert state.last_effort_resolved == "low"
    assert state.session.reasoning_effort == "auto"  # sticky setting stays auto
