"""Automatic reasoning-effort selection for `/effort auto`.

Maps prompt complexity → low | medium | high (never xhigh unless the user
picks it manually). Free heuristic — no extra API call.

  simple  → low     (greetings, short Qs)
  medium  → medium  (normal coding / fixes)
  hard    → high    (plans, architecture, multi-file, long prompts)
"""
from __future__ import annotations

import re

EFFORT_LEVELS = ("auto", "low", "medium", "high", "xhigh")
# Levels that can be cycled / set explicitly (includes auto).
MANUAL_EFFORTS = ("low", "medium", "high", "xhigh")

_HARD_RE = [
    re.compile(p, re.I)
    for p in (
        r"\b(plan|planning|architecture|architect|design system|refactor)\b",
        r"\b(multi[- ]?file|codebase|migrate|migration|overhaul)\b",
        r"\b(implement|build out|from scratch|end[- ]to[- ]end)\b",
        r"\b(debug|root cause|investigate|performance|optimize)\b",
        r"\b(deepreid|deep ?read|review pr|pull request)\b",
        r"\b(security audit|threat model|red team)\b",
        r"\b(write tests?|test suite|ci/cd)\b",
    )
]
_SIMPLE_RE = [
    re.compile(p, re.I)
    for p in (
        r"^(hi|hello|hey|yo|sup|thanks|thank you|thx|ok|okay|yes|no|yep|nope|sure|wsp)\b",
        r"\b(what time|who are you|what model|which provider)\b",
        r"^(help|/help)\b",
        r"\b(ping|status|version)\b",
    )
]


def classify_prompt(user_input: str) -> str:
    """Return 'simple' | 'medium' | 'hard'."""
    text = (user_input or "").strip()
    if not text:
        return "simple"
    length = len(text)

    for rx in _HARD_RE:
        if rx.search(text):
            return "hard"
    if length > 800 or text.count("\n") >= 12:
        return "hard"
    if len(re.findall(r"(?m)^\s*\d+[\).\]]\s+\S", text)) >= 3:
        return "hard"

    for rx in _SIMPLE_RE:
        if rx.search(text):
            return "simple"
    if length <= 100 and not any(
        c in text.lower() for c in ("```", "def ", "class ", "error", "fix", "add ")
    ):
        if "?" in text and length < 60:
            return "simple"
        if length < 40:
            return "simple"

    if any(k in text.lower() for k in ("fix", "add ", "change", "update", "file", "function", "bug")):
        return "medium"
    if length > 280:
        return "medium"
    return "medium"


_COMPLEXITY_TO_EFFORT = {
    "simple": "low",
    "medium": "medium",
    "hard": "high",
}


def auto_effort_for(user_input: str) -> str:
    """Map a user prompt to low|medium|high when session effort is `auto`."""
    return _COMPLEXITY_TO_EFFORT[classify_prompt(user_input)]


def resolve_effort(session_effort: str, user_input: str = "") -> str:
    """Effective effort for this turn.

    `auto` → classified from the prompt; anything else returned as-is
    (unknown values fall through to medium via the CoT suffix table).
    """
    effort = (session_effort or "medium").strip().lower()
    if effort == "auto":
        return auto_effort_for(user_input)
    return effort
