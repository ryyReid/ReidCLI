"""Ollie Qwen 3.8Max built-in provider + model picker wiring."""
from __future__ import annotations

from reidx.config.models import default_config
from reidx.provider.registry import default_registry
from reidx.provider.stub import StubProvider
from reidx.runtime.orchestrator import Orchestrator
from reidx.tools import default_registry as default_tools
from reidx.ui.commands import handle


def _orch() -> Orchestrator:
    reg = default_registry(default_config())
    orch = Orchestrator(default_config(), StubProvider(), default_tools(), providers=reg, provider_name="stub")
    orch.start_session()
    return orch


def test_ollie_qwen_auto_registered():
    reg = default_registry(default_config())
    assert reg.has("ollie")
    assert reg.resolve("ollie-qwen") == "Ollie Qwen 3.8Max"
    assert reg.resolve("qwen-max") == "Ollie Qwen 3.8Max"
    assert reg.resolve("qwen3.8") == "Ollie Qwen 3.8Max"


def test_ollie_qwen_endpoint_shape():
    reg = default_registry(default_config())
    p = reg.get("ollie")
    assert p.base_url == "https://qwen3-8-api.vercel.app/v1"
    assert p.default_model == "qwen3.8-max-preview"


def test_model_bare_opens_picker():
    orch = _orch()
    assert handle(orch, "/model") == "model"
    assert handle(orch, "/model list") == "model"


def test_apply_model_sets_session():
    from reidx.ui.commands import apply_model

    orch = _orch()
    apply_model(orch, "stub-v0")
    assert orch.state.session.model == "stub-v0"


def test_ollie_catalog_entry():
    from reidx.provider_manager.catalog import by_id

    d = by_id("ollie-qwen")
    assert d is not None
    assert d.name == "Ollie Qwen 3.8Max"
    assert d.popular is True
    assert "ollie" in d.aliases
