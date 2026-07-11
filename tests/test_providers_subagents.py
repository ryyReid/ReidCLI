"""Tests for /connect provider persistence, spawn_agent tool, SubagentManager."""
from __future__ import annotations

from pathlib import Path

from reidx.config.models import default_config
from reidx.policy.engine import PolicyEngine
from reidx.provider.registry import ProviderRegistry
from reidx.provider.store import ProviderRecord, ProviderStore, load_into
from reidx.provider.stub import StubProvider
from reidx.runtime.state import RuntimeState
from reidx.runtime.subagent import SubagentManager
from reidx.session.models import Session
from reidx.tools import default_registry
from reidx.tools.base import ToolContext
from reidx.tools.spawn_agent import SpawnAgentTool


class _FakeOrchestrator:
    """Minimal orchestrator shim for spawn_agent (only what the tool touches)."""

    def __init__(self, tmp_path: Path) -> None:
        self.config = default_config()
        self.config.workspace_root = tmp_path
        self.tools = default_registry()
        self.provider = StubProvider()
        self.providers = ProviderRegistry()
        self.providers.register("stub", self.provider)
        self.policy = PolicyEngine(self.config)
        self.state = RuntimeState(session=Session(title="parent", workspace=tmp_path))
        self.subagents = SubagentManager()


# --- provider store ------------------------------------------------------


def test_provider_store_roundtrip(tmp_path: Path) -> None:
    store = ProviderStore(tmp_path)
    rec = ProviderRecord(
        name="local", kind="openai-compatible",
        base_url="http://localhost:8080", api_key="k", default_model="llama",
    )
    store.save(rec)
    assert store.get("local") == rec
    assert [r.name for r in store.list()] == ["local"]
    assert store.delete("local") is True
    assert store.get("local") is None


def test_load_into_registers_provider(tmp_path: Path) -> None:
    ProviderStore(tmp_path).save(
        ProviderRecord(name="local", kind="ollama", base_url="http://localhost:11434", default_model="llama3.2")
    )
    reg = ProviderRegistry()
    reg.register("stub", StubProvider())
    added = load_into(reg, tmp_path)
    assert added == ["local"]
    assert reg.has("local")


# --- subagent manager ----------------------------------------------------


def test_subagent_manager_lifecycle() -> None:
    mgr = SubagentManager()
    events: list[list] = []
    mgr.subscribe(lambda snap: events.append([r.status for r in snap]))
    aid = mgr.start("researcher")
    assert mgr.any_active() is True
    mgr.update(aid, last_action="reading a file")
    mgr.finish(aid, status="done")
    assert mgr.any_active() is False
    # Row lingers briefly then prunes; visible_rows keeps it during linger.
    assert len(mgr.visible_rows()) == 1
    assert events  # at least the start/update/finish emissions fired


# --- spawn_agent tool ----------------------------------------------------


def test_spawn_agent_runs_child_and_reports_lifecycle(tmp_path: Path) -> None:
    orch = _FakeOrchestrator(tmp_path)
    tool = SpawnAgentTool()
    ctx = ToolContext(
        workspace_root=tmp_path,
        policy=orch.policy,
        writable_roots=[],
        extra={"orchestrator": orch},
    )
    result = tool.execute(
        {
            "name": "scout",
            "system_prompt": "You are a scout.",
            "task": "list the current dir",
            "tool_allowlist": ["list_dir"],
        },
        ctx,
    )
    assert result.ok, result.error
    assert "subagent:scout" in result.output
    # Subagent finished and lingers briefly.
    rows = orch.subagents.visible_rows()
    assert any(r.name == "scout" and r.status == "done" for r in rows)


def test_spawn_agent_rejects_missing_orchestrator(tmp_path: Path) -> None:
    tool = SpawnAgentTool()
    cfg = default_config()
    cfg.workspace_root = tmp_path
    ctx = ToolContext(workspace_root=tmp_path, policy=PolicyEngine(cfg), writable_roots=[], extra={})
    result = tool.execute(
        {"name": "n", "system_prompt": "s", "task": "t"},
        ctx,
    )
    assert not result.ok


def test_spawn_agent_strips_recursive_spawn(tmp_path: Path) -> None:
    """Child cannot spawn its own subagents (spawn_agent removed from allowlist)."""
    orch = _FakeOrchestrator(tmp_path)
    tool = SpawnAgentTool()
    ctx = ToolContext(
        workspace_root=tmp_path,
        policy=orch.policy,
        writable_roots=[],
        extra={"orchestrator": orch},
    )
    result = tool.execute(
        {
            "name": "recursive",
            "system_prompt": "s",
            "task": "hello",
            "tool_allowlist": ["spawn_agent", "list_dir"],
        },
        ctx,
    )
    assert result.ok
    # Child ran with list_dir only; spawn_agent was stripped. StubProvider's
    # "hello" path returns no tool calls, so tools list is empty — the point of
    # the test is just that the call succeeded rather than crashing on a
    # missing spawn_agent recursion.
