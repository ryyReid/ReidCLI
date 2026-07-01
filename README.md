# ReidCLI


Terminal-native personal intelligence and coding CLI with an agent-first runtime.

A real runtime — not a chat wrapper. Sessions, tasks, tools, policy gates, and
persistence are first-class. Built to grow into a durable operator surface.

**Status:** Phase 5 complete (correctness fixes + real resume + interaction upgrade).
See `docs/` for the architecture audit and phase plans.

---

## Target stack

- **Python** 3.12+
- **Typer** — CLI command surface
- **Pydantic v2** — schemas and validation
- **Rich** — terminal rendering (markdown, tables, panels, spinners)

---

## Quick start

### 1. Create a venv and install

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

> On macOS/Linux: `source .venv/bin/activate` instead of the PowerShell line.

### 2. Verify the install

```powershell
reidcli doctor
```

Expected output:

```
reidcli 0.1.0
python    <path> (3.13.x)
workspace <cwd>
storage   ~/.reidcli
provider  stub
mode      balanced
providers ['stub']
ok runtime importable; stub provider available
```

### 3. Run it

```powershell
reidcli
```

Drops you into the interactive REPL with a fresh session. Type `/help` for commands,
or just start talking. The stub provider is offline and exercisable without API keys.

---

## Command surface

### Top-level CLI commands

| Command | Purpose |
|---|---|
| `reidcli` | Launch interactive mode (default — no subcommand needed) |
| `reidcli interactive "<prompt>"` | Launch interactive mode and immediately submit `<prompt>` as the first turn — session stays open afterward |
| `reidcli --file <path>` / `-f` | Same idea, but read the initial prompt from a text file — works with `interactive`, `exec`, and the bare/no-subcommand form |
| `<cmd> \| reidcli` | Pipe a prompt via stdin as the initial turn (only applies to the bare/no-subcommand form) |
| `reidcli exec "<prompt>"` | Run a single prompt non-interactively (headless) |
| `reidcli resume <session-id>` | Resume a prior session, then enter interactive mode |
| `reidcli sessions` | List all sessions |
| `reidcli config-show` | Show the effective (merged) configuration |
| `reidcli tools` | List registered tools with risk levels |
| `reidcli doctor` | Run environment diagnostics |
| `reidcli version` | Show version and runtime info |
| `reidcli --help` | Show the command surface |

### Slash commands (inside the REPL)

**Session**

| Command | Purpose |
|---|---|
| `/status` | Show current session, mode, model, task count, workspace |
| `/sessions` | List all sessions with freshness |
| `/resume <id>` | Resume a prior session (restores message history) |
| `/transcript [n]` | Show last n messages (default 20) |
| `/rewind` | Drop the last turn from state |

**Tasks**

| Command | Purpose |
|---|---|
| `/tasks [status]` | List tasks; filter by `pending` `active` `completed` `failed` `blocked` |

**Config & Policy**

| Command | Purpose |
|---|---|
| `/model <name>` | Set the model for the session |
| `/effort <level>` | Set reasoning effort: `low` `medium` `high` |
| `/mode <mode>` | Set permission mode: `strict` `balanced` `autonomous` `custom` |
| `/permissions` | Show current policy: mode, blocked/allowed commands, writable roots, timeouts |
| `/tools` | List registered tools with risk-level badges |

**Meta**

| Command | Purpose |
|---|---|
| `/help` | Show grouped help |
| `/clear` | Clear the screen |
| `/exit` | Quit ReidCLI (also `Ctrl+D` or `Ctrl+C`) |

---

## Configuration

Config is merged in this precedence order (low → high):

1. **Built-in defaults** — a stub provider, balanced mode
2. **Global config** — `~/.reidcli/config.json`
3. **Project config** — `./.reidcli/config.json`
4. **Environment variables** (highest)

### Environment variables

| Variable | Effect |
|---|---|
| `REIDCLI_PROVIDER` | Default provider name (e.g. `stub`) |
| `REIDCLI_WORKSPACE` | Workspace root path |
| `REIDCLI_STORAGE` | Storage root path (defaults to `~/.reidcli`) |
| `REIDCLI_PERMISSION_MODE` | Permission mode: `strict` `balanced` `autonomous` `custom` |
| `REIDCLI_LOG_LEVEL` | Log level: `INFO` `DEBUG` `WARNING` `ERROR` |

### Config file example

`~/.reidcli/config.json`:

```json
{
  "default_provider": "stub",
  "policy": {
    "default_mode": "balanced",
    "allowed_commands": ["git", "ls", "pwd"],
    "shell_timeout_seconds": 30
  }
}
```

View the effective merged config:

```powershell
reidcli config-show
```

---

## Permission modes

The policy engine gates every tool call. Pick a mode that matches your trust level.

