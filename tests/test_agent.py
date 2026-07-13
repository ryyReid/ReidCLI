"""Agent loop tests: StubProvider turn, single assistant message, task outcome."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from reidx.config.models import default_config
from reidx.policy.engine import PolicyEngine
from reidx.provider.base import BaseProvider, Message, ProviderError, ProviderResponse
from reidx.provider.stub import StubProvider
from reidx.runtime.agent import PROVIDER_ERROR_PREFIX, Agent
from reidx.runtime.orchestrator import Orchestrator
from reidx.runtime.state import RuntimeState
from reidx.session.models import Session
from reidx.tasks.models import TaskStatus
from reidx.tools import default_registry


class _FailingProvider(BaseProvider):
    """Provider that always raises — used to test soft crash behavior."""

    name = "failing"

    def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
    ) -> ProviderResponse:
        raise ProviderError(
            "HTTP 404: Application not found — Check base URL and model name (/model, /providers).",
            status_code=404,
        )


def _agent(tmp_path: Path, provider: BaseProvider | None = None) -> Agent:
    cfg = default_config()
    cfg.workspace_root = tmp_path
    return Agent(provider or StubProvider(), default_registry(), PolicyEngine(cfg))


def _state(tmp_path: Path) -> RuntimeState:
    return RuntimeState(session=Session(title="t", workspace=tmp_path))


def test_stub_turn_executes_tool_call(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    state = _state(tmp_path)
    text, tools = agent.run_turn(state, "list the current dir")
    assert len(tools) == 1
    assert tools[0]["name"] == "list_dir"
    assert tools[0]["ok"]
    assert "Tool returned" in text


def test_single_assistant_message_per_turn(tmp_path: Path) -> None:
    """Audit §3.1: no double-append of assistant messages."""
    agent = _agent(tmp_path)
    state = _state(tmp_path)
    agent.run_turn(state, "list the current dir")
    assistant_msgs = [m for m in state.messages if m.role == "assistant"]
    # Exactly two: one with tool_calls, one final text (not a duplicate).
    assert len(assistant_msgs) == 2
    assert len(assistant_msgs[0].tool_calls) == 1
    assert len(assistant_msgs[1].tool_calls) == 0


def test_plain_answer_no_tool_call(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    state = _state(tmp_path)
    text, tools = agent.run_turn(state, "hello there")
    assert len(tools) == 0
    assert "hello there" in text
    assert "stub" in text.lower() or "Offline" in text


def test_system_prompt_inserted_once(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    state = _state(tmp_path)
    agent.run_turn(state, "hello")
    agent.run_turn(state, "again")
    system_msgs = [m for m in state.messages if m.role == "system"]
    assert len(system_msgs) == 1


def test_provider_error_is_soft_caught(tmp_path: Path) -> None:
    """Provider HTTP failures must not raise out of run_turn — soft crash."""
    agent = _agent(tmp_path, _FailingProvider())
    state = _state(tmp_path)
    text, tools = agent.run_turn(state, "hello")
    assert tools == []
    assert text.startswith(PROVIDER_ERROR_PREFIX)
    assert "Application not found" in text
    # Session stays usable: user + soft assistant error are both in the transcript.
    roles = [m.role for m in state.messages]
    assert roles.count("user") == 1
    assert roles.count("assistant") == 1
    # A second turn after failure must also soft-catch (not leave the loop broken).
    text2, _ = agent.run_turn(state, "try again")
    assert text2.startswith(PROVIDER_ERROR_PREFIX)


def test_orchestrator_marks_provider_failure(tmp_path: Path) -> None:
    cfg = default_config()
    cfg.workspace_root = tmp_path
    cfg.storage_root = tmp_path / "store"
    orch = Orchestrator(cfg, _FailingProvider(), default_registry())
    orch.start_session(title="soft-fail")
    result = orch.submit_task("hello")
    assert result["error"] is True
    assert "Application not found" in result["text"]
    assert not result["text"].startswith(PROVIDER_ERROR_PREFIX)
    tasks = orch.list_tasks()
    assert len(tasks) == 1
    assert tasks[0].status == TaskStatus.FAILED
