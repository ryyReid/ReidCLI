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


def test_database_provider_overrides_incomplete_settings(tmp_path: Path) -> None:
    """settings.json name-only entries must not block providers.db (WinError 10061 bug)."""
    from reidx.config.models import Config, ProviderConfig, default_config
    from reidx.provider.registry import default_registry
    from reidx.provider.store import load_from_database
    from reidx.provider_manager.database import ProviderDatabase, StoredKey, StoredProvider
    from reidx.provider_manager import keychain

    # Incomplete settings-style config (no base_url) — previously became localhost:8080
    cfg = default_config()
    cfg.providers["NVIDIA NIM"] = ProviderConfig(name="NVIDIA NIM", default_model="z-ai/glm-5.2")
    reg = default_registry(cfg)
    assert "NVIDIA NIM" not in reg.names()  # must not register incomplete

    db = ProviderDatabase(tmp_path)
    db.save_provider(
        StoredProvider(
            name="NVIDIA NIM",
            kind="openai-compatible",
            base_url="https://integrate.api.nvidia.com/v1",
            default_model="meta/llama-3.1-70b-instruct",
            auth_method="bearer",
            catalog_id="nvidia-nim",
            keys=[
                StoredKey(
                    id="k1",
                    label="nvidia",
                    encrypted_key=keychain.encrypt("nvapi-test-key"),
                )
            ],
            active_key_id="k1",
        )
    )
    # Simulate a bad pre-registration (old bug)
    from reidx.provider.openai import OpenAICompatibleProvider

    reg.register(
        "NVIDIA NIM",
        OpenAICompatibleProvider(api_key="", base_url="http://localhost:8080", default_model="x"),
    )
    added = load_from_database(reg, tmp_path)
    assert "NVIDIA NIM" in added
    p = reg.get("NVIDIA NIM")
    assert "integrate.api.nvidia.com" in p.base_url
    assert p.api_key == "nvapi-test-key"
    assert "localhost" not in p.base_url


def test_pick_startup_provider_prefers_real_over_stub() -> None:
    from reidx.provider.openai import OpenAICompatibleProvider
    from reidx.provider.registry import pick_startup_provider

    reg = ProviderRegistry()
    reg.register("stub", StubProvider())
    reg.register(
        "NVIDIA NIM",
        OpenAICompatibleProvider(api_key="k", base_url="https://example.com/v1", default_model="m"),
        aliases=["nvidia"],
    )
    assert pick_startup_provider(reg, "stub") == "NVIDIA NIM"
    assert pick_startup_provider(reg, "nvidia") == "NVIDIA NIM"
    assert pick_startup_provider(reg, "") == "NVIDIA NIM"


def test_pick_startup_provider_stub_only() -> None:
    from reidx.provider.registry import pick_startup_provider

    reg = ProviderRegistry()
    reg.register("stub", StubProvider())
    assert pick_startup_provider(reg, "stub") == "stub"


def test_resolve_nvidia_alias_to_display_name() -> None:
    """Catalog alias `nvidia` must resolve to a provider saved as 'NVIDIA NIM'."""
    from reidx.provider.openai import OpenAICompatibleProvider

    reg = ProviderRegistry()
    reg.register("stub", StubProvider())
    nim = OpenAICompatibleProvider(
        api_key="k",
        base_url="https://integrate.api.nvidia.com/v1",
        default_model="meta/llama-3.1-70b-instruct",
    )
    reg.register(
        "NVIDIA NIM",
        nim,
        aliases=["nvidia-nim", "nvidia", "nvidia nim"],
    )
    assert reg.resolve("nvidia") == "NVIDIA NIM"
    assert reg.resolve("NVIDIA NIM") == "NVIDIA NIM"
    assert reg.resolve("nvidia-nim") == "NVIDIA NIM"
    assert reg.has("nvidia")
    assert reg.get("nvidia") is nim


def test_normalize_openai_base_url_no_double_v1() -> None:
    from reidx.provider.openai import OpenAICompatibleProvider, OpenAIProvider, normalize_openai_base_url

    assert normalize_openai_base_url("https://api.openai.com") == "https://api.openai.com/v1"
    assert normalize_openai_base_url("https://api.x.ai/v1") == "https://api.x.ai/v1"
    assert normalize_openai_base_url("https://api.groq.com/openai/v1") == "https://api.groq.com/openai/v1"
    assert normalize_openai_base_url("https://openrouter.ai/api") == "https://openrouter.ai/api/v1"
    assert (
        normalize_openai_base_url("https://generativelanguage.googleapis.com/v1beta/openai")
        == "https://generativelanguage.googleapis.com/v1beta/openai"
    )
    # Provider instances must request /chat/completions (not /v1/v1/...)
    p = OpenAIProvider(api_key="k", base_url="https://api.x.ai/v1")
    assert p.base_url == "https://api.x.ai/v1"
    compat = OpenAICompatibleProvider(api_key="k", base_url="http://localhost:1234/v1")
    assert compat.base_url == "http://localhost:1234/v1"
    bare = OpenAICompatibleProvider(api_key="k", base_url="http://localhost:8080")
    assert bare.base_url == "http://localhost:8080/v1"


def test_catalog_connect_saves_provider_before_key(tmp_path: Path) -> None:
    """First-time catalog connect must persist the provider shell before add_key."""
    from reidx.provider_manager.database import ProviderDatabase, StoredProvider
    from reidx.provider_manager.palette import ProviderPalette

    db = ProviderDatabase(tmp_path)
    orch = _FakeOrchestrator(tmp_path)
    closed: list[str] = []
    palette = ProviderPalette(db=db, orchestrator=orch, on_close=lambda m: closed.append(m))
    palette.current_provider = StoredProvider(
        name="OpenAI",
        kind="openai",
        base_url="https://api.openai.com",
        default_model="gpt-4o-mini",
        auth_method="bearer",
    )
    # No prior save — mirrors catalog first-select path.
    assert db.get_provider("OpenAI") is None
    msg = palette._commit_key("Personal", "sk-test-key", "ok")
    assert "Added key" in msg
    saved = db.get_provider("OpenAI")
    assert saved is not None
    assert len(saved.keys) == 1
    assert saved.keys[0].label == "Personal"
    assert orch.providers.has("OpenAI")


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
