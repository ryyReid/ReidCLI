"""App layer: Typer command surface and dependency wiring.

This is the only place that composes ConfigLoader + ProviderRegistry + ToolRegistry
into an Orchestrator. Commands stay thin — they build an orchestrator and delegate
to runtime/ui/automation layers.
"""
from __future__ import annotations

import sys
from pathlib import Path

import typer
from rich.console import Console

from reidx import __version__
from reidx.automation.exec import exec_run
from reidx.config.loader import ConfigLoader
from reidx.config.models import Config
from reidx.config.storage import storage_root
from reidx.deepreid import format_markdown, run_deepreid, save_deepreid_result
from reidx.diagnostics.logger import get_logger
from reidx.provider.registry import default_registry
from reidx.provider.store import load_from_database
from reidx.provider.store import load_into as load_stored_providers
from reidx.runtime.orchestrator import Orchestrator
from reidx.tools import default_registry as tools_registry
from reidx.ui import render
from reidx.ui.repl import repl

log = get_logger("reidx.app")
console = Console()

app = typer.Typer(
    name="reid",
    help="Terminal-native agent-first CLI runtime.",
    no_args_is_help=False,
    add_completion=False,
)

_PROMPT_ARG_HELP = (
    "Inject this as the first prompt right after launch — same as typing it into the "
    "box and pressing Enter, except the session stays interactive afterward (unlike "
    "`reid exec`, which runs once and exits). If omitted and stdin isn't a "
    "terminal (piped input), stdin is read as the prompt instead."
)
_PROMPT_FILE_HELP = "Read the prompt from a text file instead of the command line (e.g. a long or multi-line prompt)."
_NYX_HELP = "Launch in Nyx mode: a redteam/offensive-security assistant persona for authorized pentesting and CTF work."


def _stdin_prompt() -> str | None:
    if sys.stdin.isatty():
        return None
    data = sys.stdin.read().strip()
    return data or None


def _resolve_prompt(prompt: str | None, prompt_file: Path | None, *, fall_back_to_stdin: bool) -> str | None:
    if prompt and prompt_file:
        render.print_error("pass either a prompt argument or --file, not both")
        raise typer.Exit(code=1)
    if prompt_file:
        try:
            text = prompt_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            render.print_error(f"failed to read prompt file {prompt_file}: {exc}")
            raise typer.Exit(code=1) from exc
        if not text:
            render.print_error(f"prompt file is empty: {prompt_file}")
            raise typer.Exit(code=1)
        return text
    if prompt:
        return prompt
    return _stdin_prompt() if fall_back_to_stdin else None


@app.callback(invoke_without_command=True)
def _main(
    ctx: typer.Context,
    prompt_file: Path | None = typer.Option(None, "--file", "-f", help=_PROMPT_FILE_HELP),
    nyx: bool = typer.Option(False, "--nyx", help=_NYX_HELP),
) -> None:
    """With no subcommand, launch interactive mode."""
    # No positional prompt argument here on purpose: a Typer/Click group with
    # both subcommands and its own positional argument resolves ambiguously —
    # tested and confirmed it swallows subcommand names ("reid version",
    # "reid sessions", ...) as the argument instead of dispatching to
    # them. `--file`/`-f` is a named option, not positional, so it doesn't
    # have that problem. `reid interactive "<prompt>"` is the unambiguous
    # way to inject literal prompt text; bare `reid` still just launches
    # empty (or reads piped stdin), as before.
    if ctx.invoked_subcommand is None:
        initial = _resolve_prompt(None, prompt_file, fall_back_to_stdin=True)
        orch = build_orchestrator()
        if nyx:
            orch.set_nyx(True)
        repl(orch, initial_prompt=initial)


def build_orchestrator(config: Config | None = None) -> Orchestrator:
    cfg = config or ConfigLoader().load()
    providers = default_registry(cfg)
    root = cfg.storage_root or storage_root()
    load_stored_providers(providers, root)
    load_from_database(providers, root)
    default_name = cfg.default_provider if providers.has(cfg.default_provider) else "stub"
    provider = providers.get(default_name)
    return Orchestrator(cfg, provider, tools_registry(), providers=providers)


