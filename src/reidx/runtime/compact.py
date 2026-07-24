"""Conversation context compaction.

This is **not** the status-bar token meter (`1.7k/128k`). That readout only
estimates how full the model window is. Compaction actually rewrites the
in-memory (and on-disk) transcript: older turns become one short summary so
later turns fit and stay coherent.

Strategy:
  1. Keep the system message (rebuilt next turn anyway).
  2. Keep the last N *user turns* (each user message + following
     assistant/tool messages) verbatim.
  3. Fold everything between system and that tail into a summary block.
  4. Prefer an LLM summary; fall back to a local extractive summary if the
     provider is unavailable or fails.
"""
from __future__ import annotations

from dataclasses import dataclass

from reidx.diagnostics.logger import get_logger
from reidx.provider.base import BaseProvider, Message

log = get_logger("reidx.compact")

DEFAULT_KEEP_USER_TURNS = 4
MIN_MESSAGES_TO_COMPACT = 8
# Soft auto-compact trigger: fraction of the model's context window.
AUTO_COMPACT_RATIO = 0.72
MAX_SUMMARY_CHARS = 6_000
MAX_SOURCE_CHARS_FOR_LLM = 24_000

def context_window_for_model(model: str) -> int:
    """Window size for auto-compact threshold — same registry as the status bar."""
    from reidx.provider.context_windows import context_window_for

    return context_window_for(model)

_SUMMARY_SYSTEM = (
    "You compress conversation history for an AI coding agent. "
    "Write a dense factual summary the agent can continue from: goals, "
    "decisions, file paths touched, errors fixed, open TODOs, and user "
    "preferences. No preamble, no markdown headings, no tool-call dumps. "
    "Prefer short bullets. Stay under 400 words."
)


@dataclass
class CompactResult:
    before_count: int
    after_count: int
    before_tokens: int
    after_tokens: int
    kept_user_turns: int
    summary_chars: int
    method: str  # "llm" | "heuristic" | "skipped"
    detail: str = ""


