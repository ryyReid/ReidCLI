"""Centralized theme: colors, styles, and visual constants.

ReidX wears a Claude-Code-style skin in a red palette:
  - dark technical feel, high contrast
  - rounded borders, ⏺ bullets, ⎿ tree connectors
  - clean density over decorative excess

All UI modules should pull colors + glyphs from here so the look stays consistent.
"""
from __future__ import annotations

from pathlib import Path

from rich import box
from rich.text import Text

# Brand.
APP_NAME = "ReidX"

# Core palette — red-forward, high-contrast.
PRIMARY = "#ff5f5f"       # brand, assistant, headers  (brand red)
SECONDARY = "#d75f5f"     # secondary accents
DIM = "dim"               # labels, metadata
SUCCESS = "green"         # ok, completed
WARN = "yellow"           # active, prompts, warnings
DANGER = "red"            # errors, failed, blocked
MUTED = "magenta"         # special states

# Claude-Code-style glyphs. Use text-presentation code points (not emoji) so the
# terminal renders them flat and honors the Rich style color. U+23FA (⏺) is an
# emoji and renders as a fixed-color double-width blob — avoid it.
SPARKLE = "✻"             # welcome banner + thinking mark
BULLET = "●"              # assistant / tool-call bullet  (U+25CF, text circle)
TREE = "⎿"                # tool-result connector
PROMPT = "›"              # input prompt caret

# Default border box for panels/tables.
BOX = box.ROUNDED

# Role icons for transcript display.
ROLE_ICON = {
    "system": "§",
    "user": "›",
    "assistant": BULLET,
    "tool": TREE,
}

ROLE_STYLE = {
    "system": DIM,
    "user": "bold",
    "assistant": PRIMARY,
    "tool": SUCCESS,
}

# Status badge styles.
STATUS_STYLE = {
    "draft": DIM,
    "completed": SUCCESS,
    "active": WARN,
    "failed": DANGER,
    "blocked": MUTED,
    "pending": DIM,
    "skipped": DIM,
    "archived": DIM,
    "abandoned": DIM,
}

# Risk colors.
RISK_STYLE = {
    "low": SUCCESS,
    "medium": WARN,
    "high": DANGER,
}

# Permission mode colors.
MODE_STYLE = {
    "strict": DANGER,
    "balanced": WARN,
    "autonomous": SUCCESS,
    "custom": MUTED,
}


# Visual constants.
MAX_WIDTH = 80  # constrain panels/tables so they don't span ultra-wide terminals

# Known context-window sizes (tokens) by model prefix, for the status bar's
# usage readout. Falls back to DEFAULT_CONTEXT_WINDOW for unrecognized models.
DEFAULT_CONTEXT_WINDOW = 128_000
CONTEXT_WINDOWS = {
    "stub": 32_000,
    "gpt-5": 400_000,
    "gpt-4": 128_000,
    "claude": 200_000,
    "o1": 200_000,
    "o3": 200_000,
}


def context_window_for(model: str) -> int:
    for prefix, size in CONTEXT_WINDOWS.items():
        if model.startswith(prefix):
            return size
    return DEFAULT_CONTEXT_WINDOW


def fmt_tokens(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def short_path(path: str, keep: int = 2) -> str:
    """Collapse a long workspace path to its last `keep` components."""
    parts = Path(path).parts
    if len(parts) <= keep:
        return path
    return str(Path(*parts[-keep:]))


def badge(text: str, color: str) -> Text:
    """A compact colored badge like [ok] or [strict]."""
    return Text(f"[{text}]", style=color)


def label_value(label: str, value: str, label_color: str = DIM) -> Text:
    """A dim-label followed by a value:  session abc123."""
    return Text.assemble((f"{label} ", label_color), value)
