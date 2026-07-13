# ReidX

Terminal-native personal intelligence and coding CLI with an agent-first runtime.

A real runtime — not a chat wrapper. Sessions, tasks, tools, policy gates,
providers, and persistence are first-class. A genuine full-screen TUI (not an
inline redraw hack) with a locked-to-bottom footer, scrollable history,
collapsible reasoning/tool-call output, token streaming, drag-to-copy, a live
subagent panel, and a live `/` command menu. Built to grow into a durable
operator surface.

**Status:** Phase 5 complete (correctness fixes + real resume + interaction
upgrade), plus a full-screen TUI rewrite, real HTTP providers (Anthropic /
OpenAI / OpenAI-compatible / Ollama), SSE token streaming, context compact +
cost tracking, soft provider-error handling, subagent spawning, DeepReid
(Researcher→Planner→Critic planning pipeline), Nyx (redteam persona mode),
web search, and workflows. See `docs/` for the architecture audit and phase
plans.

---

## Target stack

- **Python** 3.12+
- **Typer** — CLI command surface
- **Pydantic v2** — schemas and validation
- **Rich** — terminal rendering (markdown, tables, panels)
- **prompt_toolkit** — the full-screen TUI (layout, input, completion, mouse)
- No HTTP client dependency — providers and `web_search` use stdlib `urllib`
  (including SSE streaming), so nothing extra is required to go from the
  offline stub to a real model.

---

## Quick start

### Option A: install via npm

Requires Python 3.12+ on your `PATH` (the npm package is a thin wrapper that
pip-installs the Python package on `npm install` and seeds
`~/.reidcli/settings.json`).

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

Expected shape:

```
reid 2.x.x
settings  <path> (found|missing)
user cfg  ~/.reidcli/settings.json   # when project settings differ
python    <path> (3.12+)
workspace <cwd>
storage   ~/.reidcli
provider  stub | <connected>
mode      balanced
providers …
anthropic configured|not configured
openai    configured|not configured
ok runtime importable; provider available
```

### 3. Run it

```powershell
reid
```

Drops you into the interactive TUI with a fresh session. Type `/` to see every
available command with descriptions, or just start talking.

- **Offline:** the stub provider works without API keys and points you at
  `/connect`.
- **Real chat:** `/connect` (or env keys) + `/use <provider>`, or let startup
  pick a connected provider over stub when one exists.

---

## The interactive TUI

`reid` (with no subcommand, or `reid interactive`) launches a real
full-screen `prompt_toolkit` application — alternate screen; native scrollback
is restored on exit. Rich handles markdown/tables/panels; prompt_toolkit owns
layout, input, and mouse.

- **Locked-to-bottom footer** — spinner/streaming status, input box, optional
  subagent panel, and a status line (mode · model · effort · tokens/context ·
  workspace · selection/Copied! chip).
- **Token streaming** — with OpenAI-compatible providers (NVIDIA NIM, OpenAI,
  Groq, xAI, local `/v1`, …), replies paint live under a ● while the model
  generates. Default is **`/stream auto`** (on when the provider supports it).
  Use `/stream off` for wait-then-dump.
- **Mouse-wheel scroll** — scroll history without losing place; re-locks at
  bottom when you return.
- **Drag-to-copy** — drag over the transcript (red highlight), double-click a
  line, or triple-click a block. **`/copy`** or **Ctrl+Y** copies the last AI
  reply only (clean markdown). Release over the input box still finalizes the
  selection.
- **Collapsible reasoning + tool calls (Ctrl+O)** — chain-of-thought shows as
  a grayed `✻ Thought for Ns` line; tool calls collapse to one-line summaries.
  Reasoning is split from tags (`<think>`, GLM
  `<parameter name="reasoning">`, fences, etc.) and from short untagged
  monologues when possible.
- **`/` completion menu** — live menu of every slash command (Tab to navigate,
  Enter to accept). `/help` shows the same list grouped.
- **Large/multi-line pastes** collapse to `[Pasted text #1 +N lines]`.
- **Escape** cancels the in-flight turn at the next safe point; **Ctrl+D**
  exits the session.
