"""Slash command routing for the REPL.

Each command returns a string hint for the REPL loop:
  "continue"  -> keep the loop running
  "exit"      -> stop the loop
Commands mutate orchestrator/state in place. Add new commands here — and add
a matching entry to SLASH_COMMANDS (or WORKFLOW_SUBCOMMANDS) below, which is
the single source both /help and the "/" completion menu (ui/app.py) render
from, so they can't drift out of sync.
"""
from __future__ import annotations

from pathlib import Path

from rich.console import Group
from rich.panel import Panel
from rich.text import Text

from reidx.goals.models import Goal, GoalNodeKind, GoalStatus
from reidx.policy.models import PermissionMode
from reidx.provider.store import SUPPORTED_KINDS, ProviderRecord, ProviderStore, build_provider
from reidx.runtime.orchestrator import Orchestrator
from reidx.ui import render
from reidx.ui.theme import APP_NAME, BOX, PRIMARY
from reidx.workflows.models import Workflow

_EFFORT_LEVELS = ("low", "medium", "high", "xhigh")

# /recap and /review don't do their own work here — they expand into a normal
# user turn (see ui.app's handling of the "recap-run"/"review-run:<pr>"
# outcomes below) so they get the full existing turn pipeline for free:
# conversation history already in state.messages, the thinking spinner, and
# (for /review) the same policy-gated run_command tool call as any other
# shell command, prompting for approval in balanced/strict mode exactly like
# a user-typed `gh pr diff ...` would.
RECAP_PROMPT = (
    "Give a one-line recap of what we've accomplished in this session so far. "
    "Respond with exactly one line, no preamble."
)


def review_prompt(pr: str) -> str:
    return (
        f"Review GitHub PR {pr}. Use run_command to fetch context: `gh pr view {pr}` for the "
        f"description and `gh pr diff {pr}` for the diff (requires the gh CLI installed and "
        "authenticated). Then critique the diff for correctness bugs first, then reuse/"
        "simplification/efficiency issues, most severe first. For your own uncommitted working "
        "diff instead of a remote PR, just ask directly — this command is for a PR already "
        "pushed to GitHub."
    )

# (command, args-hint, description, help-group). Order here is display order.
SLASH_COMMANDS: list[tuple[str, str, str, str]] = [
    ("/status", "", "show current session + mode + tasks", "Session"),
    ("/sessions", "", "list all sessions", "Session"),
    ("/resume", "<id>", "resume a prior session", "Session"),
    ("/transcript", "[n]", "show last n messages (default 20)", "Session"),
    ("/rewind", "", "drop the last turn from state", "Session"),
    ("/rename", "<title>", "rename the current session", "Session"),
    ("/recap", "", "generate a one-line session recap now", "Session"),
    ("/review", "<pr>", "review a GitHub pull request via gh + the agent (for your working diff, just ask directly)", "Session"),
    ("/tasks", "[status]", "list tasks (filter: pending|active|completed|failed|blocked)", "Tasks"),
    ("/goal", "<subcommand> ...", "manage session goals (type '/goal ' for subcommands)", "Goals"),
    ("/model", "<name>", "set model for the session", "Config & Policy"),
    ("/effort", "<level>", "set reasoning effort (low|medium|high|xhigh)", "Config & Policy"),
    ("/mode", "<mode>", "set permission mode (strict|balanced|autonomous|custom)", "Config & Policy"),
    ("/nyx", "[on|off]", "toggle Nyx redteam/offensive-security persona", "Config & Policy"),
    ("/permissions", "", "show current policy + gates", "Config & Policy"),
    ("/tools", "", "list registered tools with risk levels", "Config & Policy"),
    ("/workflows", "", "list saved workflows", "Workflows"),
    ("/workflow", "<run|save|show|delete> ...", "manage saved workflows", "Workflows"),
    ("/providers", "", "list registered providers (stub is always default)", "Providers"),
    ("/connect", "", "open the interactive provider connection palette", "Providers"),
    ("/disconnect", "<name>", "remove a saved provider", "Providers"),
    ("/use", "<name>", "switch this session to a registered provider", "Providers"),
    ("/help", "", "show this help", "Meta"),
    ("/clear", "", "clear the screen", "Meta"),
    ("/exit", "", f"quit {APP_NAME}", "Meta"),
]

