from __future__ import annotations

import shutil
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from prompt_toolkit.buffer import Buffer

from reidx.diagnostics.logger import get_logger
from reidx.provider.models import (
    denormalize_model_id,
    normalize_model_id,
)
from reidx.provider.store import ProviderRecord, build_provider, validate_provider
from reidx.provider_manager.catalog import (
    ProviderDefinition,
    all_providers,
)
from reidx.provider_manager.catalog import (
    search as catalog_search,
)
from reidx.provider_manager.database import ProviderDatabase, StoredKey, StoredProvider

log = get_logger("reidx.provider_manager.palette")

SEL_BG = "#2d1820"
SEL_FG = "#ffaaaa"
SEL_ICON = "#ff6b6b"
DIM = "#6b5b5b"
ACCENT = "#ff6b6b"
OK = "#7ddb7d"
WARN = "#ffdb6b"
ERR = "#ff6b6b"
BORDER = "#3d2a2a"
BORDER_SEL = "#ff6b6b"
BG = "#131313"
BG_ALT = "#1c1818"
BG_ROW = "#181616"
SCROLL_IND = "#2a2a2a"
HEADER_BG = "#1a1010"
ITEM_FG = "#d0d0d0"

# --- animation ----------------------------------------------------------
# Transitions are event-triggered and self-terminating: each sets a start
# timestamp, renders an eased interpolation over its window, then stops
# requesting redraws. Nothing animates while the palette sits idle.
_ANIM_OPEN = 0.22   # seconds — rows unfold top-to-bottom when the box opens
_ANIM_SEL = 0.16    # seconds — selected row "settles" from a brighter tint
SEL_BG_POP = "#4a2530"  # brighter selected-row bg that eases down to SEL_BG