- **Live subagent panel** — rows for `spawn_agent` children (status, elapsed,
  last action).
- **DeepReid trigger** — start the input with `deepread` / `deep reid` to run
  the Researcher→Planner→Critic pipeline (border pulses green).
- **Nyx mode** — `/nyx on` for the redteam/offensive-security persona (prompt
  only; tools/policy unchanged).
- **Keyboard shortcuts:**
  - ↑ / ↓ — input history
  - ← / → — cycle effort (`auto` · `low` · `medium` · `high` · `xhigh`) when
    the box is empty
  - `Ctrl+O` — expand/collapse reasoning + tool calls
  - `Ctrl+Y` — copy last AI reply
  - `Ctrl+C` — copy selection if any, else clear the line
  - `Esc` — stop the in-flight response
  - `Ctrl+D` — exit

---

## Command surface

### Top-level CLI commands

| Command | Purpose |
|---|---|
| `reid` | Launch the interactive TUI (default) |
| `reid interactive "<prompt>"` | Interactive mode; submit `<prompt>` first |
| `reid --file <path>` / `-f` | Initial prompt from a file |
| `reid --nyx` | Start with Nyx persona on |
| `<cmd> \| reid` | Pipe stdin as the initial turn |
| `reid exec "<prompt>"` | One-shot headless prompt |
| `reid deepreid "<task>"` | Headless Researcher/Planner/Critic plan |
| `reid resume <session-id>` | Resume a prior session |
| `reid sessions` | List sessions |
| `reid config-show` | Show effective config |
| `reid tools` | List tools + risk levels |
| `reid doctor` | Environment diagnostics |
| `reid version` | Version / runtime info |
| `reid --help` | Command surface |

### Slash commands (inside the TUI)

Type `/` for a live menu. Highlights:

**Session**

| Command | Purpose |
|---|---|
| `/status` | Session, mode, model, tasks, workspace |
| `/sessions` | List sessions |
| `/resume <id>` | Resume (restores transcript) |
| `/transcript [n]` | Last n messages (default 20) |
| `/rewind` | Drop last turn |
| `/compact [n\|--force]` | Summarize older turns (keeps last n user turns) |
| `/cost [reset]` | Session token cost estimate |
| `/copy` | Copy last AI reply to clipboard (also **Ctrl+Y**) |
| `/rename <title>` | Rename session |
| `/recap` | One-line session recap |
| `/review <pr>` | Review a GitHub PR via `gh` + agent |

**Tasks / Goals**

| Command | Purpose |
|---|---|
| `/tasks [status]` | List tasks (`pending` `active` `completed` `failed` `blocked`) |
| `/goal …` | Goal hierarchy, evidence, milestones (see `/help`) |

**Config & Policy**

| Command | Purpose |
|---|---|
| `/model [name\|list]` | List models from the active provider, or set by id |
| `/effort <level>` | `auto` `low` `medium` `high` `xhigh` (`auto` classifies the prompt) |
| `/stream [auto\|on\|off]` | Token streaming (default **auto** = on when supported) |
| `/mode <mode>` | `strict` `balanced` `autonomous` `custom` |
| `/nyx [on\|off]` | Redteam persona |
| `/permissions` | Policy + gates |
| `/tools` | Registered tools |

**Providers**

| Command | Purpose |
|---|---|
| `/providers` | List providers (settings + `providers.db`) |
| `/connect …` | Add provider (or open the palette with bare `/connect`) |
| `/disconnect <name>` | Remove a saved provider |
| `/use <name>` | Switch session provider (aliases work, e.g. `nvidia` → `NVIDIA NIM`) |
| `/models [provider]` | List models across providers |

**Workflows / Meta**

| Command | Purpose |
|---|---|
| `/workflows` / `/workflow …` | Save / run / show / delete workflows |
| `/help` | Grouped help |
| `/clear` | Clear the output pane |
| `/exit` | Quit (`Ctrl+D`) |

---

## Providers

`stub` is always registered as an offline fallback. Real HTTP providers use
stdlib `urllib` (no extra dependency):

