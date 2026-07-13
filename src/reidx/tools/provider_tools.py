"""Agent tools for provider connect / use / list (OpenCode-style).

Lets the model add backends and switch models without the user only using
`/connect` in the palette — still policy-gated for key writes.
"""
from __future__ import annotations

import uuid
from typing import Any

from reidx.policy.models import PermissionDecision, RiskLevel
from reidx.tools.base import BaseTool, ToolContext, ToolDefinition, ToolResult

_LIST_CATALOG_PARAMS = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "Optional search (e.g. 'openai', 'go', 'nvidia', 'ollama'). Empty = popular list.",
        },
        "all": {
            "type": "boolean",
            "description": "If true, return the full catalog (can be long). Default false = popular only when query empty.",
        },
    },
}

_LIST_CONNECTED_PARAMS = {
    "type": "object",
    "properties": {},
}

_CONNECT_PARAMS = {
    "type": "object",
    "properties": {
        "catalog_id": {
            "type": "string",
            "description": (
                "Catalog provider id from list_provider_catalog "
                "(e.g. opencode-go, nvidia-nim, openai, anthropic, ollama)."
            ),
        },
        "name": {
            "type": "string",
            "description": "Display name override (default: catalog name).",
        },
        "kind": {
            "type": "string",
            "description": "If not using catalog_id: anthropic | openai | openai-compatible | ollama",
        },
        "base_url": {
            "type": "string",
            "description": "API base URL (required for custom openai-compatible; optional for catalog).",
        },
        "api_key": {
            "type": "string",
            "description": "API key / token. Empty allowed for local ollama/lm-studio.",
        },
        "default_model": {
            "type": "string",
            "description": "Default model id for this provider.",
        },
        "auth_method": {
            "type": "string",
            "enum": ["bearer", "x-api-key", "oauth", "none"],
            "description": "Authentication method. Use 'oauth' for browser/device auth (OpenAI, Anthropic).",
        },
        "oauth_flow": {
            "type": "string",
            "enum": ["browser", "device"],
            "description": "OAuth flow to use: 'browser' opens a window, 'device' shows a code for headless auth.",
        },
        "activate": {
            "type": "boolean",
            "description": "If true (default), switch the session to this provider after save.",
        },
    },
}

_USE_PARAMS = {
    "type": "object",
    "properties": {
        "provider": {
            "type": "string",
            "description": "Registered name or alias (e.g. 'OpenCode Go', 'opencode', 'nvidia').",
        },
        "model": {
            "type": "string",
            "description": "Optional model id to set after switching.",
        },
    },
    "required": ["provider"],
}

_DISCONNECT_PARAMS = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": "Exact provider display name to remove (not stub).",
        },
    },
    "required": ["name"],
}

_SET_MODEL_PARAMS = {
    "type": "object",
    "properties": {
        "model": {
            "type": "string",
            "description": "Model id for the current session (e.g. glm-5.2, deepseek-v4-pro).",
        },
    },
    "required": ["model"],
}


def _orch(ctx: ToolContext):
    return ctx.extra.get("orchestrator")


def _storage_root(orch) -> "Path":  # noqa: F821
    from pathlib import Path

    from reidx.config.storage import storage_root as default_root

    return Path(orch.config.storage_root or default_root())


def _ensure_registry(orch) -> None:
    """Attach a ProviderRegistry if the orchestrator was built without one."""
    if orch.providers is not None:
        return
    from reidx.provider.registry import ProviderRegistry
    from reidx.provider.stub import StubProvider

    reg = ProviderRegistry()
    reg.register("stub", StubProvider())
    # Keep the current live provider under its session/provider_name key.
    pname = getattr(orch, "provider_name", None) or getattr(orch.provider, "name", "active")
    if pname != "stub":
        reg.register(pname, orch.provider)
    orch.providers = reg


def _register_live(orch, name: str, provider, *, catalog_id: str | None = None) -> None:
    """Register on the live ProviderRegistry with catalog aliases."""
    _ensure_registry(orch)
    aliases: list[str] = []
    if catalog_id:
        try:
            from reidx.provider_manager.catalog import by_id

            pdef = by_id(catalog_id)
            if pdef:
                aliases = [pdef.id, *pdef.aliases, pdef.name.lower()]
        except Exception:  # noqa: BLE001
            pass
    orch.providers.register(name, provider, aliases=aliases or None)