# (subcommand, args-hint, description) for "/workflow <subcommand>".
WORKFLOW_SUBCOMMANDS: list[tuple[str, str, str]] = [
    ("run", "<name>", "run a workflow's steps in sequence"),
    ("save", "<name> [n]", "save the last n user turns as a workflow (default 5)"),
    ("show", "<name>", "show a workflow's steps"),
    ("delete", "<name>", "delete a workflow"),
]

GOAL_SUBCOMMANDS: list[tuple[str, str, str]] = [
    ("new", "<title>", "create and activate a goal"),
    ("list", "", "list goals in this session"),
    ("show", "[id]", "show the active goal or a goal by id"),
    ("active", "<id|clear>", "switch or clear the active goal"),
    ("outcome", "<text>", "set the active goal outcome"),
    ("evidence", "<add|done> ...", "manage active goal evidence"),
    ("add", "<title>", "add a child subgoal to the active goal"),
    ("milestone", "<title>", "add a milestone to the active goal"),
    ("done", "[id] [note]", "mark a goal or node completed"),
    ("block", "[id] <reason>", "mark a goal or node blocked"),
    ("revise", "<note>", "record a revision on the active goal"),
    ("abandon", "[id] <reason>", "abandon a goal or node"),
    ("delete", "<id>", "delete a goal"),
]


# Fixed value choices for commands whose argument is a small enum. The "/"
# completion menu offers these the moment you type "/<cmd> " — the same way
# "/goal " lists its subcommands — so you never have to remember the valid
# values. (command -> list of (value, description).) Kept here beside
# SLASH_COMMANDS so the menu and the code that parses these stay in sync.
ARG_CHOICES: dict[str, list[tuple[str, str]]] = {
    "/effort": [
        ("low", "minimal reasoning, fastest"),
        ("medium", "balanced reasoning (default)"),
        ("high", "deeper reasoning, slower"),
        ("xhigh", "maximum reasoning"),
    ],
    "/mode": [
        ("strict", "confirm every action"),
        ("balanced", "confirm only risky actions"),
        ("autonomous", "run without confirmations"),
        ("custom", "use custom policy gates"),
    ],
    "/nyx": [
        ("on", "enable Nyx redteam/offensive-security persona"),
        ("off", "disable Nyx persona"),
    ],
    "/tasks": [
        ("pending", "show pending tasks"),
        ("active", "show active tasks"),
        ("completed", "show completed tasks"),
        ("failed", "show failed tasks"),
        ("blocked", "show blocked tasks"),
    ],
}


def _build_help() -> Group:
    def section(header: str, body: str) -> Text:
        # Text(..., style=...) applies to just the constructor's own content
        # (the header); .append() with no style keeps the body literal — this
        # avoids Text.from_markup(), which would otherwise parse literal "["
        # in args hints like "[n]"/"[status]" as (invalid) markup tags and
        # silently swallow them.
        text = Text(f"{header}\n", style="bold")
        text.append(f"{body}\n")
        return text

    groups: dict[str, list[str]] = {}
    for cmd, args, desc, group in SLASH_COMMANDS:
        left = f"{cmd} {args}".rstrip()
        groups.setdefault(group, []).append(f"  {left:<28} {desc}")

    parts = [Panel(Text(f"{APP_NAME} commands", style=f"bold {PRIMARY}"), box=BOX, border_style=PRIMARY, padding=(0, 2))]
    for group, lines in groups.items():
        parts.append(section(group, "\n".join(lines)))

    sub_lines = "\n".join(f"    /workflow {name:<8} {args:<14} {desc}" for name, args, desc in WORKFLOW_SUBCOMMANDS)
    parts.append(section("Workflow subcommands", sub_lines))

    goal_lines = "\n".join(f"    /goal {name:<9} {args:<18} {desc}" for name, args, desc in GOAL_SUBCOMMANDS)
    parts.append(section("Goal subcommands", goal_lines))

    parts.append(
        section(
            "Tip",
            "  Type / to see a completion menu for every command above — Tab/↓ to select, Enter to accept.",
        )
    )
    return Group(*parts)


HELP = _build_help()


