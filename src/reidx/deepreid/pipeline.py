"""DeepReid: Researcher -> Planner -> Critic planning-and-review pipeline.

See deepreid-spec.md (repo root, one level above ReidX/) for the full
design. DeepReid never writes files or runs commands — it produces a
Markdown plan + critique. Building is a separate step.

Each role is a fresh, independent Agent + RuntimeState + Session +
PolicyEngine — not turns on one shared conversation, so each role's context
window stays clean and the "Planner/Critic have no tools" constraint is real
(a shared agent could otherwise still see prior tool results). All three
subagent PolicyEngines run in AUTONOMOUS mode regardless of the caller's
configured mode: Planner/Critic have zero tools registered (nothing to ever
approve), and the Researcher's tools are all read-only/search — the
restricted ToolRegistry is the actual safety boundary, so auto-approving
inside it is safe and avoids the pipeline hanging on an unattended prompt.
"""
from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from reidx.config.models import Config
from reidx.policy.engine import PolicyEngine
from reidx.policy.models import PermissionMode
from reidx.provider.base import BaseProvider
from reidx.runtime.agent import Agent
from reidx.runtime.state import RuntimeState
from reidx.session.models import Session
from reidx.tools.file_tools import FindFilesTool, GrepFilesTool, ListDirTool, ReadFileTool
from reidx.tools.registry import ToolRegistry
from reidx.tools.web_tools import WebSearchTool

MAX_REVISION_ROUNDS = 2
# The Researcher is the only role that calls tools, so it's the only one that
# can run out of Agent.run_turn's step budget mid-exploration — observed in
# practice: a broad task ("read the dir") led it to keep calling list_dir/
# read_file for all of the default 8 steps and never stop to write up
# Findings, producing "[agent] step budget exhausted" instead of any usable
# output. Give it real headroom; Planner/Critic have zero tools so they
# always finish in exactly 1 step regardless of this number.
RESEARCHER_MAX_STEPS = 20

_RESEARCHER_PROMPT = (
    "You are the Researcher in a DeepReid planning pipeline. Investigate the "
    "codebase (read_file/grep_files/find_files/list_dir) and, if useful, the "
    "web (web_search) to gather everything needed to plan the given task. You "
    "cannot write files or run commands — you only look and report.\n\n"
    "You have a limited number of tool calls. Investigate efficiently: prefer "
    "list_dir/find_files/grep_files to survey before read_file-ing whole "
    "files, and stop once you have enough to plan from — you do not need to "
    "read every file. Always end with your Findings list as plain text (no "
    "further tool calls) well before you run out of turns; a partial Findings "
    "list from what you've seen so far is far more useful than none at all.\n\n"
    "Produce a Findings list: one bullet per finding, each citing a "
    "`file:line` reference or a web_search source. Be specific, not vague. Do "
    "not propose a plan yourself; that is the Planner's job."
)

_PLANNER_PROMPT = (
    "You are the Planner in a DeepReid planning pipeline. You have no tools — "
    "reason only over the task and the Researcher's Findings given to you. "
    "Produce: numbered implementation steps (each naming the file(s) it "
    "touches and what changes), explicit risks or things you're unsure about, "
    "and open questions for a human. Do not invent findings the Researcher "
    "didn't report."
)

_PLANNER_REVISION_PROMPT = (
    "You are the Planner, revising your plan after Critic feedback. You have "
    "no tools. Given the original task, the Researcher's Findings, your prior "
    "Plan, and the Critic's Critique below, produce a revised Plan in the "
    "same format (numbered steps, risks, open questions) that addresses the "
    "critique."
)

_CRITIC_PROMPT = (
    "You are the Critic in a DeepReid planning pipeline. You have no tools — "
    "reason only over the task, the Researcher's Findings, and the Planner's "
    "Plan given to you. Find: claims in the Plan the Findings don't actually "
    "support, missing edge cases or files the Researcher didn't look at, and "
    "any internal contradictions. Write your Critique, then end your reply "
    "with a literal final line in exactly one of these forms:\n"
    "Verdict: ready to build\n"
    "Verdict: needs revision\n"
    "Verdict: blocked on: <what it's blocked on>"
)