class ListProviderCatalogTool(BaseTool):
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="list_provider_catalog",
            description=(
                "List known AI providers you can connect (OpenCode Go, NVIDIA NIM, "
                "OpenAI, Anthropic, Ollama, …). Use before connect_provider."
            ),
            parameters=_LIST_CATALOG_PARAMS,
            risk=RiskLevel.LOW,
        )

    def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        from reidx.provider_manager.catalog import all_providers, popular_providers, search

        query = str(args.get("query") or "").strip()
        want_all = bool(args.get("all"))
        if query:
            items = search(query)
        elif want_all:
            items = all_providers()
        else:
            items = popular_providers()
        if not items:
            return ToolResult.ok_("no catalog matches", count=0)
        lines = []
        for p in items[:40]:
            lines.append(
                f"{p.id}\t{p.name}\t{p.kind}\t{p.base_url or '-'}\t"
                f"model={p.default_model or '-'}\t{p.description[:60]}"
            )
        extra = ""
        if len(items) > 40:
            extra = f"\n…and {len(items) - 40} more (narrow query)"
        return ToolResult.ok_(
            "id\tname\tkind\tbase_url\tdefault_model\tdescription\n"
            + "\n".join(lines)
            + extra,
            count=len(items),
        )


class ListConnectedProvidersTool(BaseTool):
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="list_connected_providers",
            description=(
                "List providers currently registered in this ReidX session "
                "(stub, env, and /connect / connect_provider saves)."
            ),
            parameters=_LIST_CONNECTED_PARAMS,
            risk=RiskLevel.LOW,
        )

    def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        orch = _orch(ctx)
        if orch is None:
            return ToolResult.fail("no orchestrator")
        _ensure_registry(orch)
        active = ""
        if orch.state is not None:
            active = orch.state.session.provider
        lines = []
        for name in orch.providers.names():
            try:
                p = orch.providers.get(name)
            except KeyError:
                continue
            kind = getattr(p, "name", type(p).__name__)
            model = getattr(p, "default_model", "") or ""
            base = getattr(p, "base_url", "") or ""
            mark = "●" if name == active else " "
            lines.append(f"{mark} {name}\tkind={kind}\tmodel={model}\tbase={base[:48]}")
        return ToolResult.ok_("\n".join(lines) if lines else "(none)", active=active)


