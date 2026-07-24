from __future__ import annotations

import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from prompt_toolkit.buffer import Buffer

from reidx.diagnostics.logger import get_logger
from reidx.provider.context_windows import context_window_for, fmt_context_window

log = get_logger("reidx.provider_manager.model_palette")

BG = "#151111"
BG_ROW_A = "#1d1719"
BG_ROW_B = "#181314"
SEL_BG = "#3a1a22"
SEL_FG = "#ffd5d5"
DIM = "#7a6868"
ACCENT = "#ff5f5f"
ACCENT_SOFT = "#d75f5f"
OK = "#5fd75f"
WARN = "#ffd75f"
ERR = "#ff5f5f"
ITEM_FG = "#e0d8d8"
ITEM_FG_DIM = "#8a7a7a"
ACTIVE_FG = "#5fd75f"
TAG_VISION = "#c8a0d8"
TAG_REASON = "#7fbfdb"

_ANIM_OPEN = 0.22
_ANIM_SEL = 0.16
SEL_BG_POP = "#4d2228"

IC_ACTIVE = "●"
IC_BLANK = " "
IC_WARN = "!"
IC_SPIN = "◐"

_STATE_IDLE = "idle"
_STATE_LOADING = "loading"
_STATE_READY = "ready"
_STATE_ERROR = "error"