def _ease_out_cubic(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return 1 - (1 - t) ** 3


def _lerp_hex(a: str, b: str, t: float) -> str:
    """Interpolate between two "#rrggbb" colors; t in [0, 1]."""
    t = max(0.0, min(1.0, t))
    ar, ag, ab = int(a[1:3], 16), int(a[3:5], 16), int(a[5:7], 16)
    br, bg, bb = int(b[1:3], 16), int(b[3:5], 16), int(b[5:7], 16)
    return (
        f"#{round(ar + (br - ar) * t):02x}"
        f"{round(ag + (bg - ag) * t):02x}"
        f"{round(ab + (bb - ab) * t):02x}"
    )


@dataclass
class PaletteItem:
    label: str
    description: str = ""
    icon: str = " "
    kind: str = "action"
    data: Any = None


@dataclass
class WizardStep:
    field: str
    prompt: str
    is_text: bool = True
    optional: bool = False
    is_password: bool = False


WIZARD_STEPS: list[WizardStep] = [
    WizardStep("name", "Provider Name"),
    WizardStep("base_url", "API Base URL"),
    WizardStep("kind", "Provider Kind", is_text=False),
    WizardStep("default_model", "Default Model", optional=True),
    WizardStep("auth_method", "Authentication Method", is_text=False),
    WizardStep("oauth_flow", "OAuth Flow", is_text=False, optional=True),
    WizardStep("api_key", "API Key", optional=True, is_password=True),
]

KIND_OPTIONS = [
    PaletteItem("anthropic", "Anthropic Messages API", icon="A"),
    PaletteItem("openai", "OpenAI Chat Completions API", icon="O"),
    PaletteItem("openai-compatible", "OpenAI-compatible (vLLM, LM Studio, etc.)", icon="C"),
    PaletteItem("ollama", "Ollama native API", icon="L"),
]

AUTH_OPTIONS = [
    PaletteItem("bearer", "Authorization: Bearer <key>", icon="B"),
    PaletteItem("x-api-key", "x-api-key: <key> header", icon="X"),
    PaletteItem("oauth", "OAuth browser/device authorization", icon="O"),
    PaletteItem("none", "No authentication required", icon="N"),
]

OAUTH_FLOW_OPTIONS = [
    PaletteItem("browser", "Open browser for authorization (recommended)", icon="B"),
    PaletteItem("device", "Device code flow for headless/remote", icon="D"),
]


class ProviderPalette:
    LIST = "list"
    KEYS = "keys"
    MANAGE = "manage"
    KEY_LABEL = "key_label"
    KEY_INPUT = "key_input"
    RENAME_SEL = "rename_sel"
    RENAME_INPUT = "rename_input"
    DELETE_SEL = "delete_sel"
    CONFIRM = "confirm"
    WIZARD = "wizard"
    MESSAGE = "message"
    MODELS = "models"

    def __init__(
        self,
        db: ProviderDatabase,
        orchestrator: Any,
        on_close: Callable[[str], None],
        on_invalidate: Callable[[], None] | None = None,
    ) -> None:
        self.db = db
        self.orchestrator = orchestrator
        self.on_close = on_close
        self._on_invalidate = on_invalidate or (lambda: None)
        self._active = False
        self.screen = self.LIST
        self.selected_index = 0
        self._scroll_offset = 0
        self.current_provider: StoredProvider | None = None
        self.current_def: ProviderDefinition | None = None
        self._message_text = ""
        self._message_next = self.LIST
        self._pending_label = ""
        self._pending_action: Callable[[], str] | None = None
        self._confirm_text = ""
        self.wizard_step_idx = 0
        self.wizard_data: dict[str, str] = {}
        self.input_is_password = False
        self.input_prompt_text = ""

        # Animation clocks (monotonic seconds; 0.0 == long finished).
        self._anim_open_at = 0.0
        self._anim_sel_at = 0.0

        self.search_buf = Buffer(multiline=False)
        self.input_buf = Buffer(multiline=False)
        self.search_buf.on_text_changed += self._on_search_changed

    @property
    def active(self) -> bool:
        return self._active

    def term_cols(self) -> int:
        try:
            cols, _ = shutil.get_terminal_size(fallback=(80, 24))
            return max(40, cols)
        except Exception:
            return 80

    def term_rows(self) -> int:
        try:
            _, rows = shutil.get_terminal_size(fallback=(80, 24))
            return max(10, rows)
        except Exception:
            return 24

    def inner_width(self) -> int:
        return self.term_cols() - 2

    def max_content_lines(self) -> int:
        return max(3, self.term_rows() - 8)

    def is_search_screen(self) -> bool:
        return self._active and self.screen == self.LIST

    def is_input_screen(self) -> bool:
        return self._active and self.screen in (
            self.KEY_LABEL, self.KEY_INPUT, self.RENAME_INPUT, self.WIZARD,
        )

    def is_list_screen(self) -> bool:
        return self._active and self.screen in (
            self.LIST, self.KEYS, self.MANAGE, self.RENAME_SEL,
            self.DELETE_SEL, self.CONFIRM, self.WIZARD, self.MODELS,
        ) and not self.is_input_screen()

    def activate(self) -> None:
        self._active = True
        self.screen = self.LIST
        self.selected_index = 0
        self._scroll_offset = 0
        self.search_buf.text = ""
        self.current_provider = None
        self.current_def = None
        self._anim_open_at = time.monotonic()
        self._anim_sel_at = 0.0
        self._invalidate()

    def is_animating(self) -> bool:
        """True while a transition is mid-flight — drives the redraw ticker.

        Returns False once every window has elapsed so the UI stops
        repainting when nothing is moving.
        """
        if not self._active:
            return False
        now = time.monotonic()
        return (
            now - self._anim_open_at < _ANIM_OPEN
            or now - self._anim_sel_at < _ANIM_SEL
        )

    def _mark_selection_moved(self) -> None:
        self._anim_sel_at = time.monotonic()

    def deactivate(self) -> None:
        self._active = False
        self.screen = self.LIST
        self.search_buf.text = ""
        self.input_buf.text = ""
        self._scroll_offset = 0
        self._invalidate()

    def _invalidate(self) -> None:
        self._on_invalidate()

    def _close(self, message: str = "") -> None:
        self._active = False
        self.on_close(message)

    def _go_back(self) -> None:
        if self.screen == self.LIST:
            self._close()
        elif self.screen in (self.KEYS, self.WIZARD):
            self.screen = self.LIST
            self.selected_index = 0
            self._scroll_offset = 0
        elif self.screen == self.MANAGE:
            self.screen = self.LIST
            self.selected_index = 0
            self._scroll_offset = 0
        elif self.screen in (self.KEY_LABEL, self.KEY_INPUT):
            self.screen = self.KEYS if self.current_provider and not self.current_provider.keys else self.MANAGE
            self.input_buf.text = ""
            self.input_is_password = False
        elif self.screen == self.RENAME_INPUT:
            self.screen = self.RENAME_SEL
            self.input_buf.text = ""
        elif self.screen in (self.RENAME_SEL, self.DELETE_SEL):
            self.screen = self.MANAGE
            self.selected_index = 0
            self._scroll_offset = 0
        elif self.screen == self.CONFIRM:
            self.screen = self.MANAGE
            self.selected_index = 0
            self._scroll_offset = 0
        elif self.screen == self.MESSAGE:
            self.screen = self._message_next
            self.selected_index = 0
            self._scroll_offset = 0
        elif self.screen == self.MODELS:
            self.screen = self.MANAGE
            self.selected_index = 0
            self._scroll_offset = 0
        elif self.screen == self.WIZARD:
            if self.wizard_step_idx > 0:
                self.wizard_step_idx -= 1
                step = WIZARD_STEPS[self.wizard_step_idx]
                if step.is_text:
                    self.input_buf.text = self.wizard_data.get(step.field, "")
                    self.input_is_password = step.is_password
            else:
                self.screen = self.LIST
                self.selected_index = 0
                self._scroll_offset = 0
                self.wizard_data = {}
                self.wizard_step_idx = 0
        self._invalidate()

    def _on_search_changed(self, _buf: Buffer | None = None) -> None:
        self.selected_index = 0
        self._scroll_offset = 0
        self._invalidate()

    def _adjust_scroll(self, item_count: int) -> None:
        max_lines = self.max_content_lines()
        if item_count <= max_lines:
            self._scroll_offset = 0
            return
        if self.selected_index < self._scroll_offset:
            self._scroll_offset = self.selected_index
        elif self.selected_index >= self._scroll_offset + max_lines:
            self._scroll_offset = self.selected_index - max_lines + 1
        self._scroll_offset = max(0, min(self._scroll_offset, item_count - max_lines))

    def on_up(self) -> None:
        items = self._build_items()
        if items:
            self.selected_index = (self.selected_index - 1) % len(items)
            self._adjust_scroll(len(items))
            self._mark_selection_moved()
            self._invalidate()

    def on_down(self) -> None:
        items = self._build_items()
        if items:
            self.selected_index = (self.selected_index + 1) % len(items)
            self._adjust_scroll(len(items))
            self._mark_selection_moved()
            self._invalidate()

    def on_enter(self) -> None:
        if self.is_input_screen():
            self._handle_input_enter()
            self._invalidate()
            return
        if self.screen == self.MESSAGE:
            self.screen = self._message_next
            self.selected_index = 0
            self._scroll_offset = 0
            self._invalidate()
            return
        items = self._build_items()
        if not items:
            return
        idx = min(self.selected_index, len(items) - 1)
        self._handle_list_enter(items[idx])
        self._invalidate()

    def on_escape(self) -> None:
        self._go_back()

    def _build_items(self) -> list[PaletteItem]:
        if self.screen == self.LIST:
            return self._list_items()
        if self.screen == self.KEYS:
            return self._keys_items()
        if self.screen == self.MANAGE:
            return self._manage_items()
        if self.screen == self.RENAME_SEL:
            return self._key_select_items("rename")
        if self.screen == self.DELETE_SEL:
            return self._key_select_items("delete")
        if self.screen == self.CONFIRM:
            return [
                PaletteItem("Confirm", icon="Y", kind="action"),
                PaletteItem("Cancel", icon="N", kind="action"),
            ]
        if self.screen == self.WIZARD:
            step = WIZARD_STEPS[self.wizard_step_idx]
            if not step.is_text:
                if step.field == "kind":
                    return KIND_OPTIONS
                if step.field == "auth_method":
                    return AUTH_OPTIONS
                if step.field == "oauth_flow":
                    return OAUTH_FLOW_OPTIONS
            return []
        if self.screen == self.MODELS:
            return self._models_items()
        return []

    def _list_items(self) -> list[PaletteItem]:
        q = self.search_buf.text.strip().lower()
        stored = {p.name.lower(): p for p in self.db.list_providers()}
        items: list[PaletteItem] = []

        if q:
            defs = catalog_search(q)
        else:
            defs = all_providers()

        for d in defs:
            if d.name.lower() in stored:
                sp = stored[d.name.lower()]
                active_key = sp.active_key()
                icon = "?" if active_key else "	"
                items.append(PaletteItem(
                    label=d.name, description=d.description, icon=icon,
                    kind="stored", data=sp,
                ))
            else:
                items.append(PaletteItem(
                    label=d.name, description=d.description, icon="?",
                    kind="catalog", data=d,
                ))

        for sp in self.db.list_providers():
            if sp.name.lower() not in {d.name.lower() for d in defs}:
                if not q or q in sp.name.lower() or q in sp.kind.lower() or q in sp.base_url.lower():
                    active_key = sp.active_key()
                    icon = "?" if active_key else "	"
                    items.append(PaletteItem(
                        label=sp.name, description=f"{sp.kind}  {sp.base_url}", icon=icon,
                        kind="stored", data=sp,
                    ))

        items.append(PaletteItem(
            label="Add Custom Provider", description="Manually configure a provider",
            icon="+", kind="action",
        ))
        return items

    def _keys_items(self) -> list[PaletteItem]:
        items: list[PaletteItem] = []
        if self.current_provider:
            for k in self.current_provider.keys:
                is_active = k.id == self.current_provider.active_key_id
                icon = "?" if is_active else "	"
                desc = "active" if is_active else ""
                items.append(PaletteItem(label=k.label, description=desc, icon=icon, kind="action", data=k))
        items.append(PaletteItem(label="Add API Key", icon="+", kind="action"))
        return items

    def _manage_items(self) -> list[PaletteItem]:
        items: list[PaletteItem] = []
        sp = self.current_provider
        if sp and len(sp.keys) > 1:
            items.append(PaletteItem("Switch Key", "Change the active API key", icon=""))
        items.append(PaletteItem("Add Key", "Add another API key", icon="+"))
        if sp and sp.keys:
            items.append(PaletteItem("Rename Key", "Rename a key label", icon="?"))
            items.append(PaletteItem("Delete Key", "Remove a key", icon="?"))
        items.append(PaletteItem("View Models", "Browse and set default model", icon="M"))
        items.append(PaletteItem("Edit Provider", "Change base URL, model, etc.", icon="?"))
        items.append(PaletteItem("Remove Provider", "Delete provider and all keys", icon="?", kind="action"))
        return items

    def _key_select_items(self, _mode: str) -> list[PaletteItem]:
        items: list[PaletteItem] = []
        if self.current_provider:
            for k in self.current_provider.keys:
                is_active = k.id == self.current_provider.active_key_id
                icon = "?" if is_active else "	"
                items.append(PaletteItem(label=k.label, icon=icon, kind="action", data=k))
        return items

    def _handle_list_enter(self, item: PaletteItem) -> None:
        if self.screen == self.LIST:
            self._on_list_select(item)
        elif self.screen == self.KEYS:
            self._on_keys_select(item)
        elif self.screen == self.MANAGE:
            self._on_manage_select(item)
        elif self.screen == self.RENAME_SEL:
            self._on_rename_select(item)
        elif self.screen == self.DELETE_SEL:
            self._on_delete_select(item)
        elif self.screen == self.CONFIRM:
            self._on_confirm_select(item)
        elif self.screen == self.WIZARD:
            self._on_wizard_select(item)
        elif self.screen == self.MODELS:
            self._on_models_select(item)

    def _on_list_select(self, item: PaletteItem) -> None:
        if item.kind == "action":
            self._start_wizard()
            return
        if item.kind == "catalog":
            d = item.data
            self.current_def = d
            existing = self.db.get_provider(d.name)
            if existing:
                self.current_provider = existing
                self._register_current()
                self.screen = self.MANAGE
            else:
                sp = StoredProvider(
                    name=d.name, kind=d.kind, base_url=d.base_url,
                    default_model=d.default_model, auth_method=d.auth_method,
                    extra_headers=dict(d.extra_headers), catalog_id=d.id,
                )
                self.current_provider = sp
                self.screen = self.KEYS
            self.selected_index = 0
            self._scroll_offset = 0
            self._invalidate()
            return
        if item.kind == "stored":
            self.current_provider = item.data
            self.current_def = None
            self.screen = self.MANAGE
            self.selected_index = 0
            self._scroll_offset = 0
            self._register_current()
            self._invalidate()

    def _on_keys_select(self, item: PaletteItem) -> None:
        if item.label == "Add API Key":
            self.screen = self.KEY_LABEL
            self.input_buf.text = ""
            self.input_is_password = False
            self.input_prompt_text = "Label for this key (e.g. Personal, Work)"
            self._invalidate()
            return
        for k in self.current_provider.keys:
            if k.label == item.label:
                self.db.set_active_key(self.current_provider.name, k.id)
                self.current_provider = self.db.get_provider(self.current_provider.name)
                self._register_current()
                self._show_message(f"Switched to key '{k.label}'", self.KEYS)

    def _on_manage_select(self, item: PaletteItem) -> None:
        label = item.label
        if label == "Switch Key":
            self.screen = self.KEYS
            self.selected_index = 0
            self._scroll_offset = 0
            self._invalidate()
        elif label == "Add Key":
            self.screen = self.KEY_LABEL
            self.input_buf.text = ""
            self.input_is_password = False
            self.input_prompt_text = "Label for this key (e.g. Personal, Work)"
            self._invalidate()
        elif label == "Rename Key":
            self.screen = self.RENAME_SEL
            self.selected_index = 0
            self._scroll_offset = 0
            self._invalidate()
        elif label == "Delete Key":
            self.screen = self.DELETE_SEL
            self.selected_index = 0
            self._scroll_offset = 0
            self._invalidate()
        elif label == "View Models":
            self.screen = self.MODELS
            self.selected_index = 0
            self._scroll_offset = 0
            self._invalidate()
        elif label == "Edit Provider":
            self._start_wizard(edit=True)
        elif label == "Remove Provider":
            name = self.current_provider.name if self.current_provider else ""
            self._confirm_text = f"Remove provider '{name}' and all encrypted keys?"
            self._pending_action = lambda: self._do_remove_provider()
            self.screen = self.CONFIRM
            self.selected_index = 0
            self._scroll_offset = 0
            self._invalidate()

    def _on_rename_select(self, item: PaletteItem) -> None:
        k = item.data
        if k:
            self._pending_label = k.id
            self.input_buf.text = k.label
            self.input_prompt_text = "New label"
            self.screen = self.RENAME_INPUT
            self._invalidate()

    def _on_delete_select(self, item: PaletteItem) -> None:
        k = item.data
        if k and self.current_provider:
            name = self.current_provider.name
            self._confirm_text = f"Delete key '{k.label}' from provider '{name}'?"
            self._pending_action = lambda: self._do_delete_key(k.id, k.label)
            self.screen = self.CONFIRM
            self.selected_index = 0
            self._scroll_offset = 0
            self._invalidate()

    def _models_items(self) -> list[PaletteItem]:
        items: list[PaletteItem] = []
        if not self.current_provider:
            return items
        
        sp = self.current_provider
        try:
            provider = build_provider(ProviderRecord(
                name=sp.name, kind=sp.kind, base_url=sp.base_url,
                api_key=sp.decrypted_api_key(), default_model=sp.default_model,
                auth_method=sp.auth_method,
            ))
            models = provider.fetch_models()
        except Exception as exc:
            log.warning("failed to fetch models for %s: %s", sp.name, exc)
            items.append(PaletteItem("Error loading models", str(exc), icon="!", kind="action"))
            return items
        
        if not models:
            items.append(PaletteItem("No models available", "Provider returned empty model list", icon="!", kind="action"))
            return items
        
        current_model = sp.default_model or ""
        for model in models:
            is_current = model == current_model
            icon = "?" if is_current else "	"
            desc = "current" if is_current else ""
            items.append(PaletteItem(
                label=model, description=desc, icon=icon, kind="model", data=model
            ))
        
        return items

    def _on_models_select(self, item: PaletteItem) -> None:
        if item.kind == "model" and item.data:
            self.current_provider.default_model = item.data
            self.db.save_provider(self.current_provider)
            self._register_current()
            self._show_message(f"Set default model to '{item.data}'", self.MANAGE)

    def _on_confirm_select(self, item: PaletteItem) -> None:
        if item.label == "Confirm":
            if self._pending_action:
                msg = self._pending_action()
                self._pending_action = None
                self._show_message(msg, self.MANAGE)
            else:
                self.screen = self.MANAGE
                self._invalidate()
        else:
            self._pending_action = None
            self.screen = self.MANAGE
            self.selected_index = 0
            self._scroll_offset = 0
            self._invalidate()

    def _on_wizard_select(self, item: PaletteItem) -> None:
        step = WIZARD_STEPS[self.wizard_step_idx]
        self.wizard_data[step.field] = item.label
        self._wizard_advance()

    def _handle_input_enter(self) -> None:
        text = self.input_buf.text
        if self.screen == self.KEY_LABEL:
            if not text.strip():
                return
            self._pending_label = text.strip()
            self.screen = self.KEY_INPUT
            self.input_buf.text = ""
            self.input_is_password = True
            self.input_prompt_text = "Paste your API key"
            self._invalidate()
            return
        if self.screen == self.KEY_INPUT:
            self._do_add_key(self._pending_label, text)
            self.input_buf.text = ""
            self.input_is_password = False
            self._invalidate()
            return
        if self.screen == self.RENAME_INPUT:
            if not text.strip():
                return
            if self.current_provider:
                self.db.rename_key(self.current_provider.name, self._pending_label, text.strip())
                self.current_provider = self.db.get_provider(self.current_provider.name)
                self._show_message(f"Renamed key to '{text.strip()}'", self.MANAGE)
            self.input_buf.text = ""
            self._invalidate()
            return
        if self.screen == self.WIZARD:
            step = WIZARD_STEPS[self.wizard_step_idx]
            if not text.strip() and not step.optional:
                return
            self.wizard_data[step.field] = text.strip()
            self.input_buf.text = ""
            self._wizard_advance()
            self._invalidate()

    def _start_wizard(self, edit: bool = False) -> None:
        self.wizard_step_idx = 0
        self.wizard_data = {}
        if edit and self.current_provider:
            sp = self.current_provider
            self.wizard_data = {
                "name": sp.name,
                "base_url": sp.base_url,
                "kind": sp.kind,
                "default_model": sp.default_model,
                "auth_method": sp.auth_method,
            }
            self.wizard_step_idx = 5
        self.screen = self.WIZARD
        step = WIZARD_STEPS[self.wizard_step_idx]
        if step.is_text:
            self.input_buf.text = self.wizard_data.get(step.field, "")
            self.input_is_password = step.is_password
            self.input_prompt_text = step.prompt
        self.selected_index = 0
        self._scroll_offset = 0
        self._invalidate()

    def _wizard_advance(self) -> None:
        self.wizard_step_idx += 1
        if self.wizard_step_idx >= len(WIZARD_STEPS):
            self._wizard_complete()
            return
        step = WIZARD_STEPS[self.wizard_step_idx]
        if step.is_text:
            self.input_buf.text = self.wizard_data.get(step.field, "")
            self.input_is_password = step.is_password
            self.input_prompt_text = step.prompt
        else:
            self.input_buf.text = ""
        self.selected_index = 0
        self._scroll_offset = 0
        self._invalidate()

    def _wizard_complete(self) -> None:
        d = self.wizard_data
        name = d.get("name", "").strip()
        if not name:
            self._show_message("Provider name is required", self.LIST)
            return
        kind = d.get("kind", "openai-compatible")
        base_url = d.get("base_url", "").strip()
        model = d.get("default_model", "").strip()
        auth = d.get("auth_method", "bearer")
        oauth_flow = d.get("oauth_flow", "").strip()
        api_key = d.get("api_key", "").strip()

        oauth_tokens = None
        if auth == "oauth" and kind in ("openai", "anthropic"):
            if oauth_flow == "browser":
                from reidx.provider.oauth import run_browser_oauth

                self._show_message(f"Opening browser for {name} OAuth...", self.LIST)
                oauth_tokens = run_browser_oauth(kind)
                if not oauth_tokens:
                    self._show_message("OAuth authorization failed or cancelled", self.LIST)
                    return
                api_key = oauth_tokens.access_token
            elif oauth_flow == "device":
                from reidx.provider.oauth import run_device_oauth

                def show_code(user_code: str, uri: str) -> None:
                    self._show_message(f"Device code: {user_code}\nVisit: {uri}", self.LIST)
                
                oauth_tokens = run_device_oauth(kind, show_code)
                if not oauth_tokens:
                    self._show_message("OAuth device authorization failed or timed out", self.LIST)
                    return
                api_key = oauth_tokens.access_token
            else:
                self._show_message("Select OAuth flow: browser or device", self.LIST)
                return

        if api_key:
            record = ProviderRecord(
                name=name, kind=kind, base_url=base_url,
                api_key=api_key, default_model=model,
                auth_method=auth,
                oauth_access_token=oauth_tokens.access_token if oauth_tokens else "",
                oauth_refresh_token=oauth_tokens.refresh_token if oauth_tokens else "",
                oauth_expires_at=int(oauth_tokens.expires_at) if oauth_tokens else 0,
                oauth_provider=kind if oauth_tokens else "",
            )
            ok, msg = validate_provider(record)
            if not ok:
                self._confirm_text = f"Key validation failed: {msg}\nSave anyway?"
                self._pending_action = lambda: self._commit_wizard_provider(
                    name, kind, base_url, model, auth, api_key, forced_msg=msg,
                    oauth_tokens=oauth_tokens,
                )
                self.screen = self.CONFIRM
                self.selected_index = 0
                self._scroll_offset = 0
                self._invalidate()
                return
            if not model:
                try:
                    provider = build_provider(record)
                    models = provider.fetch_models()
                except Exception:
                    models = []
                if models:
                    normalized = normalize_model_id(models[0], provider_name=name)
                    if normalized.is_valid:
                        model = denormalize_model_id(normalized)

        self._commit_wizard_provider(name, kind, base_url, model, auth, api_key, oauth_tokens=oauth_tokens)

    def _commit_wizard_provider(
        self,
        name: str,
        kind: str,
        base_url: str,
        model: str,
        auth: str,
        api_key: str,
        forced_msg: str = "",
        oauth_tokens=None,
    ) -> str:
        existing = self.db.get_provider(name)
        if existing:
            existing.kind = kind
            existing.base_url = base_url
            existing.default_model = model
            existing.auth_method = auth
            if api_key:
                from reidx.provider_manager import keychain
                existing.keys.append(StoredKey(
                    id=uuid.uuid4().hex[:12],
                    label="Default",
                    encrypted_key=keychain.encrypt(api_key),
                ))
                if existing.active_key_id is None:
                    existing.active_key_id = existing.keys[-1].id
            if oauth_tokens:
                existing.oauth_tokens = oauth_tokens
            self.db.save_provider(existing)
            self.current_provider = existing
        else:
            sp = StoredProvider(
                name=name, kind=kind, base_url=base_url,
                default_model=model, auth_method=auth,
            )
            if api_key:
                from reidx.provider_manager import keychain
                k = StoredKey(
                    id=uuid.uuid4().hex[:12],
                    label="Default",
                    encrypted_key=keychain.encrypt(api_key),
                )
                sp.keys.append(k)
                sp.active_key_id = k.id
            if oauth_tokens:
                sp.oauth_tokens = oauth_tokens
            self.db.save_provider(sp)
            self.current_provider = sp

        self._register_current()
        self._try_use_provider(name)
        self.wizard_data = {}
        self.wizard_step_idx = 0
        self.input_is_password = False
        note = " (unverified)" if forced_msg else ""
        oauth_note = " [OAuth]" if auth == "oauth" else ""
        self._close(f"Saved provider '{name}' ({kind}){oauth_note}{note}. Active — try a prompt or /use {name}")
        return f"Saved provider '{name}'"

    def _do_add_key(self, label: str, api_key: str) -> None:
        if not self.current_provider:
            return
        name = self.current_provider.name
        sp = self.current_provider
        record = ProviderRecord(
            name=name, kind=sp.kind, base_url=sp.base_url,
            api_key=api_key, default_model=sp.default_model,
            auth_method=sp.auth_method,
        )
        ok, msg = validate_provider(record)
        if ok:
            self._commit_key(label, api_key, msg)
            return
        self._confirm_text = f"Key validation failed: {msg}\nSave anyway?"
        self._pending_action = lambda: self._commit_key(label, api_key, "saved (unverified)")
        self.screen = self.CONFIRM
        self.selected_index = 0
        self._scroll_offset = 0
        self._invalidate()

    def _commit_key(self, label: str, api_key: str, msg: str) -> str:
        if not self.current_provider:
            return "Error: no provider"
        sp = self.current_provider
        name = sp.name
        # Catalog first-connect path builds a StoredProvider in memory and
        # only saves it when a key is added. Persist the shell row first —
        # otherwise db.add_key() finds nothing and silently drops the key.
        if self.db.get_provider(name) is None:
            self.db.save_provider(sp)
        key = self.db.add_key(name, label, api_key)
        if key is None:
            return f"Error: failed to save key for '{name}'"
        self.current_provider = self.db.get_provider(name)
        self._register_current()
        # Switch the session onto the provider that was just wired up so
        # the next chat turn actually uses it (otherwise stub stays active).
        self._try_use_provider(name)
        self._show_message(
            f"Added key '{label}' ({msg}). Active provider: {name} — try a prompt, or /use {name}",
            self.MANAGE,
        )
        return f"Added key '{label}'"

    def _try_use_provider(self, name: str) -> None:
        """Best-effort session switch after a successful connect."""
        orch = self.orchestrator
        if orch is None or orch.providers is None or not orch.providers.has(name):
            return
        try:
            orch.use_provider(name)
        except Exception:  # noqa: BLE001 - palette must not crash on swap
            log.debug("could not auto-switch session to provider %s", name, exc_info=True)

    def _do_delete_key(self, key_id: str, label: str) -> str:
        if not self.current_provider:
            return "Error: no provider"
        name = self.current_provider.name
        self.db.remove_key(name, key_id)
        self.current_provider = self.db.get_provider(name)
        self._register_current()
        return f"Deleted key '{label}'"

    def _do_remove_provider(self) -> str:
        if not self.current_provider:
            return "Error: no provider"
        name = self.current_provider.name
        self.db.remove_provider(name)
        if self.orchestrator and self.orchestrator.providers:
            self.orchestrator.providers.unregister(name)
        self.current_provider = None
        return f"Removed provider '{name}'"

    def _register_current(self) -> None:
        if not self.current_provider or not self.orchestrator or not self.orchestrator.providers:
            return
        sp = self.current_provider
        api_key = sp.decrypted_api_key()
        try:
            record = ProviderRecord(
                name=sp.name, kind=sp.kind, base_url=sp.base_url,
                api_key=api_key, default_model=sp.default_model,
                auth_method=sp.auth_method,
            )
            provider = build_provider(record)
            if not sp.default_model:
                try:
                    models = provider.fetch_models()
                except Exception:
                    models = []
                if models:
                    normalized = normalize_model_id(models[0], provider_name=sp.name)
                    if normalized.is_valid:
                        provider.default_model = denormalize_model_id(normalized)
                        sp.default_model = denormalize_model_id(normalized)
                        self.db.save_provider(sp)
            aliases: list[str] = []
            if sp.catalog_id:
                try:
                    from reidx.provider_manager.catalog import by_id

                    pdef = by_id(sp.catalog_id)
                    if pdef:
                        aliases = [pdef.id, *pdef.aliases, pdef.name.lower()]
                except Exception:  # noqa: BLE001
                    pass
            self.orchestrator.providers.register(sp.name, provider, aliases=aliases)
        except (ValueError, TypeError):
            log.exception("failed to register provider %s", sp.name)

    def _show_message(self, text: str, next_screen: str) -> None:
        self._message_text = text
        self._message_next = next_screen
        self.screen = self.MESSAGE
        self._invalidate()

    def border_top_fragments(self) -> list[tuple[str, str]]:
        inner = self.inner_width()
        return [("class:palette-border", f"╭{'─' * inner}╮")]

    def border_bottom_fragments(self) -> list[tuple[str, str]]:
        inner = self.inner_width()
        return [("class:palette-border", f"╰{'─' * inner}╯")]

    def separator_fragments(self) -> list[tuple[str, str]]:
        inner = self.inner_width()
        return [("class:palette-sep", f"├{'─' * inner}┤")]

    def header_fragments(self) -> list[tuple[str, str]]:
        if self.screen == self.LIST:
            return [(f"bold {ACCENT}", "  ✻ Connect Provider")]
        if self.screen == self.KEYS:
            name = self.current_provider.name if self.current_provider else ""
            return [(f"bold {ACCENT}", f"  {name}"), ("", "  "), (DIM, "Keys")]
        if self.screen == self.MANAGE:
            name = self.current_provider.name if self.current_provider else ""
            sp = self.current_provider
            active = ""
            if sp and sp.active_key():
                active = f"  Active: {sp.active_key().label}"
            return [(f"bold {ACCENT}", f"  {name}"), (DIM, active)]
        if self.screen == self.KEY_LABEL:
            return [(f"bold {ACCENT}", "  Add API Key"), ("", "  "), (DIM, "Step 1/2: Label")]
        if self.screen == self.KEY_INPUT:
            return [(f"bold {ACCENT}", "  Add API Key"), ("", "  "), (DIM, f"Step 2/2: Key for '{self._pending_label}'")]
        if self.screen == self.RENAME_SEL:
            return [(f"bold {ACCENT}", "  Rename Key"), ("", "  "), (DIM, "Select a key")]
        if self.screen == self.RENAME_INPUT:
            return [(f"bold {ACCENT}", "  Rename Key"), ("", "  "), (DIM, "Enter new label")]
        if self.screen == self.DELETE_SEL:
            return [(f"bold {ACCENT}", "  Delete Key"), ("", "  "), (DIM, "Select a key")]
        if self.screen == self.CONFIRM:
            return [(f"bold {WARN}", "  Confirm")]
        if self.screen == self.WIZARD:
            step = WIZARD_STEPS[self.wizard_step_idx]
            total = len(WIZARD_STEPS)
            return [(f"bold {ACCENT}", "  + Custom Provider"), ("", "  "), (DIM, f"Step {self.wizard_step_idx + 1}/{total}: {step.prompt}")]
        if self.screen == self.MESSAGE:
            return [(f"bold {OK}", "  ✓ Done")]
        if self.screen == self.MODELS:
            name = self.current_provider.name if self.current_provider else ""
            return [(f"bold {ACCENT}", f"  {name}"), ("", "  "), (DIM, "Models")]
        return [(f"bold {ACCENT}", "  Provider")]

    def content_fragments(self) -> list[tuple[str, str]]:
        if self.screen == self.MESSAGE:
            return [(f"{OK}", f"  {self._message_text}")]
        if self.screen == self.CONFIRM:
            return [(f"{WARN}", f"  {self._confirm_text}")]
        if self.screen == self.WIZARD and self.is_input_screen():
            step = WIZARD_STEPS[self.wizard_step_idx]
            hint = " (optional - press Enter to skip)" if step.optional else ""
            return [(DIM, f"  {step.prompt}{hint}")]
        if self.is_input_screen():
            return [(DIM, f"  {self.input_prompt_text}")]
        items = self._build_items()
        if not items:
            return [(DIM, "  (no items)")]
        inner = self.inner_width()
        max_lines = self.max_content_lines()
        self._adjust_scroll(len(items))
        visible_start = self._scroll_offset
        visible_end = min(visible_start + max_lines, len(items))
        visible_count = visible_end - visible_start

        now = time.monotonic()
        # Open unfold: reveal rows top-to-bottom over _ANIM_OPEN.
        revealed = visible_count
        if self._anim_open_at and now - self._anim_open_at < _ANIM_OPEN:
            reveal = _ease_out_cubic((now - self._anim_open_at) / _ANIM_OPEN)
            revealed = max(1, min(visible_count, round(reveal * visible_count)))
        # Selection settle: selected row bg eases down from a brighter tint.
        sel_bg = SEL_BG
        if self._anim_sel_at and now - self._anim_sel_at < _ANIM_SEL:
            ts = _ease_out_cubic((now - self._anim_sel_at) / _ANIM_SEL)
            sel_bg = _lerp_hex(SEL_BG_POP, SEL_BG, ts)

        frags: list[tuple[str, str]] = []
        for i in range(visible_start, visible_end):
            # Rows past the unfold frontier render blank until they reveal.
            if i - visible_start >= revealed:
                frags.append((f"bg:{BG}", " " * inner))
                if i != visible_end - 1:
                    frags.append((f"bg:{BG}", "\n"))
                continue
            item = items[i]
            is_sel = i == self.selected_index
            row_bg = sel_bg if is_sel else (BG_ALT if i % 2 else BG_ROW)
            label_part = f"  {item.icon}  {item.label}"
            desc = item.description
            remaining = max(1, inner - len(label_part) - 1)
            if len(desc) > remaining:
                desc = desc[: remaining - 1] + "."
            desc_part = f" {desc}" if desc else ""
            pad = max(0, inner - len(label_part) - len(desc_part))
            if is_sel:
                frags.append((f"bg:{row_bg} {SEL_FG} bold", label_part))
                if desc_part:
                    frags.append((f"bg:{row_bg} {DIM}", desc_part + " " * pad))
                else:
                    frags.append((f"bg:{row_bg}", " " * pad))
            else:
                frags.append((f"bg:{row_bg} {ITEM_FG}", label_part))
                if desc_part:
                    frags.append((f"bg:{row_bg} {DIM}", desc_part + " " * pad))
                else:
                    frags.append((f"bg:{row_bg}", " " * pad))
            if i != visible_end - 1:
                frags.append((f"bg:{row_bg}", "\n"))
        remaining_lines = max_lines - (visible_end - visible_start)
        for _ in range(remaining_lines):
            if frags:
                frags.append((f"bg:{BG}", "\n"))
            frags.append((f"bg:{BG}", " " * inner))
        return frags

    def footer_fragments(self) -> list[tuple[str, str]]:
        if self.is_input_screen():
            return [(DIM, "  Enter to continue  Esc to go back")]
        if self.screen == self.CONFIRM:
            return [(DIM, "   Select  Enter Confirm  Esc Cancel")]
        if self.screen == self.MESSAGE:
            return [(DIM, "  Enter to continue")]
        if self.screen == self.LIST:
            return [(DIM, "   Navigate  Enter Select  Esc Close  Type to search")]
        if self.screen == self.MODELS:
            return [(DIM, "   Navigate  Enter Select  Esc Back")]
        return [(DIM, "   Navigate  Enter Select  Esc Back")]

    def input_label(self) -> str:
        if self.screen == self.KEY_LABEL:
            return " Label: "
        if self.screen == self.KEY_INPUT:
            return " Key:    "
        if self.screen == self.RENAME_INPUT:
            return " New:    "
        if self.screen == self.WIZARD:
            step = WIZARD_STEPS[self.wizard_step_idx]
            return f" {step.prompt}: "
        return " > "

    def search_label(self) -> str:
        if self.screen == self.LIST:
            return " ? "
        return " > "

    def content_height(self) -> int:
        if self.screen == self.MESSAGE:
            return 1
        if self.screen == self.CONFIRM:
            return 1
        if self.is_input_screen():
            return 1
        items = self._build_items()
        return max(1, min(len(items), self.max_content_lines()))

    def total_height(self) -> int:
        return self.content_height() + 6