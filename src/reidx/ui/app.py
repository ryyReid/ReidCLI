"""Full-screen chat TUI: a real split-pane layout, not an inline redraw hack.

A `prompt_toolkit` full-screen `Application` owns the whole terminal (like
`vim`/`htop`, alternate screen — native scrollback is untouched and restored
on exit). Layout: a scrollable output pane on top, and a footer permanently
pinned to the last rows — spinner row, input box, status line. Because
`prompt_toolkit` owns the screen entirely, it handles cursor tracking and
resize itself; nothing fights it (unlike the reverted VT100 scroll-region
approach, which manually repositioned the cursor behind Rich/prompt_toolkit's
backs and corrupted rendering).

Rendering reuse: rather than reimplementing Rich's markdown/table/panel
rendering in prompt_toolkit's own formatting, `render.console` (the
module-level Rich `Console` almost everything in `ui/render.py` and
`ui/commands.py` already prints through) is temporarily swapped for one
backed by an in-memory buffer. Every existing `render.print_*` call and
`ui.commands.handle` keep working completely unmodified; their ANSI output is
drained and appended into the output pane's fragment list.
"""
from __future__ import annotations

import asyncio
import functools
import io
import random
import shutil
import sys
import threading
import time
from collections.abc import Callable

from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.clipboard import ClipboardData
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import ANSI, to_formatted_text
from prompt_toolkit.formatted_text.utils import split_lines
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import Float, FloatContainer, HSplit, Layout, VSplit, Window
from prompt_toolkit.layout.containers import ConditionalContainer
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.mouse_events import MouseEvent, MouseEventType
from prompt_toolkit.styles import Style
from prompt_toolkit.utils import get_cwidth
from rich.console import Console
from rich.text import Text

from reidx.deepreid import format_markdown, run_deepreid, save_deepreid_result
from reidx.diagnostics.logger import get_logger
from reidx.provider_manager import (
    ACCENT,
    BG,
    BORDER,
    ProviderDatabase,
    ProviderPalette,
)
from reidx.runtime.orchestrator import Orchestrator
from reidx.ui import render
from reidx.ui.commands import (
    _EFFORT_LEVELS,
    ARG_CHOICES,
    GOAL_SUBCOMMANDS,
    SLASH_COMMANDS,
    WORKFLOW_SUBCOMMANDS,
)
from reidx.ui.commands import handle as handle_command
from reidx.ui.render import _GERUNDS, _STAR_FRAMES, _bullet_grid
from reidx.ui.theme import (
    APP_NAME,
    BULLET,
    DANGER,
    DIM,
    PRIMARY,
    SPARKLE,
    SUCCESS,
    TREE,
    WARN,
    context_window_for,
    fmt_tokens,
    short_path,
)

log = get_logger("reidx.ui")

# A paste collapses to a placeholder when it's multi-line (a single-line
# input box can't display embedded newlines sanely) or long enough to make
# the box unreadable. Same idea as Claude Code's own input box.
_PASTE_COLLAPSE_CHARS = 300

# Typing one of these at the very start of the box turns it green and routes
# the submission through the real Researcher->Planner->Critic DeepReid
# pipeline (deepreid/pipeline.py) instead of a normal turn — the trigger word
# itself is stripped before the task is handed to the pipeline.
_DEEPREID_TRIGGERS = ("deepread", "deep read", "deepreid", "deep reid")

# Box border/caret color. Normal is a flat color; DeepReid cycles through
# these shades over time (see _box_color/_deepread_pulse_active) for an
# actual pulse, not just a static color swap.
_BOX_COLOR_NORMAL = "#ff5f5f"
_DEEPREID_PULSE_SHADES = ("#5fd75f", "#7fe77f", "#9ff09f", "#7fe77f")

_MODE_COLOR = {
    "strict": "#ff5555",
    "balanced": "#ffd75f",
    "autonomous": "#5fd75f",
    "custom": "#d75fd7",
}

# Themed style for the "/" completion menu + its scrollbar. Without this,
# prompt_toolkit falls back to its built-in grey/blue menu (which clashes with
# the red skin and renders black-on-grey descriptions). Classes map to the
# fragments menus.py emits; output-pane/status fragments carry absolute colors
# so this global style never touches them.
_MENU_BG = "#1c1818"
_MENU_BG_ALT = "#262220"
_UI_STYLE = Style.from_dict(
    {
        "completion-menu": f"bg:{_MENU_BG} {PRIMARY}",
        "completion-menu.completion": f"bg:{_MENU_BG} #d0d0d0",
        "completion-menu.completion.current": f"bg:{PRIMARY} {_MENU_BG} bold",
        "completion-menu.meta.completion": f"bg:{_MENU_BG_ALT} #8a7a7a",
        "completion-menu.meta.completion.current": f"bg:#d75f5f {_MENU_BG}",
        "scrollbar.background": f"bg:{_MENU_BG_ALT}",
        "scrollbar.button": f"bg:{PRIMARY}",
        "scrollbar.arrow": f"bg:{_MENU_BG} {PRIMARY}",
        "palette-border": f"{ACCENT}",
        "palette-bg": f"bg:{BG}",
        "palette-header": f"bg:#1a1010 bold {ACCENT}",
        "palette-footer": f"bg:{BG} {DIM}",
        "palette-search": f"bg:{BG} #d0d0d0",
        "palette-search-label": f"bg:{BG} {ACCENT}",
        "palette-sep": f"bg:{BG} {BORDER}",
        "dim-overlay": "bg:#080808",
        # Mouse drag selection highlight (transcript pane) — bright brand red.
        "selected": "bg:#ff2a2a #ffffff bold",
    }
)

# Inline absolute style for drag-select highlight. The transcript pane is built
# from Rich ANSI fragments (absolute colors), so class styles alone can look
# muted or get overridden — paint the red highlighter directly on the range.
_SELECTION_HIGHLIGHT = "bg:#ff2a2a #ffffff bold"


def _fragments_to_plain(line_frags) -> str:  # type: ignore[no-untyped-def]
    return "".join(text for _style, text in line_frags)


def _display_width(text: str) -> int:
    """Visual column width (CJK/emoji) — matches prompt_toolkit wrapping."""
    try:
        return get_cwidth(text)
    except Exception:  # noqa: BLE001
        return len(text or "")


def _line_wrap_height(text: str, width: int) -> int:
    """Rows a logical line occupies when wrapped to `width` (display columns)."""
    if width <= 0:
        return 1
    if not text:
        return 1
    # Walk like a terminal: accumulate display width, wrap at width.
    rows = 1
    col = 0
    for ch in text:
        w = get_cwidth(ch) if ch not in ("\n", "\r") else 0
        if w <= 0:
            continue
        if col + w > width:
            rows += 1
            col = w
        else:
            col += w
    return max(1, rows)


def _clean_selected_text(text: str) -> str:
    """Strip Rich full-width padding spaces from selected transcript lines."""
    if not text:
        return ""
    lines = [ln.rstrip() for ln in text.splitlines()]
    # Drop purely empty trailing lines from padding; keep intentional blanks mid-block.
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def _set_system_clipboard(text: str) -> bool:
    """Best-effort system clipboard write (Windows + prompt_toolkit fallbacks)."""
    if not text:
        return False
    ok = False
    # 1) Win32 CF_UNICODETEXT — most reliable for multi-line AI markdown on Windows.
    if sys.platform == "win32":
        try:
            import ctypes

            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            CF_UNICODETEXT = 13
            GMEM_MOVEABLE = 0x0002

            if user32.OpenClipboard(None):
                try:
                    user32.EmptyClipboard()
                    # UTF-16-LE + NUL terminator
                    data = text.encode("utf-16-le") + b"\x00\x00"
                    h_global = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
                    if h_global:
                        locked = kernel32.GlobalLock(h_global)
                        if locked:
                            ctypes.memmove(locked, data, len(data))
                            kernel32.GlobalUnlock(h_global)
                            if user32.SetClipboardData(CF_UNICODETEXT, h_global):
                                ok = True
                            else:
                                kernel32.GlobalFree(h_global)
                finally:
                    user32.CloseClipboard()
        except Exception:  # noqa: BLE001
            pass
        if not ok:
            try:
                import subprocess

                payload = text.replace("\n", "\r\n").encode("utf-16-le")
                proc = subprocess.run(
                    ["clip"],
                    input=b"\xff\xfe" + payload,
                    check=False,
                    capture_output=True,
                )
                if proc.returncode == 0:
                    ok = True
            except Exception:  # noqa: BLE001
                pass
    # 2) macOS / Linux common tools
    if not ok and sys.platform == "darwin":
        try:
            import subprocess

            proc = subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=False)
            ok = proc.returncode == 0
        except Exception:  # noqa: BLE001
            pass
    if not ok and sys.platform.startswith("linux"):
        for cmd in (
            ["xclip", "-selection", "clipboard"],
            ["xsel", "--clipboard", "--input"],
            ["wl-copy"],
        ):
            try:
                import subprocess

                proc = subprocess.run(cmd, input=text.encode("utf-8"), check=False)
                if proc.returncode == 0:
                    ok = True
                    break
            except Exception:  # noqa: BLE001
                continue
    return ok


def _apply_selection_highlight(
    line_frags,  # type: ignore[no-untyped-def]
    *,
    line_index: int,
    sel_a: tuple[int, int],
    sel_b: tuple[int, int],
):
    """Return fragments for one line with the selected range in bright red.

    Never paints Rich's full-width trailing pad spaces — that was making the
    whole terminal look selected when only a few words were in the range.
    """
    plain = _fragments_to_plain(line_frags)
    if not plain:
        return list(line_frags)

    content_end = len(plain.rstrip())
    if content_end == 0:
        # Pure padding / blank line — no red bar.
        return list(line_frags)

    (a_line, a_col), (b_line, b_col) = sel_a, sel_b
    if (a_line, a_col) > (b_line, b_col):
        a_line, a_col, b_line, b_col = b_line, b_col, a_line, a_col

    if line_index < a_line or line_index > b_line:
        return list(line_frags)
    start = a_col if line_index == a_line else 0
    end = b_col if line_index == b_line else content_end
    # Clamp to real text only (ignore terminal-width space padding).
    start = max(0, min(start, content_end))
    end = max(0, min(end, content_end))
    if start >= end:
        return list(line_frags)

    # Rebuild character-by-character styles, then paint [start, end) red.
    chars: list[tuple[str, str]] = []
    for style, text in line_frags:
        for ch in text:
            chars.append((style, ch))
    out: list[tuple[str, str]] = []
    buf_style = ""
    buf_text = ""
    for i, (style, ch) in enumerate(chars):
        use = _SELECTION_HIGHLIGHT if start <= i < end else style
        if use != buf_style and buf_text:
            out.append((buf_style, buf_text))
            buf_text = ""
        buf_style = use
        buf_text += ch
    if buf_text:
        out.append((buf_style, buf_text))
    return out


def _is_turn_boundary_line(text: str) -> bool:
    """True for blank / section separators so block-select stays on one turn."""
    s = (text or "").rstrip()
    if not s:
        return True
    t = s.strip()
    # User echo block
    if t == "User" or t.startswith("User "):
        return True
    if t.startswith("›") or t.startswith("> "):
        return True
    # Status / meta lines (not part of the AI answer body)
    low = t.lower()
    if low.startswith("provider:"):
        return True
    if low.startswith("cost ") or low.startswith("auto-compacted"):
        return True
    if low.startswith("copied last reply") or low.startswith("nothing to copy"):
        return True
    # Panel / box drawing from the welcome banner
    if any(ch in t for ch in "╭╮╰╯┌┐└┘│─┌"):
        return True
    return False


