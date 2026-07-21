"""OAuth integration for provider authentication.

Browser (authorization code + PKCE) and device authorization flows.

HTTP goes through `reidx.provider._http`, which carries the project's shared
SSL context and error handling — no extra HTTP client dependency.

Token persistence is the caller's job: `run_browser_oauth` / `run_device_oauth`
return the tokens, and the provider database stores them.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from urllib.parse import parse_qs, urlencode, urlparse

from reidx.diagnostics.logger import get_logger
from reidx.provider._http import post_form, post_json
from reidx.provider.base import ProviderError
from reidx.provider_manager.database import OAuthTokens

log = get_logger("reidx.provider.oauth")

_OAUTH_TIMEOUT = 30


@dataclass
class OAuthConfig:
    client_id: str
    client_secret: str
    issuer: str
    authorize_endpoint: str
    token_endpoint: str
    device_endpoint: str = ""
    scopes: list[str] | None = None
    callback_port: int = 1455
    redirect_uri: str = ""
    # "form" (application/x-www-form-urlencoded, OpenAI/Codex) or "json"
    # (application/json, Anthropic) for the token endpoint body.
    token_style: str = "form"
    # True when the provider redirects to a hosted callback page and the user
    # must copy the returned code back in (Anthropic) rather than a loopback
    # server catching it automatically (OpenAI/Codex).
    manual: bool = False
    # Extra static query params on the authorize URL (Anthropic: code=true).
    authorize_params: dict[str, str] | None = None
    # Anthropic uses the PKCE verifier as the `state` value...
    state_is_verifier: bool = False
    # ...and echoes it back as `code#state`, which must be sent in the token body.
    send_state_in_token: bool = False

    def __post_init__(self) -> None:
        if self.scopes is None:
            self.scopes = ["openid", "profile", "email", "offline_access"]
        if not self.redirect_uri:
            self.redirect_uri = f"http://localhost:{self.callback_port}/auth/callback"


@dataclass
class DeviceAuthResponse:
    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str
    expires_in: int
    interval: int


class OAuthProvider:
    # OpenAI / Codex CLI public client — loopback callback on :1455, plus a
    # device-code flow. Token endpoint is form-encoded.
    OPEN_AI = OAuthConfig(
        client_id="app_EMoamEEZ73f0CkXaXp7hrann",
        client_secret="",
        issuer="https://auth.openai.com",
        authorize_endpoint="/oauth/authorize",
        token_endpoint="/oauth/token",
        device_endpoint="/api/accounts/deviceauth/usercode",
        scopes=["openid", "profile", "email", "offline_access"],
        callback_port=1455,
    )

    # Anthropic / Claude Code public client. Authorization happens on
    # claude.ai; the code is shown on a hosted callback page and pasted back
    # (manual). Tokens are exchanged as JSON on console.anthropic.com.
    ANTHROPIC = OAuthConfig(
        client_id="9d1c250a-e61b-44d9-88ed-5944d1962f5e",
        client_secret="",
        issuer="https://claude.ai",
        authorize_endpoint="/oauth/authorize",
        token_endpoint="https://console.anthropic.com/v1/oauth/token",
        scopes=["org:create_api_key", "user:profile", "user:inference"],
        redirect_uri="https://console.anthropic.com/oauth/code/callback",
        token_style="json",
        manual=True,
        authorize_params={"code": "true"},
        state_is_verifier=True,
        send_state_in_token=True,
    )


class PKCE:
    @staticmethod
    def generate() -> tuple[str, str]:
        verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()
        ).decode().rstrip("=")
        return verifier, challenge


class LocalCallbackServer:
    """Single-shot loopback listener for the OAuth redirect.

    `expected_state` is compared against the `state` the provider echoes back;
    a mismatch is rejected rather than exchanged (CSRF / code injection).
    """

    def __init__(self, port: int, expected_state: str, handler: Callable[[dict], None]):
        self.port = port
        self.expected_state = expected_state
        self.handler = handler
        self.server: HTTPServer | None = None
        self.thread: Thread | None = None
        self._code: str | None = None
        self._error: str | None = None

    def start(self) -> None:
        outer = self

        class CallbackHandler(BaseHTTPRequestHandler):
            def _reply(inner_self, status: int, title: str, body: str) -> None:
                inner_self.send_response(status)
                inner_self.send_header("Content-Type", "text/html; charset=utf-8")
                inner_self.end_headers()
                inner_self.wfile.write(
                    f"<html><body><h2>{title}</h2><p>{body}</p></body></html>".encode()
                )

            def do_GET(inner_self) -> None:
                params = parse_qs(urlparse(inner_self.path).query)
                if "code" in params:
                    state = (params.get("state") or [""])[0]
                    if not secrets.compare_digest(state, outer.expected_state):
                        outer._error = "state mismatch — possible CSRF, authorization rejected"
                        inner_self._reply(400, "Authorization failed", outer._error)
                    else:
                        outer._code = params["code"][0]
                        inner_self._reply(
                            200, "Authorization successful!", "You can close this window."
                        )
                elif "error" in params:
                    outer._error = params.get("error_description", [params["error"][0]])[0]
                    inner_self._reply(400, "Authorization failed", outer._error)
                else:
                    inner_self._reply(400, "Authorization failed", "Missing code.")
                outer.handler(params)

            def log_message(inner_self, format, *args) -> None:
                pass

        self.server = HTTPServer(("localhost", self.port), CallbackHandler)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        if self.thread:
            self.thread.join(timeout=1)

    def wait_for_callback(self, timeout: int = 120) -> tuple[str | None, str | None]:
        start = time.time()
        while time.time() - start < timeout:
            if self._code or self._error:
                return self._code, self._error
            time.sleep(0.1)
        return None, "Authorization timeout"


class OAuthClient:
    def __init__(self, config: OAuthConfig, storage_key: str):
        self.config = config
        self.storage_key = storage_key

    def _get_issuer_url(self, endpoint: str) -> str:
        # Some providers host the token endpoint on a different origin than the
        # authorize endpoint (Anthropic: claude.ai vs console.anthropic.com),
        # so an absolute endpoint is used verbatim.
        if endpoint.startswith("http://") or endpoint.startswith("https://"):
            return endpoint
        return f"{self.config.issuer.rstrip('/')}{endpoint}"

    def _post_token(self, data: dict) -> dict:
        url = self._get_issuer_url(self.config.token_endpoint)
        if self.config.token_style == "json":
            # JSON body; keep the default (browser-ish) User-Agent — forcing an
            # unusual one (e.g. "anthropic") gets 429'd by the edge/WAF.
            return post_json(url, data, headers={}, timeout=_OAUTH_TIMEOUT)
        return post_form(url, data, timeout=_OAUTH_TIMEOUT)

    def build_authorize_url(self, state: str, code_challenge: str) -> str:
        params = {
            "response_type": "code",
            "client_id": self.config.client_id,
            "redirect_uri": self.config.redirect_uri,
            "scope": " ".join(self.config.scopes or []),
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        if self.config.issuer == "https://auth.openai.com":
            params["codex_cli_simplified_flow"] = "true"
            params["originator"] = "opencode"
        if self.config.authorize_params:
            params.update(self.config.authorize_params)
        return f"{self._get_issuer_url(self.config.authorize_endpoint)}?{urlencode(params)}"

    def _tokens_from_payload(self, data: dict, fallback_refresh: str = "") -> OAuthTokens:
        return OAuthTokens(
            access_token=data.get("access_token", ""),
            refresh_token=data.get("refresh_token", fallback_refresh),
            id_token=data.get("id_token", ""),
            expires_at=time.time() + data.get("expires_in", 3600),
            scope=data.get("scope", ""),
            token_type=data.get("token_type", "Bearer"),
        )

    def exchange_code(
        self, code: str, code_verifier: str, redirect_uri: str, state: str = ""
    ) -> OAuthTokens:
        # Manual (paste-back) flows return "code#state"; split the halves.
        code_part, _, state_part = code.partition("#")
        code_part = code_part.strip()
        data = {
            "grant_type": "authorization_code",
            "code": code_part,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
            "client_id": self.config.client_id,
        }
        # Anthropic requires the returned state echoed back in the token body.
        if self.config.send_state_in_token:
            data["state"] = state or state_part.strip()
        if self.config.client_secret:
            data["client_secret"] = self.config.client_secret
        return self._tokens_from_payload(self._post_token(data))

    def refresh_tokens(self, refresh_token: str) -> OAuthTokens:
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": self.config.client_id,
        }
        if self.config.client_secret:
            data["client_secret"] = self.config.client_secret
        return self._tokens_from_payload(self._post_token(data), fallback_refresh=refresh_token)

    def start_device_auth(self) -> DeviceAuthResponse:
        if not self.config.device_endpoint:
            raise ValueError("Device authorization not supported for this provider")
        data = post_json(
            self._get_issuer_url(self.config.device_endpoint),
            {"client_id": self.config.client_id},
            headers={},
            timeout=_OAUTH_TIMEOUT,
        )
        interval = int(data.get("interval", 5))
        return DeviceAuthResponse(
            device_code=data["device_auth_id"],
            user_code=data["user_code"],
            verification_uri=f"{self.config.issuer}/codex/device",
            verification_uri_complete=(
                f"{self.config.issuer}/codex/device?user_code={data['user_code']}"
            ),
            expires_in=int(data.get("expires_in", 900)),
            interval=interval,
        )

    def poll_device_token(self, device_code: str) -> OAuthTokens | None:
        """One poll. None means "not authorized yet" — the caller keeps waiting."""
        if not self.config.device_endpoint:
            return None
        try:
            data = post_json(
                f"{self.config.issuer}/api/accounts/deviceauth/token",
                {"device_auth_id": device_code, "client_id": self.config.client_id},
                headers={},
                timeout=_OAUTH_TIMEOUT,
            )
        except ProviderError:
            return None
        tokens = self._tokens_from_payload(data)
        # A 200 with no token means the user has not approved yet.
        return tokens if tokens.access_token else None


def create_oauth_client(provider_kind: str) -> OAuthClient | None:
    configs = {
        "openai": (OAuthProvider.OPEN_AI, "OPENAI"),
        "anthropic": (OAuthProvider.ANTHROPIC, "ANTHROPIC"),
    }
    if provider_kind not in configs:
        return None
    config, key = configs[provider_kind]
    return OAuthClient(config, key)


def oauth_supported(provider_kind: str) -> bool:
    """Whether `provider_kind` has a usable OAuth client registered."""
    return create_oauth_client(provider_kind) is not None


def is_manual_oauth(provider_kind: str) -> bool:
    """True when the flow needs the user to paste a code back (Anthropic)."""
    client = create_oauth_client(provider_kind)
    return bool(client and client.config.manual)


def begin_manual_oauth(provider_kind: str) -> tuple[str, str, str] | None:
    """Start a paste-back OAuth flow.

    Returns (authorize_url, code_verifier, state). The caller opens the URL,
    collects the pasted code, and passes everything to `complete_manual_oauth`.
    """
    client = create_oauth_client(provider_kind)
    if not client:
        log.error("OAuth is not configured for provider kind %r", provider_kind)
        return None
    verifier, challenge = PKCE.generate()
    state = verifier if client.config.state_is_verifier else secrets.token_urlsafe(32)
    return client.build_authorize_url(state, challenge), verifier, state


def complete_manual_oauth(
    provider_kind: str, pasted: str, verifier: str, state: str
) -> OAuthTokens | None:
    """Finish a paste-back flow: validate state, exchange the code for tokens."""
    client = create_oauth_client(provider_kind)
    if not client:
        return None
    pasted = (pasted or "").strip()
    if not pasted:
        log.error("No authorization code provided")
        return None
    # Anthropic returns "code#state"; verify the state half if present.
    if "#" in pasted:
        _code, _, returned_state = pasted.partition("#")
        if returned_state and not secrets.compare_digest(returned_state.strip(), state):
            log.error("OAuth state mismatch — possible CSRF, authorization rejected")
            return None
    try:
        tokens = client.exchange_code(pasted, verifier, client.config.redirect_uri, state=state)
    except ProviderError as exc:
        log.error("OAuth token exchange failed: %s", exc)
        return None
    if not tokens.access_token:
        log.error("OAuth token exchange returned no access token")
        return None
    return tokens


def run_browser_oauth(provider_kind: str) -> OAuthTokens | None:
    client = create_oauth_client(provider_kind)
    if not client:
        log.error("OAuth is not configured for provider kind %r", provider_kind)
        return None

    state = secrets.token_urlsafe(32)
    verifier, challenge = PKCE.generate()

    server = LocalCallbackServer(client.config.callback_port, state, lambda _: None)
    try:
        server.start()
    except OSError as exc:
        log.error("Cannot listen on port %s for OAuth callback: %s", client.config.callback_port, exc)
        return None

    try:
        auth_url = client.build_authorize_url(state, challenge)
        log.info("Opening browser for %s authorization", provider_kind)

        import webbrowser

        webbrowser.open(auth_url)

        code, error = server.wait_for_callback(120)
        if error or not code:
            log.error("OAuth authorization failed: %s", error or "no authorization code")
            return None

        try:
            tokens = client.exchange_code(code, verifier, client.config.redirect_uri)
        except ProviderError as exc:
            log.error("OAuth token exchange failed: %s", exc)
            return None
        if not tokens.access_token:
            log.error("OAuth token exchange returned no access token")
            return None
        return tokens
    finally:
        server.stop()


def run_device_oauth(
    provider_kind: str, on_user_code: Callable[[str, str], None]
) -> OAuthTokens | None:
    client = create_oauth_client(provider_kind)
    if not client or not client.config.device_endpoint:
        log.error("Device OAuth is not configured for provider kind %r", provider_kind)
        return None

    try:
        device_auth = client.start_device_auth()
    except (ProviderError, ValueError, KeyError) as exc:
        log.error("Device authorization request failed: %s", exc)
        return None

    on_user_code(device_auth.user_code, device_auth.verification_uri_complete)

    deadline = time.time() + device_auth.expires_in
    while time.time() < deadline:
        tokens = client.poll_device_token(device_auth.device_code)
        if tokens:
            return tokens
        time.sleep(device_auth.interval)

    log.error("Device authorization timed out")
    return None
