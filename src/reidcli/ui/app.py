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
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import ANSI, to_formatted_text
from prompt_toolkit.formatted_text.utils import split_lines
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, VSplit, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.mouse_events import MouseEvent, MouseEventType
from prompt_toolkit.styles import Style
from rich.console import Console
from rich.text import Text

from reidcli.diagnostics.logger import get_logger
from reidcli.runtime.orchestrator import Orchestrator
from reidcli.ui import render
from reidcli.ui.commands import handle as handle_command
from reidcli.ui.render import _GERUNDS, _STAR_FRAMES, _bullet_grid
from reidcli.ui.theme import (
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

log = get_logger("reidcli.ui")

# prompt_toolkit style classes for the box-drawing chrome (borders/caret).
_STYLE = Style.from_dict({"box": "#ff5f5f", "caret": "#ff5f5f bold"})

_MODE_COLOR = {
    "strict": "#ff5555",
    "balanced": "#ffd75f",
    "autonomous": "#5fd75f",
    "custom": "#d75fd7",
}


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

    def get_fragments(self):  # type: ignore[no-untyped-def]
        lines = list(split_lines(self._all_fragments()))
        total = len(lines)
        if total == 0:
            return [("[SetCursorPosition]", "")]

        target = (total - 1) if self.pinned else min(self._cursor_line, total - 1)
        out: list = []
        for i, line in enumerate(lines):
            if i == target:
                out.append(("[SetCursorPosition]", ""))
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


class ChatApp:
    """Owns the full-screen layout, input handling, and turn dispatch."""

    def __init__(self, orchestrator: Orchestrator, initial_prompt: str | None = None) -> None:
        self.orchestrator = orchestrator
        self.capture = _ConsoleCapture()
        self.output = _OutputPane()
        self._history = InMemoryHistory()
        self._thinking = {"flag": False, "start": 0.0, "gerund": "", "last_swap": 0.0}
        self._approving: dict = {"flag": False, "prompt": "", "result": False, "event": None}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._initial_prompt = (initial_prompt or "").strip()

        self._buf = Buffer(
            history=self._history,
            multiline=False,
            read_only=Condition(lambda: self._approving["flag"]),
        )
        self.app: Application = Application(
            layout=self._build_layout(),
            key_bindings=self._build_key_bindings(),
            style=_STYLE,
            full_screen=True,
            mouse_support=True,
        )

    # --- setup -----------------------------------------------------------

    def start(self) -> None:
        if self.orchestrator.state is None:
            self.orchestrator.start_session(title="interactive")
        self._append_output(render.banner)

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
            if self._thinking["flag"] or self._approving["flag"]:
                self.app.invalidate()

    # --- rendering bridge --------------------------------------------------

    def _append_output(self, fn: Callable[[], None]) -> None:
        fn()
        self.output.append_static(self.capture.drain())
        if self.app.is_running:
            self.app.invalidate()

    def _render_thinking_variants(self, text: str, seconds: int) -> tuple[str, str] | None:
        """Render both display variants of the chain-of-thought block once.

        Collapsed: a single grayed-out "Thought for Ns" header, matching the
        spinner's elapsed-time readout. Expanded: the same header plus the
        full thinking text beneath it. Neither variant is ever re-rendered —
        Ctrl+O just picks which was already captured.
        """
        if not text or not text.strip():
            return None
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
        thinking_variants = self._render_thinking_variants(result.get("thinking") or "", thinking_seconds)
        if thinking_variants is not None:
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
        output_window = Window(
            content=_ScrollableOutputControl(
                self.output.get_fragments,
                on_scroll_up=self._scroll_up,
                on_scroll_down=self._scroll_down,
                focusable=False,
            ),
            wrap_lines=True,
        )
        spinner_window = Window(content=FormattedTextControl(self._spinner_fragments), height=1)

        def corner(ch: str) -> Window:
            return Window(FormattedTextControl([("class:box", ch)]), width=1, height=1)

        def hline() -> Window:
            return Window(char="─", style="class:box", height=1)

        input_window = Window(BufferControl(buffer=self._buf), wrap_lines=False, height=1)

        box = HSplit(
            [
                VSplit([corner("╭"), hline(), corner("╮")], height=1),
                VSplit(
                    [
                        Window(FormattedTextControl([("class:box", "│")]), width=1, height=1),
                        Window(FormattedTextControl([("class:caret", " › ")]), width=3, height=1),
                        input_window,
                        Window(FormattedTextControl([("class:box", "│")]), width=1, height=1),
                    ],
                    height=1,
                ),
                VSplit([corner("╰"), hline(), corner("╯")], height=1),
            ]
        )
        status_window = Window(content=FormattedTextControl(self._status_fragments), height=1)

        root = HSplit([output_window, spinner_window, box, status_window])
        return Layout(root, focused_element=input_window)

    # --- input handling --------------------------------------------------

    def _build_key_bindings(self) -> KeyBindings:
        kb = KeyBindings()
        is_thinking = Condition(lambda: self._thinking["flag"])
        is_approving = Condition(lambda: self._approving["flag"])

        @kb.add("enter", filter=~is_thinking & ~is_approving)
        async def _submit(event) -> None:  # type: ignore[no-untyped-def]
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

        @kb.add("c-c")
        def _clear_line(event) -> None:  # type: ignore[no-untyped-def]
            if self._buf.text:
                self._buf.reset()

        @kb.add("c-d")
        def _exit(event) -> None:  # type: ignore[no-untyped-def]
            self.app.exit(result=0)

        @kb.add("c-o")
        def _toggle_collapse(event) -> None:  # type: ignore[no-untyped-def]
            self.output.toggle_expanded()
            self.app.invalidate()

        return kb

    async def _on_submit(self) -> None:
        text = self._buf.text
        if not text.strip():
            return
        self._buf.reset()
        await self._submit_text(text)

    async def _submit_text(self, text: str) -> None:
        """Run one turn for `text` — shared by the Enter key binding and by
        an injected initial prompt (`reidcli "<prompt>"` / piped stdin)."""
        if not text.strip():
            return

        if text.startswith("/"):
            outcome = self._run_slash(text)
            if outcome == "exit":
                self.app.exit(result=0)
            return

        self._append_output(lambda: render.print_user(text))
        self._thinking["flag"] = True
        self._thinking["start"] = time.monotonic()
        self._thinking["gerund"] = random.choice(_GERUNDS)
        self._thinking["last_swap"] = self._thinking["start"]
        self.app.invalidate()

        approver = self._make_approver()
        assert self._loop is not None
        try:
            result = await self._loop.run_in_executor(
                None, functools.partial(self.orchestrator.submit_task, text, approver=approver)
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
    `reidcli exec`, which runs one prompt headless and exits).
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