class _ConsoleCapture:
    """A Rich Console backed by an in-memory buffer, so existing render.py /
    commands.py code keeps writing ANSI-styled output unmodified — it just
    lands in a buffer we drain instead of stdout."""

    def __init__(self) -> None:
        self._buf = io.StringIO()
        self.console = Console(
            file=self._buf,
            width=self._measure_width(),
            force_terminal=True,
            color_system="truecolor",
            highlight=False,
            soft_wrap=False,
        )
        self._pos = 0

    @staticmethod
    def _measure_width() -> int:
        # Track the live terminal so tables/markdown fill the pane (not a
        # fixed 80-col column stuck in the middle of a wide Windows Terminal).
        try:
            from reidx.ui.theme import content_width

            return content_width(margin=2)
        except Exception:  # noqa: BLE001
            cols, _ = shutil.get_terminal_size(fallback=(100, 30))
            return max(40, cols - 2)

    def sync_width(self) -> None:
        """Refresh Rich width before a turn (resize-friendly)."""
        try:
            self.console.width = self._measure_width()
        except Exception:  # noqa: BLE001
            pass

    def drain(self) -> str:
        text = self._buf.getvalue()
        new = text[self._pos :]
        self._pos = len(text)
        return new


class _Block:
    """One unit of output pane content.

    Most blocks are static (fixed fragments). Thinking and tool-call blocks
    are collapsible: two ANSI variants are rendered once (at turn-completion
    time, never replayed) and the pane picks whichever the global Ctrl+O
    toggle currently wants at assembly time — no re-rendering, no re-running
    any side-effecting code (slash commands, etc.) on toggle.
    """

    __slots__ = ("fragments", "collapsed", "expanded")

    def __init__(self, fragments=None, collapsed=None, expanded=None) -> None:  # type: ignore[no-untyped-def]
        self.fragments = fragments
        self.collapsed = collapsed
        self.expanded = expanded

    @property
    def is_collapsible(self) -> bool:
        return self.collapsed is not None


class _OutputPane:
    """Accumulated blocks for the scrollable output window.

    Tail-to-bottom auto-follow uses prompt_toolkit's documented
    `[SetCursorPosition]` sentinel mechanism (see widgets.base.Label): the
    renderer scrolls to keep whichever fragment carries that marker visible.
    Scrolling manually (PageUp/PageDown, mouse wheel) works by *relocating*
    that marker to the target line rather than fighting the renderer's
    per-frame scroll recomputation — `Window._scroll_up/_down` (used by the
    default page-navigation bindings and the default mouse wheel handler)
    only adjust `vertical_scroll` directly, which gets silently overwritten
    right back by that same per-frame recomputation as long as the marker
    stays fixed at the bottom; relocating the marker is what actually moves
    the view. While `pinned` is True (the default, and whenever scrolled back
    down to the last line) the marker tracks the newest line automatically —
    "locked to bottom" exactly as before. Scrolling up unpins so new output
    appends below the fold without disturbing what's being read.
    """

    def __init__(self) -> None:
        self._blocks: list[_Block] = []
        self.expanded = False  # global Ctrl+O toggle for collapsible blocks
        self.pinned = True
        self._cursor_line = 0  # only meaningful while not pinned
        # Selection: (line, col) in logical plain-text lines; inclusive start
        # and exclusive end after normalize. None when nothing selected.
        self.sel_anchor: tuple[int, int] | None = None
        self.sel_cursor: tuple[int, int] | None = None
        self.selecting = False
        # Viewport hints written by _OutputWindow for mouse hit-testing.
        self.view_top = 0
        self.view_width = 80
        # Last copy stats for the status bar (right side) — "Copied!" toast.
        self.sel_line_count = 0
        self.sel_char_count = 0
        self.sel_copied_at = 0.0
        self.sel_copied_lines = 0  # lines at moment of last copy (for toast)
        # Click tracking: 1=start, 2=line, 3=paragraph/block.
        self._last_click_t = 0.0
        self._last_click_line = -1
        self._click_count = 0
        # Inclusive line range of the last rendered assistant reply in the pane
        # (so double-click / Ctrl+Y can select *only* the AI output).
        self.last_assistant_range: tuple[int, int] | None = None
        # Raw markdown for that reply (preferred clipboard payload).
        self.last_assistant_text: str = ""
        # Live token stream buffer (painted while the model is still generating).
        self.live_stream: str = ""

    def append_static(self, ansi_text: str) -> None:
        if not ansi_text:
            return
        self._blocks.append(_Block(fragments=to_formatted_text(ANSI(ansi_text))))

    def append_collapsible(self, collapsed_ansi: str, expanded_ansi: str) -> None:
        self._blocks.append(
            _Block(
                collapsed=to_formatted_text(ANSI(collapsed_ansi)),
                expanded=to_formatted_text(ANSI(expanded_ansi)),
            )
        )

    def toggle_expanded(self) -> None:
        self.expanded = not self.expanded

    def reset(self) -> None:
        self._blocks = []
        self.pinned = True
        self._cursor_line = 0
        self.clear_selection()

    def clear_selection(self) -> None:
        self.sel_anchor = None
        self.sel_cursor = None
        self.selecting = False
        self.sel_line_count = 0
        self.sel_char_count = 0
        self._click_count = 0

    def select_line(self, line: int) -> str:
        """Select an entire logical line; return its text.

        If the line is inside the last assistant reply range, select that whole
        reply instead (users almost always want the AI block, not one row).
        """
        lines = self.plain_lines()
        if not lines:
            self.clear_selection()
            return ""
        line = max(0, min(line, len(lines) - 1))
        ar = self.last_assistant_range
        if ar is not None:
            a0, a1 = ar
            a0 = max(0, min(a0, len(lines) - 1))
            a1 = max(a0, min(a1, len(lines) - 1))
            if a0 <= line <= a1:
                return self.select_line_range(a0, a1)
        content_end = len(lines[line].rstrip())
        self.sel_anchor = (line, 0)
        self.sel_cursor = (line, content_end)
        self.selecting = False
        self.update_selection_stats()
        return self.selected_text()

    def select_line_range(self, start: int, end: int) -> str:
        """Select inclusive logical lines [start, end] (content only, no pad)."""
        lines = self.plain_lines()
        if not lines:
            self.clear_selection()
            return ""
        start = max(0, min(start, len(lines) - 1))
        end = max(start, min(end, len(lines) - 1))
        self.sel_anchor = (start, 0)
        self.sel_cursor = (end, len(lines[end].rstrip()))
        self.selecting = False
        self.update_selection_stats()
        return self.selected_text()

    def select_last_assistant(self) -> str:
        """Select only the last AI reply block in the transcript pane."""
        if self.last_assistant_range is None:
            return ""
        a0, a1 = self.last_assistant_range
        return self.select_line_range(a0, a1)

    def _all_fragments(self):  # type: ignore[no-untyped-def]
        out: list = []
        for block in self._blocks:
            if block.is_collapsible:
                out.extend(block.expanded if self.expanded else block.collapsed)
            else:
                out.extend(block.fragments)
        return out

    def plain_lines(self) -> list[str]:
        return [_fragments_to_plain(line) for line in split_lines(self._all_fragments())]

    def selection_bounds(self) -> tuple[tuple[int, int], tuple[int, int]] | None:
        if self.sel_anchor is None or self.sel_cursor is None:
            return None
        a, b = self.sel_anchor, self.sel_cursor
        return (a, b) if a <= b else (b, a)

    def selected_text(self) -> str:
        bounds = self.selection_bounds()
        if bounds is None:
            return ""
        (a_line, a_col), (b_line, b_col) = bounds
        lines = self.plain_lines()
        if not lines:
            return ""
        a_line = max(0, min(a_line, len(lines) - 1))
        b_line = max(0, min(b_line, len(lines) - 1))
        if a_line == b_line:
            raw = lines[a_line][a_col:b_col]
        else:
            parts = [lines[a_line][a_col:]]
            for i in range(a_line + 1, b_line):
                parts.append(lines[i])
            parts.append(lines[b_line][:b_col])
            raw = "\n".join(parts)
        # Rich pads lines to full console width with trailing spaces — strip
        # those so AI markdown copies cleanly (and strip() checks aren't empty).
        return _clean_selected_text(raw)

    def select_block_around(self, line: int) -> str:
        """Select one turn/paragraph around `line` — never the whole session.

        Stops at blank lines and turn markers (User / provider / banner boxes)
        so triple-click on the AI answer does not also grab the banner + user.
        Prefers the last assistant range when the click lands inside it.
        """
        lines = self.plain_lines()
        if not lines:
            self.clear_selection()
            return ""
        line = max(0, min(line, len(lines) - 1))

        # Prefer the exact last AI reply when the user clicks inside it.
        ar = self.last_assistant_range
        if ar is not None:
            a0, a1 = ar
            a0 = max(0, min(a0, len(lines) - 1))
            a1 = max(a0, min(a1, len(lines) - 1))
            if a0 <= line <= a1:
                return self.select_line_range(a0, a1)

        def can_include(i: int) -> bool:
            if i < 0 or i >= len(lines):
                return False
            if _is_turn_boundary_line(lines[i]):
                return False
            return bool(lines[i].rstrip())

        if not can_include(line):
            for delta in (1, -1, 2, -2):
                j = line + delta
                if can_include(j):
                    line = j
                    break
            else:
                return self.select_line(line)

        # If we landed on an assistant bullet block, expand only that answer
        # (from the ● line through following body lines until a boundary).
        lo = line
        while lo > 0 and can_include(lo - 1):
            lo -= 1
        hi = line
        while hi + 1 < len(lines) and can_include(hi + 1):
            hi += 1
        return self.select_line_range(lo, hi)

    def update_selection_stats(self) -> None:
        text = self.selected_text()
        if not text:
            self.sel_line_count = 0
            self.sel_char_count = 0
            return
        # Line count = newlines + 1 for non-empty selection (matches editors).
        self.sel_line_count = text.count("\n") + 1
        self.sel_char_count = len(text)

    def screen_to_pos(self, y: int, x: int) -> tuple[int, int]:
        """Map viewport (row, col) to logical (line, col) using wrap + view_top.

        Uses display width (not raw len) so wrapped AI markdown / CJK lines
        map to the same rows prompt_toolkit actually paints.
        """
        lines = self.plain_lines()
        if not lines:
            return 0, 0
        width = max(1, self.view_width)
        y = max(0, y)
        x = max(0, x)
        row = 0
        for li in range(self.view_top, len(lines)):
            plain = lines[li]
            h = _line_wrap_height(plain, width)
            if row + h > y:
                # Character index for the visual column on this wrap row.
                target_cols = (y - row) * width + x
                col = 0
                disp = 0
                for ch in plain:
                    cw = get_cwidth(ch)
                    if disp + cw > target_cols:
                        break
                    disp += cw
                    col += 1
                col = max(0, min(len(plain), col))
                # Prefer landing inside real text, not Rich trailing pad spaces.
                stripped = plain.rstrip()
                if col > len(stripped) and stripped:
                    col = len(stripped)
                return li, col
            row += h
        last = len(lines) - 1
        return last, len(lines[last].rstrip())

    def scroll_up(self, lines: int = 3) -> None:
        total = max(1, len(list(split_lines(self._all_fragments()))))
        base = (total - 1) if self.pinned else self._cursor_line
        self._cursor_line = max(0, base - lines)
        self.pinned = False

    def scroll_down(self, lines: int = 3) -> None:
        if self.pinned:
            return
        total = max(1, len(list(split_lines(self._all_fragments()))))
        self._cursor_line = min(total - 1, self._cursor_line + lines)
        if self._cursor_line >= total - 1:
            self.pinned = True

    def scroll_to_top(self) -> None:
        self._cursor_line = 0
        self.pinned = False

    def scroll_to_bottom(self) -> None:
        self.pinned = True

    def bottom_line(self, total: int) -> int:
        """Logical line index that should sit at the bottom of the viewport.

        Pinned tracks the newest line; otherwise it's the scrolled-to cursor.
        `_OutputWindow` turns this into a concrete (wrap-aware) vertical scroll.
        """
        if total <= 0:
            return 0
        return (total - 1) if self.pinned else min(self._cursor_line, total - 1)

    def get_fragments(self):  # type: ignore[no-untyped-def]
        # No [SetCursorPosition] marker: `_OutputWindow` computes the vertical
        # scroll directly from `bottom_line`, which gives continuous,
        # bottom-anchored scrolling that offset/cursor tricks can't (the
        # renderer refuses to leave blank space below the last line).
        try:
            return self._build_fragments()
        except Exception:  # noqa: BLE001 - never crash the PT render loop
            log.exception("output pane render failed")
            return [("#ff5f5f", "  (output render error)")]

    def _build_fragments(self):  # type: ignore[no-untyped-def]
        # Snapshot live_stream once (worker thread may mutate it during stream).
        live = self.live_stream or ""
        lines = list(split_lines(self._all_fragments()))
        # Live stream tokens (while the model is still generating).
        if live:
            stream_frags = [
                ("", "\n"),
                (f"{PRIMARY} bold", f"{BULLET} "),
                ("#d0d0d0", live),
                ("#9e9e9e", " ▍"),
            ]
            lines.extend(list(split_lines(stream_frags)))
        total = len(lines)
        if total == 0:
            return []
        bounds = self.selection_bounds()
        out: list = []
        for i, line in enumerate(lines):
            if bounds is not None:
                out.extend(
                    _apply_selection_highlight(
                        line, line_index=i, sel_a=bounds[0], sel_b=bounds[1]
                    )
                )
            else:
                out.extend(line)
            if i != total - 1:
                out.append(("", "\n"))
        # Guard: prompt_toolkit requires every fragment to be (style, text[, handler]).
        return [
            f
            for f in out
            if isinstance(f, tuple) and len(f) >= 2 and isinstance(f[1], str)
        ]


