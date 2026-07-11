"""DeepReid pipeline tests: role sequencing, revision-loop cap, output format."""
from __future__ import annotations

import uuid
from pathlib import Path

from reidx.config.models import default_config
from reidx.deepreid.pipeline import (
    MAX_REVISION_ROUNDS,
    RESEARCHER_MAX_STEPS,
    format_markdown,
    run_deepreid,
)
from reidx.provider.base import BaseProvider, ProviderResponse, ToolCall
from reidx.provider.stub import StubProvider


def test_stub_pipeline_runs_without_revision(tmp_path: Path) -> None:
    cfg = default_config()
    progress: list[str] = []
    result = run_deepreid(cfg, StubProvider(), tmp_path, "add a logout button", on_progress=progress.append)
    assert progress == ["researching", "planning", "reviewing"]
    assert result.rounds == 1
    assert result.findings and result.plan


def test_format_markdown_has_expected_sections(tmp_path: Path) -> None:
    cfg = default_config()
    result = run_deepreid(cfg, StubProvider(), tmp_path, "add a logout button")
    md = format_markdown(result)
    for heading in ("# DeepReid: add a logout button", "## Findings", "## Plan", "## Critique", "## Verdict"):
        assert heading in md


class _RoleAwareProvider(BaseProvider):
    """Returns a canned response based on which role's system prompt is
    active, so the revision loop can be driven deterministically."""

    name = "role-aware"

    def __init__(self, critic_verdict: str) -> None:
        self.critic_verdict = critic_verdict
        self.researcher_calls = 0
        self.planner_calls = 0
        self.critic_calls = 0

    def chat(self, messages, tools=None, model=None) -> ProviderResponse:  # type: ignore[no-untyped-def]
        system = messages[0].content if messages and messages[0].role == "system" else ""
        if system.startswith("You are the Critic"):
            self.critic_calls += 1
            return ProviderResponse(text=f"Some critique.\nVerdict: {self.critic_verdict}")
        if system.startswith("You are the Researcher"):
            self.researcher_calls += 1
            return ProviderResponse(text="- file.py:10 - found it")
        self.planner_calls += 1
        return ProviderResponse(text="1. Do the thing, touches file.py")


def test_no_revision_needed_stops_after_one_round(tmp_path: Path) -> None:
    cfg = default_config()
    provider = _RoleAwareProvider("ready to build")
    result = run_deepreid(cfg, provider, tmp_path, "task")
    assert result.rounds == 1
    assert result.verdict == "ready to build"
    assert provider.researcher_calls == 1
    assert provider.planner_calls == 1
    assert provider.critic_calls == 1


def test_revision_loop_caps_at_max_rounds(tmp_path: Path) -> None:
    cfg = default_config()
    # Critic always asks for revision -- must not loop forever.
    provider = _RoleAwareProvider("needs revision")
    result = run_deepreid(cfg, provider, tmp_path, "task")
    assert result.rounds == MAX_REVISION_ROUNDS
    assert provider.researcher_calls == 1
    assert provider.planner_calls == MAX_REVISION_ROUNDS
    assert provider.critic_calls == MAX_REVISION_ROUNDS


class _EndlessToolCallProvider(BaseProvider):
    """Always returns a tool call, never a plain-text final answer -- models
    the real failure observed in practice ("read the dir" against a big repo
    kept exploring and never wrote up Findings). Counts provider.chat() calls
    to confirm the Researcher actually gets RESEARCHER_MAX_STEPS, not the
    default 8-step budget every other role uses."""

    name = "endless-tool-call"

    def __init__(self) -> None:
        self.chat_calls = 0

    def chat(self, messages, tools=None, model=None) -> ProviderResponse:  # type: ignore[no-untyped-def]
        self.chat_calls += 1
        # No accompanying text -- matches real tool-call-only responses (the
        # observed failure had *no* text at any step, which is exactly what
        # leaves Agent.run_turn's final_text empty and triggers its "step
        # budget exhausted" fallback once max_steps runs out).
        return ProviderResponse(
            text="",
            tool_calls=[ToolCall(id=uuid.uuid4().hex[:8], name="list_dir", arguments={})],
        )


def test_researcher_gets_larger_step_budget_than_default(tmp_path: Path) -> None:
    from reidx.deepreid.pipeline import _RESEARCHER_PROMPT, _researcher_registry, _run_role

    provider = _EndlessToolCallProvider()
    cfg = default_config()
    findings = _run_role(
        cfg, provider, tmp_path, _researcher_registry(), _RESEARCHER_PROMPT, "read the dir",
        max_steps=RESEARCHER_MAX_STEPS,
    )
    assert "step budget exhausted" in findings  # this provider never stops, so it should still exhaust eventually
    assert provider.chat_calls == RESEARCHER_MAX_STEPS  # but only after the *larger* budget, not the default 8
    assert RESEARCHER_MAX_STEPS > 8, "regression guard: budget must actually exceed Agent's default MAX_STEPS"