def _set_mode(orchestrator: Orchestrator, value: str) -> bool:
    try:
        mode = PermissionMode(value)
    except ValueError:
        render.print_error(f"unknown mode: {value}")
        return False
    orchestrator.set_permission_mode(mode)
    render.print_info(f"mode → {mode.value}")
    return True


def _handle_nyx(orchestrator: Orchestrator, arg: str) -> None:
    value = arg.strip().lower()
    if not value:
        render.print_info(f"nyx: {'on' if orchestrator.nyx_enabled else 'off'}")
        return
    if value not in ("on", "off"):
        render.print_error("usage: /nyx [on|off]")
        return
    orchestrator.set_nyx(value == "on")
    render.print_info(f"nyx → {value}")


def _handle_workflow(orchestrator: Orchestrator, arg: str) -> str | None:
    """Handles /workflow <run|save|show|delete> ...

    Returns "workflow-run:<name>" for /workflow run (the caller — ui.app's
    async turn loop — is the only thing that can actually execute a
    workflow's steps, since that requires awaiting each step's turn); returns
    None for every other subcommand (handled fully here).
    """
    parts = arg.split(None, 1)
    if not parts:
        render.print_error("usage: /workflow <run|save|show|delete> <name> ...")
        return None
    sub, rest = parts[0], (parts[1] if len(parts) > 1 else "").strip()

    if sub == "run":
        if not rest:
            render.print_error("usage: /workflow run <name>")
        elif orchestrator.workflow_store.get(rest) is None:
            render.print_error(f"no such workflow: {rest}")
        else:
            return f"workflow-run:{rest}"
        return None

    if sub == "show":
        wf = orchestrator.workflow_store.get(rest) if rest else None
        if wf is None:
            render.print_error(f"no such workflow: {rest or '(missing name)'}")
        else:
            render.print_workflow_steps(wf)
        return None

    if sub == "save":
        save_parts = rest.split(None, 1)
        if not save_parts:
            render.print_error("usage: /workflow save <name> [n]  (n = last n user turns, default 5)")
            return None
        name = save_parts[0]
        count_str = save_parts[1].strip() if len(save_parts) > 1 else ""
        n = int(count_str) if count_str.isdigit() else 5
        if orchestrator.state is None or not orchestrator.state.messages:
            render.print_error("no turns to save yet")
            return None
        steps = [m.content for m in orchestrator.state.messages if m.role == "user"][-n:]
        if not steps:
            render.print_error("no user turns to save yet")
            return None
        orchestrator.workflow_store.save(Workflow(name=name, steps=steps, description=f"last {len(steps)} turn(s)"))
        render.print_info(f"saved workflow '{name}' ({len(steps)} steps)")
        return None

    if sub == "delete":
        if not rest:
            render.print_error("usage: /workflow delete <name>")
        elif orchestrator.workflow_store.delete(rest):
            render.print_info(f"deleted workflow '{rest}'")
        else:
            render.print_error(f"no such workflow: {rest}")
        return None

    render.print_error(f"unknown /workflow subcommand: {sub} (try run|save|show|delete)")
    return None


def _goal_store(orchestrator: Orchestrator):
    try:
        return orchestrator.goal_store()
    except RuntimeError:
        render.print_error("no active session")
        return None


def _active_goal(store) -> Goal | None:  # type: ignore[no-untyped-def]
    goal = store.active()
    if goal is None:
        render.print_error("no active goal (try /goal new <title>)")
    return goal


def _split_first(text: str) -> tuple[str, str]:
    parts = text.strip().split(None, 1)
    if not parts:
        return "", ""
    return parts[0], (parts[1] if len(parts) > 1 else "").strip()


def _resolve_goal_target(store, text: str) -> tuple[Goal | None, str | None, str]:  # type: ignore[no-untyped-def]
    """Resolve optional target syntax: goal id, active-goal node id, or active goal."""
    token, rest = _split_first(text)
    active = store.active()
    if not token:
        return active, None, ""

    goals = {goal.id: goal for goal in store.list()}
    if token in goals:
        return goals[token], None, rest

    if active is not None and any(node.id == token for node in active.nodes):
        return active, token, rest

    return active, None, text.strip()


