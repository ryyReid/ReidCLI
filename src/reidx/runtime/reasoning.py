"""Chain-of-thought support for prompt-based reasoning.

The model is asked (via the agent's system prompt) to wrap its step-by-step
reasoning in <think>…</think> tags before the final answer. `split_reasoning`
separates that reasoning from the answer so the UI can show the thinking above
the response and keep it out of the stored transcript (thinking is ephemeral).

This is prompt-based CoT — it works with any instruction-following model behind
the provider, including OpenAI-family models served through the Reidchat proxy,
which have no native Anthropic "thinking" content blocks.

Many models (esp. smaller / non-Claude) ignore the tags and still emit a short
internal monologue as the first paragraph. A conservative heuristic catches
that so it still lands in the collapsible thinking UI instead of the answer.
"""
from __future__ import annotations

import re

# Appended to the agent's system prompt to elicit the reasoning block. This is
# the "medium" tier and also the fallback for any unrecognized effort value.
COT_SYSTEM_SUFFIX = (
    "\n\nAlways begin your reply with concise, first-person step-by-step reasoning "
    "enclosed in <think> and </think> tags. Put nothing but that reasoning between "
    "the tags — never put reasoning as plain text above the answer. "
    "After the closing </think> tag, write your final answer for the user. "
    "Example:\n"
    "<think>User greeted me; no tools needed; reply briefly.</think>\n"
    "Hello! How can I help?"
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
        "block (1-2 sentences) when a request is genuinely ambiguous or multi-step. "
        "Never write bare reasoning outside the tags."
    ),
    "medium": COT_SYSTEM_SUFFIX,
    "high": (
        "\n\nAlways begin your reply with thorough, first-person step-by-step "
        "reasoning enclosed in <think> and </think> tags — work through the request "
        "carefully, note relevant constraints, and consider your approach before "
        "answering. Put nothing but that reasoning between the tags. After the "
        "closing </think> tag, write your final answer for the user. "
        "Never put reasoning as plain text outside the tags."
    ),
    "xhigh": (
        "\n\nAlways begin your reply with exhaustive, first-person step-by-step "
        "reasoning enclosed in <think> and </think> tags — consider multiple "
        "approaches, weigh tradeoffs, double-check your logic, and note edge cases "
        "before settling on an answer. Put nothing but that reasoning between the "
        "tags. After the closing </think> tag, write your final answer for the user. "
        "Never put reasoning as plain text outside the tags."
    ),
}


def system_prompt_suffix(effort: str) -> str:
    """Suffix for the agent system prompt.

    `effort` should already be resolved (low|medium|high|xhigh). Passing
    `auto` falls back to medium — callers should resolve via effort_auto first.
    """
    if effort == "auto":
        return EFFORT_SYSTEM_SUFFIXES["medium"]
    return EFFORT_SYSTEM_SUFFIXES.get(effort, COT_SYSTEM_SUFFIX)


# <think>…</think> or <thinking>…</thinking>, case-insensitive, multiline.
_THINK_RE = re.compile(r"<think(?:ing)?>(.*?)</think(?:ing)?>", re.DOTALL | re.IGNORECASE)

# Alternate formats some models use when they ignore the preferred tags.
# Order matters: more specific patterns first.
_ALT_THINK_RES = (
    # GLM / tool-style: <parameter name="reasoning">…</parameter>
    # (also name='reasoning', name=reasoning without quotes)
    re.compile(
        r"<\s*parameter\s+name\s*=\s*[\"']?(?:reasoning|thinking|thought|scratchpad)[\"']?\s*>"
        r"(.*?)"
        r"<\s*/\s*parameter\s*>",
        re.DOTALL | re.IGNORECASE,
    ),
    # <reasoning>…</reasoning> / <thought>…</thought>
    re.compile(
        r"<\s*(?:reasoning|thought|scratchpad)\s*>(.*?)<\s*/\s*(?:reasoning|thought|scratchpad)\s*>",
        re.DOTALL | re.IGNORECASE,
    ),
    # ```think / ```thinking fenced blocks
    re.compile(r"```(?:think|thinking|reasoning)\s*\n(.*?)```", re.DOTALL | re.IGNORECASE),
    # <redacted_reasoning> style (common in some OSS finetunes)
    re.compile(
        r"<redacted_reasoning>(.*?)</redacted_reasoning>",
        re.DOTALL | re.IGNORECASE,
    ),
    # <|think|>…<|/think|> or similar
    re.compile(r"<\|think\|>(.*?)<\|/?think\|>", re.DOTALL | re.IGNORECASE),
)

# Cues that a short first paragraph is internal monologue, not the user answer.
_MONOLOGUE_CUES = (
    "no tool",
    "tool use",
    "tools needed",
    "user just",
    "the user said",
    "the user asked",
    "user greeted",
    "user said",
    "i need to",
    "i should",
    "let me ",
    "no need to",
    "don't need",
    "do not need",
    "simple request",
    "simple greeting",
    "just a greeting",
    "just say",
    "respond with",
    "reply with",
    "planning:",
    "approach:",
    "my plan",
)


def _looks_like_internal_monologue(para: str) -> bool:
    """True for short meta/planning lines models dump without <think> tags."""
    p = (para or "").strip()
    if not p or len(p) > 320:
        return False
    # Real multi-paragraph answers often start with a long first sentence.
    if p.count(". ") >= 3 and len(p) > 180:
        return False
    low = p.lower()
    return any(cue in low for cue in _MONOLOGUE_CUES)


def _split_untagged_monologue(text: str) -> tuple[str | None, str]:
    """If the model put reasoning as plain text above a blank line + answer."""
    # Require a blank-line split so we never steal a single-paragraph answer.
    parts = re.split(r"\n\s*\n", text.strip(), maxsplit=1)
    if len(parts) != 2:
        return None, text.strip()
    head, tail = parts[0].strip(), parts[1].strip()
    if not head or not tail:
        return None, text.strip()
    if not _looks_like_internal_monologue(head):
        return None, text.strip()
    # Tail should look like the user-facing reply (not another meta line only).
    if len(tail) < 2:
        return None, text.strip()
    return head, tail


def _clean_answer(text: str) -> str:
    """Trim whitespace and leading BOM left after stripping a reasoning block."""
    return (text or "").replace("\ufeff", "").strip()


def split_reasoning(text: str) -> tuple[str | None, str]:
    """Split model output into (reasoning, answer).

    Returns (None, text) when there is no well-formed reasoning block, so a model
    that ignores the format never has its answer hidden. Whitespace is trimmed.

    Order:
      1. Explicit <think>/<thinking> tags
      2. Alternate formats (incl. GLM ``<parameter name="reasoning">``)
      3. Heuristic: short monologue paragraph + blank line + answer
    """
    if not text:
        return None, text

    # Drop a leading BOM so tag matchers see the real first character.
    text = text.lstrip("\ufeff")

    match = _THINK_RE.search(text)
    if match is not None:
        thinking = match.group(1).strip()
        answer = _clean_answer(text[: match.start()] + text[match.end() :])
        return (thinking or None), answer

    for alt in _ALT_THINK_RES:
        m = alt.search(text)
        if m is not None:
            thinking = m.group(1).strip()
            answer = _clean_answer(text[: m.start()] + text[m.end() :])
            if thinking:
                return thinking, answer

    return _split_untagged_monologue(text)