@app.command()
def interactive(
    prompt: str | None = typer.Argument(None, help=_PROMPT_ARG_HELP),
    prompt_file: Path | None = typer.Option(None, "--file", "-f", help=_PROMPT_FILE_HELP),
    nyx: bool = typer.Option(False, "--nyx", help=_NYX_HELP),
) -> None:
    """Launch interactive mode (default)."""
    initial = _resolve_prompt(prompt, prompt_file, fall_back_to_stdin=True)
    orch = build_orchestrator()
    if nyx:
        orch.set_nyx(True)
    repl(orch, initial_prompt=initial)


@app.command(name="exec")
def exec_(
    prompt: str | None = typer.Argument(None, help="Prompt to run non-interactively."),
    prompt_file: Path | None = typer.Option(None, "--file", "-f", help=_PROMPT_FILE_HELP),
    nyx: bool = typer.Option(False, "--nyx", help=_NYX_HELP),
) -> None:
    """Run a single prompt non-interactively (headless)."""
    resolved = _resolve_prompt(prompt, prompt_file, fall_back_to_stdin=False)
    if not resolved:
        render.print_error("usage: reid exec \"<prompt>\" (or --file <path>)")
        raise typer.Exit(code=1)
    orch = build_orchestrator()
    if nyx:
        orch.set_nyx(True)
    raise typer.Exit(code=exec_run(orch, resolved))


@app.command()
def deepreid(
    task: str | None = typer.Argument(None, help=_PROMPT_ARG_HELP),
    prompt_file: Path | None = typer.Option(None, "--file", "-f", help=_PROMPT_FILE_HELP),
) -> None:
    """Plan + review a task via Researcher/Planner/Critic subagents (no code changes)."""
    resolved = _resolve_prompt(task, prompt_file, fall_back_to_stdin=True)
    if not resolved:
        render.print_error('usage: reid deepreid "<task>" (or --file <path>)')
        raise typer.Exit(code=1)
    cfg = ConfigLoader().load()
    provider = default_registry(cfg).get(cfg.default_provider)
    result = run_deepreid(cfg, provider, Path.cwd(), resolved, on_progress=render.print_info)
    path = save_deepreid_result(cfg, result)
    console.print(format_markdown(result))
    render.print_info(f"saved to {path}")


@app.command()
def resume(session_id: str = typer.Argument(..., help="Session id to resume.")) -> None:
    """Resume a prior session, then enter interactive mode."""
    orch = build_orchestrator()
    try:
        orch.resume_session(session_id)
    except KeyError as exc:
        render.print_error(str(exc))
        raise typer.Exit(code=1) from exc
    render.print_info(f"resumed {session_id}")
    repl(orch)


@app.command()
def sessions() -> None:
    """List sessions."""
    orch = build_orchestrator()
    render.print_sessions(orch.session_store.list())


@app.command()
def config_show() -> None:
    """Show the effective (merged) configuration."""
    cfg = ConfigLoader().load()
    console.print(cfg.model_dump_json(indent=2, exclude={"providers": {"__all__": {"api_key"}}}))


@app.command()
def tools() -> None:
    """List registered tools."""
    orch = build_orchestrator()
    from reidx.ui.render import print_tools

    print_tools(orch.tools.definitions())


@app.command()
def doctor() -> None:
    """Run environment diagnostics."""
    cfg = ConfigLoader().load()
    orch = build_orchestrator(cfg)
    from reidx.config.settings import settings_path

    sp = settings_path()
    console.print(f"[bold]reid[/] {__version__}")
    console.print(f"settings  {sp} ({'found' if sp.exists() else 'missing'})")
    console.print(f"python    {sys.executable} ({sys.version.split()[0]})")
    console.print(f"workspace {cfg.workspace_root}")
    console.print(f"storage   {cfg.storage_root}")
    console.print(f"provider  {cfg.default_provider}")
    console.print(f"mode      {cfg.policy.default_mode.value}")
    console.print(f"providers {orch.provider.name} (active), {len(orch.tools.definitions())} tools")
    import os

    has_key = bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())
    console.print(f"anthropic {'configured' if has_key else 'not configured'} (env: ANTHROPIC_API_KEY)")
    console.print("[green]ok[/] runtime importable; provider available")


@app.command()
def version() -> None:
    """Show version and runtime info."""
    console.print(f"reid {__version__}")
