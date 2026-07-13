"""Token cost estimation and session cost ledger.

Prices are USD per 1M tokens (input / output). Matched by longest model-id
fragment — same style as context_windows. Unknown models cost $0 (logged as
unpriced) so local/ollama runs don't invent charges.

Ledger is per-session JSONL under sessions/<id>/costs.jsonl plus in-memory
totals on RuntimeState.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# (fragment, input_usd_per_mtok, output_usd_per_mtok) — longest match wins.
# Approximate public list prices; update as vendors change.
_PRICE_TABLE: list[tuple[str, float, float]] = [
    # OpenAI
    ("gpt-4.1-nano", 0.10, 0.40),
    ("gpt-4.1-mini", 0.40, 1.60),
    ("gpt-4.1", 2.00, 8.00),
    ("gpt-4o-mini", 0.15, 0.60),
    ("gpt-4o", 2.50, 10.00),
    ("gpt-4-turbo", 10.00, 30.00),
    ("gpt-4", 30.00, 60.00),
    ("o4-mini", 1.10, 4.40),
    ("o3-mini", 1.10, 4.40),
    ("o3", 10.00, 40.00),
    ("o1-mini", 3.00, 12.00),
    ("o1", 15.00, 60.00),
    ("gpt-5", 5.00, 15.00),
    # Anthropic
    ("claude-opus-4", 15.00, 75.00),
    ("claude-sonnet-4", 3.00, 15.00),
    ("claude-haiku-4", 0.80, 4.00),
    ("claude-3-7-sonnet", 3.00, 15.00),
    ("claude-3-5-sonnet", 3.00, 15.00),
    ("claude-3-5-haiku", 0.80, 4.00),
    ("claude-3-opus", 15.00, 75.00),
    ("claude-3-sonnet", 3.00, 15.00),
    ("claude-3-haiku", 0.25, 1.25),
    ("claude", 3.00, 15.00),
    # DeepSeek (often very cheap via API)
    ("deepseek-v4-pro", 0.55, 2.19),
    ("deepseek-v4-flash", 0.14, 0.28),
    ("deepseek-v4", 0.55, 2.19),
    ("deepseek-chat", 0.14, 0.28),
    ("deepseek-reasoner", 0.55, 2.19),
    ("deepseek-r1", 0.55, 2.19),
    ("deepseek", 0.14, 0.28),
    # Google
    ("gemini-2.5-pro", 1.25, 10.00),
    ("gemini-2.5-flash", 0.15, 0.60),
    ("gemini-2.0-flash", 0.10, 0.40),
    ("gemini-1.5-pro", 1.25, 5.00),
    ("gemini-1.5-flash", 0.075, 0.30),
    ("gemini", 0.15, 0.60),
    # xAI
    ("grok-3", 3.00, 15.00),
    ("grok-2", 2.00, 10.00),
    ("grok", 2.00, 10.00),
    # Mistral
    ("mistral-large", 2.00, 6.00),
    ("mistral-small", 0.20, 0.60),
    ("mistral", 0.20, 0.60),
    # Meta / local-ish — treat hosted llama cheap; pure local still $0 via zero match below
    ("llama-3.3", 0.20, 0.20),
    ("llama-3.1-70b", 0.50, 0.50),
    ("llama-3.1-8b", 0.05, 0.05),
    ("llama-3.1", 0.20, 0.20),
    # Free / local
    ("stub", 0.0, 0.0),
    ("ollama", 0.0, 0.0),
    ("local", 0.0, 0.0),
]

_PRICE_SORTED = sorted(_PRICE_TABLE, key=lambda t: len(t[0]), reverse=True)


@dataclass
class PriceQuote:
    model: str
    input_per_mtok: float
    output_per_mtok: float
    priced: bool  # False if we fell back to $0 unknown


def price_for_model(model: str) -> PriceQuote:
    m = (model or "").lower().replace("_", "-")
    for frag, inp, out in _PRICE_SORTED:
        if frag in m:
            return PriceQuote(model=model, input_per_mtok=inp, output_per_mtok=out, priced=True)
    return PriceQuote(model=model, input_per_mtok=0.0, output_per_mtok=0.0, priced=False)


def estimate_cost_usd(
    model: str,
    *,
    prompt_tokens: int,
    completion_tokens: int,
) -> tuple[float, PriceQuote]:
    quote = price_for_model(model)
    cost = (
        (max(0, prompt_tokens) / 1_000_000.0) * quote.input_per_mtok
        + (max(0, completion_tokens) / 1_000_000.0) * quote.output_per_mtok
    )
    return cost, quote


@dataclass
class CostEvent:
    ts: str
    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    priced: bool
    complexity: str = ""
    task_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "provider": self.provider,
            "model": self.model,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cost_usd": round(self.cost_usd, 8),
            "priced": self.priced,
            "complexity": self.complexity,
            "task_id": self.task_id,
        }


@dataclass
class CostLedger:
    """In-memory session totals + optional JSONL persistence."""

    events: list[CostEvent] = field(default_factory=list)
    total_usd: float = 0.0
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    unpriced_turns: int = 0
    path: Path | None = None

    def record(
        self,
        *,
        provider: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        complexity: str = "",
        task_id: str = "",
    ) -> CostEvent:
        cost, quote = estimate_cost_usd(
            model, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens
        )
        ev = CostEvent(
            ts=datetime.now(UTC).isoformat(),
            provider=provider,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=cost,
            priced=quote.priced,
            complexity=complexity,
            task_id=task_id,
        )
        self.events.append(ev)
        self.total_usd += cost
        self.total_prompt_tokens += prompt_tokens
        self.total_completion_tokens += completion_tokens
        if not quote.priced:
            self.unpriced_turns += 1
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(ev.to_dict()) + "\n")
        return ev

    def summary(self) -> dict[str, Any]:
        by_model: dict[str, float] = {}
        for ev in self.events:
            by_model[ev.model or "?"] = by_model.get(ev.model or "?", 0.0) + ev.cost_usd
        return {
            "turns": len(self.events),
            "total_usd": round(self.total_usd, 6),
            "prompt_tokens": self.total_prompt_tokens,
            "completion_tokens": self.total_completion_tokens,
            "unpriced_turns": self.unpriced_turns,
            "by_model": {k: round(v, 6) for k, v in sorted(by_model.items(), key=lambda x: -x[1])},
        }

    def reset(self) -> None:
        self.events.clear()
        self.total_usd = 0.0
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self.unpriced_turns = 0
        if self.path is not None and self.path.exists():
            self.path.write_text("", encoding="utf-8")


def fmt_usd(amount: float) -> str:
    if amount <= 0:
        return "$0.00"
    if amount < 0.01:
        return f"${amount:.4f}"
    if amount < 1:
        return f"${amount:.3f}"
    return f"${amount:.2f}"
