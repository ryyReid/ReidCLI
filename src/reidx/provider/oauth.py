"""OAuth integration for provider authentication.

Follows opencode patterns for browser and device authorization flows.
Supports OpenAI, Anthropic, and other OAuth-enabled providers.
"""
from __future__ import annotations

import base64
import hashlib
import os
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from reidx.diagnostics.logger import get_logger

log = get_logger("reidx.provider.oauth")


@dataclass
class OAuthConfig:
    client_id: str
    client_secret: str
    issuer: str
    authorize_endpoint: str
    token_endpoint: str
    device_endpoint: str = ""
    scopes: list[str] = None
    callback_port: int = 1455
    redirect_uri: str = ""

    def __post_init__(self):
        if self.scopes is None:
            self.scopes = ["openid", "profile", "email", "offline_access"]
        if not self.redirect_uri:
            self.redirect_uri = f"http://localhost:{self.callback_port}/auth/callback"


@dataclass
class OAuthTokens:
    access_token: str
    refresh_token: str
    id_token: str = ""
    expires_at: float = 0
    scope: str = ""
    token_type: str = "Bearer"

    def is_expired(self, buffer_seconds: int = 300) -> bool:
        return self.expires_at - time.time() < buffer_seconds

    def to_dict(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "id_token": self.id_token,
            "expires_at": self.expires_at,
            "scope": self.scope,
            "token_type": self.token_type,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OAuthTokens:
        return cls(
            access_token=data["access_token"],
            refresh_token=data["refresh_token"],
            id_token=data.get("id_token", ""),
            expires_at=data.get("expires_at", 0),
            scope=data.get("scope", ""),
            token_type=data.get("token_type", "Bearer"),
        )


@dataclass
class DeviceAuthResponse:
    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str
    expires_in: int
    interval: int


class OAuthProvider:
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

    ANTHROPIC = OAuthConfig(
        client_id="",
        client_secret="",
        issuer="https://console.anthropic.com",
        authorize_endpoint="/oauth/authorize",
        token_endpoint="/oauth/token",
        scopes=["openid", "profile", "email"],
    )

    GOOGLE = OAuthConfig(
        client_id="",
        client_secret="",
        issuer="https://accounts.google.com",
        authorize_endpoint="/o/oauth2/v2/auth",
        token_endpoint="/o/oauth2/v2/token",
        scopes=["openid", "profile", "email"],
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
    def __init__(self, port: int, handler: Callable[[dict], None]):
        self.port = port
        self.handler = handler
        self.server: HTTPServer | None = None
        self.thread: Thread | None = None
        self._code: str | None = None
        self._error: str | None = None

    def start(self) -> None:
        class CallbackHandler(BaseHTTPRequestHandler):
            def do_GET(inner_self):
                parsed = urlparse(inner_self.path)
                params = parse_qs(parsed.query)
                if "code" in params:
                    self._code = params["code"][0]
                    inner_self.send_response(200)
                    inner_self.send_header("Content-Type", "text/html")
                    inner_self.end_headers()
                    inner_self.wfile.write(b"""
                        <html><body>
                        <h2>Authorization successful!</h2>
                        <p>You can close this window.</p>
                        <script>window.close();</script>
                        </body></html>
                    """)
                elif "error" in params:
                    self._error = params.get("error_description", [params["error"][0]])[0]
                    inner_self.send_response(400)
                    inner_self.send_header("Content-Type", "text/html")
                    inner_self.end_headers()
                    inner_self.wfile.write(f"""
                        <html><body>
                        <h2>Authorization failed</h2>
                        <p>{self._error}</p>
                        </body></html>
                    """.encode())
                else:
                    inner_self.send_response(400)
                    inner_self.end_headers()
                self.handler(params)

            def log_message(inner_self, format, *args):
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
        return f"{self.config.issuer.rstrip('/')}{endpoint}"

    def build_authorize_url(self, state: str, code_challenge: str) -> str:
        params = {
            "response_type": "code",
            "client_id": self.config.client_id,
            "redirect_uri": self.config.redirect_uri,
            "scope": " ".join(self.config.scopes),
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        if self.config.issuer == "https://auth.openai.com":
            params["codex_cli_simplified_flow"] = "true"
            params["originator"] = "opencode"
        return f"{self._get_issuer_url(self.config.authorize_endpoint)}?{urlencode(params)}"

    def exchange_code(self, code: str, code_verifier: str, redirect_uri: str) -> OAuthTokens:
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
            "client_id": self.config.client_id,
        }
        if self.config.client_secret:
            data["client_secret"] = self.config.client_secret

        with httpx.Client(timeout=30.0) as client:
            resp = client.post(
                self._get_issuer_url(self.config.token_endpoint),
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            resp.raise_for_status()
            token_data = resp.json()

        return OAuthTokens(
            access_token=token_data["access_token"],
            refresh_token=token_data.get("refresh_token", ""),
            id_token=token_data.get("id_token", ""),
            expires_at=time.time() + token_data.get("expires_in", 3600),
            scope=token_data.get("scope", ""),
            token_type=token_data.get("token_type", "Bearer"),
        )

    def refresh_tokens(self, refresh_token: str) -> OAuthTokens:
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": self.config.client_id,
        }
        if self.config.client_secret:
            data["client_secret"] = self.config.client_secret

        with httpx.Client(timeout=30.0) as client:
            resp = client.post(
                self._get_issuer_url(self.config.token_endpoint),
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            resp.raise_for_status()
            token_data = resp.json()

        return OAuthTokens(
            access_token=token_data["access_token"],
            refresh_token=token_data.get("refresh_token", refresh_token),
            id_token=token_data.get("id_token", ""),
            expires_at=time.time() + token_data.get("expires_in", 3600),
            scope=token_data.get("scope", ""),
            token_type=token_data.get("token_type", "Bearer"),
        )

    def start_device_auth(self) -> DeviceAuthResponse:
        if not self.config.device_endpoint:
            raise ValueError("Device authorization not supported for this provider")

        with httpx.Client(timeout=30.0) as client:
            resp = client.post(
                self._get_issuer_url(self.config.device_endpoint),
                json={"client_id": self.config.client_id},
                headers={"Content-Type": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()

        return DeviceAuthResponse(
            device_code=data["device_auth_id"],
            user_code=data["user_code"],
            verification_uri=f"{self.config.issuer}/codex/device",
            verification_uri_complete=f"{self.config.issuer}/codex/device?user_code={data['user_code']}",
            expires_in=int(data.get("interval", 5)) * 600,
            interval=int(data.get("interval", 5)),
        )

    def poll_device_token(self, device_code: str) -> OAuthTokens | None:
        if not self.config.device_endpoint:
            return None

        with httpx.Client(timeout=30.0) as client:
            resp = client.post(
                f"{self.config.issuer}/api/accounts/deviceauth/token",
                json={
                    "device_auth_id": device_code,
                    "client_id": self.config.client_id,
                },
                headers={"Content-Type": "application/json"},
            )
            if resp.status_code == 403 or resp.status_code == 404:
                return None
            if not resp.is_success:
                return None
            data = resp.json()

        return OAuthTokens(
            access_token=data.get("access_token", ""),
            refresh_token=data.get("refresh_token", ""),
            id_token=data.get("id_token", ""),
            expires_at=time.time() + data.get("expires_in", 3600),
            scope=data.get("scope", ""),
            token_type=data.get("token_type", "Bearer"),
        )

    def save_tokens(self, tokens: OAuthTokens) -> None:
        from reidx.provider_manager.keychain import encrypt
        encrypted = encrypt(str(tokens.to_dict()))
        os.environ[f"REIDX_OAUTH_{self.storage_key}"] = encrypted

    def load_tokens(self) -> OAuthTokens | None:
        from reidx.provider_manager.keychain import decrypt
        encrypted = os.environ.get(f"REIDX_OAUTH_{self.storage_key}")
        if not encrypted:
            return None
        try:
            data = eval(decrypt(encrypted))
            return OAuthTokens.from_dict(data)
        except Exception:
            return None


def create_oauth_client(provider_kind: str) -> OAuthClient | None:
    configs = {
        "openai": (OAuthProvider.OPEN_AI, "OPENAI"),
    }
    if provider_kind not in configs:
        return None
    config, key = configs[provider_kind]
    return OAuthClient(config, key)


def run_browser_oauth(provider_kind: str) -> OAuthTokens | None:
    client = create_oauth_client(provider_kind)
    if not client:
        return None

    state = secrets.token_urlsafe(32)
    verifier, challenge = PKCE.generate()

    server = LocalCallbackServer(client.config.callback_port, lambda _: None)
    server.start()

    try:
        auth_url = client.build_authorize_url(state, challenge)
        log.info("Opening browser for %s authorization: %s", provider_kind, auth_url)

        import webbrowser
        webbrowser.open(auth_url)

        code, error = server.wait_for_callback(120)
        if error:
            log.error("OAuth authorization failed: %s", error)
            return None

        tokens = client.exchange_code(code, verifier, client.config.redirect_uri)
        client.save_tokens(tokens)
        return tokens

    finally:
        server.stop()


def run_device_oauth(provider_kind: str, on_user_code: Callable[[str, str], None]) -> OAuthTokens | None:
    client = create_oauth_client(provider_kind)
    if not client or not client.config.device_endpoint:
        return None

    device_auth = client.start_device_auth()
    on_user_code(device_auth.user_code, device_auth.verification_uri_complete)

    deadline = time.time() + device_auth.expires_in
    while time.time() < deadline:
        tokens = client.poll_device_token(device_auth.device_code)
        if tokens:
            client.save_tokens(tokens)
            return tokens
        time.sleep(device_auth.interval)

    return None