def _handle_goal_evidence(store, arg: str) -> None:  # type: ignore[no-untyped-def]
    sub, rest = _split_first(arg)
    goal = _active_goal(store)
    if goal is None:
        return
    if sub == "add":
        if not rest:
            render.print_error("usage: /goal evidence add <text>")
            return
        store.add_evidence(goal.id, rest)
        render.print_info("evidence added")
        return
    if sub == "done":
        idx, note = _split_first(rest)
        if not idx.isdigit():
            render.print_error("usage: /goal evidence done <index> [note]")
            return
        updated = store.satisfy_evidence(goal.id, int(idx) - 1, note)
        if updated is None:
            render.print_error(f"no evidence item #{idx}")
        else:
            render.print_info(f"evidence #{idx} satisfied")
        return
    render.print_error("usage: /goal evidence <add|done> ...")


def _handle_goal(orchestrator: Orchestrator, arg: str) -> str | None:
    store = _goal_store(orchestrator)
    if store is None:
        return None
    sub, rest = _split_first(arg)
    sub = sub or "show"

    if sub == "new":
        if not rest:
            render.print_error("usage: /goal new <title>")
            return None
        goal = store.create(rest)
        render.print_info(f"created active goal {goal.id}")
        render.print_goal(goal)
        return None

    if sub == "list":
        render.print_goals(store.list(), store.active_id())
        return None

    if sub == "show":
        goal = store.get(rest) if rest else store.active()
        if goal is None:
            render.print_error(f"no such goal: {rest or '(active)'}")
        else:
            render.print_goal(goal)
        return None

    if sub == "active":
        if not rest:
            goal = store.active()
            render.print_info(f"active goal: {goal.id} {goal.title}" if goal else "no active goal")
            return None
        if rest == "clear":
            store.set_active(None)
            render.print_info("active goal cleared")
            return None
        goal = store.set_active(rest)
        if goal is None:
            render.print_error(f"no such goal: {rest}")
        else:
            render.print_info(f"active goal -> {goal.id} {goal.title}")
        return None

    if sub == "outcome":
        goal = _active_goal(store)
        if goal is None:
            return None
        if not rest:
            render.print_error("usage: /goal outcome <text>")
            return None
        store.set_outcome(goal.id, rest)
        render.print_info("outcome updated")
        return None

    if sub == "evidence":
        _handle_goal_evidence(store, rest)
        return None

    if sub in ("add", "milestone"):
        goal = _active_goal(store)
        if goal is None:
            return None
        if not rest:
            render.print_error(f"usage: /goal {sub} <title>")
            return None
        kind = GoalNodeKind.MILESTONE if sub == "milestone" else GoalNodeKind.SUBGOAL
        node = store.add_node(goal.id, rest, kind)
        if node is None:
            render.print_error("failed to add goal node")
        else:
            render.print_info(f"added {kind.value} {node.id}")
        return None

    if sub == "done":
        goal, node_id, note = _resolve_goal_target(store, rest)
        if goal is None:
            render.print_error("no active goal")
            return None
        if node_id:
            store.update_status(goal.id, GoalStatus.COMPLETED, note or "completed", node_id=node_id)
            render.print_info(f"completed node {node_id}")
            return None
        if not goal.evidence:
            render.print_error("cannot complete a goal with no evidence (add /goal evidence add <text>)")
            return None
        if any(not evidence.satisfied for evidence in goal.evidence):
            render.print_warn("goal has unsatisfied evidence; marking completed anyway")
        store.update_status(goal.id, GoalStatus.COMPLETED, note or "completed")
        render.print_info(f"completed goal {goal.id}")
        return None

    if sub == "block":
        goal, node_id, reason = _resolve_goal_target(store, rest)
        if goal is None:
            render.print_error("no active goal")
            return None
        if not reason:
            render.print_error("usage: /goal block [id] <reason>")
            return None
        store.update_status(goal.id, GoalStatus.BLOCKED, reason, node_id=node_id)
        render.print_info(f"blocked {'node ' + node_id if node_id else 'goal ' + goal.id}")
        return None

    if sub == "revise":
        goal = _active_goal(store)
        if goal is None:
            return None
        if not rest:
            render.print_error("usage: /goal revise <note>")
            return None
        store.add_note(goal.id, rest)
        render.print_info("revision recorded")
        return None

    if sub == "abandon":
        goal, node_id, reason = _resolve_goal_target(store, rest)
        if goal is None:
            render.print_error("no active goal")
            return None
        if not reason:
            render.print_error("usage: /goal abandon [id] <reason>")
            return None
        store.update_status(goal.id, GoalStatus.ABANDONED, reason, node_id=node_id)
        render.print_info(f"abandoned {'node ' + node_id if node_id else 'goal ' + goal.id}")
        return None

    if sub == "delete":
        if not rest:
            render.print_error("usage: /goal delete <id>")
        elif store.delete(rest):
            render.print_info(f"deleted goal {rest}")
        else:
            render.print_error(f"no such goal: {rest}")
        return None

    # Natural fallback: `/goal make me a report` should create a goal, not
    # force the user to remember `/goal new ...`.
    goal = store.create(arg.strip())
    render.print_info(f"created active goal {goal.id}")
    render.print_goal(goal)
    return None


