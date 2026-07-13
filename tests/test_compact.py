"""Context compaction tests — transcript rewrite, not the status-bar token meter."""
from __future__ import annotations

from pathlib import Path

from reidx.config.models import default_config
from reidx.provider.base import Message
from reidx.provider.stub import StubProvider
from reidx.runtime.compact import (
    compact_messages,
    estimate_tokens,
    heuristic_summary,
    partition_for_compact,
    should_auto_compact,
)
from reidx.runtime.orchestrator import Orchestrator
from reidx.tools import default_registry


def _long_conversation(n_turns: int = 6) -> list[Message]:
    msgs = [Message(role="system", content="You are a helpful coding agent.")]
    for i in range(n_turns):
        msgs.append(Message(role="user", content=f"Please do task number {i}: edit file_{i}.py"))
        msgs.append(Message(role="assistant", content=f"Done with task {i}. Changed file_{i}.py."))
    return msgs


def test_partition_keeps_recent_user_turns() -> None:
    msgs = _long_conversation(6)
    prefix, middle, tail = partition_for_compact(msgs, keep_user_turns=2)
    assert prefix and prefix[0].role == "system"
    assert middle
    # Tail starts at the 5th user turn (0-based: turns 4 and 5 kept)
    user_in_tail = [m for m in tail if m.role == "user"]
    assert len(user_in_tail) == 2
    assert "task number 4" in user_in_tail[0].content
    assert "task number 5" in user_in_tail[1].content


def test_heuristic_summary_mentions_user_work() -> None:
    msgs = _long_conversation(3)[1:]  # skip system
    summary = heuristic_summary(msgs)
    assert "User:" in summary
    assert "task number 0" in summary


def test_compact_messages_shrinks_transcript() -> None:
    # Long tool-style payloads so compaction actually saves tokens (short
    # toy turns can grow slightly due to the summary wrapper text).
    msgs = [Message(role="system", content="You are a helpful coding agent.")]
    for i in range(8):
        msgs.append(
            Message(
                role="user",
                content=f"Please do task number {i}: " + ("edit the module and fix bugs. " * 40),
            )
        )
        msgs.append(
            Message(
                role="assistant",
                content=f"Done with task {i}. " + ("Updated exports and tests. " * 40),
            )
        )
    before = len(msgs)
    before_tok = estimate_tokens(msgs)
    new_msgs, result = compact_messages(
        msgs, provider=StubProvider(), keep_user_turns=2, prefer_llm=False
    )
    assert result.method == "heuristic"
    assert result.after_count < before
    assert result.after_tokens < before_tok
    # System kept; summary user+assistant inserted; recent turns kept
    assert new_msgs[0].role == "system"
    assert new_msgs[1].role == "user" and new_msgs[1].content.startswith("[Context summary]")
    assert any(m.role == "user" and "task number 7" in m.content for m in new_msgs)


def test_compact_skipped_when_too_small() -> None:
    msgs = [
        Message(role="system", content="sys"),
        Message(role="user", content="hi"),
        Message(role="assistant", content="hello"),
    ]
    new_msgs, result = compact_messages(msgs, force=False)
    assert result.method == "skipped"
    assert new_msgs is msgs


def test_should_auto_compact_threshold() -> None:
    msgs = _long_conversation(10)
    # Tiny window → should trigger
    assert should_auto_compact(msgs, context_window=100) is True
    # Huge window → no
    assert should_auto_compact(msgs, context_window=10_000_000) is False


def test_orchestrator_compact_rewrites_transcript(tmp_path: Path) -> None:
    cfg = default_config()
    cfg.workspace_root = tmp_path
    cfg.storage_root = tmp_path / "store"
    orch = Orchestrator(cfg, StubProvider(), default_registry())
    orch.start_session(title="compact-test")
    assert orch.state is not None
    # Seed a long transcript in memory + on disk
    msgs = _long_conversation(8)
    orch.state.messages = msgs
    orch.session_store.rewrite_messages(orch.state.session.id, msgs)

    result = orch.compact_context(keep_user_turns=2, prefer_llm=False)
    assert result.method == "heuristic"
    assert len(orch.state.messages) < len(msgs)
    restored = orch.session_store.read_messages(orch.state.session.id, limit=500)
    assert len(restored) == len(orch.state.messages)
    assert restored[1].content.startswith("[Context summary]")
