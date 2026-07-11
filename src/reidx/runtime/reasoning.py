"""Chain-of-thought support for prompt-based reasoning.

The model is asked (via the agent's system prompt) to wrap its step-by-step
reasoning in <think>…</think> tags before the final answer. `split_reasoning`
separates that reasoning from the answer so the UI can show the thinking above
the response and keep it out of the stored transcript (thinking is ephemeral).

This is prompt-based CoT — it works with any instruction-following model behind
the provider, including OpenAI-family models served through the Reidchat proxy,
which have no native Anthropic "thinking" content blocks.
"""
from __future__ import annotations

import re

# Appended to the agent's system prompt to elicit the reasoning block. This is
# the "medium" tier and also the fallback for any unrecognized effort value.
COT_SYSTEM_SUFFIX = (
    "\n\nAlways begin your reply with concise, first-person step-by-step reasoning "
    "enclosed in <think> and </think> tags. Put nothing but that reasoning between "
    "the tags. After the closing </think> tag, write your final answer for the user."
)

# Session.reasoning_effort -> system-prompt suffix. This is prompt-based, not a
# native provider reasoning-effort/thinking-budget parameter — no provider
# integration currently sends a structured effort/thinking API param, so this
# is the only lever available that works uniformly across providers.
# (Anthropic's Messages API does support native extended thinking via a
# `thinking` payload field, which would give sturdier, non-refusable reasoning
# output than this tag-based convention — deferred: it requires preserving
# `thinking` content blocks across multi-turn tool-call replays per Anthropic's
# docs, which needs verifying against a live API before shipping.)
EFFORT_SYSTEM_SUFFIXES: dict[str, str] = {
    "low": (
        "\n\nKeep internal reasoning minimal. For simple requests, skip the <think> "
        "block entirely and answer directly. Only use a brief <think>...</think> "
        "block (1-2 sentences) when a request is genuinely ambiguous or multi-step."
    ),
    "medium": COT_SYSTEM_SUFFIX,
    "high": (
        "\n\nAlways begin your reply with thorough, first-person step-by-step "
        "reasoning enclosed in <think> and </think> tags — work through the request "
        "carefully, note relevant constraints, and consider your approach before "
        "answering. Put nothing but that reasoning between the tags. After the "
        "closing </think> tag, write your final answer for the user."
    ),
    "xhigh": (
        "\n\nAlways begin your reply with exhaustive, first-person step-by-step "
        "reasoning enclosed in <think> and </think> tags — consider multiple "
        "approaches, weigh tradeoffs, double-check your logic, and note edge cases "
        "before settling on an answer. Put nothing but that reasoning between the "
        "tags. After the closing </think> tag, write your final answer for the user."
    ),
}


def system_prompt_suffix(effort: str) -> str:
    return EFFORT_SYSTEM_SUFFIXES.get(effort, COT_SYSTEM_SUFFIX)

# Matches <think>…</think> or <thinking>…</thinking>, case-insensitively, across lines.
_THINK_RE = re.compile(r"<think(?:ing)?>(.*?)</think(?:ing)?>", re.DOTALL | re.IGNORECASE)


def split_reasoning(text: str) -> tuple[str | None, str]:
    """Split model output into (reasoning, answer).

    Returns (None, text) when there is no well-formed reasoning block, so a model
    that ignores the format never has its answer hidden. Whitespace is trimmed.
    """
    if not text:
        return None, text
    match = _THINK_RE.search(text)
    if match is None:
        return None, text.strip()
    thinking = match.group(1).strip()
    answer = (text[: match.start()] + text[match.end():]).strip()
    return (thinking or None), answer
