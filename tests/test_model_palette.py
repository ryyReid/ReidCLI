"""ModelPalette state machine + filtering + selection."""
from __future__ import annotations

from reidx.provider_manager.model_palette import ModelPalette


def _make(current: str = "qwen3.8-max-preview") -> ModelPalette:
    selected: list[str] = []
    mp = ModelPalette(
        fetch_models=lambda: ([], None),
        on_select=selected.append,
        on_close=lambda: None,
        current_model=current,
        provider_name="Ollie Qwen 3.8Max",
        on_invalidate=lambda: None,
    )
    mp.term_cols = lambda: 80
    mp.term_rows = lambda: 24
    mp._selected_sink = selected  # type: ignore[attr-defined]
    return mp


def test_loading_then_ready():
    mp = _make()
    mp.activate()
    assert mp._state == "loading"
    mp.deliver_models(["a", "b"], None)
    assert mp._state == "ready"
    assert len(mp._models) == 2


def test_cursor_lands_on_current_model():
    mp = _make(current="b")
    mp.activate()
    mp.deliver_models(["a", "b", "c"], None)
    assert mp.selected_index == 1
    assert mp._models[mp.selected_index].id == "b"


def test_search_filters_models():
    mp = _make()
    mp.activate()
    mp.deliver_models(["qwen3.8-max", "deepseek-v4-flash", "qwen3.7-plus"], None)
    mp.search_buf.text = "deepseek"
    assert [m.id for m in mp._models] == ["deepseek-v4-flash"]


def test_select_fires_callback():
    mp = _make()
    mp.activate()
    mp.deliver_models(["a", "b"], None)
    mp.selected_index = 1
    mp.on_enter()
    assert mp._selected_sink == ["b"]  # type: ignore[attr-defined]
    assert mp.active is False


def test_error_state_renders():
    mp = _make()
    mp.activate()
    mp.deliver_models([], "HTTP 401: bad key")
    assert mp._state == "error"
    text = "".join(t for _, t in mp.content_fragments())
    assert "could not list models" in text
    assert "HTTP 401" in text


def test_enter_during_loading_is_noop():
    mp = _make()
    mp.activate()
    assert mp._state == "loading"
    mp.on_enter()
    assert mp._selected_sink == []  # type: ignore[attr-defined]


def test_tags_detected():
    mp = _make()
    mp.activate()
    mp.deliver_models(["deepseek-v4-vision", "deepseek-r1", "plain-model"], None)
    by_id = {m.id: m for m in mp._models}
    assert "vision" in by_id["deepseek-v4-vision"].tags
    assert "reason" in by_id["deepseek-r1"].tags
    assert by_id["plain-model"].tags == []
