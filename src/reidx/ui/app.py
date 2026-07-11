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
import threading
import time
from collections.abc import Callable

from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
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
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.mouse_events import MouseEvent, MouseEventType
from prompt_toolkit.styles import Style
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
    }
)


class _ConsoleCapture:
    """A Rich Console backed by an in-memory buffer, so existing render.py /
    commands.py code keeps writing ANSI-styled output unmodified — it just
    lands in a buffer we drain instead of stdout."""

    def __init__(self) -> None:
        cols, _rows = shutil.get_terminal_size(fallback=(100, 30))
        self._buf = io.StringIO()
        self.console = Console(
            file=self._buf,
            width=max(40, cols - 2),
            force_terminal=True,
            color_system="truecolor",
            highlight=False,
            soft_wrap=False,
        )
        self._pos = 0

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

    def _all_fragments(self):  # type: ignore[no-untyped-def]
        out: list = []
        for block in self._blocks:
            if block.is_collapsible:
                out.extend(block.expanded if self.expanded else block.collapsed)
            else:
                out.extend(block.fragments)
        return out

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
        lines = list(split_lines(self._all_fragments()))
        total = len(lines)
        if total == 0:
            return []
        out: list = []
        for i, line in enumerate(lines):
            out.extend(line)
            if i != total - 1:
                out.append(("", "\n"))
        return out


class _ScrollableOutputControl(FormattedTextControl):
    """FormattedTextControl that routes mouse wheel scroll to callbacks.

    The default `Window._mouse_handler` fallback for scroll events just
    nudges `vertical_scroll` by +-1, which is exactly what gets fought and
    reverted by the cursor-follow recomputation described on `_OutputPane`.
    Intercepting here lets scroll wheel drive the same marker-relocation
    logic as the PageUp/PageDown key bindings.
    """

    def __init__(self, get_fragments, on_scroll_up, on_scroll_down, **kwargs) -> None:  # type: ignore[no-untyped-def]
        super().__init__(get_fragments, **kwargs)
        self._on_scroll_up = on_scroll_up
        self._on_scroll_down = on_scroll_down

    def mouse_handler(self, mouse_event: MouseEvent):  # type: ignore[no-untyped-def]
        if mouse_event.event_type == MouseEventType.SCROLL_UP:
            self._on_scroll_up()
            return None
        if mouse_event.event_type == MouseEventType.SCROLL_DOWN:
            self._on_scroll_down()
            return None
        return super().mouse_handler(mouse_event)


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

    def __init__(self, pane: _OutputPane, **kwargs) -> None:  # type: ignore[no-untyped-def]
        super().__init__(**kwargs)
        self._pane = pane

    def _scroll_when_linewrapping(self, ui_content, width, height):  # type: ignore[no-untyped-def]
        self.horizontal_scroll = 0
        self.vertical_scroll_2 = 0
        total = ui_content.line_count
        if total <= 0 or width <= 0 or height <= 0:
            self.vertical_scroll = 0
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


class SlashCommandCompleter(Completer):
    """Completion menu for the input box: typing "/" lists every command
    from `ui.commands.SLASH_COMMANDS` (the same source `/help` renders from,
    so the two can't drift apart); typing "/workflow " lists its
    subcommands from `WORKFLOW_SUBCOMMANDS`. Returns nothing for anything
    else, so it's invisible while typing a normal prompt.
    """

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


