"""Context window: live API payload, known-model table, id tags, default."""
from __future__ import annotations

from reidx.provider.context_windows import (
    DEFAULT_CONTEXT_WINDOW,
    clear_live_cache,
    context_window_for,
    extract_context_from_model_obj,
    ingest_models_payload,
    known_context_for,
    remember_context,
    refresh_context_from_provider,
)


def setup_function() -> None:
    clear_live_cache()


def test_extract_from_openai_style_entry() -> None:
    n = extract_context_from_model_obj(
        {"id": "z-ai/glm-5.2", "context_length": 1_000_000}
    )
    assert n == 1_000_000


def test_extract_nested_openrouter_style() -> None:
    n = extract_context_from_model_obj(
        {"id": "x", "top_provider": {"context_length": 200_000}}
    )
    assert n == 200_000


def test_extract_nested_meta() -> None:
    n = extract_context_from_model_obj(
        {"id": "foo", "meta": {"max_model_len": 262_144}}
    )
    assert n == 262_144


def test_extract_input_token_limit() -> None:
    n = extract_context_from_model_obj(
        {"id": "m", "limits": {"input_token_limit": 200_000}}
    )
    assert n == 200_000


def test_live_api_payload_is_used() -> None:
    ingest_models_payload(
        [
            {"id": "z-ai/glm-5.2", "context_length": 1_000_000},
            {"id": "custom/weird", "max_model_len": 64_000},
        ]
    )
    assert context_window_for("z-ai/glm-5.2") == 1_000_000
    assert context_window_for("glm-5.2") == 1_000_000  # bare name cache
    assert context_window_for("custom/weird") == 64_000


def test_ingest_seeds_known_when_api_omits_context() -> None:
    """OpenAI/NIM-style catalogs often return only id — still show real windows."""
    ingest_models_payload(
        [
            {"id": "gpt-4o", "object": "model"},
            {"id": "claude-sonnet-4-20250514", "object": "model"},
            {"id": "meta/llama-3.1-70b-instruct", "object": "model"},
        ]
    )
    assert context_window_for("gpt-4o") == 128_000
    assert context_window_for("claude-sonnet-4-20250514") == 200_000
    assert context_window_for("meta/llama-3.1-70b-instruct") == 128_000


def test_known_table_without_live_cache() -> None:
    clear_live_cache()
    assert known_context_for("z-ai/glm-5.2") == 202_752
    assert context_window_for("z-ai/glm-5.2") == 202_752
    assert context_window_for("claude-3-5-sonnet-latest") == 200_000
    assert context_window_for("gpt-4.1-mini") == 1_047_576
    assert context_window_for("o3-mini") == 200_000
    # Longest fragment: gpt-4o-mini must not collapse to gpt-4 (8k)
    assert context_window_for("gpt-4o-mini") == 128_000
    assert context_window_for("gpt-4") == 8_192


def test_live_beats_known_and_session() -> None:
    remember_context("z-ai/glm-5.2", 1_000_000)
    # Live API value wins over known table (202752) and session (128k)
    assert context_window_for("z-ai/glm-5.2", session_window=128_000) == 1_000_000


def test_known_beats_stale_session_default() -> None:
    """Startup often stores 128k before any catalog — don't freeze wrong size."""
    clear_live_cache()
    assert context_window_for("claude-sonnet-4", session_window=128_000) == 200_000


def test_session_window_when_nothing_else() -> None:
    clear_live_cache()
    assert context_window_for("totally-unknown-xyz", session_window=999_000) == 999_000


def test_id_size_hint() -> None:
    clear_live_cache()
    assert context_window_for("vendor/model-32k-chat") == 32_000
    assert context_window_for("vendor/model[1m]") == 1_000_000
    # Unknown product with no table match → default
    assert context_window_for("acme/brand-new-model-xyz") == DEFAULT_CONTEXT_WINDOW


def test_refresh_from_provider_detailed() -> None:
    class _Prov:
        def fetch_models_detailed(self):
            return [{"id": "acme/big", "context_length": 500_000}]

    n = refresh_context_from_provider(_Prov(), "acme/big")
    assert n == 500_000
    assert context_window_for("acme/big") == 500_000


def test_refresh_network_false_uses_known() -> None:
    class _Prov:
        def fetch_models_detailed(self):
            raise AssertionError("must not hit network")

    n = refresh_context_from_provider(_Prov(), "gpt-4.1", network=False)
    assert n == 1_047_576


def test_remember_and_clear() -> None:
    remember_context("foo/bar", 12345)
    # 12345 < 1024 guard — remember requires >= 1024
    remember_context("foo/bar", 16000)
    assert context_window_for("foo/bar") == 16000
    clear_live_cache()
    assert context_window_for("totally-unknown-model-xyz") == DEFAULT_CONTEXT_WINDOW