_VERDICT_RE = re.compile(r"^\s*Verdict:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)


@dataclass
class DeepReidResult:
    task: str
    findings: str
    plan: str
    critique: str
    verdict: str
    rounds: int


def _researcher_registry() -> ToolRegistry:
    reg = ToolRegistry()
    for tool in (ReadFileTool(), GrepFilesTool(), FindFilesTool(), ListDirTool(), WebSearchTool()):
        reg.register(tool)
    return reg  # write_file/patch_file/run_command are never registered at all


def _make_subagent(
    config: Config, provider: BaseProvider, workspace: Path, registry: ToolRegistry, base_prompt: str
) -> tuple[Agent, RuntimeState]:
    policy = PolicyEngine(config)
    policy.set_mode(PermissionMode.AUTONOMOUS)
    session = Session(title="deepreid", workspace=workspace)
    agent = Agent(provider, registry, policy, base_system_prompt=base_prompt)
    return agent, RuntimeState(session=session)


def _run_role(
    config: Config,
    provider: BaseProvider,
    workspace: Path,
    registry: ToolRegistry,
    base_prompt: str,
    user_input: str,
    *,
    max_steps: int | None = None,
) -> str:
    agent, state = _make_subagent(config, provider, workspace, registry, base_prompt)
    kwargs = {"max_steps": max_steps} if max_steps is not None else {}
    text, _tools = agent.run_turn(state, user_input, approver=lambda _prompt: True, **kwargs)
    return text


def _report(on_progress: Callable[[str], None] | None, stage: str) -> None:
    if on_progress is not None:
        on_progress(stage)


def _split_verdict(critic_text: str) -> tuple[str, str]:
    """Split the Critic's reply into (critique_body, verdict_line).

    Falls back to treating the whole reply as critique with an empty verdict
    (never crashes on a Critic that ignores the requested format — same
    "never hide the answer" philosophy as split_reasoning)."""
    match = _VERDICT_RE.search(critic_text)
    if match is None:
        return critic_text.strip(), ""
    verdict = match.group(1).strip()
    critique = (critic_text[: match.start()] + critic_text[match.end() :]).strip()
    return critique, verdict


def run_deepreid(
    config: Config,
    provider: BaseProvider,
    workspace: Path,
    task: str,
    on_progress: Callable[[str], None] | None = None,
) -> DeepReidResult:
    researcher_registry = _researcher_registry()
    no_tools_registry = ToolRegistry()

    _report(on_progress, "researching")
    findings = _run_role(
        config, provider, workspace, researcher_registry, _RESEARCHER_PROMPT, task,
        max_steps=RESEARCHER_MAX_STEPS,
    )

    _report(on_progress, "planning")
    plan = _run_role(
        config, provider, workspace, no_tools_registry, _PLANNER_PROMPT,
        f"Task: {task}\n\nFindings:\n{findings}",
    )

    _report(on_progress, "reviewing")
    critic_text = _run_role(
        config, provider, workspace, no_tools_registry, _CRITIC_PROMPT,
        f"Task: {task}\n\nFindings:\n{findings}\n\nPlan:\n{plan}",
    )
    critique, verdict = _split_verdict(critic_text)

    rounds = 1
    while "revision" in verdict.lower() and rounds < MAX_REVISION_ROUNDS:
        _report(on_progress, "revising plan")
        plan = _run_role(
            config, provider, workspace, no_tools_registry, _PLANNER_REVISION_PROMPT,
            f"Task: {task}\n\nFindings:\n{findings}\n\nPrior Plan:\n{plan}\n\nCritique:\n{critique}",
        )
        _report(on_progress, "reviewing revision")
        critic_text = _run_role(
            config, provider, workspace, no_tools_registry, _CRITIC_PROMPT,
            f"Task: {task}\n\nFindings:\n{findings}\n\nPlan:\n{plan}",
        )
        critique, verdict = _split_verdict(critic_text)
        rounds += 1

    return DeepReidResult(task=task, findings=findings, plan=plan, critique=critique, verdict=verdict, rounds=rounds)


def format_markdown(result: DeepReidResult) -> str:
    return (
        f"# DeepReid: {result.task}\n\n"
        f"## Findings\n{result.findings}\n\n"
        f"## Plan\n{result.plan}\n\n"
        f"## Critique\n{result.critique or '(none)'}\n\n"
        f"## Verdict\n{result.verdict or '(not provided)'} "
        f"({result.rounds} round{'s' if result.rounds != 1 else ''})\n"
    )


def save_deepreid_result(config: Config, result: DeepReidResult) -> Path:
    storage_root = config.storage_root or (Path.home() / ".reidx")
    out_dir = storage_root / "deepreid"
    out_dir.mkdir(parents=True, exist_ok=True)
    run_id = time.strftime("%Y%m%d-%H%M%S")
    path = out_dir / f"{run_id}.md"
    path.write_text(format_markdown(result), encoding="utf-8")
    return path