class ConnectProviderTool(BaseTool):
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="connect_provider",
            description=(
                "Save and register an AI provider (like /connect). Prefer catalog_id "
                "from list_provider_catalog (e.g. opencode-go, nvidia-nim). "
                "Stores the API key encrypted in providers.db. User approval required."
            ),
            parameters=_CONNECT_PARAMS,
            risk=RiskLevel.HIGH,
        )

    def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        orch = _orch(ctx)
        if orch is None:
            return ToolResult.fail("no orchestrator")

        catalog_id = str(args.get("catalog_id") or "").strip()
        name = str(args.get("name") or "").strip()
        kind = str(args.get("kind") or "").strip()
        base_url = str(args.get("base_url") or "").strip()
        api_key = str(args.get("api_key") or "").strip()
        default_model = str(args.get("default_model") or "").strip()
        auth_method = str(args.get("auth_method") or "").strip()
        oauth_flow = str(args.get("oauth_flow") or "").strip()
        activate = args.get("activate")
        if activate is None:
            activate = True

        pdef = None
        if catalog_id:
            from reidx.provider_manager.catalog import by_id, search

            pdef = by_id(catalog_id)
            if pdef is None:
                hits = search(catalog_id)
                pdef = hits[0] if len(hits) == 1 else None
            if pdef is None:
                return ToolResult.fail(
                    f"unknown catalog_id '{catalog_id}' — call list_provider_catalog first"
                )
            name = name or pdef.name
            kind = kind or pdef.kind
            base_url = base_url or pdef.base_url
            default_model = default_model or pdef.default_model
            auth_method = auth_method or pdef.auth_method
            catalog_id = pdef.id
        else:
            if not auth_method:
                auth_method = "bearer"
            if not name:
                return ToolResult.fail("name or catalog_id required")
            if kind not in ("anthropic", "openai", "openai-compatible", "ollama"):
                return ToolResult.fail(
                    "kind must be anthropic|openai|openai-compatible|ollama "
                    "(or pass catalog_id)"
                )
            if kind == "openai-compatible" and not base_url:
                return ToolResult.fail("base_url required for openai-compatible")

        if name.lower() in ("stub",):
            return ToolResult.fail("cannot replace built-in stub via connect_provider")

        oauth_tokens = None
        if auth_method == "oauth" and kind in ("openai", "anthropic"):
            if oauth_flow == "browser":
                from reidx.provider.oauth import run_browser_oauth

                prompt = f"Open browser to authorize {name} ({kind})?"
                if ctx.resolve_decision(prompt) is PermissionDecision.DENY:
                    return ToolResult.fail("oauth authorization denied by user")
                oauth_tokens = run_browser_oauth(kind)
                if not oauth_tokens:
                    return ToolResult.fail("oauth authorization failed or was cancelled")
            elif oauth_flow == "device":
                from reidx.provider.oauth import run_device_oauth

                def show_code(user_code: str, uri: str) -> None:
                    ctx.emit("oauth_device_code", {"user_code": user_code, "url": uri})

                prompt = f"Use device code flow for {name} ({kind})? User code will be shown."
                if ctx.resolve_decision(prompt) is PermissionDecision.DENY:
                    return ToolResult.fail("oauth authorization denied by user")
                oauth_tokens = run_device_oauth(kind, show_code)
                if not oauth_tokens:
                    return ToolResult.fail("oauth device authorization failed or timed out")
            else:
                return ToolResult.fail("oauth_flow must be 'browser' or 'device' for OAuth providers")
            api_key = oauth_tokens.access_token

        # User must approve storing a key / adding a remote backend.
        prompt = f"Connect provider '{name}' ({kind}) and store credentials?"
        if api_key:
            prompt += f" API key ends with …{api_key[-4:]}" if len(api_key) >= 4 else ""
        if ctx.resolve_decision(prompt) is PermissionDecision.DENY:
            return ToolResult.fail("connect denied by user")

        from reidx.provider.store import ProviderRecord, build_provider
        from reidx.provider_manager import keychain
        from reidx.provider_manager.database import ProviderDatabase, StoredKey, StoredProvider
        from reidx.provider.oauth import OAuthTokens

        root = _storage_root(orch)
        db = ProviderDatabase(root)

        try:
            provider = build_provider(
                ProviderRecord(
                    name=name,
                    kind=kind,
                    base_url=base_url,
                    api_key=api_key,
                    default_model=default_model,
                    auth_method=auth_method if catalog_id else "bearer",
                    oauth_access_token=oauth_tokens.access_token if oauth_tokens else "",
                    oauth_refresh_token=oauth_tokens.refresh_token if oauth_tokens else "",
                    oauth_expires_at=oauth_tokens.expires_at if oauth_tokens else 0,
                    oauth_provider=kind if oauth_tokens else "",
                )
            )
        except (ValueError, TypeError) as exc:
            return ToolResult.fail(f"failed to build provider: {exc}")

        keys: list[StoredKey] = []
        active_key_id = None
        existing = db.get_provider(name)
        if existing and existing.keys and not api_key and not oauth_tokens:
            keys = existing.keys
            active_key_id = existing.active_key_id
        elif api_key and not oauth_tokens:
            kid = uuid.uuid4().hex[:12]
            keys = [
                StoredKey(
                    id=kid,
                    label="default",
                    encrypted_key=keychain.encrypt(api_key),
                )
            ]
            active_key_id = kid

        sp = StoredProvider(
            name=name,
            kind=kind,
            base_url=base_url,
            default_model=default_model,
            auth_method=auth_method if pdef else "bearer",
            catalog_id=catalog_id or None,
            keys=keys,
            active_key_id=active_key_id,
        )
        if oauth_tokens:
            sp.oauth_tokens = oauth_tokens
        db.save_provider(sp)
        _register_live(orch, name, provider, catalog_id=catalog_id or None)

        activated = False
        model_set = default_model
        if activate and orch.providers is not None and orch.providers.has(name):
            try:
                orch.use_provider(name)
                activated = True
                if default_model and orch.state is not None:
                    orch.state.session.model = default_model
                    if getattr(orch.provider, "default_model", None) is not None:
                        orch.provider.default_model = default_model
                    from reidx.provider.context_windows import bind_model_context

                    orch.state.session.context_window = bind_model_context(
                        default_model, orch.provider, network=False
                    )
                    orch.session_store.update(orch.state.session)
                    model_set = default_model
            except Exception as exc:  # noqa: BLE001
                return ToolResult.ok_(
                    f"saved provider '{name}' but could not activate: {exc}. "
                    f"Try use_provider(provider={name!r})",
                    name=name,
                    activated=False,
                )

        msg = f"connected '{name}' ({kind})"
        if auth_method == "oauth":
            msg += " [OAuth]"
        if activated:
            msg += f" · active · model={model_set or '?'}"
        else:
            msg += " · not activated (use use_provider)"
        return ToolResult.ok_(
            msg,
            name=name,
            kind=kind,
            activated=activated,
            model=model_set,
            catalog_id=catalog_id or "",
        )


