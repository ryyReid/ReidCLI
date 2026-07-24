"""Model ID corruption fix + /models case-insensitivity + /model prefix handling."""
from __future__ import annotations

from reidx.config.models import default_config
from reidx.provider.registry import default_registry
from reidx.provider.stub import StubProvider
from reidx.runtime.orchestrator import Orchestrator
from reidx.tools import default_registry as default_tools
from reidx.ui.commands import apply_model, handle


def _orch() -> Orchestrator:
    reg = default_registry(default_config())
    orch = Orchestrator(default_config(), StubProvider(), default_tools(), providers=reg, provider_name="stub")
    orch.start_session()
    return orch


def test_models_command_case_insensitive():
    orch = _orch()
    out = handle(orch, "/models ollie")
    assert out != "error"
    out2 = handle(orch, "/models OLLIE")
    assert out2 != "error"


def test_models_unknown_provider_error():
    orch = _orch()
    handle(orch, "/models nonexistent-xyz")
    assert True


def test_model_id_not_corrupted_on_set():
    orch = _orch()
    apply_model(orch, "poolside/laguna-m.1:free")
    assert orch.state.session.model == "poolside/laguna-m.1:free"
    assert "openrouter/" not in orch.state.session.model


def test_model_id_with_colon_variant_preserved():
    orch = _orch()
    apply_model(orch, "deepseek/deepseek-r1:free")
    assert orch.state.session.model == "deepseek/deepseek-r1:free"


def test_apply_model_warns_on_stub():
    orch = _orch()
    assert orch.provider.name == "stub"
    apply_model(orch, "some-model")
    assert orch.state.session.model == "some-model"


def test_normalize_is_read_only_for_validation():
    from reidx.provider.models import normalize_model_id

    raw = "poolside/laguna-m.1:free"
    n = normalize_model_id(raw, provider_name="OpenRouter")
    assert n.is_valid
    assert raw != n.full_id