_BUILTIN_PROVIDER_NAMES = ("stub", "anthropic")


def _providers_store(orchestrator: Orchestrator) -> ProviderStore:
    root = orchestrator.config.storage_root or (Path.home() / ".reidx")
    return ProviderStore(root)


def _handle_providers(orchestrator: Orchestrator) -> None:
    store = _providers_store(orchestrator)
    persisted = store.list()
    persisted_names = {r.name for r in persisted}
    active = orchestrator.state.session.provider if orchestrator.state else orchestrator.config.default_provider
    extra: list[str] = []
    if orchestrator.providers is not None:
        for name in orchestrator.providers.names():
            if name not in persisted_names:
                extra.append(name)
    render.print_providers(persisted, active, extra)


def _handle_connect(orchestrator: Orchestrator, arg: str) -> None:
    parts = arg.split()
    if len(parts) < 3:
        render.print_error(
            "usage: /connect <name> <kind> <base_url> [api_key] [model]  "
            f"(kind: {'|'.join(SUPPORTED_KINDS)})"
        )
        return
    name, kind, base_url = parts[0], parts[1], parts[2]
    if kind not in SUPPORTED_KINDS:
        render.print_error(f"unknown kind: {kind} (try {'|'.join(SUPPORTED_KINDS)})")
        return
    if name in _BUILTIN_PROVIDER_NAMES and kind != "anthropic":
        render.print_error(f"name '{name}' is reserved for the built-in provider")
        return
    api_key = parts[3] if len(parts) > 3 else ""
    model = parts[4] if len(parts) > 4 else ""
    record = ProviderRecord(name=name, kind=kind, base_url=base_url, api_key=api_key, default_model=model)
    try:
        provider = build_provider(record)
    except ValueError as exc:
        render.print_error(f"failed to build provider: {exc}")
        return
    _providers_store(orchestrator).save(record)
    if orchestrator.providers is not None:
        orchestrator.providers.register(name, provider)
    render.print_info(f"connected provider '{name}' ({kind}) → {base_url or '(default)'}")
    render.print_info(f"switch with: /use {name}")


def _handle_disconnect(orchestrator: Orchestrator, arg: str) -> None:
    name = arg.strip()
    if not name:
        render.print_error("usage: /disconnect <name>")
        return
    if name in _BUILTIN_PROVIDER_NAMES:
        render.print_error(f"cannot disconnect built-in provider '{name}'")
        return
    active = orchestrator.state.session.provider if orchestrator.state else ""
    if name == active:
        render.print_error(f"'{name}' is active; /use stub first, then disconnect")
        return
    removed = _providers_store(orchestrator).delete(name)
    if orchestrator.providers is not None:
        orchestrator.providers.unregister(name)
    if removed:
        render.print_info(f"disconnected '{name}'")
    else:
        render.print_error(f"no saved provider named '{name}'")