class UseProviderTool(BaseTool):
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="use_provider",
            description=(
                "Switch the active session to a registered provider (like /use). "
                "Optionally set the model id at the same time."
            ),
            parameters=_USE_PARAMS,
            risk=RiskLevel.LOW,
        )

    def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        orch = _orch(ctx)
        if orch is None:
            return ToolResult.fail("no orchestrator")
        _ensure_registry(orch)
        name = str(args.get("provider") or "").strip()
        model = str(args.get("model") or "").strip()
        if not name:
            return ToolResult.fail("provider required")
        if not orch.providers.has(name):
            suggestions = orch.providers.suggestions(name) if hasattr(orch.providers, "suggestions") else []
            hint = f" — try: {', '.join(suggestions)}" if suggestions else ""
            return ToolResult.fail(f"provider '{name}' not registered{hint}")
        try:
            orch.use_provider(name)
        except Exception as exc:  # noqa: BLE001
            return ToolResult.fail(str(exc))
        resolved = orch.providers.resolve(name) or name
        if model and orch.state is not None:
            orch.state.session.model = model
            if getattr(orch.provider, "default_model", None) is not None:
                orch.provider.default_model = model
            from reidx.provider.context_windows import bind_model_context

            orch.state.session.context_window = bind_model_context(
                model, orch.provider, network=False
            )
            orch.session_store.update(orch.state.session)
        cur_model = ""
        if orch.state is not None:
            cur_model = orch.state.session.model or getattr(orch.provider, "default_model", "") or ""
        return ToolResult.ok_(
            f"active provider → {resolved} · model={cur_model or '?'}",
            provider=resolved,
            model=cur_model,
        )


class DisconnectProviderTool(BaseTool):
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="disconnect_provider",
            description=(
                "Remove a saved provider from providers.db and the live registry "
                "(like /disconnect). Cannot remove stub or the currently active provider."
            ),
            parameters=_DISCONNECT_PARAMS,
            risk=RiskLevel.HIGH,
        )

    def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        orch = _orch(ctx)
        if orch is None:
            return ToolResult.fail("no orchestrator")
        name = str(args.get("name") or "").strip()
        if not name:
            return ToolResult.fail("name required")
        if name.lower() in ("stub", "anthropic") and name == "stub":
            return ToolResult.fail("cannot disconnect built-in stub")
        if orch.state is not None and orch.state.session.provider == name:
            return ToolResult.fail("cannot disconnect the active provider — /use another first")
        if ctx.resolve_decision(f"Disconnect and delete saved provider '{name}'?") is PermissionDecision.DENY:
            return ToolResult.fail("disconnect denied by user")

        from reidx.provider_manager.database import ProviderDatabase

        root = _storage_root(orch)
        db = ProviderDatabase(root)
        removed_db = db.remove_provider(name)
        removed_reg = False
        if orch.providers is not None:
            removed_reg = orch.providers.unregister(name)
        if not removed_db and not removed_reg:
            return ToolResult.fail(f"provider '{name}' not found")
        return ToolResult.ok_(f"disconnected '{name}'", name=name)


class SetSessionModelTool(BaseTool):
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="set_model",
            description=(
                "Set the session model id for the active provider (like /model <id>). "
                "Updates the context-window meter for that model."
            ),
            parameters=_SET_MODEL_PARAMS,
            risk=RiskLevel.LOW,
        )

    def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        orch = _orch(ctx)
        if orch is None or orch.state is None:
            return ToolResult.fail("no active session")
        model = str(args.get("model") or "").strip()
        if not model:
            return ToolResult.fail("model required")
        orch.state.session.model = model
        if getattr(orch.provider, "default_model", None) is not None:
            orch.provider.default_model = model
        from reidx.provider.context_windows import bind_model_context, fmt_context_window

        window = bind_model_context(model, orch.provider, network=False)
        orch.state.session.context_window = window
        orch.session_store.update(orch.state.session)
        try:
            from reidx.config.settings import persist_default_model

            persist_default_model(model, provider_name=orch.state.session.provider)
        except Exception:  # noqa: BLE001
            pass
        return ToolResult.ok_(
            f"model → {model} · context {fmt_context_window(window)}",
            model=model,
            context_window=window,
        )


def register_provider_tools(registry) -> None:  # type: ignore[no-untyped-def]
    for tool in (
        ListProviderCatalogTool(),
        ListConnectedProvidersTool(),
        ConnectProviderTool(),
        UseProviderTool(),
        DisconnectProviderTool(),
        SetSessionModelTool(),
    ):
        registry.register(tool)


__all__ = [
    "ConnectProviderTool",
    "DisconnectProviderTool",
    "ListConnectedProvidersTool",
    "ListProviderCatalogTool",
    "SetSessionModelTool",
    "UseProviderTool",
    "register_provider_tools",
]