| Kind | Notes |
|---|---|
| `anthropic` | Messages API (`/v1/messages`) |
| `openai` | Chat Completions + **SSE streaming** |
| `openai-compatible` | Same wire format (NIM, Groq, xAI, vLLM, LM Studio, …) + **streaming** |
| `ollama` | Local Ollama tags/chat API |

### Connect

1. **Env vars** — `ANTHROPIC_*` / `OPENAI_*` auto-register when set.
2. **`/connect`** or the provider palette — keys land in `providers.db`
   (encrypted via keychain); startup prefers a real connected provider over
   stub.

```
/connect
/use nvidia
/model list
/model z-ai/glm-5.2
```

`/use` updates the live session **and** persists `default_provider` so the
next launch does not fall back to offline stub. Provider aliases
(`nvidia`, catalog ids, case-insensitive names) resolve to the stored display
name.

Incomplete settings-only rows (name + model, no `base_url`) no longer block
the real database entry (fixes bad `localhost:8080` registrations).

### Streaming

| Mode | Behavior |
|---|---|
| `auto` (default) | Stream when `provider.supports_streaming` |
| `on` | Prefer streaming |
| `off` | Full response before display |

OpenAI-compatible SSE (`stream: true`) paints tokens in the transcript while
the footer shows `streaming…`. Tool-call fragments are still accumulated
before tools run.

---

## Nyx (redteam mode)

Nyx swaps the system prompt to a redteam/offensive-security persona for
authorized pentesting, CTFs, and security research. **Tools and policy are
unchanged** — the registry/policy engine remains the safety boundary.

```powershell
reid --nyx
```

```
/nyx on
/nyx off
```

---

## Subagents (`spawn_agent`)

Scoped child agents with their own policy instance, tool allowlist (default
read-only), and optional provider/model override. One nesting layer only.
Progress appears in the live subagent panel.

---

## DeepReid

Researcher → Planner → Critic pipeline for planning before implementation:

- **Researcher** — read-only files + `web_search`
- **Planner** — plan from findings (no tools)
- **Critic** — revision loop (capped)

```powershell
reid deepreid "<task>"
```

Or type `deepread` / `deep reid` at the start of the TUI input. Plans save
under `~/.reidcli/deepreid/`.

---

## Configuration

Precedence (low → high):

1. Built-in defaults  
2. Global `~/.reidcli/config.json`  
3. Project `./.reidx/config.json`  
4. `settings.json` `reidx` block  
5. Environment variables  

### Storage root

Default user data lives under **`~/.reidcli`** (override with `REIDX_STORAGE`).
Older installs (`.reidx`, Windows `%APPDATA%\Reid`) are migrated once on first
access.

### `settings.json`

Auto-created at `~/.reidcli/settings.json` on first run / `npm install`. Empty
env placeholders do **not** wipe ambient `ANTHROPIC_API_KEY` / `OPENAI_API_KEY`.
Project-local `settings.json` still wins when found by walking up from CWD.
See `settings.example.json` in the repo.

Path resolution:

1. `$REIDCHAT_SETTINGS`  
2. Project `settings.json` (walk upward)  
3. `~/.reidcli/settings.json` (seeded if missing)  
4. Legacy `~/Reidchat.json` only until the new path exists  

### Environment variables

| Variable | Effect |
|---|---|
| `REIDX_PROVIDER` | Default provider name |
| `REIDX_WORKSPACE` | Workspace root |
| `REIDX_STORAGE` | Storage root (default `~/.reidcli`) |
| `REIDX_PERMISSION_MODE` | `strict` `balanced` `autonomous` `custom` |
| `REIDX_LOG_LEVEL` | Log level |
| `REIDX_INSECURE` | Disable TLS verify (dev only) |
| `REIDX_COLOR` | `auto` `truecolor` `256` `16` `none` |
| `NO_COLOR` | Disable colour |
| `REIDCHAT_SETTINGS` | Explicit settings path |
| `ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL` / `ANTHROPIC_MODEL` | Anthropic provider |
| `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL` | OpenAI / compatible |

```powershell
reid config-show
```

---