def _ease_out_cubic(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return 1 - (1 - t) ** 3


def _lerp_hex(a: str, b: str, t: float) -> str:
    t = max(0.0, min(1.0, t))
    ar, ag, ab = int(a[1:3], 16), int(a[3:5], 16), int(a[5:7], 16)
    br, bg, bb = int(b[1:3], 16), int(b[3:5], 16), int(b[5:7], 16)
    return (
        f"#{round(ar + (br - ar) * t):02x}"
        f"{round(ag + (bg - ag) * t):02x}"
        f"{round(ab + (bb - ab) * t):02x}"
    )


@dataclass
class ModelItem:
    id: str
    label: str = ""
    context: str = ""
    tags: list[str] = field(default_factory=list)


class ModelPalette:
    def __init__(
        self,
        fetch_models: Callable[[], tuple[list[str], str | None]],
        on_select: Callable[[str], None],
        on_close: Callable[[], None],
        *,
        current_model: str = "",
        provider_name: str = "",
        on_invalidate: Callable[[], None] | None = None,
        on_retry: Callable[[], None] | None = None,
    ) -> None:
        self._fetch_models = fetch_models
        self._on_select = on_select
        self._on_close = on_close
        self._on_invalidate = on_invalidate or (lambda: None)
        self._on_retry = on_retry or (lambda: None)
        self._active = False
        self.selected_index = 0
        self._scroll_offset = 0
        self._provider_name = provider_name
        self._current = current_model
        self._state = _STATE_IDLE
        self._error = ""
        self._models: list[ModelItem] = []
        self._all_models: list[ModelItem] = []
        self._spin_phase = 0.0
        self._anim_open_at = 0.0
        self._anim_sel_at = 0.0
        self._fetch_gen = 0
        self.search_buf = Buffer(multiline=False)
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
        return self._active

    def activate(self) -> None:
        self._active = True
        self.selected_index = 0
        self._scroll_offset = 0
        self.search_buf.text = ""
        self._anim_open_at = time.monotonic()
        self._anim_sel_at = 0.0
        self._begin_fetch()
        self._invalidate()

    def deactivate(self) -> None:
        self._active = False
        self.search_buf.text = ""
        self._scroll_offset = 0
        self._invalidate()

    def is_animating(self) -> bool:
        if not self._active:
            return False
        if self._state == _STATE_LOADING:
            return True
        now = time.monotonic()
        return (
            now - self._anim_open_at < _ANIM_OPEN
            or now - self._anim_sel_at < _ANIM_SEL
        )

    def tick_spin(self) -> None:
        self._spin_phase = (self._spin_phase + 0.15) % 1.0

    def on_up(self) -> None:
        items = self._models
        if items:
            self.selected_index = (self.selected_index - 1) % len(items)
            self._adjust_scroll(len(items))
            self._anim_sel_at = time.monotonic()
            self._invalidate()

    def on_down(self) -> None:
        items = self._models
        if items:
            self.selected_index = (self.selected_index + 1) % len(items)
            self._adjust_scroll(len(items))
            self._anim_sel_at = time.monotonic()
            self._invalidate()

    def on_enter(self) -> None:
        if self._state != _STATE_READY:
            return
        items = self._models
        if not items:
            return
        idx = min(self.selected_index, len(items) - 1)
        chosen = items[idx].id
        self._active = False
        self._on_select(chosen)

    def on_escape(self) -> None:
        self._active = False
        self._on_close()

    def _invalidate(self) -> None:
        self._on_invalidate()

    def _on_search_changed(self, _buf: Buffer | None = None) -> None:
        self._apply_filter()
        self.selected_index = 0
        self._scroll_offset = 0
        self._invalidate()

    def _apply_filter(self) -> None:
        q = self.search_buf.text.strip().lower()
        if not q:
            self._models = list(self._all_models)
            return
        scored: list[tuple[int, ModelItem]] = []
        for m in self._all_models:
            ml = m.label.lower()
            mid = m.id.lower()
            if ml == q or mid == q:
                scored.append((100, m))
            elif mid.startswith(q) or ml.startswith(q):
                scored.append((80, m))
            elif q in mid or q in ml:
                scored.append((60, m))
            elif any(q in t.lower() for t in m.tags):
                scored.append((40, m))
        scored.sort(key=lambda x: (-x[0], x[1].id))
        self._models = [m for _, m in scored]

    def _begin_fetch(self) -> None:
        self._state = _STATE_LOADING
        self._error = ""
        self._models = []
        self._all_models = []
        self._fetch_gen += 1
        self._invalidate()

    def deliver_models(self, models: list[str], error: str | None, *, gen: int = 0) -> None:
        if not self._active:
            return
        if gen and gen != self._fetch_gen:
            return
        if error:
            self._state = _STATE_ERROR
            self._error = error
            self._models = []
            self._all_models = []
            self._invalidate()
            return
        items: list[ModelItem] = []
        for mid in models:
            window = context_window_for(mid, session_window=0)
            ctx = fmt_context_window(window) if window else ""
            tags = self._tags_for(mid)
            items.append(ModelItem(id=mid, label=mid, context=ctx, tags=tags))
        self._all_models = items
        self._apply_filter()
        self._state = _STATE_READY
        cur_idx = 0
        if self._current:
            for i, m in enumerate(self._models):
                if m.id == self._current:
                    cur_idx = i
                    break
        self.selected_index = cur_idx
        self._scroll_offset = 0
        self._anim_open_at = time.monotonic()
        self._invalidate()

    def _tags_for(self, model_id: str) -> list[str]:
        m = (model_id or "").lower()
        tags: list[str] = []
        if any(k in m for k in ("vision", "vl", "image", "-v")):
            tags.append("vision")
        if any(k in m for k in ("reason", "r1", "thinking", "o1", "o3", "deepseek-r")):
            tags.append("reason")
        return tags

    def _wrap_text(self, text: str, width: int) -> list[str]:
        if width < 8:
            return [text[:width]] if text else []
        words = text.split()
        lines: list[str] = []
        cur = ""
        for w in words:
            if cur and len(cur) + 1 + len(w) > width:
                lines.append(cur)
                cur = w
            else:
                cur = f"{cur} {w}".strip()
        if cur:
            lines.append(cur)
        return lines or [""]

    def retry(self) -> None:
        if self._state == _STATE_ERROR:
            self._begin_fetch()
            self._on_retry()

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
        title = "  ✦ Select Model"
        right = self._provider_name or ""
        inner = self.inner_width()
        pad = max(2, inner - len(title) - len(right))
        return [
            (f"bold {ACCENT}", title),
            (f"{DIM}", " " * pad),
            (f"{DIM}", right + " "),
        ]

    def content_fragments(self) -> list[tuple[str, str]]:
        inner = self.inner_width()
        if self._state == _STATE_LOADING:
            glyph = IC_SPIN
            msg = "fetching models…"
            return [
                (f"{ACCENT}", f"  {glyph} "),
                (f"{ITEM_FG}", msg),
            ]
        if self._state == _STATE_ERROR:
            frags: list[tuple[str, str]] = [
                (f"{ERR}", f"  {IC_WARN} "),
                (f"{ITEM_FG}", "could not list models"),
            ]
            if self._provider_name:
                frags.append((f"{DIM}", "\n  "))
                frags.append((f"{DIM}", f"from {self._provider_name}"))
            wrapped = self._wrap_text(self._error, max(20, inner - 4))
            for line in wrapped[:3]:
                frags.append((f"{DIM}", "\n  "))
                frags.append((f"{DIM}", line))
            frags.append((f"{DIM}", "\n  "))
            frags.append((f"{ACCENT_SOFT}", "press r to retry  ·  Esc closes"))
            return frags
        if self._state == _STATE_READY and not self._models:
            return [(f"{DIM}", "  no models match your search")]
        if self._state != _STATE_READY:
            return [(f"{DIM}", "  …")]

        max_lines = self.max_content_lines()
        self._adjust_scroll(len(self._models))
        visible_start = self._scroll_offset
        visible_end = min(visible_start + max_lines, len(self._models))
        visible_count = visible_end - visible_start

        now = time.monotonic()
        revealed = visible_count
        if self._anim_open_at and now - self._anim_open_at < _ANIM_OPEN:
            reveal = _ease_out_cubic((now - self._anim_open_at) / _ANIM_OPEN)
            revealed = max(1, min(visible_count, round(reveal * visible_count)))
        sel_bg = SEL_BG
        if self._anim_sel_at and now - self._anim_sel_at < _ANIM_SEL:
            ts = _ease_out_cubic((now - self._anim_sel_at) / _ANIM_SEL)
            sel_bg = _lerp_hex(SEL_BG_POP, SEL_BG, ts)

        frags: list[tuple[str, str]] = []
        for i in range(visible_start, visible_end):
            if i - visible_start >= revealed:
                frags.append((f"bg:{BG}", " " * inner))
                if i != visible_end - 1:
                    frags.append((f"bg:{BG}", "\n"))
                continue
            item = self._models[i]
            is_sel = i == self.selected_index
            is_current = item.id == self._current
            row_bg = sel_bg if is_sel else (BG_ROW_A if i % 2 else BG_ROW_B)
            icon = IC_ACTIVE if is_current else IC_BLANK
            icon_color = ACTIVE_FG if is_current else (SEL_FG if is_sel else DIM)
            label_color = SEL_FG if is_sel else ITEM_FG
            label_weight = " bold" if (is_sel or is_current) else ""
            icon_style = f"bg:{row_bg} {icon_color} bold"
            label_style = f"bg:{row_bg} {label_color}{label_weight}"
            icon_part = f"  {icon}  "
            tags_str = ""
            tag_style = ""
            if item.tags:
                tag_style = f"bg:{row_bg} {TAG_VISION}"
                tags_str = "  ".join(item.tags) + "  "
            ctx = item.context
            ctx_part = f"{ctx}" if ctx else ""
            ctx_style = f"bg:{row_bg} {DIM}"
            label_w = inner - len(icon_part) - len(tags_str) - len(ctx_part)
            if len(item.label) > label_w:
                shown = item.label[: max(0, label_w - 1)] + "…"
            else:
                shown = item.label
            label_padded = shown + " " * max(0, label_w - len(shown))
            frags.append((icon_style, icon_part))
            if tags_str:
                frags.append((tag_style, tags_str))
            frags.append((label_style, label_padded))
            if ctx_part:
                frags.append((ctx_style, ctx_part))
            if i != visible_end - 1:
                frags.append((f"bg:{row_bg}", "\n"))
        remaining_lines = max_lines - (visible_end - visible_start)
        for _ in range(remaining_lines):
            if frags:
                frags.append((f"bg:{BG}", "\n"))
            frags.append((f"bg:{BG}", " " * inner))
        return frags

    def footer_fragments(self) -> list[tuple[str, str]]:
        if self._state == _STATE_LOADING:
            return self._key_hints([("Esc", "cancel")])
        if self._state == _STATE_ERROR:
            return self._key_hints([("r", "retry"), ("Esc", "close")])
        return self._key_hints([("\u2191\u2193", "navigate"), ("Enter", "select"), ("Esc", "close"), ("type", "filter")])

    def _key_hints(self, hints: list[tuple[str, str]]) -> list[tuple[str, str]]:
        frags: list[tuple[str, str]] = [(DIM, " ")]
        for i, (key, action) in enumerate(hints):
            if i:
                frags.append((f"{DIM}", "  "))
            frags.append((f"bold {ITEM_FG_DIM}", key))
            frags.append((f"{DIM}", f" {action}"))
        return frags

    def search_label(self) -> str:
        return " ? "

    def content_height(self) -> int:
        if self._state == _STATE_LOADING:
            return 1
        if self._state == _STATE_ERROR:
            wrapped = self._wrap_text(self._error, max(20, self.inner_width() - 4))
            return min(6, 2 + len(wrapped[:3]))
        if not self._models:
            return 1
        return max(1, min(len(self._models), self.max_content_lines()))

    def total_height(self) -> int:
        return self.content_height() + 6
