# ReidX

Terminal-native personal intelligence and coding CLI with an agent-first runtime.

A real runtime — not a chat wrapper. Sessions, tasks, tools, policy gates,
providers, and persistence are first-class. A genuine full-screen TUI (not an
inline redraw hack) with a locked-to-bottom footer, scrollable history,
collapsible reasoning/tool-call output, a live subagent panel, and a live "/"
command menu. Built to grow into a durable operator surface.

**Status:** Phase 5 complete (correctness fixes + real resume + interaction
upgrade), plus a full-screen TUI rewrite, real HTTP providers (Anthropic /
OpenAI / OpenAI-compatible / Ollama), subagent spawning, DeepReid
(Researcher→Planner→Critic planning pipeline), Nyx (redteam persona mode),
web search, and workflows on top. See `docs/` for the architecture audit and
phase plans.

---

## Target stack

- **Python** 3.12+
- **Typer** — CLI command surface
- **Pydantic v2** — schemas and validation
- **Rich** — terminal rendering (markdown, tables, panels)
- **prompt_toolkit** — the full-screen TUI (layout, input, completion, mouse)
- No HTTP client dependency — the Anthropic/OpenAI/Ollama providers and
  `web_search` all speak stdlib `urllib`, so there's nothing extra to install
  to go from the offline stub to a real model.

---

## Quick start

### Option A: install via npm

Requires Python 3.12+ on your `PATH` (the npm package is a thin wrapper that
pip-installs the Python package on `npm install`).

```powershell
npm install -g reidx
```