def finish_output_selection(  # type: ignore[no-untyped-def]
    pane: _OutputPane,
    *,
    on_copy_selection,
    on_selection_changed,
    line: int | None = None,
    col: int | None = None,
) -> bool:
    """End an in-progress drag and copy if anything is selected.

    Called from the output pane on mouse-up *and* from other windows (input
    box) so releasing the button outside the transcript still copies.
    Returns True if a selection was finalized.

    If the drag lands entirely inside the last AI reply, copy the clean raw
    markdown instead of the rendered pane text (no bullet/padding junk).
    """
    if not pane.selecting and pane.selection_bounds() is None:
        return False
    if pane.selecting:
        if line is not None and col is not None:
            pane.sel_cursor = (line, col)
        pane.selecting = False
    pane.update_selection_stats()
    text = pane.selected_text()
    ar = pane.last_assistant_range
    raw = (pane.last_assistant_text or "").strip()
    bounds = pane.selection_bounds()
    # Full last-reply selection → clean markdown; partial drag keeps rendered slice.
    if ar is not None and bounds is not None and raw:
        (b0, _), (b1, _) = bounds
        a0, a1 = ar
        if b0 == a0 and b1 == a1:
            text = raw
    if text.strip():
        on_copy_selection(text)
        on_selection_changed()
        return True
    pane.clear_selection()
    on_selection_changed()
    return False


def _handle_output_mouse(  # type: ignore[no-untyped-def]
    pane: _OutputPane,
    mouse_event: MouseEvent,
    *,
    on_scroll_up,
    on_scroll_down,
    on_selection_changed,
    on_copy_selection,
) -> object | None:
    """Shared mouse logic: drag over text → highlight; release → clipboard.

    Returns None when handled (prompt_toolkit: stop propagation).
    """
    et = mouse_event.event_type
    if et == MouseEventType.SCROLL_UP:
        on_scroll_up()
        return None
    if et == MouseEventType.SCROLL_DOWN:
        on_scroll_down()
        return None

    pos = mouse_event.position
    y = int(getattr(pos, "y", 0) or 0)
    x = int(getattr(pos, "x", 0) or 0)

    if et == MouseEventType.MOUSE_DOWN:
        line, col = pane.screen_to_pos(y, x)
        now = time.monotonic()

        def _copy_payload_for_selection() -> str:
            """Prefer raw AI markdown when the whole last reply is selected."""
            ar = pane.last_assistant_range
            bounds = pane.selection_bounds()
            raw = (pane.last_assistant_text or "").strip()
            if ar is not None and bounds is not None and raw:
                (b0, _c0), (b1, _c1) = bounds
                a0, a1 = ar
                if b0 == a0 and b1 == a1:
                    return raw
            return pane.selected_text()

        # Triple-click → one turn block (AI-only when on the last reply).
        if (
            now - pane._last_click_t < 0.45
            and abs(pane._last_click_line - line) <= 1
            and getattr(pane, "_click_count", 0) >= 1
        ):
            if getattr(pane, "_click_count", 0) >= 2:
                text = pane.select_block_around(line)
                pane._last_click_t = 0.0
                pane._last_click_line = -1
                pane._click_count = 0
                payload = _copy_payload_for_selection() or text
                if payload.strip():
                    on_copy_selection(payload)
                on_selection_changed()
                return None
            # Double-click → last AI reply if inside it, else one line.
            text = pane.select_line(line)
            pane._click_count = 2
            pane._last_click_t = now
            pane._last_click_line = line
            payload = _copy_payload_for_selection() or text
            if payload.strip():
                on_copy_selection(payload)
            on_selection_changed()
            return None
        pane._last_click_t = now
        pane._last_click_line = line
        pane._click_count = 1
        pane.sel_anchor = (line, col)
        pane.sel_cursor = (line, col)
        pane.selecting = True
        pane.update_selection_stats()
        on_selection_changed()
        return None

    # Drag: update highlight while the button is held (MOUSE_MOVE while selecting).
    if et == MouseEventType.MOUSE_MOVE:
        if pane.selecting:
            line, col = pane.screen_to_pos(y, x)
            pane.sel_cursor = (line, col)
            pane.update_selection_stats()
            on_selection_changed()
        return None

    # Release: copy whatever is highlighted to the system clipboard.
    if et == MouseEventType.MOUSE_UP:
        if pane.selecting:
            line, col = pane.screen_to_pos(y, x)
            finish_output_selection(
                pane,
                on_copy_selection=on_copy_selection,
                on_selection_changed=on_selection_changed,
                line=line,
                col=col,
            )
        return None

    return NotImplemented


class _ScrollableOutputControl(FormattedTextControl):
    """FormattedTextControl: wheel scroll + drag-select → copy on mouse-up."""

    def __init__(  # type: ignore[no-untyped-def]
        self,
        get_fragments,
        on_scroll_up,
        on_scroll_down,
        pane: _OutputPane,
        on_selection_changed,
        on_copy_selection,
        **kwargs,
    ) -> None:
        super().__init__(get_fragments, **kwargs)
        self._on_scroll_up = on_scroll_up
        self._on_scroll_down = on_scroll_down
        self._pane = pane
        self._on_selection_changed = on_selection_changed
        self._on_copy_selection = on_copy_selection

    def mouse_handler(self, mouse_event: MouseEvent):  # type: ignore[no-untyped-def]
        result = _handle_output_mouse(
            self._pane,
            mouse_event,
            on_scroll_up=self._on_scroll_up,
            on_scroll_down=self._on_scroll_down,
            on_selection_changed=self._on_selection_changed,
            on_copy_selection=self._on_copy_selection,
        )
        if result is NotImplemented:
            return super().mouse_handler(mouse_event)
        return result


