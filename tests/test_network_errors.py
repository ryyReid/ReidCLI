"""Network error translation + model picker retry/generation guard."""
from __future__ import annotations

from reidx.provider._http import _network_error
from reidx.provider_manager.model_palette import ModelPalette


class _Exc(Exception):
    def __init__(self, reason) -> None:
        super().__init__(str(reason))
        self.reason = reason


def test_dns_failure_message_is_human():
    try:
        import urllib.request
        urllib.request.urlopen("https://nonexistent-host-xyz.invalid/x", timeout=3)
    except urllib.error.URLError as exc:
        err = _network_error(exc, 8)
        assert "resolve host" in str(err).lower()
        assert "11001" not in str(err)
        assert "getaddrinfo" not in str(err)


def test_timeout_message():
    err = _network_error(_Exc(TimeoutError("timed out")), 8)
    assert "timed out after 8s" in str(err)


def test_connection_refused_message():
    err = _network_error(_Exc(Exception("Connection refused")), 8)
    assert "refused" in str(err).lower()


def test_ssl_message_has_hint():
    err = _network_error(_Exc(Exception("SSL: CERTIFICATE_VERIFY_FAILED")), 8)
    assert "ssl" in str(err).lower()
    assert "REIDX_INSECURE" in str(err)


def test_model_retry_resets_to_loading():
    calls = []
    mp = ModelPalette(
        fetch_models=lambda: ([], None),
        on_select=lambda m: None,
        on_close=lambda: None,
        on_invalidate=lambda: None,
        on_retry=lambda: calls.append("retry"),
    )
    mp.activate()
    mp.deliver_models([], "could not resolve host")
    assert mp._state == "error"
    mp.retry()
    assert mp._state == "loading"
    assert calls == ["retry"]


def test_stale_fetch_delivery_ignored():
    mp = ModelPalette(
        fetch_models=lambda: ([], None),
        on_select=lambda m: None,
        on_close=lambda: None,
        on_invalidate=lambda: None,
    )
    mp.activate()
    old_gen = mp._fetch_gen
    mp._begin_fetch()  # simulates a second open → new generation
    new_gen = mp._fetch_gen
    assert new_gen > old_gen
    mp.deliver_models(["stale-model"], None, gen=old_gen)
    assert mp._models == []
    mp.deliver_models(["fresh-model"], None, gen=new_gen)
    assert len(mp._models) == 1
    assert mp._models[0].id == "fresh-model"


def test_delivery_after_close_is_ignored():
    mp = ModelPalette(
        fetch_models=lambda: ([], None),
        on_select=lambda m: None,
        on_close=lambda: None,
        on_invalidate=lambda: None,
    )
    mp.activate()
    mp.deactivate()
    mp.deliver_models(["a", "b"], None)
    assert mp._models == []