## Permission modes

| Mode | Behavior |
|---|---|
| `strict` | Approve nearly everything; shell often denied |
| `balanced` | (Default) Low allowed; medium/high prompt |
| `autonomous` | Low/medium allowed; high still prompts |
| `custom` | Explicit allowlists only |

Path confinement and shell denylist apply in all modes.

---

## Tools

| Tool | Risk | Purpose |
|---|---|---|
| `read_file` | low | Read file text |
| `write_file` | medium | Create/overwrite file |
| `patch_file` | medium | Unique substring replace |
| `list_dir` | low | List directory |
| `find_files` | low | Glob search |
| `grep_files` | low | Regex search |
| `run_command` | high | Shell with policy + timeout |
| `web_search` | high | DuckDuckGo (stdlib, no API key) |
| `spawn_agent` | medium | Scoped child agent |

---

## Sessions and persistence

```
~/.reidcli/
  settings.json
  providers.db / providers.json
  workflows.json
  sessions/<id>/
    meta.json
    transcript.jsonl
    tasks.json
    goals.json
    events.jsonl
    costs.jsonl
  deepreid/<run-id>.md
```

**Resume is real:** `reid resume <id>` reloads transcript messages (recent
cap). Context can be compacted with `/compact` or auto-compact near the model
window limit.

---

## Headless / exec mode

```powershell
reid exec "list the current dir"
reid exec --nyx "recon plan for <authorized target>"
```

Stdout = answer; tool count on stderr. Exit `1` on provider errors. Approver
auto-allows in exec mode.

---

## Development

```powershell
pytest
ruff check src tests
```

Tests cover policy, tools, session, reasoning/thinking split (including GLM
`<parameter name="reasoning">`), agent soft-errors, providers/aliases,
context windows, compact, cost, effort auto, settings seed, and **SSE
streaming**.

### Project structure

```
ReidX/
  pyproject.toml
  package.json              # npm wrapper (reid → python -m reidx)
  settings.example.json
  bin/reidx.js
  scripts/postinstall.js    # pip install + seed ~/.reidcli/settings.json
  src/reidx/
    app/                    # Typer CLI
    config/                 # models, loader, settings seed, storage (~/.reidcli)
    diagnostics/
    session/ tasks/ goals/ workflows/
    policy/
    provider/               # HTTP providers, SSE stream, context_windows, registry
    provider_manager/       # catalog, keychain DB, connect palette
    tools/
    runtime/                # agent, orchestrator, compact, cost, effort_auto, reasoning
    deepreid/ nyx/
    ui/                     # full-screen TUI, slash commands, terminal_host
  tests/
  docs/
```

---

## What works now

- Full-screen TUI: footer, scroll, collapsible CoT/tools, subagent panel,
  Escape-to-stop, `/` menu, host-friendly terminal colours
- **Token streaming** (OpenAI-compatible SSE) with `/stream auto|on|off`
- **Drag-select + red highlight**, `/copy` / Ctrl+Y for last AI reply
- Soft provider errors (HTTP/network) keep the session up
- Provider connect reliability: aliases, startup pick over stub, DB overrides
  incomplete settings, palette key-save fix
- Context windows from API metadata + known-model table; auto-compact; `/cost`
- `/effort auto` prompt classification; expanded thinking-tag parsers
- Real agent loop + Anthropic/OpenAI/compatible/Ollama providers
- Session resume, tasks, goals, workflows, DeepReid, Nyx
- Settings auto-seed under `~/.reidcli` (npm + first launch)
- Headless `exec`, structured persistence, test suite

## What is stubbed (extension-ready)

- **MCP** — config schema + lifecycle slots; full stdio/JSON-RPC TODO  
- **Patch tool** — single exact replace; structured diff preview TODO  
- **Automation** — one-shot exec; scheduling/background TODO  
- **Anthropic native stream** — OpenAI-compatible stream is implemented;
  Anthropic event-stream TODO  
- **DeepReid Builder role** — plan only in v1; implementation still main agent  
- **Escape-to-stop** does not cancel DeepReid pipeline stages  

---

## License

MIT