class ChatApp:
    """Owns the full-screen layout, input handling, and turn dispatch."""

    def __init__(self, orchestrator: Orchestrator, initial_prompt: str | None = None) -> None:
        self.orchestrator = orchestrator
        self.capture = _ConsoleCapture()
        self.output = _OutputPane()
        self._history = InMemoryHistory()
        self._thinking = {"flag": False, "start": 0.0, "gerund": "", "last_swap": 0.0}
        self._cancel_event: threading.Event | None = None
        self._approving: dict = {"flag": False, "prompt": "", "result": False, "event": None}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._initial_prompt = (initial_prompt or "").strip()
        self._pastes: dict[str, str] = {}
        self._paste_counter = 0
        self._deepreid_running = False

        self._buf = Buffer(
            history=self._history,
            multiline=False,
            read_only=Condition(lambda: self._approving["flag"]),
            completer=SlashCommandCompleter(),
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

    def start(self) -> None:
        if self.orchestrator.state is None:
            self.orchestrator.start_session(title="interactive")
        self._append_output(render.banner)

    def _init_palette(self) -> None:
        if self._palette is not None:
            return
        from pathlib import Path
        storage_root = self.orchestrator.config.storage_root or (Path.home() / ".reidx")
        db = ProviderDatabase(Path(storage_root))
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
                (" "),
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

        render.print_assistant(result["text"])
        self.output.append_static(self.capture.drain())

        if self.app.is_running:
            self.app.invalidate()

    # --- scrolling (mouse wheel only) ---------------------------------------

    def _scroll_up(self) -> None:
        self.output.scroll_up(3)
        self.app.invalidate()

    def _scroll_down(self) -> None:
        self.output.scroll_down(3)
        self.app.invalidate()

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
            "tokens_used": self._estimate_tokens(),
            "context_window": context_window_for(st.session.model),
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

    def _build_status_fragments(self):  # type: ignore[no-untyped-def]
        status = self._status()
        window = status.get("context_window", 0)
        used = status.get("tokens_used", 0)
        pct = f"{(used / window * 100):.0f}%" if window else "—"
        usage = f"{fmt_tokens(used)}/{fmt_tokens(window)} ({pct})" if window else fmt_tokens(used)
        mode = status.get("mode", "—")
        mode_color = _MODE_COLOR.get(mode, "#9e9e9e")
        sep = ("#6c6c6c", "  ·  ")
        frags = [
            ("#ff5f5f bold", f"  {APP_NAME}"), sep,
            (f"{mode_color} bold", mode), sep,
            ("#9e9e9e", status.get("model", "—")), sep,
            ("#9e9e9e", f"effort:{status.get('effort', '—')}"), sep,
            ("#9e9e9e", usage), sep,
            ("#9e9e9e", short_path(status.get("workspace", "—"))), sep,
            ("#9e9e9e", f"{status.get('tasks', 0)} tasks"),
        ]
        if not self.output.pinned:
            frags += [sep, ("#ffd75f bold", "scrolled ↑ (scroll down to return)")]
        return frags

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
        frags = [
            ("#ff5f5f", f"  {star} "),
            ("#ff5f5f", f"{self._thinking['gerund']}… "),
            ("#9e9e9e", f"({elapsed}s"),
            ("#9e9e9e", f" · ↑ {fmt_tokens(self._estimate_tokens())} tokens"),
            ("#9e9e9e", ")"),
        ]
        return frags

    # --- layout --------------------------------------------------------

    def _build_layout(self) -> Layout:
        output_window = _OutputWindow(
            self.output,
            content=_ScrollableOutputControl(
                self.output.get_fragments,
                on_scroll_up=self._scroll_up,
                on_scroll_down=self._scroll_down,
                focusable=False,
            ),
            wrap_lines=True,
        )
        spinner_window = Window(content=FormattedTextControl(self._spinner_fragments), height=1)

        # Box border/caret color is a callable, not a static style, so it
        # re-evaluates every render — that's what makes it turn green live as
        # soon as the buffer starts with a DeepReid trigger word.
        def corner(ch: str) -> Window:
            return Window(FormattedTextControl(lambda: [(self._box_color(), ch)]), width=1, height=1)

        def hline() -> Window:
            return Window(char="─", style=self._box_color, height=1)

        input_window = Window(BufferControl(buffer=self._buf), wrap_lines=False, height=1)

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
        status_window = Window(content=FormattedTextControl(self._status_fragments), height=1)

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
            if buf.complete_state is not None:
                # A completion menu is open — Enter accepts the highlighted
                # entry (or just closes the menu if nothing's highlighted
                # yet), matching every other tool's "/" menu. It does not
                # submit; that needs a second Enter once the text is filled in.
                completion = buf.complete_state.current_completion
                if completion is not None:
                    buf.apply_completion(completion)
                else:
                    buf.cancel_completion()
                return
            await self._on_submit()

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
        def _clear_line(event) -> None:  # type: ignore[no-untyped-def]
            if self._buf.text:
                self._buf.reset()

        @kb.add("c-d", filter=~is_palette)
        def _exit(event) -> None:  # type: ignore[no-untyped-def]
            self.app.exit(result=0)

        @kb.add("c-o", filter=~is_palette)
        def _toggle_collapse(event) -> None:  # type: ignore[no-untyped-def]
            self.output.toggle_expanded()
            self.app.invalidate()

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

        @kb.add("left", filter=is_buffer_empty & ~is_thinking & ~is_approving & ~is_palette)
        def _effort_prev(event) -> None:  # type: ignore[no-untyped-def]
            self._cycle_effort(-1)

        @kb.add("right", filter=is_buffer_empty & ~is_thinking & ~is_approving & ~is_palette)
        def _effort_next(event) -> None:  # type: ignore[no-untyped-def]
            self._cycle_effort(1)

        return kb

    def _cycle_effort(self, delta: int) -> None:
        if self.orchestrator.state is None:
            return
        session = self.orchestrator.state.session
        try:
            idx = _EFFORT_LEVELS.index(session.reasoning_effort)
        except ValueError:
            idx = 0
        session.reasoning_effort = _EFFORT_LEVELS[(idx + delta) % len(_EFFORT_LEVELS)]
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
        self._buf.reset()
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
            log.exception("deepreid failed")
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
            elif outcome.startswith("workflow-run:"):
                await self._run_workflow(outcome.split(":", 1)[1])
            return

        self._append_output(lambda: render.print_user(text))
        self._thinking["flag"] = True
        self._thinking["start"] = time.monotonic()
        self._thinking["gerund"] = random.choice(_GERUNDS)
        self._thinking["last_swap"] = self._thinking["start"]
        self.app.invalidate()

        cancel_event = threading.Event()
        self._cancel_event = cancel_event
        approver = self._make_approver()
        assert self._loop is not None
        try:
            result = await self._loop.run_in_executor(
                None,
                functools.partial(
                    self.orchestrator.submit_task, text, approver=approver, cancel=cancel_event.is_set
                ),
            )
        except Exception as exc:  # noqa: BLE001 - the TUI must not die on runtime errors
            log.exception("turn failed")
            error_text = str(exc)
            self._append_output(lambda: render.print_error(error_text))
        else:
            seconds = int(time.monotonic() - self._thinking["start"])
            self._emit_turn_result(result, seconds)
        finally:
            self._thinking["flag"] = False
            self._cancel_event = None
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
    """
    chat = ChatApp(orchestrator, initial_prompt=initial_prompt)
    original_console = render.console
    render.console = chat.capture.console
    try:
        chat.start()
        code = asyncio.run(chat.main())
    finally:
        render.console = original_console
    render.console.print(Text("bye.", style="dim"))
    return code or 0
