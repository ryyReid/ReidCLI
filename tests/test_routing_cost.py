"""Session cost ledger tests."""
from __future__ import annotations

from pathlib import Path

from reidx.config.models import default_config
from reidx.provider.registry import ProviderRegistry
from reidx.provider.stub import StubProvider
from reidx.runtime.cost import CostLedger, estimate_cost_usd, fmt_usd, price_for_model
from reidx.runtime.orchestrator import Orchestrator
from reidx.tools import default_registry


def test_price_claude_and_local() -> None:
    q = price_for_model("claude-sonnet-4-20250514")
    assert q.priced and q.input_per_mtok > 0
    cost, _ = estimate_cost_usd(
        "claude-sonnet-4-20250514", prompt_tokens=1_000_000, completion_tokens=0
    )
    assert abs(cost - 3.0) < 0.01
    q2 = price_for_model("stub-v0")
    assert q2.input_per_mtok == 0.0


def test_cost_ledger_records(tmp_path: Path) -> None:
    ledger = CostLedger(path=tmp_path / "costs.jsonl")
    ev = ledger.record(
        provider="anthropic",
        model="claude-sonnet-4",
        prompt_tokens=1000,
        completion_tokens=500,
    )
    assert ev.cost_usd > 0
    assert ledger.total_usd == ev.cost_usd
    assert (tmp_path / "costs.jsonl").exists()
    s = ledger.summary()
    assert s["turns"] == 1
    assert "claude-sonnet-4" in s["by_model"]
    assert fmt_usd(s["total_usd"]).startswith("$")


def test_orchestrator_has_cost_ledger(tmp_path: Path) -> None:
    cfg = default_config()
    cfg.workspace_root = tmp_path
    cfg.storage_root = tmp_path / "store"
    reg = ProviderRegistry()
    reg.register("stub", StubProvider())
    orch = Orchestrator(cfg, StubProvider(), default_registry(), providers=reg)
    orch.start_session("t")
    assert orch.state is not None
    assert orch.state.costs is not None
    assert orch.state.costs.path is not None