| Mode | Behavior |
|---|---|
| `strict` | Approve nearly everything. File reads allowed; writes prompt; shell denied. |
| `balanced` | (Default) Low-risk allowed; medium and high risk prompt for approval. |
| `autonomous` | Low and medium allowed without prompts; high risk still prompts. |
| `custom` | Only explicit allowlists permit; everything else prompts. |

Path confinement is enforced in all modes — file tools cannot read or write outside
the workspace root (plus any configured `additional_writable_roots`). Shell commands
in the default denylist (`rm`, `rmdir`, `del`, `format`, `shutdown`, `reboot`, `mkfs`)
are always blocked.

Switch modes at runtime:

```
/mode strict
/mode autonomous
```

---

## Tools

The agent loop calls tools through the registry. Each tool is policy-gated.

| Tool | Risk | Purpose |
|---|---|---|
| `read_file` | low | Read a file's text content |
| `write_file` | medium | Create or overwrite a file |
| `patch_file` | medium | Replace one exact substring occurrence (unique match required) |
| `list_dir` | low | List entries in a directory |
| `find_files` | low | Find files matching a glob pattern |
| `grep_files` | low | Search file contents with a regex |
| `run_command` | high | Run a shell command with policy approval and timeout |

All file tools confine access to the workspace root. Traversal outside is denied.

---

## Sessions and persistence

Each session gets a structured directory under `~/.reidcli/sessions/<id>/`:

```
~/.reidcli/sessions/<session-id>/
  meta.json         # Session record (id, workspace, model, mode, status, timestamps)
  transcript.jsonl  # One Message per line (restorable into state on resume)
  tasks.json        # Task state for the session
  events.jsonl      # Runtime action log (turn summaries, lifecycle events)
```

**Resume is real:** `reidcli resume <id>` reloads the transcript into the agent's
message history so the conversation continues with full context (capped at the 100
most recent messages).

List sessions:

```powershell
reidcli sessions
```

---

## Headless / exec mode

Run a single prompt without entering the REPL:

```powershell
reidcli exec "list the current dir"
reidcli exec "read README.md"
```

Output goes to stdout; tool-call count goes to stderr. Exit code is `0` on success,
`1` if no text was produced. The approver auto-allows in exec mode — set
`REIDCLI_PERMISSION_MODE=autonomous` for unattended runs, or `strict` to deny all
risky actions silently.

---

## Development

### Run the test suite

```powershell
pytest
```

18 focused tests across policy, tools, session, and agent loop.

### Lint

```powershell
ruff check src
ruff check --fix src   # auto-fix
```

### Project structure

```
ReidCLI/
  pyproject.toml
  src/reidcli/
    app/         # Typer CLI commands, dependency wiring
    config/      # Pydantic config models + loader (global/project/env merge)
    diagnostics/ # logger + JSONL event log
    session/     # Session model + structured per-session store
    tasks/       # Task model + store (state machine)
    policy/      # PermissionMode, decisions, risk, PolicyEngine
    provider/    # BaseProvider + StubProvider + registry
    tools/       # ToolDefinition/Result, registry, file tools, shell tool
    runtime/     # RuntimeState, agent loop, orchestrator (composition root)
    integrations/# MCP foundation (config-driven, stubbed lifecycle)
    automation/  # exec mode (headless)
    ui/          # theme, render, REPL, slash commands
  tests/         # policy, tools, session, agent loop
  docs/          # architecture audit, phase plans
```

### Architecture intent

See the parent repo's design docs:

- `reidcli-build-plan.md` — full product definition and phase plan
- `agent-first-cli-spec.md` — generic agent-first CLI specification
- `docs/reidcli-architecture-audit.md` — file-aware critique of this scaffold
- `docs/reidcli-phase-5-plan.md` — correctness fixes + interaction upgrade

---

## What works now

- Interactive REPL with markdown-rendered output and spinner
- Real agent loop with tool calls (StubProvider, no API keys needed)
- Session create / list / resume with message history restoration
- Task tracking with status state machine (pending → active → completed/failed)
- Policy engine with 4 modes, path confinement, command allowlist/denylist
- 7 tools (file read/write/patch/list/find/grep + shell) all policy-gated
- Headless exec mode
- Structured persistence (meta / transcript / tasks / events per session)
- 18 passing tests, ruff clean

## What is stubbed (extension-ready)

- **Real providers** — OpenAI/Anthropic clients plug into `ProviderRegistry`
- **MCP** — config schema + lifecycle slots; stdio/JSON-RPC is TODO
- **Patch tool** — single exact-match replace; structured edits + diff preview TODO
- **Automation** — one-shot exec; scheduling/background TODO
- **Subagents** — not yet implemented (Phase 9)

---

## License

MIT