class _OutputWindow(Window):
    """Output pane that computes its own vertical scroll from the pane state.

    prompt_toolkit's built-in scrolling keeps a cursor/marker merely *visible*
    with minimal movement, which produces a dead-zone (the view doesn't move
    for the first few scroll steps out of the pinned bottom, then jumps and
    re-anchors to the top). Forcing it with scroll offsets instead pushes short
    content — like the startup banner — off the top of the pane.

    Overriding the wrap-aware scroll pass lets us bottom-anchor a chosen line
    directly: `_OutputPane.bottom_line` picks the logical line that should sit
    on the last row, and we walk up (honoring line wrapping) to find the top
    line. Short content lands at scroll 0 (top-aligned); overflowing content is
    bottom-anchored; scrolling moves the view by exactly the requested lines
    with no jump.
    """

    def __init__(  # type: ignore[no-untyped-def]
        self,
        pane: _OutputPane,
        on_scroll_up=None,
        on_scroll_down=None,
        on_selection_changed=None,
        on_copy_selection=None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._pane = pane
        self._on_scroll_up = on_scroll_up
        self._on_scroll_down = on_scroll_down
        self._on_selection_changed = on_selection_changed
        self._on_copy_selection = on_copy_selection

    def mouse_handler(self, mouse_event: MouseEvent):  # type: ignore[no-untyped-def]
        # Handle selection on the Window as well as the control — some hosts
        # deliver drag/release events here first.
        if (
            self._on_scroll_up is not None
            and self._on_copy_selection is not None
            and self._on_selection_changed is not None
        ):
            result = _handle_output_mouse(
                self._pane,
                mouse_event,
                on_scroll_up=self._on_scroll_up,
                on_scroll_down=self._on_scroll_down,
                on_selection_changed=self._on_selection_changed,
                on_copy_selection=self._on_copy_selection,
            )
            if result is not NotImplemented:
                return result
        return super().mouse_handler(mouse_event)

    def _scroll_when_linewrapping(self, ui_content, width, height):  # type: ignore[no-untyped-def]
        self.horizontal_scroll = 0
        self.vertical_scroll_2 = 0
        total = ui_content.line_count
        if total <= 0 or width <= 0 or height <= 0:
            self.vertical_scroll = 0
            self._pane.view_top = 0
            self._pane.view_width = max(1, width)
            return

        bottom = min(self._pane.bottom_line(total), total - 1)

        # Walk up from the bottom-anchored line, summing wrapped heights, until
        # the viewport is full; the last line that still fits is the top.
        used = 0
        top = bottom
        for lineno in range(bottom, -1, -1):
            used += ui_content.get_height_for_line(lineno, width, self.get_line_prefix)
            if used > height:
                break
            top = lineno
        self.vertical_scroll = top
        # Expose to mouse hit-testing (drag-select).
        self._pane.view_top = top
        self._pane.view_width = max(1, width)


class SlashCommandCompleter(Completer):
    """Completion menu for the input box: typing "/" lists every command
    from `ui.commands.SLASH_COMMANDS` (the same source `/help` renders from,
    so the two can't drift apart); typing "/workflow " lists its
    subcommands from `WORKFLOW_SUBCOMMANDS`. `/model ` lists models from the
    active provider (fetched live, cached briefly). Returns nothing for
    normal prompts.
    """

    def __init__(self, orchestrator: Orchestrator | None = None) -> None:
        self.orchestrator = orchestrator
        # (monotonic_ts, provider_name, models) — avoid hammering the API
        # while the user types filters character by character.
        self._model_cache: tuple[float, str, list[str]] | None = None
        self._model_cache_ttl = 45.0

    def _cached_models(self) -> tuple[str, list[str], str | None]:
        from reidx.ui.commands import fetch_provider_models

        orch = self.orchestrator
        if orch is None:
            return "?", [], "no orchestrator"
        name = ""
        if orch.state is not None:
            name = orch.state.session.provider
        else:
            name = orch.config.default_provider
        now = time.monotonic()
        if self._model_cache is not None:
            ts, cached_name, models = self._model_cache
            if cached_name == name and (now - ts) < self._model_cache_ttl:
                return name, models, None
        # Short timeout — completion must not freeze typing on huge catalogs.
        pname, models, err = fetch_provider_models(orch, name or None, timeout=5)
        if err is None:
            self._model_cache = (now, pname, models)
        return pname, models, err

    def get_completions(self, document, complete_event):  # type: ignore[no-untyped-def]
        text = document.text_before_cursor
        if not text.startswith("/"):
            return

        if text.startswith("/workflow "):
            prefix = text[len("/workflow ") :]
            if " " in prefix:
                return
            for name, args, desc in WORKFLOW_SUBCOMMANDS:
                if name.startswith(prefix):
                    display = f"{name} {args}".rstrip()
                    yield Completion(name, start_position=-len(prefix), display=display, display_meta=desc)
            return

        if text.startswith("/goal "):
            prefix = text[len("/goal ") :]
            if " " in prefix:
                return
            for name, args, desc in GOAL_SUBCOMMANDS:
                if name.startswith(prefix):
                    display = f"{name} {args}".rstrip()
                    yield Completion(name, start_position=-len(prefix), display=display, display_meta=desc)
            return

        # /use <name> — registered providers + aliases (nvidia → NVIDIA NIM)
        if text.startswith("/use "):
            prefix = text[len("/use ") :]
            if " " in prefix:
                return
            orch = self.orchestrator
            if orch is None or orch.providers is None:
                return
            needle = prefix.lower()
            names = list(orch.providers.names())
            # Surface short aliases next to display names when known.
            try:
                from reidx.provider_manager.catalog import all_providers

                for pdef in all_providers():
                    for a in (pdef.id, *pdef.aliases):
                        if a and a not in names and orch.providers.resolve(a) is not None:
                            names.append(a)
            except Exception:  # noqa: BLE001
                pass
            for name in sorted(set(names), key=str.lower):
                if needle and needle not in name.lower():
                    continue
                meta = ""
                resolved = orch.providers.resolve(name)
                if resolved and resolved != name:
                    meta = f"→ {resolved}"
                yield Completion(name, start_position=-len(prefix), display=name, display_meta=meta)
            return

        # /model <id> — live list from the active provider (NVIDIA NIM, etc.)
        if text.startswith("/model "):
            arg = text[len("/model ") :]
            # Model ids can contain `/` (meta/llama-...) but not spaces.
            if " " in arg:
                return
            _pname, models, err = self._cached_models()
            if err or not models:
                return
            current = ""
            if self.orchestrator and self.orchestrator.state is not None:
                current = self.orchestrator.state.session.model or ""
            needle = arg.lower()
            for model in models:
                if needle and needle not in model.lower():
                    continue
                meta = "current" if model == current else ""
                yield Completion(
                    model,
                    start_position=-len(arg),
                    display=model,
                    display_meta=meta,
                )
            return

        # /resume <id> — list known sessions (newest first)
        if text.startswith("/resume "):
            prefix = text[len("/resume ") :]
            if " " in prefix:
                return
            yield from self._resume_completions(prefix)
            return

        # Enum-valued commands: "/effort " -> low|medium|high|xhigh, etc. Offers
        # the choice list from ARG_CHOICES as soon as the command + space is
        # typed, so values don't have to be memorized.
        for cmd, choices in ARG_CHOICES.items():
            marker = f"{cmd} "
            if text.startswith(marker):
                arg = text[len(marker) :]
                if " " in arg:
                    return
                for value, desc in choices:
                    if value.startswith(arg):
                        yield Completion(value, start_position=-len(arg), display=value, display_meta=desc)
                return

        word = text[1:]
        if " " in word:
            return
        for cmd, args, desc, _group in SLASH_COMMANDS:
            token = cmd[1:]
            if token.startswith(word):
                display = f"{cmd} {args}".rstrip()
                yield Completion(f"/{token}", start_position=-len(text), display=display, display_meta=desc)

    def _resume_completions(self, prefix: str):  # type: ignore[no-untyped-def]
        """Yield session-id completions for `/resume `."""
        orch = self.orchestrator
        if orch is None:
            return
        try:
            sessions = list(orch.session_store.list())
        except Exception:  # noqa: BLE001 - completion must never crash the TUI
            return
        if not sessions:
            # Hint so the menu isn't empty and silent.
            yield Completion(
                "",
                start_position=-len(prefix),
                display="(no sessions yet)",
                display_meta="chat first, then /sessions",
            )
            return
        # Newest updated first (most likely resume target).
        try:
            sessions.sort(key=lambda s: s.updated_at, reverse=True)
        except Exception:  # noqa: BLE001
            pass
        current_id = ""
        if orch.state is not None:
            current_id = orch.state.session.id
        needle = prefix.lower()
        for sess in sessions:
            sid = sess.id
            if needle and needle not in sid.lower() and needle not in (sess.title or "").lower():
                continue
            title = (sess.title or "untitled").strip() or "untitled"
            if len(title) > 36:
                title = title[:33] + "…"
            model = (sess.model or "").strip()
            status = getattr(sess.status, "value", str(sess.status))
            meta_bits = [title]
            if model:
                meta_bits.append(model)
            if status and status != "active":
                meta_bits.append(status)
            if sid == current_id:
                meta_bits.append("current")
            display = sid
            yield Completion(
                sid,
                start_position=-len(prefix),
                display=display,
                display_meta=" · ".join(meta_bits),
            )


def _completion_wants_trailing_space(text: str) -> bool:
    """True when accepting this buffer text should append a space so the next
    completion menu (subcommand / enum args) opens immediately."""
    if not text or text.endswith(" "):
        return False
    if text in ARG_CHOICES or text in ("/goal", "/workflow", "/model", "/use", "/resume"):
        return True
    if text.startswith("/goal "):
        sub = text[len("/goal ") :]
        return any(name == sub and args for name, args, _desc in GOAL_SUBCOMMANDS)
    if text.startswith("/workflow "):
        sub = text[len("/workflow ") :]
        return any(name == sub and args for name, args, _desc in WORKFLOW_SUBCOMMANDS)
    return any(cmd == text and args for cmd, args, _desc, _group in SLASH_COMMANDS)


def accept_slash_completion(buf) -> bool:  # type: ignore[no-untyped-def]
    """Apply the highlighted (or first) "/" completion. Returns True if handled.

    Used by Tab, Right Arrow, and Enter so all three auto-fill the same way
    for commands, subcommands, and enum options.
    """
    if buf.complete_state is None:
        return False
    completion = buf.complete_state.current_completion
    if completion is None:
        completions = buf.complete_state.completions
        if not completions:
            buf.cancel_completion()
            return True
        completion = completions[0]
    buf.apply_completion(completion)
    if _completion_wants_trailing_space(buf.text):
        buf.insert_text(" ")
    return True


class ChatApp:
    """Owns the full-screen layout, input handling, and turn dispatch."""

    def __init__(self, orchestrator: Orchestrator, initial_prompt: str | None = None) -> None:
        self.orchestrator = orchestrator
        self.capture = _ConsoleCapture()
        self.output = _OutputPane()
        self._history = self._make_input_history()
        self._thinking = {"flag": False, "start": 0.0, "gerund": "", "last_swap": 0.0}
        self._cancel_event: threading.Event | None = None
        self._interrupt_armed_at = 0.0
        self._approving: dict = {"flag": False, "prompt": "", "result": False, "event": None}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._initial_prompt = (initial_prompt or "").strip()
        self._pastes: dict[str, str] = {}
        self._paste_counter = 0
        self._deepreid_running = False
        # Raw text of the last assistant reply (markdown) — for /copy & Ctrl+Shift+C
        # even when mouse drag selection is flaky on long wrapped AI output.
        self._last_assistant_text = ""

        self._buf = Buffer(
            history=self._history,
            multiline=False,
            enable_history_search=True,
            read_only=Condition(lambda: self._approving["flag"]),
            completer=SlashCommandCompleter(orchestrator),
            complete_while_typing=True,
        )
        self._palette: ProviderPalette | None = None
        self._palette_search_control: BufferControl | None = None
        self._palette_input_control: BufferControl | None = None
        self._init_palette()
        self.app: Application = Application(
            layout=self._build_layout(),
            key_bindings=self._build_key_bindings(),
            full_screen=True,
            mouse_support=True,
            style=_UI_STYLE,
        )

    # --- setup -----------------------------------------------------------

    def _make_input_history(self):  # type: ignore[no-untyped-def]
        """Session + on-disk input history for ↑/↓ recall."""
        try:
            from prompt_toolkit.history import FileHistory

            from reidx.config.storage import storage_root

            path = storage_root() / "input_history"
            path.parent.mkdir(parents=True, exist_ok=True)
            # Touch so FileHistory can open on first run.
            if not path.exists():
                path.write_text("", encoding="utf-8")
            return FileHistory(str(path))
        except Exception:  # noqa: BLE001 - history is convenience, never block launch
            return InMemoryHistory()

    def _commit_input_to_history(self, text: str) -> None:
        """Save submitted text so ↑ walks prior prompts immediately.

        Must be called **after** `buffer.reset()` — reset wipes working_lines.
        We append to the History store, then rebuild working_lines from it so
        `auto_up` / `history_backward` see the new entry right away.
        """
        text = (text or "").rstrip("\n")
        if not text.strip():
            return
        buf = self._buf
        # 1) Durable history (skip consecutive duplicates).
        try:
            strings = buf.history.get_strings()
            if not strings or strings[-1] != text:
                buf.history.append_string(text)
        except Exception:  # noqa: BLE001
            pass
        # 2) Working lines for ↑/↓ this session (oldest … newest, then draft "").
        try:
            from collections import deque

            entries = list(buf.history.get_strings())
            wl = entries + [""]
            buf._working_lines = deque(wl)
            buf.working_index = len(wl) - 1
        except Exception:  # noqa: BLE001
            pass

    def start(self) -> None:
        if self.orchestrator.state is None:
            self.orchestrator.start_session(title="interactive")
        self._append_output(render.banner)
        # Warn clearly when still on offline stub so people don't think chat is broken.
        st = self.orchestrator.state
        if st is not None and st.session.provider == "stub":
            self._append_output(
                lambda: render.print_warn(
                    "Offline mode (stub) — not a real AI. "
                    "Run /connect or /use <provider> (e.g. /use nvidia) to chat for real."
                )
            )
        elif st is not None:
            self._append_output(
                lambda: render.print_info(
                    f"provider: {st.session.provider} · model: {st.session.model or '(default)'}"
                )
            )

    def _init_palette(self) -> None:
        if self._palette is not None:
            return
        from pathlib import Path

        from reidx.config.storage import storage_root as get_storage_root
        root = self.orchestrator.config.storage_root or get_storage_root()
        db = ProviderDatabase(Path(root))
        self._palette = ProviderPalette(
            db=db,
            orchestrator=self.orchestrator,
            on_close=self._on_palette_close,
            on_invalidate=lambda: self.app.invalidate() if self.app.is_running else None,
        )
        self._palette_search_control = BufferControl(
            buffer=self._palette.search_buf,
            input_processors=[],
        )
        self._palette_input_control = BufferControl(
            buffer=self._palette.input_buf,
            input_processors=[],
        )

    def _activate_palette(self) -> None:
        self._init_palette()
        assert self._palette is not None
        self._palette.activate()
        assert self._palette_search_control is not None
        self.app.layout.focus(self._palette_search_control)
        self.app.invalidate()

    def _on_palette_close(self, message: str) -> None:
        self.app.layout.focus(self._buf)
        if message:
            self._append_output(lambda: render.print_info(message))
        else:
            self._append_output(lambda: render.print_info("provider palette closed"))
        self.app.invalidate()

    def _sync_palette_focus(self) -> None:
        if self._palette is None or not self._palette.active:
            self.app.layout.focus(self._buf)
            return
        if self._palette.is_input_screen():
            assert self._palette_input_control is not None
            self.app.layout.focus(self._palette_input_control)
        else:
            assert self._palette_search_control is not None
            self.app.layout.focus(self._palette_search_control)

    def _palette_border_top(self) -> list[tuple[str, str]]:
        if self._palette is None:
            return []
        return self._palette.border_top_fragments()

    def _palette_border_bottom(self) -> list[tuple[str, str]]:
        if self._palette is None:
            return []
        return self._palette.border_bottom_fragments()

    def _palette_sep(self) -> list[tuple[str, str]]:
        if self._palette is None:
            return []
        return self._palette.separator_fragments()

    async def main(self) -> int:
        self._loop = asyncio.get_running_loop()
        self.app.create_background_task(self._spinner_ticker())
        if self._initial_prompt:
            # Run as a background task rather than awaiting inline, so the app
            # starts rendering (banner, empty input box) immediately instead
            # of appearing to hang until the injected prompt's turn finishes.
            self.app.create_background_task(self._submit_text(self._initial_prompt))
        result = await self.app.run_async()
        return result or 0

    async def _spinner_ticker(self) -> None:
        while True:
            await asyncio.sleep(0.125)
            # Prune finished subagent rows past their linger window so the panel
            # actually shrinks when children complete.
            try:
                self.orchestrator.subagents.prune_finished()
            except AttributeError:
                pass
            if (
                self._thinking["flag"]
                or self._approving["flag"]
                or self._deepread_pulse_active()
                or self._subagent_rows_visible()
            ):
                self.app.invalidate()

    # --- subagent panel --------------------------------------------------

    _SUBAGENT_PANEL_MAX = 5

    def _subagent_rows_visible(self) -> bool:
        """True when there's anything to render in the panel (running or lingering)."""
        try:
            return bool(self.orchestrator.subagents.visible_rows())
        except AttributeError:
            return False

    def _subagent_fragments(self):  # type: ignore[no-untyped-def]
        try:
            return self._build_subagent_fragments()
        except Exception:  # noqa: BLE001 - cosmetic; never break the render loop
            log.exception("subagent panel render failed")
            return [("#9e9e9e", "  subagents: (render error)")]

    def _build_subagent_fragments(self):  # type: ignore[no-untyped-def]
        rows = self.orchestrator.subagents.visible_rows()
        if not rows:
            return [("", "")]
        # Cap displayed rows; overflow gets a "+N more" line.
        shown = rows[: self._SUBAGENT_PANEL_MAX]
        overflow = len(rows) - len(shown)

        status_glyph = {
            "running": ("#ffd75f", "◐"),
            "done": ("#5fd75f", "●"),
            "error": ("#ff5f5f", "●"),
        }
        frags: list = []
        for i, row in enumerate(shown):
            color, glyph = status_glyph.get(row.status, ("#9e9e9e", "○"))
            elapsed = int(row.elapsed_seconds)
            name = row.name[:16].ljust(16)
            status_text = row.status.ljust(7)
            action = (row.error or row.last_action or "").strip()
            action = action[:60]
            frags += [
                (color, f"  {glyph} "),
                ("#ffffff bold", name),
                # Must be a (style, text) pair — a bare (" ") is just the string
                # " " in Python and crashes prompt_toolkit split_lines unpack.
                ("", " "),
                (f"{color}", status_text),
                ("#9e9e9e", f" {elapsed}s"),
            ]
            if action:
                frags += [("#6c6c6c", "  · "), ("#9e9e9e", action)]
            if i != len(shown) - 1 or overflow > 0:
                frags.append(("", "\n"))
        if overflow > 0:
            frags.append(("#9e9e9e", f"  … +{overflow} more subagent(s)"))
        return frags

    def _deepread_pulse_active(self) -> bool:
        """Whether the box border should be pulsing right now — either the
        trigger word is currently typed (not yet submitted) or the pipeline
        is actively running. Without this, `_box_color()` would only ever be
        re-evaluated on buffer-edit events, so it'd show one static shade
        instead of animating while just sitting there."""
        return bool(self._deepread_prefix_len()) or self._deepreid_running

    # --- rendering bridge --------------------------------------------------

    def _append_output(self, fn: Callable[[], None]) -> None:
        fn()
        self.output.append_static(self.capture.drain())
        if self.app.is_running:
            self.app.invalidate()

    def _render_thinking_variants(self, text: str, seconds: int) -> tuple[str, str]:
        """Render both display variants of the chain-of-thought block once.

        Only called when the model actually produced reasoning (see
        `_emit_turn_result`) — an empty turn no longer renders a filler
        block. Collapsed: a single grayed-out "Thought for Ns" header
        matching the spinner's elapsed-time readout. Expanded: the same
        header plus the full thinking text beneath it. Neither variant is
        ever re-rendered — Ctrl+O just picks which was already captured.
        """
        header = Text(f"  {SPARKLE} Thought for {seconds}s", style=DIM)

        render.console.print(header)
        collapsed = self.capture.drain()

        render.console.print(header)
        render.print_thinking(text)
        expanded = self.capture.drain()
        return collapsed, expanded

    def _render_tool_call_variants(self, entry: dict) -> tuple[str, str]:
        """Render both display variants of one tool-call log entry.

        Collapsed: header line (name + args) with an inline ok/error status.
        Expanded: today's two-line layout — header, then a tree-connector
        result line beneath it.
        """
        name = entry["name"]
        ok = entry["ok"]
        error = entry.get("error", "")
        args = entry.get("args", {})
        args_str = ", ".join(f"{k}={v!r}" for k, v in args.items()) if args else ""
        header = Text.assemble((name, "bold"), ("(", DIM), (args_str, DIM), (")", DIM))

        status = Text(" ok", style=SUCCESS) if ok else Text(" error", style=DANGER)
        collapsed_line = Text.assemble(header, status, ("  (ctrl+o)", DIM))
        render.console.print(_bullet_grid(Text(BULLET, style=PRIMARY), collapsed_line))
        collapsed = self.capture.drain()

        render.console.print(_bullet_grid(Text(BULLET, style=PRIMARY), header))
        result = Text("ok", style=SUCCESS) if ok else Text(f"Error: {error}", style=DANGER)
        render.console.print(Text.assemble(("  ", ""), (TREE, DIM), ("  ", ""), result))
        expanded = self.capture.drain()

        return collapsed, expanded

    def _emit_turn_result(self, result: dict, thinking_seconds: int) -> None:
        # Only render the thinking block when the model actually produced
        # reasoning. Empty <think> content gets suppressed entirely rather
        # than leaving a "(model returned no reasoning for this turn)" line
        # floating above the answer, which was noisy and confusing when the
        # provider skipped CoT (short answers, safety refusals, low-effort
        # runs).
        thinking_text = (result.get("thinking") or "").strip()
        if thinking_text:
            thinking_variants = self._render_thinking_variants(thinking_text, thinking_seconds)
            self.output.append_collapsible(*thinking_variants)

        for entry in result.get("tools", []):
            self.output.append_collapsible(*self._render_tool_call_variants(entry))

        # Auto-compact notice (transcript rewrite) — not the footer token meter.
        compacted = result.get("compacted")
        if compacted:
            render.print_info(
                f"auto-compacted context: {compacted['before']} → {compacted['after']} messages "
                f"(~{compacted.get('before_tokens', '?')} → ~{compacted.get('after_tokens', '?')} tokens, "
                f"{compacted.get('method', '?')})"
            )

        # Soft provider failures (HTTP 404, bad key, network) land here as
        # result["error"] rather than as a raised exception — keep the session
        # up and show a single red Error line instead of a traceback dump.
        assistant_start: int | None = None
        if result.get("error"):
            render.print_error(result["text"])
            self._last_assistant_text = ""
            self.output.last_assistant_text = ""
            self.output.last_assistant_range = None
        else:
            # Keep the *raw* markdown (not the ANSI-rendered pane text) so
            # /copy and Ctrl+Y paste cleanly into editors/chat.
            self._last_assistant_text = result.get("text") or ""
            self.output.last_assistant_text = self._last_assistant_text
            # Line range covers only the assistant block (not user / banner / cost).
            assistant_start = len(self.output.plain_lines())
            # Drain anything already buffered (compact notice etc.) before the reply.
            pre = self.capture.drain()
            if pre:
                self.output.append_static(pre)
                assistant_start = len(self.output.plain_lines())
            render.print_assistant(result["text"])
            self.output.append_static(self.capture.drain())
            end = max(assistant_start, len(self.output.plain_lines()) - 1)
            # Skip trailing blank/pad lines so selection hugs the answer.
            lines = self.output.plain_lines()
            while end > assistant_start and not lines[end].rstrip():
                end -= 1
            self.output.last_assistant_range = (assistant_start, end)

        cost = result.get("cost")
        if cost and (cost.get("turn_usd") or cost.get("session_usd")):
            from reidx.runtime.cost import fmt_usd

            turn = fmt_usd(float(cost.get("turn_usd") or 0))
            sess = fmt_usd(float(cost.get("session_usd") or 0))
            tag = "" if cost.get("priced", True) else " (unpriced model)"
            render.print_info(f"cost {turn} this turn · {sess} session{tag}")

        # Remaining buffered output (cost line, etc.) — after assistant range.
        tail = self.capture.drain()
        if tail:
            self.output.append_static(tail)

        if self.app.is_running:
            self.app.invalidate()

    # --- scrolling / selection ----------------------------------------------

    def _scroll_up(self) -> None:
        self.output.scroll_up(3)
        self.app.invalidate()

    def _scroll_down(self) -> None:
        self.output.scroll_down(3)
        self.app.invalidate()

    def _on_selection_changed(self) -> None:
        if self.app.is_running:
            self.app.invalidate()

    def _copy_selection(self, text: str) -> None:
        """Copy selected transcript text to the system clipboard + show Copied!."""
        if not text:
            return
        text = _clean_selected_text(text) if "\n" in text or text.endswith(" ") else text
        ok = False
        try:
            self.app.clipboard.set_data(ClipboardData(text))
            ok = True
        except Exception:  # noqa: BLE001
            pass
        if _set_system_clipboard(text):
            ok = True
        self.output.update_selection_stats()
        self.output.sel_copied_lines = self.output.sel_line_count or (
            text.count("\n") + 1 if text.strip() else 0
        )
        self.output.sel_copied_at = time.monotonic()
        # Keep selection highlight briefly so the user sees what was copied,
        # then return focus to the input box.
        try:
            if self.app.is_running and not (self._palette and self._palette.active):
                self.app.layout.focus(self._buf)
        except Exception:  # noqa: BLE001
            pass
        if self.app.is_running:
            self.app.invalidate()
        if not ok:
            log.debug("clipboard write may have failed for %s chars", len(text))

    def _copy_last_assistant(self) -> None:
        """Copy *only* the last AI reply (raw markdown) — never banner/user text."""
        text = (self._last_assistant_text or self.output.last_assistant_text or "").strip()
        if not text and self.orchestrator.state is not None:
            # Fallback: last assistant message in the live transcript.
            for msg in reversed(self.orchestrator.state.messages):
                if msg.role == "assistant" and (msg.content or "").strip():
                    # Skip soft provider-error placeholders.
                    if msg.content.startswith("[provider error]"):
                        continue
                    text = msg.content.strip()
                    break
        if not text:
            self._append_output(
                lambda: render.print_warn(
                    "nothing to copy — no assistant reply yet. "
                    "Wait for a reply, then /copy or Ctrl+Y."
                )
            )
            return
        # Highlight only the AI block in the pane (not User / banner / cost).
        if self.output.last_assistant_range is not None:
            self.output.select_last_assistant()
        self._copy_selection(text)
        self.output.sel_copied_lines = text.count("\n") + 1
        self.output.sel_copied_at = time.monotonic()
        if self.app.is_running:
            self.app.invalidate()
        # Don't append a transcript line here — it would shift ranges and
        # look like another message. The status-bar "Copied!" chip is enough.

    # --- status / spinner content -----------------------------------------

    def _estimate_tokens(self) -> int:
        st = self.orchestrator.state
        if st is None:
            return 0
        # Prefer real usage from the provider's last response over a guess —
        # StubProvider (and any provider that doesn't report usage) leaves
        # these at 0, so the char-based estimate below is still the fallback
        # for it, but real providers (e.g. Anthropic) report actual token
        # counts and that's what's shown once at least one turn has run.
        real = st.last_usage_prompt_tokens + st.last_usage_completion_tokens
        if real > 0:
            return real
        try:
            chars = sum(len(m.content or "") for m in list(st.messages))
        except (RuntimeError, AttributeError):
            return 0
        return max(1, chars // 4)

    def _status(self) -> dict:
        st = self.orchestrator.state
        if st is None:
            return {
                "mode": "—", "model": "—", "effort": "—",
                "tokens_used": 0, "context_window": 0,
                "workspace": "—", "tasks": 0,
            }
        return {
            "mode": st.effective_mode.value,
            "model": st.session.model,
            "effort": st.session.reasoning_effort,
            "effort_resolved": st.last_effort_resolved,
            "tokens_used": self._estimate_tokens(),
            "context_window": context_window_for(
                st.session.model, session_window=st.session.context_window
            ),
            "workspace": str(st.session.workspace),
            "tasks": len(self.orchestrator.list_tasks()),
        }

    def _status_fragments(self):  # type: ignore[no-untyped-def]
        # Called on every redraw by prompt_toolkit's core render loop, with no
        # error boundary above it — an uncaught exception here kills the whole
        # app (that's what happened when tasks.json read failed). Never let a
        # status-computation bug take the TUI down.
        try:
            return self._build_status_fragments()
        except Exception:  # noqa: BLE001 - cosmetic; never break the render loop
            log.exception("status bar render failed")
            return [("#9e9e9e", "  status unavailable")]

    def _format_effort_status(self, status: dict) -> str:
        """Footer label: effort:auto→low or effort:medium."""
        effort = status.get("effort") or "—"
        resolved = status.get("effort_resolved") or ""
        if effort == "auto" and resolved:
            return f"effort:auto→{resolved}"
        return f"effort:{effort}"

    def _build_status_fragments(self):  # type: ignore[no-untyped-def]
        """One compact footer line that fits the terminal (no wrap/clip mess)."""

        status = self._status()
        window = status.get("context_window", 0)
        used = status.get("tokens_used", 0)
        pct = f"{(used / window * 100):.0f}%" if window else "—"
        usage = f"{fmt_tokens(used)}/{fmt_tokens(window)} ({pct})" if window else fmt_tokens(used)
        mode = status.get("mode", "—")
        mode_color = _MODE_COLOR.get(mode, "#9e9e9e")
        model = str(status.get("model") or "—")
        if len(model) > 28:
            model = model[:25] + "…"
        sep = ("#6c6c6c", " · ")
        # Compact layout matching a clean full-width TUI footer.
        frags: list = [
            ("#ff5f5f bold", f" {APP_NAME}"),
            sep,
            (f"{mode_color} bold", mode),
            sep,
            ("#9e9e9e", model),
            sep,
            ("#9e9e9e", self._format_effort_status(status)),
            sep,
            ("#9e9e9e", usage),
        ]
        # Workspace: last folder name is enough (avoid "gith" truncation of long paths).
        wp_raw = str(status.get("workspace") or "")
        if wp_raw and wp_raw != "—":
            try:
                from pathlib import Path as _P

                wp = _P(wp_raw).name or short_path(wp_raw, keep=1)
            except Exception:  # noqa: BLE001
                wp = short_path(wp_raw, keep=1)
            if len(wp) > 24:
                wp = "…" + wp[-23:]
            frags += [sep, ("#9e9e9e", wp)]
        st = self.orchestrator.state
        if st is not None and st.costs.total_usd > 0:
            from reidx.runtime.cost import fmt_usd

            frags += [sep, ("#9e9e9e", fmt_usd(st.costs.total_usd))]
        if not self.output.pinned:
            frags += [sep, ("#ffd75f bold", "↑ scroll")]
        return frags

    def _selection_status_fragments(self):  # type: ignore[no-untyped-def]
        """Bottom-right toast: selection size while dragging, then 'Copied!'."""
        try:
            copied_ago = (
                time.monotonic() - self.output.sel_copied_at
                if self.output.sel_copied_at
                else 999.0
            )
            # Match the product "Copied!" chip after a successful copy.
            if copied_ago < 2.5:
                n = self.output.sel_copied_lines or self.output.sel_line_count
                if n > 0:
                    unit = "line" if n == 1 else "lines"
                    return [("#5fd75f bold", f"  {n} {unit} · Copied!  ")]
                return [("#5fd75f bold", "  Copied!  ")]
            n = self.output.sel_line_count
            if n <= 0:
                return [("", "")]
            unit = "line" if n == 1 else "lines"
            if self.output.selecting:
                return [("#ffd75f bold", f"  {n} {unit}  ")]
            return [("#ffd75f", f"  {n} {unit}  ")]
        except Exception:  # noqa: BLE001
            return [("", "")]

    def _spinner_fragments(self):  # type: ignore[no-untyped-def]
        # Same rationale as _status_fragments: this runs every redraw with no
        # error boundary above it in prompt_toolkit's render loop.
        try:
            return self._build_spinner_fragments()
        except Exception:  # noqa: BLE001 - cosmetic; never break the render loop
            log.exception("spinner render failed")
            return [("#9e9e9e", "  …")]

    def _build_spinner_fragments(self):  # type: ignore[no-untyped-def]
        if self._approving["flag"]:
            prompt_text = self._approving.get("prompt", "")
            return [("#ffd75f bold", f"  {prompt_text}  allow? [y/N]")]
        if not self._thinking["flag"]:
            return [("", "")]
        if self._cancel_event is not None and self._cancel_event.is_set():
            return [("#ffd75f bold", "  ◐ stopping… "), ("#9e9e9e", "(esc pressed, finishing current step)")]
        now = time.monotonic()
        if now - self._thinking["last_swap"] > 8.0:
            self._thinking["gerund"] = random.choice(_GERUNDS)
            self._thinking["last_swap"] = now
        elapsed = int(now - self._thinking["start"])
        star = _STAR_FRAMES[int(now * 6) % len(_STAR_FRAMES)]
        streaming = bool(self.output.live_stream)
        label = "streaming" if streaming else self._thinking["gerund"]
        n_stream = len(self.output.live_stream) if streaming else 0
        frags = [
            ("#ff5f5f", f"  {star} "),
            ("#ff5f5f", f"{label}… "),
            ("#9e9e9e", f"({elapsed}s"),
            ("#9e9e9e", f" · ↑ {fmt_tokens(self._estimate_tokens())} tokens"),
        ]
        if streaming and n_stream:
            frags.append(("#9e9e9e", f" · {n_stream} chars"))
        frags.append(("#9e9e9e", ")"))
        return frags

    # --- layout --------------------------------------------------------

    def _build_layout(self) -> Layout:
        output_window = _OutputWindow(
            self.output,
            on_scroll_up=self._scroll_up,
            on_scroll_down=self._scroll_down,
            on_selection_changed=self._on_selection_changed,
            on_copy_selection=self._copy_selection,
            content=_ScrollableOutputControl(
                self.output.get_fragments,
                on_scroll_up=self._scroll_up,
                on_scroll_down=self._scroll_down,
                pane=self.output,
                on_selection_changed=self._on_selection_changed,
                on_copy_selection=self._copy_selection,
                focusable=True,
            ),
            wrap_lines=True,
            # Don’t let the window steal focus from input unless the user
            # is actually selecting; still receives mouse for drag-copy.
            dont_extend_height=False,
        )
        spinner_window = Window(content=FormattedTextControl(self._spinner_fragments), height=1)

        # Box border/caret color is a callable, not a static style, so it
        # re-evaluates every render — that's what makes it turn green live as
        # soon as the buffer starts with a DeepReid trigger word.
        def corner(ch: str) -> Window:
            return Window(FormattedTextControl(lambda: [(self._box_color(), ch)]), width=1, height=1)

        def hline() -> Window:
            return Window(char="─", style=self._box_color, height=1)

        # Finalize transcript drag-select if the user releases the mouse over
        # the input box (common when selecting the last AI lines near the bottom).
        class _InputWindow(Window):
            def mouse_handler(inner_self, mouse_event: MouseEvent):  # type: ignore[no-untyped-def]
                if mouse_event.event_type == MouseEventType.MOUSE_UP and self.output.selecting:
                    finish_output_selection(
                        self.output,
                        on_copy_selection=self._copy_selection,
                        on_selection_changed=self._on_selection_changed,
                    )
                return super(_InputWindow, inner_self).mouse_handler(mouse_event)

        input_window = _InputWindow(
            content=BufferControl(buffer=self._buf), wrap_lines=False, height=1
        )

        box = HSplit(
            [
                VSplit([corner("╭"), hline(), corner("╮")], height=1),
                VSplit(
                    [
                        Window(FormattedTextControl(lambda: [(self._box_color(), "│")]), width=1, height=1),
                        Window(FormattedTextControl(lambda: [(f"{self._box_color()} bold", " › ")]), width=3, height=1),
                        input_window,
                        Window(FormattedTextControl(lambda: [(self._box_color(), "│")]), width=1, height=1),
                    ],
                    height=1,
                ),
                VSplit([corner("╰"), hline(), corner("╯")], height=1),
            ]
        )
        # Status left (session info) + right (selection / Copied! chip).
        status_window = VSplit(
            [
                Window(content=FormattedTextControl(self._status_fragments), height=1),
                Window(
                    content=FormattedTextControl(self._selection_status_fragments),
                    height=1,
                    width=Dimension(min=12, preferred=22, max=28),
                ),
            ],
            height=1,
        )

        # Subagent panel: appears directly under the input box (pushing it up
        # visually because HSplit re-layouts) whenever there are running or
        # recently-finished subagents. Sits above the status line so the
        # footer's app/mode/model/tokens readout stays the last row.
        subagent_panel = ConditionalContainer(
            content=Window(content=FormattedTextControl(self._subagent_fragments)),
            filter=Condition(self._subagent_rows_visible),
        )

        root = HSplit([output_window, spinner_window, box, subagent_panel, status_window])
        floated = FloatContainer(
            content=root,
            floats=[
                Float(
                    xcursor=True,
                    ycursor=True,
                    content=CompletionsMenu(max_height=10, scroll_offset=1, display_arrows=True),
                ),
                Float(
                    left=0,
                    top=0,
                    width=None,
                    height=None,
                    content=ConditionalContainer(
                        content=self._build_palette_overlay(),
                        filter=Condition(lambda: self._palette is not None and self._palette.active),
                    ),
                ),
            ],
        )
        return Layout(floated, focused_element=input_window)

    # --- palette overlay --------------------------------------------------

    def _build_palette_overlay(self):
        return self._build_palette_box()

    def _build_palette_box(self):
        p = self._palette

        is_search = Condition(lambda: p is not None and p.is_search_screen())
        is_input = Condition(lambda: p is not None and p.is_input_screen())

        def header_frags():
            return p.header_fragments() if p is not None else []

        def content_frags():
            return p.content_fragments() if p is not None else []

        def footer_frags():
            return p.footer_fragments() if p is not None else []

        top_border = Window(
            FormattedTextControl(self._palette_border_top),
            height=1,
            style="class:palette-border",
        )
        header = Window(
            FormattedTextControl(header_frags),
            height=1,
            style="class:palette-header",
        )
        sep = Window(
            FormattedTextControl(self._palette_sep),
            height=1,
            style="class:palette-sep",
        )
        search_line = ConditionalContainer(
            content=VSplit([
                Window(
                    FormattedTextControl(lambda: [("class:palette-search-label", f"│{p.search_label()}")]),
                    width=5,
                    height=1,
                ),
                Window(
                    self._palette_search_control,
                    height=1,
                    style="class:palette-search",
                ),
                Window(
                    FormattedTextControl(lambda: [("class:palette-search", "│")]),
                    width=1,
                    height=1,
                ),
            ]),
            filter=is_search,
        )
        input_line = ConditionalContainer(
            content=VSplit([
                Window(
                    FormattedTextControl(lambda: [("class:palette-search-label", f"│{p.input_label()}")]),
                    width=14,
                    height=1,
                ),
                Window(
                    self._palette_input_control,
                    height=1,
                    style="class:palette-search",
                ),
                Window(
                    FormattedTextControl(lambda: [("class:palette-search", "│")]),
                    width=1,
                    height=1,
                ),
            ]),
            filter=is_input,
        )
        filler = ConditionalContainer(
            content=Window(
                FormattedTextControl(lambda: [("class:palette-bg", f"│{' ' * (p.inner_width())}│")]),
                height=1,
                style="class:palette-bg",
            ),
            filter=~is_search & ~is_input,
        )
        content = Window(
            FormattedTextControl(content_frags),
            style="class:palette-bg",
        )
        footer = Window(
            FormattedTextControl(footer_frags),
            height=1,
            style="class:palette-footer",
        )
        bottom_border = Window(
            FormattedTextControl(self._palette_border_bottom),
            height=1,
            style="class:palette-border",
        )
        return HSplit([
            top_border,
            header,
            sep,
            search_line,
            input_line,
            filler,
            sep,
            content,
            sep,
            footer,
            bottom_border,
        ])

    # --- input handling --------------------------------------------------

    def _build_key_bindings(self) -> KeyBindings:
        kb = KeyBindings()
        is_thinking = Condition(lambda: self._thinking["flag"])
        is_approving = Condition(lambda: self._approving["flag"])
        is_buffer_empty = Condition(lambda: not self._buf.text)
        is_palette = Condition(lambda: self._palette is not None and self._palette.active)
        is_completing = Condition(lambda: self._buf.complete_state is not None)
        can_edit = Condition(
            lambda: not self._thinking["flag"]
            and not self._approving["flag"]
            and not (self._palette is not None and self._palette.active)
        )

        @kb.add("up", filter=is_palette)
        def _palette_up(event) -> None:  # type: ignore[no-untyped-def]
            self._palette.on_up()
            self._sync_palette_focus()

        @kb.add("down", filter=is_palette)
        def _palette_down(event) -> None:  # type: ignore[no-untyped-def]
            self._palette.on_down()
            self._sync_palette_focus()

        @kb.add("enter", filter=is_palette)
        def _palette_enter(event) -> None:  # type: ignore[no-untyped-def]
            self._palette.on_enter()
            self._sync_palette_focus()

        @kb.add("escape", filter=is_palette)
        def _palette_escape(event) -> None:  # type: ignore[no-untyped-def]
            self._palette.on_escape()
            self._sync_palette_focus()

        @kb.add("c-c", filter=is_palette)
        def _palette_cancel(event) -> None:  # type: ignore[no-untyped-def]
            self._palette.deactivate()
            self._sync_palette_focus()

        @kb.add("enter", filter=~is_thinking & ~is_approving & ~is_palette)
        async def _submit(event) -> None:  # type: ignore[no-untyped-def]
            buf = event.current_buffer
            # Menu open → auto-fill highlighted (or first) option; does not
            # submit. Second Enter runs the completed command.
            if accept_slash_completion(buf):
                return
            await self._on_submit()

        # Tab / Right: accept the highlighted "/" completion (commands,
        # subcommands, and enum options like /effort high). Tab with no menu
        # open but a slash in the buffer starts the menu with the first item
        # selected so a second Tab fills it in.
        @kb.add("tab", filter=can_edit)
        def _tab_complete(event) -> None:  # type: ignore[no-untyped-def]
            buf = event.current_buffer
            if accept_slash_completion(buf):
                return
            text = buf.document.text_before_cursor
            if text.startswith("/"):
                buf.start_completion(select_first=True)

        @kb.add("right", filter=is_completing & can_edit)
        def _right_accept_completion(event) -> None:  # type: ignore[no-untyped-def]
            if not accept_slash_completion(event.current_buffer):
                # Fallback: move cursor right if somehow nothing to accept.
                event.current_buffer.cursor_right()

        @kb.add("y", filter=is_approving)
        @kb.add("Y", filter=is_approving)
        def _approve_yes(event) -> None:  # type: ignore[no-untyped-def]
            self._resolve_approval(True)

        @kb.add("n", filter=is_approving)
        @kb.add("N", filter=is_approving)
        @kb.add("enter", filter=is_approving)
        def _approve_no(event) -> None:  # type: ignore[no-untyped-def]
            self._resolve_approval(False)

        @kb.add("escape", filter=is_thinking & ~is_palette)
        def _cancel_turn(event) -> None:  # type: ignore[no-untyped-def]
            # Stops the in-flight response, not the session — the running turn
            # ends at its next safe point (see Agent.run_turn's `cancel` polling)
            # instead of the whole app exiting, matching Claude Code's Escape.
            if self._cancel_event is not None:
                self._cancel_event.set()

        @kb.add("c-c", filter=~is_palette)
        def _copy_or_clear(event) -> None:  # type: ignore[no-untyped-def]
            # Ctrl-C priority (matches Claude Code / terminal muscle memory):
            #   1) finalize an in-progress drag-selection → copy
            #   2) copy an existing selection
            #   3) interrupt a running turn
            #   4) clear a non-empty input box
            #   5) double-tap within 1.5s on an idle empty prompt → exit
            # Prefer copy when the transcript has a selection (hover/drag copy UX).
            if self.output.selecting:
                finish_output_selection(
                    self.output,
                    on_copy_selection=self._copy_selection,
                    on_selection_changed=self._on_selection_changed,
                )
                if self.output.selected_text().strip():
                    self._interrupt_armed_at = 0.0
                    return
            selected = self.output.selected_text()
            if selected.strip():
                self._copy_selection(selected)
                self._interrupt_armed_at = 0.0
                return
            # Interrupt an in-flight response — Ctrl-C is the reflex for "stop".
            if self._thinking["flag"] and self._cancel_event is not None:
                self._cancel_event.set()
                self._interrupt_armed_at = 0.0
                return
            if self._buf.text:
                self._buf.reset()
                self._interrupt_armed_at = 0.0
                return
            # Nothing to copy/cancel/clear: double Ctrl-C exits.
            now = time.monotonic()
            if now - self._interrupt_armed_at <= 1.5:
                self.app.exit(result=0)
                return
            self._interrupt_armed_at = now
            self._append_output(
                lambda: render.print_info("Press Ctrl+C again to exit (or Ctrl+D)")
            )

        # Ctrl+Y = yank last AI reply. (Ctrl+Shift+C is not a valid
        # prompt_toolkit key name and is also stolen by many terminals.)
        @kb.add("c-y", filter=~is_palette)
        def _copy_last_reply(event) -> None:  # type: ignore[no-untyped-def]
            self._copy_last_assistant()

        @kb.add("c-d", filter=~is_palette)
        def _exit(event) -> None:  # type: ignore[no-untyped-def]
            self.app.exit(result=0)

        @kb.add("c-o", filter=~is_palette)
        def _toggle_collapse(event) -> None:  # type: ignore[no-untyped-def]
            self.output.toggle_expanded()
            self.app.invalidate()

        # Input history: ↑ / ↓ (and Ctrl+P / Ctrl+N) walk prior prompts.
        # When the "/" completion menu is open, leave arrows to the default
        # completion navigator (don't steal history while picking /resume ids).
        # Shift+↑/↓ still scrolls the transcript.
        @kb.add("up", filter=can_edit & ~is_completing & ~is_palette)
        def _history_up(event) -> None:  # type: ignore[no-untyped-def]
            event.current_buffer.auto_up(count=event.arg)

        @kb.add("down", filter=can_edit & ~is_completing & ~is_palette)
        def _history_down(event) -> None:  # type: ignore[no-untyped-def]
            event.current_buffer.auto_down(count=event.arg)

        @kb.add("c-p", filter=can_edit & ~is_completing & ~is_palette)
        def _history_prev(event) -> None:  # type: ignore[no-untyped-def]
            event.current_buffer.auto_up(count=event.arg)

        @kb.add("c-n", filter=can_edit & ~is_completing & ~is_palette)
        def _history_next(event) -> None:  # type: ignore[no-untyped-def]
            event.current_buffer.auto_down(count=event.arg)

        # Keyboard scrollback for the transcript pane. A page is roughly the
        # output window's height; we don't have it here without the renderer,
        # so a fixed page of 15 lines keeps PageUp/PageDown predictable.
        @kb.add("pageup", filter=~is_approving & ~is_palette)
        def _page_up(event) -> None:  # type: ignore[no-untyped-def]
            self.output.scroll_up(15)
            self.app.invalidate()

        @kb.add("pagedown", filter=~is_approving & ~is_palette)
        def _page_down(event) -> None:  # type: ignore[no-untyped-def]
            self.output.scroll_down(15)
            self.app.invalidate()

        @kb.add("s-up", filter=~is_approving & ~is_palette)
        def _line_up(event) -> None:  # type: ignore[no-untyped-def]
            self.output.scroll_up(1)
            self.app.invalidate()

        @kb.add("s-down", filter=~is_approving & ~is_palette)
        def _line_down(event) -> None:  # type: ignore[no-untyped-def]
            self.output.scroll_down(1)
            self.app.invalidate()

        @kb.add(Keys.BracketedPaste, filter=~is_approving & ~is_palette)
        def _paste(event) -> None:  # type: ignore[no-untyped-def]
            data = event.data
            if "\n" in data or len(data) > _PASTE_COLLAPSE_CHARS:
                event.current_buffer.insert_text(self._collapse_paste(data))
            else:
                event.current_buffer.insert_text(data)

        # Left/Right on an empty prompt still cycle effort — but Right while
        # the completion menu is open accepts the selection instead (above).
        @kb.add("left", filter=is_buffer_empty & ~is_completing & ~is_thinking & ~is_approving & ~is_palette)
        def _effort_prev(event) -> None:  # type: ignore[no-untyped-def]
            self._cycle_effort(-1)

        @kb.add("right", filter=is_buffer_empty & ~is_completing & ~is_thinking & ~is_approving & ~is_palette)
        def _effort_next(event) -> None:  # type: ignore[no-untyped-def]
            self._cycle_effort(1)

        return kb

    def _cycle_effort(self, delta: int) -> None:
        if self.orchestrator.state is None:
            return
        session = self.orchestrator.state.session
        levels = _EFFORT_LEVELS  # auto, low, medium, high, xhigh
        try:
            idx = levels.index(session.reasoning_effort)
        except ValueError:
            idx = levels.index("medium") if "medium" in levels else 0
        session.reasoning_effort = levels[(idx + delta) % len(levels)]
        self.orchestrator.session_store.update(session)
        self.app.invalidate()

    def _deepread_prefix_len(self) -> int:
        """Length of a DeepReid trigger word at the start of the buffer, or 0
        if there isn't one — 0 also means "not triggered", so this doubles as
        the truthiness check. Requires a word boundary right after the
        trigger (end-of-text or whitespace) so "deepreading..." doesn't
        false-positive on "deepread"."""
        text = self._buf.text.lstrip()
        lead = len(self._buf.text) - len(text)
        lowered = text.lower()
        for trigger in _DEEPREID_TRIGGERS:
            if lowered.startswith(trigger):
                rest = text[len(trigger) :]
                if not rest or rest[0].isspace():
                    return lead + len(trigger)
        return 0

    def _box_color(self) -> str:
        # Faster pulse while the pipeline is actually working, gentler pulse
        # while just sitting there with the trigger typed but not submitted.
        if self._deepreid_running:
            idx = int(time.monotonic() * 6) % len(_DEEPREID_PULSE_SHADES)
            return _DEEPREID_PULSE_SHADES[idx]
        if self._deepread_prefix_len():
            idx = int(time.monotonic() * 2) % len(_DEEPREID_PULSE_SHADES)
            return _DEEPREID_PULSE_SHADES[idx]
        return _BOX_COLOR_NORMAL

    def _collapse_paste(self, data: str) -> str:
        """Store a large/multi-line paste and return a short placeholder for
        the input box — same idea as Claude Code's own `[Pasted text]`
        collapse. The full text is substituted back in at submit time."""
        self._paste_counter += 1
        lines = data.count("\n") + 1
        label = f"[Pasted text #{self._paste_counter} +{lines} lines]" if lines > 1 else f"[Pasted text #{self._paste_counter} +{len(data)} chars]"
        self._pastes[label] = data
        return label

    def _expand_pastes(self, text: str) -> str:
        for label, data in self._pastes.items():
            text = text.replace(label, data)
        return text

    async def _on_submit(self) -> None:
        text = self._buf.text
        if not text.strip():
            return
        prefix_len = self._deepread_prefix_len()
        # Clear the box first (reset wipes working_lines), then rebuild history.
        self._buf.reset(append_to_history=False)
        self._commit_input_to_history(text)
        if prefix_len:
            task = self._expand_pastes(text[prefix_len:].lstrip())
            self._pastes.clear()
            await self._run_deepreid(task)
            return
        text = self._expand_pastes(text)
        self._pastes.clear()
        await self._submit_text(text)

    async def _run_deepreid(self, task: str) -> None:
        """Run the real Researcher->Planner->Critic pipeline (deepreid/pipeline.py)
        and render its Markdown output, instead of a normal single-agent turn."""
        if not task.strip():
            return
        self._append_output(lambda: render.console.print(Text(f"  DeepReid: {task}", style="bold #5fd75f")))
        self._deepreid_running = True
        self.app.invalidate()

        assert self._loop is not None
        loop = self._loop

        def progress(stage: str) -> None:
            loop.call_soon_threadsafe(lambda: self._append_output(lambda: render.print_info(f"  {stage}...")))

        try:
            result = await loop.run_in_executor(
                None,
                functools.partial(
                    run_deepreid,
                    self.orchestrator.config,
                    self.orchestrator.provider,
                    self.orchestrator.state.session.workspace,
                    task,
                    on_progress=progress,
                ),
            )
        except Exception as exc:  # noqa: BLE001 - the TUI must not die on runtime errors
            # Never log to the console StreamHandler from the TUI path —
            # stderr bleeds into the full-screen buffer. UI shows the error once.
            log.debug("deepreid failed: %s: %s", type(exc).__name__, exc)
            error_text = str(exc)
            self._append_output(lambda: render.print_error(error_text))
        else:
            path = save_deepreid_result(self.orchestrator.config, result)
            self._append_output(lambda: render.print_assistant(format_markdown(result)))
            self._append_output(lambda: render.print_info(f"saved to {path}"))
        finally:
            self._deepreid_running = False
            self.app.invalidate()

    async def _submit_text(self, text: str) -> None:
        """Run one turn for `text` — shared by the Enter key binding and by
        an injected initial prompt (`reid "<prompt>"` / piped stdin)."""
        if not text.strip():
            return

        if text.startswith("/"):
            outcome = self._run_slash(text)
            if outcome == "exit":
                self.app.exit(result=0)
            elif outcome == "connect":
                self._activate_palette()
            elif outcome == "copy-last":
                self._copy_last_assistant()
            elif outcome.startswith("workflow-run:"):
                await self._run_workflow(outcome.split(":", 1)[1])
            return

        self.capture.sync_width()
        self._append_output(lambda: render.print_user(text))
        self._thinking["flag"] = True
        self._thinking["start"] = time.monotonic()
        self._thinking["gerund"] = random.choice(_GERUNDS)
        self._thinking["last_swap"] = self._thinking["start"]
        self.output.live_stream = ""
        self.app.invalidate()

        cancel_event = threading.Event()
        self._cancel_event = cancel_event
        approver = self._make_approver()
        assert self._loop is not None

        def _append_stream_chunk(chunk: str) -> None:
            """UI-thread only: own live_stream + redraw (throttled)."""
            if not chunk:
                return
            self.output.live_stream += chunk
            if len(self.output.live_stream) > 200_000:
                self.output.live_stream = self.output.live_stream[-160_000:]
            now = time.monotonic()
            last = getattr(self, "_stream_last_invalidate", 0.0)
            # ~25 Hz max so fast token streams don't starve the event loop.
            if now - last >= 0.04 or len(self.output.live_stream) < 40:
                self._stream_last_invalidate = now
                if self.app.is_running:
                    self.app.invalidate()

        def _on_text_delta(chunk: str) -> None:
            # Worker thread → marshal buffer update onto the asyncio/UI loop.
            if not chunk:
                return
            loop = self._loop
            if loop is not None and self.app.is_running:
                try:
                    loop.call_soon_threadsafe(_append_stream_chunk, chunk)
                    return
                except Exception:  # noqa: BLE001
                    pass
            # Fallback if loop not ready (shouldn't happen mid-turn).
            _append_stream_chunk(chunk)

        try:
            result = await self._loop.run_in_executor(
                None,
                functools.partial(
                    self.orchestrator.submit_task,
                    text,
                    approver=approver,
                    cancel=cancel_event.is_set,
                    on_text_delta=_on_text_delta,
                ),
            )
        except Exception as exc:  # noqa: BLE001 - the TUI must not die on runtime errors
            # Safety net only — provider failures are soft-caught in Agent.run_turn.
            # No console log: stderr corrupts the full-screen UI.
            log.debug("turn failed: %s: %s", type(exc).__name__, exc)
            error_text = str(exc)
            self._append_output(lambda: render.print_error(error_text))
        else:
            seconds = int(time.monotonic() - self._thinking["start"])
            self._emit_turn_result(result, seconds)
        finally:
            self._thinking["flag"] = False
            self._cancel_event = None
            self.output.live_stream = ""
            self.app.invalidate()

    def _run_slash(self, text: str) -> str:
        if text.strip() == "/clear":
            self.output.reset()
            if self.app.is_running:
                self.app.invalidate()
            return "continue"
        outcome = "continue"

        def _do() -> None:
            nonlocal outcome
            outcome = handle_command(self.orchestrator, text)

        self._append_output(_do)
        return outcome

    async def _run_workflow(self, name: str) -> None:
        """Run a saved workflow's steps in sequence through `_submit_text`,
        so each step gets identical treatment to typing it in directly —
        slash commands and prompts both work, spinner/approval included."""
        workflow = self.orchestrator.workflow_store.get(name)
        if workflow is None:
            self._append_output(lambda: render.print_error(f"no such workflow: {name}"))
            return
        self._append_output(
            lambda: render.print_info(f"running workflow '{name}' ({len(workflow.steps)} steps)")
        )
        for step in workflow.steps:
            await self._submit_text(step)

    # --- approval bridge (worker thread <-> main loop thread) --------------

    def _make_approver(self) -> Callable[[str], bool]:
        loop = self._loop
        assert loop is not None

        def approve(prompt_text: str) -> bool:
            done = threading.Event()

            def _show() -> None:
                self._approving["prompt"] = prompt_text
                self._approving["result"] = False
                self._approving["event"] = done
                self._approving["flag"] = True
                self._append_output(lambda: render.console.print(Text(prompt_text, style=f"bold {WARN}")))

            loop.call_soon_threadsafe(_show)
            done.wait()
            return bool(self._approving["result"])

        return approve

    def _resolve_approval(self, value: bool) -> None:
        self._approving["result"] = value
        self._approving["flag"] = False
        event: threading.Event | None = self._approving["event"]
        self._approving["event"] = None
        self.app.invalidate()
        if event is not None:
            event.set()


def run(orchestrator: Orchestrator, initial_prompt: str | None = None) -> int:
    """Entry point: build and run the full-screen chat app.

    Reuses an already-resumed session or starts fresh (matching the prior
    ui/repl.py::repl behavior). Returns 0 on a clean exit.

    If `initial_prompt` is given, it's submitted as the first turn as soon as
    the app starts rendering — the interactive equivalent of typing it into
    the box and pressing Enter, so the session stays open afterward (unlike
    `reid exec`, which runs one prompt headless and exits).

    Wraps the session in `TerminalHostSession` so PowerShell / Windows Terminal
    colour schemes and prompt themes (Oh My Posh, etc.) are restored on exit.
    """
    from reidx.ui.terminal_host import TerminalHostSession

    with TerminalHostSession():
        chat = ChatApp(orchestrator, initial_prompt=initial_prompt)
        original_console = render.console
        render.console = chat.capture.console
        try:
            chat.start()
            code = asyncio.run(chat.main())
        finally:
            render.console = original_console
        try:
            render.console.print(Text("bye.", style="dim"))
        except Exception:  # noqa: BLE001 - never block exit on a goodbye line
            pass
        return code or 0