This gives you `reid` on your `PATH`. Skip to
[step 2](#2-verify-the-install) to check it worked.

### Option B: install from source

#### 1. Create a venv and install

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

> On macOS/Linux: `source .venv/bin/activate` instead of the PowerShell line.

### 2. Verify the install

```powershell
reid doctor
```

Expected output:

```
reid 2.0.3
settings  <path> (found|missing)
python    <path> (3.13.x)
workspace <cwd>
storage   ~/.reidx
provider  stub
mode      balanced
providers stub (active), N tools
anthropic not configured (env: ANTHROPIC_API_KEY)
ok runtime importable; provider available
```

### 3. Run it

```powershell
reid
```

Drops you into the interactive TUI with a fresh session. Type `/` to see every
available command with descriptions, or just start talking. The stub provider
is offline and exercisable without API keys.

---

## The interactive TUI

`reid` (with no subcommand, or `reid interactive`) launches a real
full-screen `prompt_toolkit` application — the same style of terminal
ownership as `vim`/`htop` (alternate screen; your native scrollback is
untouched and restored exactly as it was on exit). Rich handles all the
actual rendering (markdown, tables, panels); prompt_toolkit owns the screen,
input, and layout around it.

- **Locked-to-bottom footer** — a spinner row, the input box, an optional
  subagent panel, and a status line (app name · mode · model · effort ·
  token/context-window usage · workspace · task count) are always pinned to
  the terminal's actual last rows. The scrollable output pane fills
  everything above it.
- **Mouse-wheel scroll** — scroll up to read history without losing your
  place; new replies keep arriving below the fold instead of yanking you back
  down. Scroll back to the bottom (or far enough) and it re-locks
  automatically. (Hold **Shift** while click-dragging for native text
  selection/copy — mouse support for scrolling means the terminal hands mouse
  events to the app, and Shift is the standard bypass every terminal supports
  for that.)
- **Collapsible reasoning + tool calls (Ctrl+O)** — chain-of-thought shows as
  a grayed-out `✻ Thought for Ns` line and each tool call as a one-line
  `● tool(args) ok/error` summary, collapsed by default. `Ctrl+O` toggles
  every collapsed block open at once to see the full detail.
- **`/` completion menu** — type `/` for a live menu of every slash command
  with its description (Tab/↓/↑ to navigate, Enter to accept); no need to run
  `/help` first, though `/help` still works and shows the same list grouped
  by category.
- **Large/multi-line pastes collapse** to a placeholder like
  `[Pasted text #1 +42 lines]` (same idea as Claude Code's own input box) —
  the full text is still sent when you submit, only the box display is
  compact.
- **Escape to stop a response** — while a reply is generating, `Esc` cancels
  just that turn (the spinner switches to `◐ stopping…`) and returns whatever
  partial answer/tool results it already had; the session itself stays open.
  It's polled at safe points (before the next model call, between tool
  calls), so it can't kill a request already in flight, but it ends the turn
  at the next opportunity instead of waiting for it to run to completion.
  `Ctrl+D` is still the one that exits the whole session.
- **Live subagent panel** — appears under the input box whenever the model
  calls `spawn_agent` (see Tools below), showing one row per child agent:
  name, status (running/done/error), elapsed time, and its last action.
  Finished rows linger for ~2s then disappear; auto-hidden when nothing's
  running.
- **DeepReid trigger** — type `deepread`/`deep reid` (a few spellings
  accepted) at the very start of the box: it pulses green, and your message
  runs through the real Researcher→Planner→Critic pipeline instead of a
  normal turn. See "DeepReid" under Tools/What works now below.
- **Nyx mode** — `/nyx on` swaps the assistant's persona to a redteam/
  offensive-security assistant for authorized pentesting and CTF work,
  without changing what tools it can call or how the policy engine gates
  them. See "Nyx (redteam mode)" below.
- **Keyboard shortcuts in the input box:**
  - `↑` / `↓` — input history
  - `←` / `→` — cycle reasoning effort (`low → medium → high → xhigh`) when
    the box is empty; otherwise they move the cursor normally
  - `Ctrl+O` — toggle collapsed/expanded reasoning + tool calls
  - `Esc` — stop the in-flight response (only while one is generating)
  - `Ctrl+C` — clear the current line; `Ctrl+D` — exit
- A small mascot renders next to the welcome banner on launch
  (`render.py::banner`/`_MASCOT`) — purely cosmetic, easy to swap out.

---

## Command surface

### Top-level CLI commands

| Command | Purpose |
|---|---|
| `reid` | Launch the interactive TUI (default — no subcommand needed) |
| `reid interactive "<prompt>"` | Launch interactive mode and immediately submit `<prompt>` as the first turn — session stays open afterward |
| `reid --file <path>` / `-f` | Same idea, but read the initial prompt from a text file — works with `interactive`, `exec`, and the bare/no-subcommand form |
| `reid --nyx` | Launch with the Nyx redteam/offensive-security persona active from the start (also on `interactive` and `exec`) |
| `<cmd> \| reid` | Pipe a prompt via stdin as the initial turn (only applies to the bare/no-subcommand form) |
| `reid exec "<prompt>"` | Run a single prompt non-interactively (headless) |
| `reid deepreid "<task>"` | Plan + review `<task>` via the Researcher/Planner/Critic pipeline (headless, like `exec`) — no code changes, saves a Markdown plan |
| `reid resume <session-id>` | Resume a prior session, then enter interactive mode |
| `reid sessions` | List all sessions |
| `reid config-show` | Show the effective (merged) configuration |
| `reid tools` | List registered tools with risk levels |
| `reid doctor` | Run environment diagnostics |
| `reid version` | Show version and runtime info |
| `reid --help` | Show the command surface |

### Slash commands (inside the TUI)

Type `/` in the input box for a live completion menu of all of these with
descriptions — the table below is the same information, grouped.

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
| `/tasks [status]` | List tasks; filter by `pending` `active` `completed` `failed` `blocked` `skipped` |

**Goals**

| Command | Purpose |
|---|---|
| `/goal` / `/goal show [id]` | Show the active goal, or a goal by id |
| `/goal <title>` | Create and activate a goal from free text |
| `/goal new <title>` | Create and activate a structured session goal |
| `/goal outcome <text>` | Set the active goal's success outcome |
| `/goal evidence add <text>` | Add observable success evidence |
| `/goal evidence done <index> [note]` | Mark evidence satisfied |
| `/goal add <title>` | Add a child subgoal |
| `/goal milestone <title>` | Add a milestone |
| `/goal list` | List session goals |
| `/goal active <id\|clear>` | Switch or clear the active goal |
| `/goal done [id] [note]` | Mark a goal or goal node completed |
| `/goal block [id] <reason>` | Mark a goal or goal node blocked |
| `/goal revise <note>` | Record a revision note |
| `/goal abandon [id] <reason>` | Abandon a goal or goal node |
| `/goal delete <id>` | Delete a goal |

How `/goal` works:

- A **goal** is the durable outcome you are trying to reach in the current
  session. It is not the same thing as a task.
- A **task** is still one agent turn in `tasks.json`; a **goal** lives in
  `goals.json` and tracks the larger outcome behind those turns.
- The **active goal** is the goal new tasks are linked to automatically. Use
  `/goal active <id>` to switch it, or `/goal active clear` to stop linking
  new tasks.
- `outcome` is the success condition in plain language.
- `evidence` is what would prove the goal is done. `/goal done` refuses to
  complete a goal with no evidence, so the feature nudges you away from vague
  activity tracking.
- `add` creates child subgoals. `milestone` creates progress markers. `block`,
  `revise`, and `abandon` keep the history honest when reality changes.
- Free text is accepted as a shortcut for creating a new active goal, so
  `/goal make me a report of ReidX` is the same kind of action as
  `/goal new make me a report of ReidX`.

Typical flow:

```text
/goal make me a report of ReidX
/goal outcome A useful report exists and cites the relevant code/docs
/goal evidence add Report covers command surface, runtime, tools, and persistence
/goal add Inspect README and docs
/goal milestone Draft report outline
/goal show
```

After that, normal prompts you send in the TUI become tasks linked to the
active goal. Use `/tasks` to inspect execution history and `/goal show` to
inspect the goal hierarchy, evidence, blockers, and revisions.

**Config & Policy**

| Command | Purpose |
|---|---|
| `/model <name>` | Set the model for the session |
| `/effort <level>` | Set reasoning effort: `low` `medium` `high` `xhigh` |
| `/mode <mode>` | Set permission mode: `strict` `balanced` `autonomous` `custom` |
| `/nyx [on\|off]` | Toggle the Nyx redteam/offensive-security persona for this session (no args shows current state) |
| `/permissions` | Show current policy: mode, blocked/allowed commands, writable roots, timeouts |
| `/tools` | List registered tools with risk-level badges |

**Workflows**

| Command | Purpose |
|---|---|
| `/workflows` | List saved workflows |
| `/workflow save <name> [n]` | Save the last `n` user turns as a reusable workflow (default 5) |
| `/workflow show <name>` | Show a workflow's steps |
| `/workflow run <name>` | Run a workflow's steps in sequence — each step gets the same handling as typing it directly (slash commands and prompts both work, spinner/approval included) |
| `/workflow delete <name>` | Delete a workflow |

Workflows are global (not tied to a session or workspace) and persist to
`~/.reidx/workflows.json`, so a workflow saved in one session is runnable
from any other.

**Providers**

| Command | Purpose |
|---|---|
| `/providers` | List registered providers (persisted + auto-registered), showing which is active |
| `/connect <name> <kind> <base_url> [api_key] [model]` | Add a provider (`kind`: `anthropic` `openai` `openai-compatible` `ollama`); persists to disk |
| `/disconnect <name>` | Remove a saved provider (not the active one, not the built-ins) |
| `/use <name>` | Switch this session to a registered provider |

**Meta**

| Command | Purpose |
|---|---|
| `/help` | Show grouped help |
| `/clear` | Clear the output pane |
| `/exit` | Quit ReidX (also `Ctrl+D`; `Ctrl+C` clears the current input line) |

---

## Providers

`stub` (offline, deterministic) is always registered and always the default —
nothing auto-promotes over it. Real HTTP providers all speak stdlib `urllib`
(no extra dependency):

| Kind | Notes |
|---|---|
| `anthropic` | Anthropic Messages API (`/v1/messages`); also works against Anthropic-compatible proxies via `base_url` |
| `openai` | OpenAI-compatible chat completions API |
| `openai-compatible` | Same wire format as `openai`, for arbitrary self-hosted/compatible endpoints |
| `ollama` | Local Ollama server |

Two ways to get a real provider registered:

1. **Env vars** (Anthropic only, auto-registered at startup, never made
   default): `ANTHROPIC_API_KEY`, `ANTHROPIC_BASE_URL`, `ANTHROPIC_MODEL`.
2. **`/connect`** — persists a `ProviderRecord` to
   `<storage_root>/providers.json` (chmod `600` on POSIX) and registers it
   immediately:

   ```
   /connect anthropic anthropic "" sk-ant-... claude-sonnet-5
   /connect local ollama http://localhost:11434 "" llama3
   /use local
   ```

`/use` is session-scoped: it rebuilds the agent against the new provider but
never changes `config.default_provider`, so `stub` is still what a fresh
session starts on until you `/use` again. `/disconnect` refuses to remove the
active provider or either built-in name (`stub`, `anthropic`).

`spawn_agent` (see Tools below) inherits the parent's active provider by
default, so switching to a local model at the top level applies to child
agents too — it can also be given a one-off `provider`/`model` override.

---

## Nyx (redteam mode)

Nyx swaps the assistant's system prompt to a redteam/offensive-security
persona for authorized penetration testing, CTF competitions, and security
research — recon, exploit development, payload construction, privilege
escalation, C2 usage, and report writing, described directly rather than with
vague hedging. It still pushes back on requests with no stated engagement
scope, or mass-scale/destructive/indiscriminate techniques.

Nyx **only changes the system prompt.** Tool access and policy gating are
completely unaffected — the `ToolRegistry`/`PolicyEngine`, not the prompt, is
the actual safety boundary (the same design used by DeepReid's role prompts).

Turn it on three ways:

```powershell
reid --nyx                # from launch
reid interactive --nyx "<prompt>"
reid exec --nyx "<prompt>"
```

or at runtime inside the TUI:

```
/nyx on
/nyx off
/nyx            # show current state
```

---

## Subagents (`spawn_agent`)

The agent can call `spawn_agent` to run a scoped child agent inline and block
on its result — useful for parallel research, focused review, or anything
that shouldn't pollute the main conversation's context. Each child gets:

- its **own** `Agent` + `PolicyEngine` (same config/mode as the parent, but
  an independent instance — a child running in a stricter mode never mutates
  the parent's)
- a **filtered `ToolRegistry`** built from an explicit `tool_allowlist` (default:
  `read_file`, `list_dir`, `find_files`, `grep_files` — read-only). The child
  literally cannot see or call anything outside that allowlist.
- the parent's **provider/model by default**, with optional per-call overrides
- **no** `spawn_agent` of its own — one layer of nesting only, so a runaway
  chain of subagents can't spawn subagents of their own

Lifecycle is reported to a `SubagentManager` that the TUI's subagent panel
subscribes to (see "The interactive TUI" above) — running/done/error, elapsed
time, last action.

---

## DeepReid

A real Researcher→Planner→Critic subagent pipeline for planning and
reviewing a task before any code gets written:

- **Researcher** — read-only file tools + `web_search`, investigates the
  codebase/web and produces a cited Findings list. Never writes files or
  runs commands.
- **Planner** — no tools, reasons only over the Findings, produces numbered
  implementation steps + risks + open questions.
- **Critic** — no tools, checks the Plan against the Findings for
  unsupported claims, missing cases, and contradictions; ends with a
  `Verdict: ready to build | needs revision | blocked on: ...` line.
- If the Critic asks for a revision, the Planner gets one more pass (capped
  at 2 rounds total) before the pipeline returns.

Each role is a fresh, independent `Agent` + `RuntimeState` + `Session` +
`PolicyEngine` — not turns on one shared conversation — so the "Planner/Critic
have no tools" constraint is real (they can't see any prior tool output). All
three run in `AUTONOMOUS` mode internally regardless of the caller's
configured mode; the restricted tool registries are the actual safety
boundary, so auto-approving inside them is safe.

Output is a Markdown plan+critique, saved to
`~/.reidx/deepreid/<run-id>.md`. Two entry points:

```powershell
reid deepreid "<task>"          # headless, like exec
```

or type `deepread`/`deep reid` at the very start of the TUI's input box (the
border pulses green while the pipeline runs, with real-time progress shown
per stage).

DeepReid never writes files or runs commands itself — building the plan is
still the regular single-agent loop, or a human, for now (see "What is
stubbed" below).

---

## Configuration

Config is merged in this precedence order (low → high):

1. **Built-in defaults** — a stub provider, balanced mode
2. **Global config** — `~/.reidx/config.json`
3. **Project config** — `./.reidx/config.json`
4. **`settings.json`'s `reidx` block** (see below)
5. **Environment variables** (highest)

### `settings.json` (Claude-Code-shaped project settings)

ReidX also reads a Claude-Code-style `settings.json`: an `env` block
(applied to `os.environ` before anything reads credentials — this is how a
project can bake in Anthropic-compatible proxy credentials even when the
shell's own `ANTHROPIC_*` vars point somewhere else) and an optional
`reidx` block (baked-in `default_provider`, `policy`, etc.). Unknown keys
(`theme`, `effortLevel`, ...) are ignored harmlessly, so an existing Claude
Code settings file works as-is.

Path resolution (first hit wins):

1. `$REIDCHAT_SETTINGS` (explicit override)
2. A project-local `settings.json`, found by walking upward from the current
   directory the way `git` finds `.git` — so launching `reid` from any
   subdirectory of a project still finds that project's file
3. `~/.reidx/settings.json` (global default)
4. `E:/leech/Reidchat.json` (legacy shared file)

### Environment variables

| Variable | Effect |
|---|---|
| `REIDX_PROVIDER` | Default provider name (e.g. `stub`) |
| `REIDX_WORKSPACE` | Workspace root path |
| `REIDX_STORAGE` | Storage root path (defaults to `~/.reidx`) |
| `REIDX_PERMISSION_MODE` | Permission mode: `strict` `balanced` `autonomous` `custom` |
| `REIDX_LOG_LEVEL` | Log level: `INFO` `DEBUG` `WARNING` `ERROR` |
| `REIDCHAT_SETTINGS` | Explicit path override for the `settings.json` lookup above |
| `ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL` / `ANTHROPIC_MODEL` | Auto-registers an `anthropic` provider at startup (never made default; `/use anthropic`) |

### Config file example

`~/.reidx/config.json`:

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
reid config-show
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

This same policy engine is what actually gates `spawn_agent` children and Nyx
mode — neither persona nor tool inheritance bypasses it.

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
| `web_search` | high | Search the web (DuckDuckGo, free, no API key) — see below |
| `spawn_agent` | medium | Run a scoped child agent (own policy engine + tool allowlist) and block for its result — see "Subagents" above |

All file tools confine access to the workspace root. Traversal outside is denied.

### `web_search`

Free, no API key, stdlib-only (`urllib` + `re`). Two DuckDuckGo sources, tried
in order:

1. The official Instant Answer JSON API — fast (~0.3s), but only populated
   for factual/entity queries ("what is X").
2. The HTML-only search endpoint — slower and more exposed to DuckDuckGo's
   anti-bot rate limiting, but covers general search queries the fast path
   doesn't.

Results are cached in-memory per session (5 minute TTL) so repeated queries
don't re-hit the network. Sponsored/ad results are filtered out rather than
surfaced as raw tracking links. Gated as `ActionKind.NETWORK` (HIGH risk by
default) through the same policy engine as every other tool — expect an
approval prompt in `balanced`/`strict` mode.

---

## Sessions and persistence

Each session gets a structured directory under `~/.reidx/sessions/<id>/`:

```
~/.reidx/sessions/<session-id>/
  meta.json         # Session record (id, workspace, model, mode, status, timestamps)
  transcript.jsonl  # One Message per line (restorable into state on resume)
  tasks.json        # Task state for the session
  goals.json        # Goal hierarchy, evidence, active goal, revisions
  events.jsonl      # Runtime action log (turn summaries, lifecycle events)
```

Workflows and connected providers live one level up, outside any single session:

```
~/.reidx/workflows.json   # {"workflows": [{name, description, steps, created_at}, ...]}
~/.reidx/providers.json   # {"providers": [{name, kind, base_url, api_key, default_model}, ...]} (chmod 600)
```

**Resume is real:** `reid resume <id>` reloads the transcript into the agent's
message history so the conversation continues with full context (capped at the 100
most recent messages).

List sessions:

```powershell
reid sessions
```

---

## Headless / exec mode

Run a single prompt without entering the TUI:

```powershell
reid exec "list the current dir"
reid exec "read README.md"
reid exec --file prompt.txt
reid exec --nyx "recon plan for <authorized target>"
```

Output goes to stdout; tool-call count goes to stderr. Exit code is `0` on success,
`1` if no text was produced. The approver auto-allows in exec mode — set
`REIDX_PERMISSION_MODE=autonomous` for unattended runs, or `strict` to deny all
risky actions silently.

---

## Development

### Run the test suite

```powershell
pytest
```

43 focused tests cover policy, tools, session/task/goal persistence, reasoning,
the agent loop, DeepReid, and providers/subagents.

### Lint

```powershell
ruff check src tests
ruff check --fix src tests   # auto-fix
```

### Project structure

```
ReidX/
  pyproject.toml
  src/reidx/
    app/         # Typer CLI commands, dependency wiring
    config/      # Pydantic config models + loader (global/project/settings.json/env merge)
    diagnostics/ # logger + JSONL event log
    session/     # Session model + structured per-session store
    tasks/       # Task model + store (state machine)
    goals/       # Goal model + per-session goal hierarchy/evidence store
    policy/      # PermissionMode, decisions, risk, PolicyEngine
    provider/    # BaseProvider + StubProvider + Anthropic/OpenAI/OpenAI-compatible/Ollama
                 # + ProviderRegistry + ProviderStore (persisted /connect records)
    tools/       # ToolDefinition/Result, registry, file/shell/web-search/spawn_agent tools
    workflows/   # Workflow model + global WorkflowStore (~/.reidx/workflows.json)
    nyx/         # Nyx redteam/offensive-security persona (system-prompt swap only)
    deepreid/    # Researcher->Planner->Critic planning-and-review pipeline
    runtime/     # RuntimeState, agent loop, orchestrator (composition root), subagent manager
    integrations/# MCP foundation (config-driven, stubbed lifecycle)
    automation/  # exec mode (headless)
    ui/          # theme, render (Rich), app (full-screen prompt_toolkit TUI),
                 # commands (slash-command routing + completion source), repl (entry point)
  tests/         # policy, tools, session, reasoning, agent loop, deepreid, providers/subagents
  docs/          # architecture audit, phase plans
```

### Architecture intent

See the parent repo's design docs:

- `reidx-build-plan.md` — full product definition and phase plan
- `agent-first-cli-spec.md` — generic agent-first CLI specification
- `docs/reidx-architecture-audit.md` — file-aware critique of this scaffold
- `docs/reidx-phase-5-plan.md` — correctness fixes + interaction upgrade
- `../deepreid-spec.md` — spec for DeepReid, the planning/review multi-agent
  subsystem, now implemented (see above)

---

## What works now

- Full-screen TUI (prompt_toolkit): locked-to-bottom footer, mouse-wheel
  scrollable history, collapsible reasoning/tool-call output (`Ctrl+O`), live
  subagent panel, `Esc`-to-stop-response, live `/` completion menu
- Real agent loop with tool calls (StubProvider by default, no API keys
  needed; real Anthropic/OpenAI/OpenAI-compatible/Ollama providers available
  via env vars or `/connect`)
- Session create / list / resume with message history restoration
- Task tracking with status state machine (pending → active → completed/failed/skipped)
- Goals: `/goal` manages per-session outcome/evidence/subgoal/milestone state
  in `goals.json`; active goals are linked into new task metadata
- Policy engine with 4 modes, path confinement, command allowlist/denylist —
  the single safety boundary for every tool, including subagents and Nyx mode
- 9 tools (file read/write/patch/list/find/grep + shell + free web search +
  spawn_agent) all policy-gated
- Workflows: save/list/show/run/delete reusable multi-step command sequences
- Providers: real HTTP clients (Anthropic/OpenAI/OpenAI-compatible/Ollama),
  `/connect`/`/disconnect`/`/use`/`/providers`, persisted to
  `~/.reidx/providers.json`
- Subagents: `spawn_agent` tool runs a scoped child agent (own policy engine +
  tool allowlist, one layer of nesting), live TUI panel showing progress
- **DeepReid** (`src/reidx/deepreid/`): a real Researcher→Planner→Critic
  subagent pipeline — each role is an independent `Agent`/`PolicyEngine`
  (Planner/Critic get zero tools; Researcher gets read-only file tools +
  `web_search` only), sequential, with a Critic-driven revision loop capped
  at 2 rounds. Never writes files or runs commands — output is a Markdown
  plan+critique, saved to `~/.reidx/deepreid/<run-id>.md`. Two entry
  points: `reid deepreid "<task>"` (headless CLI, like `exec`) and typing
  `deepread`/`deep reid` at the start of the TUI's input box (border pulses
  green while active, real-time progress shown per stage).
- **Nyx**: redteam/offensive-security persona, toggled via `--nyx` or
  `/nyx on|off` — swaps the system prompt only; tool access/policy gating are
  unchanged
- Claude-Code-shaped `settings.json` support (env block + baked-in `reidx`
  config block), project-local with upward directory search
- Prompt injection at launch: literal argument, `--file`, or piped stdin
- Headless exec mode
- Structured persistence (meta / transcript / tasks / goals / events per session;
  global workflows, providers, and DeepReid runs)
- Test suite passing, ruff clean

## What is stubbed (extension-ready)

- **MCP** — config schema + lifecycle slots; stdio/JSON-RPC is TODO
- **Patch tool** — single exact-match replace; structured edits + diff preview TODO
- **Automation** — one-shot exec; scheduling/background TODO
- **DeepReid Builder role** — a subagent that actually implements an
  approved plan is explicitly out of scope for v1 (per `../deepreid-spec.md`);
  building is still the regular single-agent loop, or a human, for now.
- **Escape-to-stop** only interrupts the normal turn loop (`Agent.run_turn`);
  the DeepReid pipeline and its subagent calls don't currently check
  cancellation.

---

## License

MIT