def estimate_tokens(messages: list[Message]) -> int:
    """Rough token estimate (~4 chars/token). Good enough for thresholds."""
    total = 0
    for m in messages:
        total += len(m.content or "")
        for tc in m.tool_calls or []:
            total += len(tc.name) + len(str(tc.arguments))
    return max(1, total // 4) if messages else 0


def _user_turn_starts(messages: list[Message]) -> list[int]:
    """Indices of user messages that begin a turn (skip synthetic summary markers)."""
    starts: list[int] = []
    for i, m in enumerate(messages):
        if m.role == "user" and not (m.content or "").startswith("[Context summary]"):
            starts.append(i)
        elif m.role == "user" and (m.content or "").startswith("[Context summary]"):
            # Prior compaction already inserted a summary — treat as boundary, not a turn.
            continue
    return starts


def partition_for_compact(
    messages: list[Message],
    *,
    keep_user_turns: int = DEFAULT_KEEP_USER_TURNS,
) -> tuple[list[Message], list[Message], list[Message]]:
    """Split into (prefix_keep, middle_to_summarize, tail_keep).

    prefix_keep is usually the system message only.
    """
    if not messages:
        return [], [], []

    prefix: list[Message] = []
    rest = list(messages)
    if rest[0].role == "system":
        prefix = [rest[0]]
        rest = rest[1:]

    starts = _user_turn_starts(rest)
    if len(starts) <= keep_user_turns:
        return prefix, [], rest

    cut = starts[-keep_user_turns]
    middle = rest[:cut]
    tail = rest[cut:]
    return prefix, middle, tail


def heuristic_summary(messages: list[Message]) -> str:
    """Local extractive summary — no network. Always available."""
    lines: list[str] = []
    for m in messages:
        role = m.role
        text = (m.content or "").strip().replace("\n", " ")
        if not text and not m.tool_calls:
            continue
        if role == "system":
            continue
        if role == "user":
            lines.append(f"- User: {text[:240]}")
        elif role == "assistant":
            if m.tool_calls:
                names = ", ".join(tc.name for tc in m.tool_calls)
                snippet = text[:120] + ("…" if len(text) > 120 else "")
                lines.append(f"- Assistant called tools [{names}]" + (f": {snippet}" if snippet else ""))
            elif text:
                lines.append(f"- Assistant: {text[:200]}")
        elif role == "tool":
            lines.append(f"- Tool result: {text[:160]}")
        if len(lines) >= 40:
            lines.append("- …(earlier detail truncated)")
            break
    if not lines:
        return "Earlier conversation had little textual content."
    body = "\n".join(lines)
    if len(body) > MAX_SUMMARY_CHARS:
        body = body[: MAX_SUMMARY_CHARS - 20] + "\n- …(truncated)"
    return body


def llm_summary(
    provider: BaseProvider,
    messages: list[Message],
    *,
    model: str | None = None,
) -> str | None:
    """Ask the active provider to summarize middle messages. None on failure."""
    blob_parts: list[str] = []
    size = 0
    for m in messages:
        chunk = f"{m.role.upper()}: {(m.content or '').strip()}"
        if m.tool_calls:
            names = ", ".join(tc.name for tc in m.tool_calls)
            chunk += f" [tools: {names}]"
        if size + len(chunk[:1500]) > MAX_SOURCE_CHARS_FOR_LLM:
            blob_parts.append("…(earlier messages omitted for length)")
            break
        blob_parts.append(chunk[:1500])
        size += len(chunk[:1500])
    blob = "\n\n".join(blob_parts)
    if not blob.strip():
        return None
    try:
        resp = provider.chat(
            [
                Message(role="system", content=_SUMMARY_SYSTEM),
                Message(
                    role="user",
                    content=(
                        "Summarize the following earlier conversation so a coding "
                        "agent can continue without the raw transcript:\n\n"
                        f"{blob}"
                    ),
                ),
            ],
            tools=None,
            model=model,
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("llm summary failed: %s", exc)
        return None
    text = (resp.text or "").strip()
    if not text or text.startswith("[stub]"):
        # Stub just echoes — not useful as a summary.
        return None
    if len(text) > MAX_SUMMARY_CHARS:
        text = text[: MAX_SUMMARY_CHARS - 20] + "\n…"
    return text


def build_compacted_messages(
    prefix: list[Message],
    summary: str,
    tail: list[Message],
) -> list[Message]:
    """Assemble the new transcript: system + summary exchange + recent tail."""
    out = list(prefix)
    out.append(
        Message(
            role="user",
            content=(
                "[Context summary] Earlier turns were compacted to save context. "
                "Use this as ground truth for prior work; do not re-ask for it.\n\n"
                f"{summary.strip()}"
            ),
        )
    )
    out.append(
        Message(
            role="assistant",
            content=(
                "Understood — I have the compacted context summary and will "
                "continue from the recent messages below."
            ),
        )
    )
    out.extend(tail)
    return out


def compact_messages(
    messages: list[Message],
    *,
    provider: BaseProvider | None = None,
    model: str | None = None,
    keep_user_turns: int = DEFAULT_KEEP_USER_TURNS,
    force: bool = False,
    prefer_llm: bool = True,
) -> tuple[list[Message], CompactResult]:
    """Compact `messages`. Returns (new_messages, result).

    If there is nothing useful to compact, returns the original list with
    method="skipped".
    """
    before_n = len(messages)
    before_tok = estimate_tokens(messages)
    keep_user_turns = max(1, keep_user_turns)

    if before_n < MIN_MESSAGES_TO_COMPACT and not force:
        return messages, CompactResult(
            before_count=before_n,
            after_count=before_n,
            before_tokens=before_tok,
            after_tokens=before_tok,
            kept_user_turns=keep_user_turns,
            summary_chars=0,
            method="skipped",
            detail=f"need at least {MIN_MESSAGES_TO_COMPACT} messages (have {before_n}); use /compact --force",
        )

    prefix, middle, tail = partition_for_compact(messages, keep_user_turns=keep_user_turns)
    if not middle:
        return messages, CompactResult(
            before_count=before_n,
            after_count=before_n,
            before_tokens=before_tok,
            after_tokens=before_tok,
            kept_user_turns=keep_user_turns,
            summary_chars=0,
            method="skipped",
            detail=f"nothing older than the last {keep_user_turns} user turn(s) to compact",
        )

    method = "heuristic"
    summary = ""
    if prefer_llm and provider is not None:
        llm = llm_summary(provider, middle, model=model)
        if llm:
            summary = llm
            method = "llm"
    if not summary:
        summary = heuristic_summary(middle)
        method = "heuristic"

    new_msgs = build_compacted_messages(prefix, summary, tail)
    after_tok = estimate_tokens(new_msgs)
    return new_msgs, CompactResult(
        before_count=before_n,
        after_count=len(new_msgs),
        before_tokens=before_tok,
        after_tokens=after_tok,
        kept_user_turns=keep_user_turns,
        summary_chars=len(summary),
        method=method,
        detail=f"summarized {len(middle)} message(s) → {len(summary)} chars ({method})",
    )


def should_auto_compact(
    messages: list[Message],
    *,
    context_window: int,
    ratio: float = AUTO_COMPACT_RATIO,
) -> bool:
    """True when estimated tokens exceed `ratio` of the context window."""
    if context_window <= 0 or len(messages) < MIN_MESSAGES_TO_COMPACT:
        return False
    return estimate_tokens(messages) >= int(context_window * ratio)
