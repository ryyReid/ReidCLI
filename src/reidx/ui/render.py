"""UI rendering helpers (Rich-based).

A Claude-Code-style skin: a rounded welcome box, ⏺ bullets for assistant turns
and tool calls, ⎿ connectors for tool results, and a low-noise status line.
Rendered in ReidX's red palette. Assistant output stays markdown with
syntax highlighting; tool calls hang under a bullet with their result.
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from rich.console import Console, Group
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

from reidx.goals.models import Goal, GoalNode
from reidx.policy.engine import PolicyEngine
from reidx.policy.models import PermissionMode
from reidx.provider.base import Message
from reidx.session.models import Session
from reidx.tasks.models import Task
from reidx.ui.terminal_host import color_system_for_host
from reidx.ui.theme import (
    APP_NAME,
    BOX,
    BULLET,
    DANGER,
    DIM,
    MAX_WIDTH,
    MODE_STYLE,
    PRIMARY,
    PROMPT,
    RISK_STYLE,
    ROLE_ICON,
    ROLE_STYLE,
    SPARKLE,
    STATUS_STYLE,
    SUCCESS,
    TREE,
    WARN,
    context_window_for,
    fmt_tokens,
    short_path,
)

# Do NOT permanently reconfigure stdout/stderr here — that sticks the parent
# PowerShell session on UTF-8 and can break Oh My Posh / profile themes.
# Encoding is scoped for the TUI lifetime via `TerminalHostSession` in app.run.

# colour_system="auto" follows the host (Windows Terminal scheme, PowerShell
# theme). Override with REIDX_COLOR=truecolor|256|16|none or NO_COLOR=1.
console = Console(color_system=color_system_for_host())


# --- Thinking spinner constants (Claude-Code-style: "✻ Gerund… (12s · ↑ 2.1k tokens)") ---
# Rendered natively by ui.app's persistent footer (prompt_toolkit), not Rich —
# the spinner ticks too often (~8Hz) to round-trip through the console-capture
# bridge each frame. These constants are shared so both look the same.

_GERUNDS = [
    "Flibbertigibbeting", "Cogitating", "Percolating", "Ruminating", "Noodling",
    "Conjuring", "Finagling", "Marinating", "Puttering", "Simmering",
    "Frolicking", "Galavanting", "Bamboozling", "Whirring", "Pondering",
    "Scheming", "Effervescing", "Transmuting", "Kerfuffling", "Wrangling",
    "Vibing", "Tinkering", "Synthesizing", "Meandering", "Incubating",
    "Hornswoggling", "Discombobulating", "Moseying", "Percolatin'", "Brewing",
]


_STAR_FRAMES = "✶✸✹✺✹✷"


def _bullet_grid(marker: Text, body) -> Table:  # type: ignore[no-untyped-def]
    """A two-column grid: a bullet marker + hanging-indented body.

    Wrapped lines in the body align under the body column, mirroring the
    Claude Code '⏺ text' layout.
    """
    grid = Table.grid(padding=(0, 1))
    grid.add_column(width=1, no_wrap=True)
    grid.add_column(overflow="fold")
    grid.add_row(marker, body)
    return grid


# Mascot ASCII art, printed to the left of the welcome panel in the empty
# space there. Purely decorative — kept as its own constant so it's easy to
# swap out.
_MASCOT = r"""                    "...'...                     
               ^l'+??!_????:^'...:`"             
            ,`????????????????"'......           
          r^???????i-+??????:?-;Il"...           
          .^`_???+,.i`..'<,???????~..." ^        
        .-??<.....?l-""?????i::i???l>~..         
      .`?_~>>'`";;.^'i???????????~!-?>.."        
     ^.:???~^~??>!ll_??-???__??+.!~-.''. :       
     ^..i....!;Iii!!;!????????<l?+,`-<...^       
     `...""..",^";'....;!l!<>??^??>!<.i.`.       
      `.".      ..^."`;-~~?,?.I?.I?.i>..  ?      
       ...         `.'....+?.I.+?._'.'.".x       
          x         :.....<.;..>? `.`..' -       
                     :.....~...:'.`. '.          
                      .....I..."....  .          
                      ..........'.'.  `          
                      .......... .i.             
                       .^l..^.. .                
                      l `.. .                    
                       ~       """


def banner() -> None:
    """Claude-Code-style welcome box, with a mascot to its left."""
    from reidx import __version__

    body = Text.assemble(
        (f"{SPARKLE} ", PRIMARY),
        ("Welcome to ", "bold"),
        (APP_NAME, f"bold {PRIMARY}"),
        ("!", "bold"),
        (f"  v{__version__}\n\n", DIM),
        ("  /help for help, /status for your current setup\n\n", DIM),
        ("  cwd: ", DIM),
        (str(Path.cwd()), DIM),
    )
    panel = Panel(body, box=BOX, border_style=PRIMARY, padding=(0, 1), width=MAX_WIDTH)

    grid = Table.grid(padding=(0, 2))
    grid.add_column()
    grid.add_column()
    grid.add_row(Text(_MASCOT, style=PRIMARY), panel)
    console.print(grid)


def status_line_text(status: dict) -> Text:
    """Build the full status line: app · mode · model · effort · tokens · cwd · tasks."""
    mode = status.get("mode", "—")
    model = status.get("model", "—")
    effort = status.get("effort", "—")
    used = status.get("tokens_used", 0)
    window = status.get("context_window", 0)
    workspace = status.get("workspace", "—")
    tasks = status.get("tasks", 0)

    mode_color = MODE_STYLE.get(mode, DIM)
    sep = ("  ·  ", DIM)
    pct = f"{(used / window * 100):.0f}%" if window else "—"
    usage = f"{fmt_tokens(used)}/{fmt_tokens(window)} ({pct})" if window else fmt_tokens(used)

    return Text.assemble(
        (f"  {APP_NAME}", f"bold {PRIMARY}"), sep,
        (mode, f"bold {mode_color}"), sep,
        (model, DIM), sep,
        (f"effort:{effort}", DIM), sep,
        (usage, DIM), sep,
        (short_path(workspace), DIM), sep,
        (f"{tasks} tasks", WARN if tasks else DIM),
    )


def status_bar(session: Session | None, mode: PermissionMode, task_count: int = 0, tokens_used: int = 0) -> None:
    """Low-noise status line, Claude-Code-style — dim, dotted, no box."""
    if session is None:
        console.print(Text("  no active session", style=DIM))
        return
    console.print(
        status_line_text(
            {
                "mode": mode.value,
                "model": session.model,
                "effort": session.reasoning_effort,
                "tokens_used": tokens_used,
                "context_window": context_window_for(
                    session.model, session_window=getattr(session, "context_window", 0) or 0
                ),
                "workspace": str(session.workspace),
                "tasks": task_count,
            }
        )
    )


def status_prompt(session: Session | None, mode: PermissionMode | None) -> Text:
    """Input caret, Claude-Code-style. Session context lives in the status line."""
    return Text(f"{PROMPT} ", style=f"bold {PRIMARY}")


def print_user(text: str) -> None:
    """Echo the submitted prompt after the input box collapses, under a small
    "User" label, with extra blank lines before/after to clearly separate it
    from whatever came before (the prior reply) and after (this turn's
    reply)."""
    console.print()
    console.print()
    console.print(Text("  User", style=DIM))
    console.print(Text.assemble((f"{PROMPT} ", f"bold {PRIMARY}"), (text, "bold")))
    console.print()
    console.print()


def print_thinking(text: str) -> None:
    """Chain-of-thought shown above the answer: dim italic under a ✻ marker."""
    if not text or not text.strip():
        return
    console.print(_bullet_grid(Text(SPARKLE, style=DIM), Text(text.strip(), style="dim italic")))


def print_assistant(text: str) -> None:
    """Markdown assistant output hanging under a ⏺ bullet."""
    console.print(_bullet_grid(Text(BULLET, style=PRIMARY), Markdown(text)))


def print_tool_calls(tool_log: list[dict]) -> None:
    """Tool calls as ⏺ Name(args) with a ⎿ result line beneath each."""
    if not tool_log:
        return
    for entry in tool_log:
        name = entry["name"]
        ok = entry["ok"]
        error = entry.get("error", "")
        args = entry.get("args", {})
        args_str = ", ".join(f"{k}={v!r}" for k, v in args.items()) if args else ""
        header = Text.assemble(
            (name, "bold"), ("(", DIM), (args_str, DIM), (")", DIM),
        )
        console.print(_bullet_grid(Text(BULLET, style=PRIMARY), header))
        if ok:
            result = Text("ok", style=SUCCESS)
        else:
            result = Text(f"Error: {error}", style=DANGER)
        console.print(Text.assemble(("  ", ""), (TREE, DIM), ("  ", ""), result))


def print_tasks(tasks: list[Task]) -> None:
    if not tasks:
        console.print(Text("no tasks", style=DIM))
        return
    table = Table(title="tasks", box=BOX, show_header=True, header_style=f"bold {PRIMARY}", border_style=PRIMARY, width=MAX_WIDTH)
    table.add_column("id", style=DIM, width=12)
    table.add_column("status", width=12)
    table.add_column("title")
    for t in tasks:
        color = STATUS_STYLE.get(t.status.value, "white")
        table.add_row(t.id, Text(t.status.value, style=f"bold {color}"), t.title)
    console.print(table)
    # Summary line.
    counts: dict[str, int] = {}
    for t in tasks:
        counts[t.status.value] = counts.get(t.status.value, 0) + 1
    parts = []
    for k, v in counts.items():
        color = STATUS_STYLE.get(k, DIM)
        parts.append(Text(f"{v} {k}", style=color))
    summary = Text("  ").join(parts)
    console.print(summary)


def _status_text(value: str) -> Text:
    color = STATUS_STYLE.get(value, "white")
    return Text(value, style=f"bold {color}")


def _goal_evidence_progress(goal: Goal) -> str:
    total = len(goal.evidence)
    done = sum(1 for evidence in goal.evidence if evidence.satisfied)
    return f"{done}/{total}" if total else "-"


def _goal_node_progress(goal: Goal) -> str:
    total = len(goal.nodes)
    done = sum(1 for node in goal.nodes if node.status.value == "completed")
    return f"{done}/{total}" if total else "-"


def print_goals(goals: list[Goal], active_goal_id: str | None = None) -> None:
    if not goals:
        console.print(Text("no goals", style=DIM))
        return
    table = Table(title="goals", box=BOX, show_header=True, header_style=f"bold {PRIMARY}", border_style=PRIMARY, width=MAX_WIDTH)
    table.add_column("", width=2)
    table.add_column("id", style=DIM, width=12)
    table.add_column("status", width=12)
    table.add_column("evidence", width=9)
    table.add_column("nodes", width=7)
    table.add_column("title")
    for goal in goals:
        marker = "●" if goal.id == active_goal_id else ""
        table.add_row(
            marker,
            goal.id,
            _status_text(goal.status.value),
            _goal_evidence_progress(goal),
            _goal_node_progress(goal),
            goal.title,
        )
    console.print(table)
    console.print(Text("  ● = active goal", style=DIM))


def print_goal(goal: Goal) -> None:
    evidence_done = sum(1 for evidence in goal.evidence if evidence.satisfied)
    header = Text.assemble(
        (goal.title, f"bold {PRIMARY}"),
        ("  ", ""),
        (goal.id, DIM),
        ("  ", ""),
        (goal.status.value, f"bold {STATUS_STYLE.get(goal.status.value, DIM)}"),
    )
    body = Table.grid(padding=(0, 1))
    body.add_column(width=12, style=DIM)
    body.add_column()
    body.add_row("outcome", goal.outcome or "(none)")
    body.add_row("evidence", f"{evidence_done}/{len(goal.evidence)} satisfied" if goal.evidence else "(none)")
    body.add_row("nodes", f"{len(goal.nodes)}")
    body.add_row("tasks", f"{len(goal.task_ids)} linked")
    console.print(Panel(Group(header, body), box=BOX, border_style=PRIMARY, width=MAX_WIDTH))

    if goal.evidence:
        table = Table(title="evidence", box=BOX, show_header=True, header_style=f"bold {PRIMARY}", border_style=PRIMARY, width=MAX_WIDTH)
        table.add_column("#", style=DIM, width=4)
        table.add_column("done", width=6)
        table.add_column("description")
        table.add_column("note", style=DIM)
        for index, evidence in enumerate(goal.evidence, 1):
            table.add_row(
                str(index),
                "yes" if evidence.satisfied else "no",
                evidence.description,
                evidence.note or "",
            )
        console.print(table)

    if goal.constraints:
        table = Table(title="constraints", box=BOX, show_header=True, header_style=f"bold {PRIMARY}", border_style=PRIMARY, width=MAX_WIDTH)
        table.add_column("kind", style=DIM, width=12)
        table.add_column("description")
        for constraint in goal.constraints:
            table.add_row(constraint.kind, constraint.description)
        console.print(table)

    if goal.nodes:
        print_goal_tree(goal)

    if goal.revisions:
        console.print(Text("revisions", style=f"bold {PRIMARY}"))
        for revision in goal.revisions[-5:]:
            console.print(Text.assemble(("  - ", DIM), (revision.note, "white")))


def print_goal_tree(goal: Goal) -> None:
    by_parent: dict[str | None, list[GoalNode]] = {}
    for node in goal.nodes:
        by_parent.setdefault(node.parent_id, []).append(node)

    root = Tree(Text("nodes", style=f"bold {PRIMARY}"))

    def add_nodes(parent_tree: Tree, parent_id: str | None) -> None:
        for node in by_parent.get(parent_id, []):
            deps = f" deps:{','.join(node.depends_on)}" if node.depends_on else ""
            label = Text.assemble(
                (node.id, DIM),
                (" ", ""),
                (node.kind.value, DIM),
                (" ", ""),
                (node.status.value, f"bold {STATUS_STYLE.get(node.status.value, DIM)}"),
                ("  ", ""),
                (node.title, "white"),
                (deps, DIM),
            )
            branch = parent_tree.add(label)
            add_nodes(branch, node.id)

    add_nodes(root, None)
    console.print(root)


def print_sessions(sessions: list[Session]) -> None:
    if not sessions:
        console.print(Text("no sessions", style=DIM))
        return
    table = Table(title="sessions", box=BOX, show_header=True, header_style=f"bold {PRIMARY}", border_style=PRIMARY, width=MAX_WIDTH)
    table.add_column("id", style=DIM, width=14)
    table.add_column("status", width=10)
    table.add_column("title")
    table.add_column("updated", width=12)
    table.add_column("workspace", style=DIM)
    now = datetime.now(UTC)
    for s in sessions:
        color = STATUS_STYLE.get(s.status.value, "white")
        age = now - s.updated_at
        mins = int(age.total_seconds() // 60)
        when = f"{mins}m ago" if mins < 60 else f"{mins // 60}h ago"
        table.add_row(
            s.id,
            Text(s.status.value, style=f"bold {color}"),
            s.title,
            when,
            str(s.workspace),
        )
    console.print(table)


def print_workflows(workflows: list) -> None:  # type: ignore[no-untyped-def]
    if not workflows:
        console.print(Text("no workflows", style=DIM))
        return
    table = Table(title="workflows", box=BOX, show_header=True, header_style=f"bold {PRIMARY}", border_style=PRIMARY, width=MAX_WIDTH)
    table.add_column("name", style="bold", width=18)
    table.add_column("steps", width=6)
    table.add_column("description")
    for wf in workflows:
        table.add_row(wf.name, str(len(wf.steps)), wf.description or "(none)")
    console.print(table)


def print_workflow_steps(workflow) -> None:  # type: ignore[no-untyped-def]
    console.print(Text.assemble((workflow.name, f"bold {PRIMARY}"), ("  ", ""), (workflow.description, DIM)))
    for i, step in enumerate(workflow.steps, 1):
        console.print(Text.assemble((f"  {i}. ", DIM), (step, "white")))


def print_providers(records, active_name: str, extra_names: list[str]) -> None:  # type: ignore[no-untyped-def]
    """Show connected providers.

    `records` is the persisted list (name, kind, base_url, default_model);
    `extra_names` is any provider registered but not persisted (e.g. `stub`,
    or `anthropic` picked up from env vars) — those show as kind=built-in.
    `active_name` is the current session provider (highlighted).
    """
    table = Table(title="providers", box=BOX, show_header=True, header_style=f"bold {PRIMARY}", border_style=PRIMARY, width=MAX_WIDTH)
    table.add_column("name", style="bold", width=18)
    table.add_column("kind", width=20)
    table.add_column("model", width=22)
    table.add_column("base_url")
    for name in extra_names:
        marker = "● " if name == active_name else "  "
        table.add_row(f"{marker}{name}", "built-in", "-", "-")
    for r in records:
        marker = "● " if r.name == active_name else "  "
        table.add_row(f"{marker}{r.name}", r.kind, r.default_model or "-", r.base_url or "-")
    console.print(table)
    console.print(Text("  ● = active provider (use /use <name> to switch)", style=DIM))


def print_permissions(policy: PolicyEngine) -> None:
    """Structured permissions view using a table for readability."""
    cfg = policy.config.policy
    table = Table(title="permissions", box=BOX, show_header=False, border_style=PRIMARY, padding=(0, 1), width=MAX_WIDTH)
    table.add_column("key", style=DIM, width=18)
    table.add_column("value")
    mode_color = MODE_STYLE.get(policy.mode.value, DIM)
    table.add_row("mode", Text(policy.mode.value, style=f"bold {mode_color}"))
    table.add_row("blocked commands", ", ".join(sorted(policy.blocked_commands)) or "(none)")
    table.add_row("allowed commands", ", ".join(sorted(policy.allowed_commands)) or "(none)")
    table.add_row(
        "writable roots",
        ", ".join(str(r) for r in cfg.additional_writable_roots) or "(workspace only)",
    )
    table.add_row("read-only paths", ", ".join(str(r) for r in cfg.read_only_paths) or "(none)")
    table.add_row("shell timeout", f"{cfg.shell_timeout_seconds}s")
    console.print(table)


def print_transcript(messages: list[Message], n: int = 20) -> None:
    if not messages:
        console.print(Text("no transcript", style=DIM))
        return
    for m in messages[-n:]:
        icon = ROLE_ICON.get(m.role, "·")
        style = ROLE_STYLE.get(m.role, "white")
        if m.tool_calls:
            calls = ", ".join(c.name for c in m.tool_calls)
            console.print(Text.assemble((f"{icon} ", style), (f"{m.role} ", f"bold {style}"), (f"tools: {calls}", DIM)))
        else:
            text = m.content[:300] + ("…" if len(m.content) > 300 else "")
            console.print(Text.assemble((f"{icon} ", style), (f"{m.role} ", f"bold {style}"), (text, "white")))


def print_tools(definitions: list) -> None:  # type: ignore[no-untyped-def]
    """Grouped tool listing with risk badges."""
    table = Table(title="tools", box=BOX, show_header=True, header_style=f"bold {PRIMARY}", border_style=PRIMARY, width=MAX_WIDTH)
    table.add_column("name", style="bold", width=18)
    table.add_column("risk", width=8)
    table.add_column("description")
    for d in definitions:
        risk_color = RISK_STYLE.get(d.risk.value, DIM)
        table.add_row(d.name, Text(d.risk.value, style=f"bold {risk_color}"), d.description)
    console.print(table)


def print_error(text: str) -> None:
    """Inline red error under a ⏺ bullet, Claude-Code-style."""
    console.print(_bullet_grid(Text(BULLET, style=DANGER), Text(f"Error: {text}", style=DANGER)))


def print_info(text: str) -> None:
    console.print(Text(text, style=DIM))


def print_warn(text: str) -> None:
    console.print(Text(text, style=WARN))


def rule(title: str = "") -> None:
    """Horizontal separator between sections/turns."""
    if title:
        console.rule(Text(title, style=DIM), style=DIM, align="left")
    else:
        console.rule(style=DIM, align="left")


# Backward-compat aliases for any callers expecting the old names.
status_line = status_bar