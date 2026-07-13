"""Host-terminal compatibility (PowerShell, Windows Terminal, Oh My Posh, etc.).

Goals:
  - Let the user's terminal *theme* (background, scheme, prompt fonts) keep
    working — ReidX draws with transparent backgrounds and restores host
    state on exit.
  - Use UTF-8 only for the lifetime of a ReidX session, then put stdout /
    stderr encodings back so the parent PowerShell / profile theme is not
    stuck on a forced code page.
  - Enable VT/ANSI processing on Windows without leaving the console in a
    broken mode after exit.
  - Honour NO_COLOR / REIDX_COLOR so hosts that prefer 16/256-colour
    schemes still look right.
"""
from __future__ import annotations

import os
import sys
from types import TracebackType
from typing import TextIO


def color_system_for_host() -> str | None:
    """Rich color_system string that respects the host, not a forced theme.

    - NO_COLOR / REIDX_COLOR=none  → no colour
    - REIDX_COLOR=truecolor|256|16|auto
    - default: auto (Rich probes the terminal — works with Windows Terminal
      schemes and PowerShell profiles)
    """
    if os.environ.get("NO_COLOR", "").strip():
        return None
    choice = os.environ.get("REIDX_COLOR", "auto").strip().lower()
    if choice in ("none", "off", "0", "false", "no"):
        return None
    if choice in ("truecolor", "24bit", "true"):
        return "truecolor"
    if choice in ("256", "8bit"):
        return "256"
    if choice in ("16", "standard", "ansi"):
        return "standard"
    # "auto" or anything else — let Rich detect (keeps host theme usable).
    return "auto"


def _stream_encoding(stream: TextIO) -> str | None:
    return getattr(stream, "encoding", None)


def _set_stream_encoding(stream: TextIO, encoding: str, errors: str = "replace") -> bool:
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:
        return False
    try:
        reconfigure(encoding=encoding, errors=errors)
        return True
    except (AttributeError, ValueError, OSError):
        return False


class _WindowsConsoleState:
    """Save/restore Windows console mode + input/output code pages."""

    def __init__(self) -> None:
        self._saved = False
        self._out_mode = 0
        self._in_mode = 0
        self._out_cp = 0
        self._in_cp = 0
        self._kernel32 = None
        self._out_handle = None
        self._in_handle = None

    def enter(self) -> None:
        if sys.platform != "win32":
            return
        try:
            import ctypes
            from ctypes import wintypes
        except ImportError:
            return

        kernel32 = ctypes.windll.kernel32
        # STD_OUTPUT_HANDLE = -11, STD_INPUT_HANDLE = -10
        out_h = kernel32.GetStdHandle(wintypes.DWORD(-11 & 0xFFFFFFFF))
        in_h = kernel32.GetStdHandle(wintypes.DWORD(-10 & 0xFFFFFFFF))
        if not out_h or out_h == wintypes.HANDLE(-1).value:
            return

        out_mode = wintypes.DWORD()
        in_mode = wintypes.DWORD()
        if not kernel32.GetConsoleMode(out_h, ctypes.byref(out_mode)):
            return

        self._kernel32 = kernel32
        self._out_handle = out_h
        self._in_handle = in_h
        self._out_mode = out_mode.value
        self._out_cp = kernel32.GetConsoleOutputCP()
        self._in_cp = kernel32.GetConsoleCP()
        if in_h and kernel32.GetConsoleMode(in_h, ctypes.byref(in_mode)):
            self._in_mode = in_mode.value
        self._saved = True

        # ENABLE_PROCESSED_OUTPUT | ENABLE_WRAP_AT_EOL_OUTPUT |
        # ENABLE_VIRTUAL_TERMINAL_PROCESSING (0x4 | 0x2 | 0x1 = already often on;
        # VT flag is 0x0004 for input? Output VT = 0x0004)
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        ENABLE_PROCESSED_OUTPUT = 0x0001
        ENABLE_WRAP_AT_EOL_OUTPUT = 0x0002
        new_out = self._out_mode | ENABLE_VIRTUAL_TERMINAL_PROCESSING | ENABLE_PROCESSED_OUTPUT | ENABLE_WRAP_AT_EOL_OUTPUT
        kernel32.SetConsoleMode(out_h, new_out)
        # Prefer UTF-8 code page for glyphs while we run; restored on exit so
        # PowerShell / Oh My Posh keep their prior CP (often 65001 already).
        try:
            kernel32.SetConsoleOutputCP(65001)
            kernel32.SetConsoleCP(65001)
        except Exception:  # noqa: BLE001
            pass

    def exit(self) -> None:
        if not self._saved or self._kernel32 is None:
            return
        k = self._kernel32
        try:
            if self._out_handle:
                k.SetConsoleMode(self._out_handle, self._out_mode)
            if self._in_handle and self._in_mode:
                k.SetConsoleMode(self._in_handle, self._in_mode)
            if self._out_cp:
                k.SetConsoleOutputCP(self._out_cp)
            if self._in_cp:
                k.SetConsoleCP(self._in_cp)
        except Exception:  # noqa: BLE001
            pass
        self._saved = False


class TerminalHostSession:
    """Context manager: prepare the host for ReidX, then restore it.

    Safe to nest; only the outermost session restores. Designed so people can
    keep Windows Terminal colour schemes, PowerShell profiles, and Oh My Posh
    themes after exiting `reid`.
    """

    _depth = 0

    def __init__(self) -> None:
        self._stdout_enc: str | None = None
        self._stderr_enc: str | None = None
        self._win = _WindowsConsoleState()
        self._active = False

    def __enter__(self) -> TerminalHostSession:
        type(self)._depth += 1
        if type(self)._depth > 1:
            return self
        self._stdout_enc = _stream_encoding(sys.stdout)
        self._stderr_enc = _stream_encoding(sys.stderr)
        # Scoped UTF-8 so UI glyphs work; restored in __exit__ for the host shell.
        _set_stream_encoding(sys.stdout, "utf-8")
        _set_stream_encoding(sys.stderr, "utf-8")
        self._win.enter()
        self._active = True
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        type(self)._depth = max(0, type(self)._depth - 1)
        if type(self)._depth > 0 or not self._active:
            return None
        # Drop any residual SGR without clearing the screen — leaves the
        # user's prompt theme free to paint next.
        try:
            if sys.stdout.isatty():
                sys.stdout.write("\033[0m")
                sys.stdout.flush()
        except Exception:  # noqa: BLE001
            pass
        self._win.exit()
        if self._stdout_enc:
            _set_stream_encoding(sys.stdout, self._stdout_enc)
        if self._stderr_enc:
            _set_stream_encoding(sys.stderr, self._stderr_enc)
        self._active = False
        return None