def _handle_use(orchestrator: Orchestrator, arg: str) -> None:
    name = arg.strip()
    if not name:
        render.print_error("usage: /use <name> (see /providers)")
        return
    if orchestrator.providers is None or not orchestrator.providers.has(name):
        render.print_error(f"provider '{name}' is not registered (see /providers)")
        return
    if orchestrator.state is None:
        render.print_error("no active session")
        return
    try:
        orchestrator.use_provider(name)
    except (KeyError, RuntimeError) as exc:
        render.print_error(str(exc))
        return
    render.print_info(f"active provider → {name}  (model: {orchestrator.state.session.model})")


def handle(orchestrator: Orchestrator, line: str) -> str:
    parts = line.strip().split(None, 1)
    cmd = parts[0].lstrip("/")
    arg = parts[1] if len(parts) > 1 else ""

    if cmd in ("help", "?"):
        render.console.print(HELP)
    elif cmd == "status":
        if orchestrator.state:
            chars = sum(len(m.content or "") for m in orchestrator.state.messages)
            render.status_bar(
                orchestrator.state.session,
                orchestrator.state.effective_mode,
                len(orchestrator.list_tasks()),
                tokens_used=max(1, chars // 4),
            )
        else:
            render.print_info("no active session")
    elif cmd == "sessions":
        render.print_sessions(orchestrator.session_store.list())
    elif cmd == "resume":
        if not arg:
            render.print_error("usage: /resume <session-id>")
        else:
            try:
                orchestrator.resume_session(arg)
                count = len(orchestrator.state.messages) if orchestrator.state else 0
                render.print_info(f"resumed {arg} ({count} messages restored)")
            except KeyError as exc:
                render.print_error(str(exc))
    elif cmd == "tasks":
        tasks = orchestrator.list_tasks()
        if arg:
            tasks = [t for t in tasks if t.status.value == arg]
        render.print_tasks(tasks)
    elif cmd == "goal":
        _handle_goal(orchestrator, arg)
    elif cmd == "transcript":
        if orchestrator.state is None:
            render.print_info("no active session")
        else:
            n = int(arg) if arg.isdigit() else 20
            render.print_transcript(orchestrator.state.messages, n)
    elif cmd == "model":
        if not arg or orchestrator.state is None:
            render.print_error("usage: /model <name> (with an active session)")
        else:
            orchestrator.state.session.model = arg
            orchestrator.session_store.update(orchestrator.state.session)
            render.print_info(f"model → {arg}")
    elif cmd == "effort":
        if orchestrator.state is None:
            render.print_error("usage: /effort <low|medium|high|xhigh> (with an active session)")
        elif not arg:
            render.print_info(f"current effort: {orchestrator.state.session.reasoning_effort}")
        elif arg not in _EFFORT_LEVELS:
            render.print_error(f"unknown effort: {arg} (try low|medium|high|xhigh)")
        else:
            orchestrator.state.session.reasoning_effort = arg
            orchestrator.session_store.update(orchestrator.state.session)
            render.print_info(f"effort → {arg}")
    elif cmd == "mode":
        if not arg:
            render.print_info(f"current mode: {orchestrator.policy.mode.value}")
        else:
            _set_mode(orchestrator, arg)
    elif cmd == "nyx":
        _handle_nyx(orchestrator, arg)
    elif cmd == "permissions":
        render.print_permissions(orchestrator.policy)
    elif cmd == "tools":
        render.print_tools(orchestrator.tools.definitions())
    elif cmd == "rewind":
        if orchestrator.state is None or not orchestrator.state.messages:
            render.print_info("nothing to rewind")
        else:
            orchestrator.rewind()
            render.print_info(f"rewound to {len(orchestrator.state.messages)} messages")
    elif cmd == "workflows":
        render.print_workflows(orchestrator.workflow_store.list())
    elif cmd == "workflow":
        outcome = _handle_workflow(orchestrator, arg)
        if outcome is not None:
            return outcome
    elif cmd == "providers":
        _handle_providers(orchestrator)
    elif cmd == "connect":
        if arg.strip():
            _handle_connect(orchestrator, arg)
        else:
            return "connect"
    elif cmd == "disconnect":
        _handle_disconnect(orchestrator, arg)
    elif cmd == "use":
        _handle_use(orchestrator, arg)
    elif cmd == "clear":
        render.console.clear()
    elif cmd in ("exit", "quit", "q"):
        return "exit"
    else:
        render.print_error(f"unknown command: /{cmd} (try /help)")
    return "continue"
