# ReidX

Terminal-native personal intelligence and coding CLI with an agent-first runtime.

A real runtime — not a chat wrapper. Sessions, tasks, tools, policy gates,
providers, and persistence are first-class. A genuine full-screen TUI (not an
inline redraw hack) with a locked-to-bottom footer, full-width transcript,
input history, token streaming, drag-to-copy, collapsible reasoning, a live
subagent panel, and a live `/` command menu.

**Status:** Phase 5 complete, plus TUI rewrite, multi-provider HTTP clients
(Anthropic / OpenAI / OpenAI-compatible / Ollama / **OpenCode Go**), SSE
streaming, auto context windows, compact + cost, soft provider errors, DeepReid,
Nyx, web search, and workflows. See `docs/` for architecture notes.

---

## Target stack

- **Python** 3.12+
- **Typer** — CLI command surface
- **Pydantic v2** — schemas and validation
- **Rich** — markdown, tables, panels (width tracks the live terminal)
- **prompt_toolkit** — full-screen TUI (layout, input, completion, mouse)
- **stdlib `urllib`** only for HTTP/SSE (no extra HTTP client package)

---

## Quick start

### Option A: install via npm

Requires Python 3.12+ on `PATH`. The package pip-installs on `npm install` and
seeds `~/.reidcli/settings.json`.

```powershell
npm install -g @agxnte/reidx
```

### Option B: install from source

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

> macOS/Linux: `source .venv/bin/activate`

### Verify and run

```powershell
reid doctor
reid
```

- **Offline:** stub provider works without keys; use `/connect` for a real model.
- **Real chat:** env keys and/or `/connect`, then `/use <provider>`.

---

## The interactive TUI

Full-screen `prompt_toolkit` app (alternate screen; host scrollback restored on
exit). Output fills the **live terminal width**; the footer stays one compact line.

| Feature | Details |
|---|---|
| **Footer** | Mode · model · effort · tokens/context · workspace · cost · Copied! |
| **Token streaming** | OpenAI-compatible SSE; `/stream auto` (default) / `on` / `off` |
| **Input history** | **↑ / ↓** (and Ctrl+P / Ctrl+N); saved under `~/.reidcli/input_history` |
| **Scroll** | Mouse wheel; PageUp/PageDown; Shift+↑/↓ line scroll |
| **Copy** | Drag (red highlight), double/triple-click; `/copy` or **Ctrl+Y** = last AI reply |
| **Reasoning** | Collapsible CoT (`Ctrl+O`); splits `<think>`, GLM `<parameter name="reasoning">`, fences |
| **`/` menu** | Commands, enums, `/use` aliases, `/model` list, **`/resume` session pick list** |
| **Pastes** | Large/multi-line → `[Pasted text #N +… lines]` |
| **Escape** | Cancel in-flight turn at next safe point |
| **DeepReid** | Type `deepread` / `deep reid` at start of input (border pulses green) |
| **Nyx** | `/nyx on` redteam persona (prompt only) |

### Keyboard shortcuts

| Key | Action |
|---|---|
| **↑ / ↓** | Input history (when not in `/` completion menu) |
| **Ctrl+P / Ctrl+N** | Same as ↑ / ↓ history |
| **← / →** | Cycle effort when the box is empty; else move cursor |
| **Shift+↑ / ↓** | Scroll transcript by line |
| **PageUp / PageDown** | Scroll transcript by page |
| **Tab** | Accept `/` completion |
| **Ctrl+O** | Expand/collapse reasoning + tool calls |
| **Ctrl+Y** | Copy last AI reply |
| **Ctrl+C** | Copy selection if any, else clear line |
| **Esc** | Stop in-flight response |
| **Ctrl+D** | Exit |

---

## Command surface

### Top-level CLI

| Command | Purpose |
|---|---|
| `reid` | Interactive TUI (default) |
| `reid interactive "<prompt>"` | TUI + first turn |
| `reid --file <path>` / `-f` | Initial prompt from file |
| `reid --nyx` | Start with Nyx on |
| `<cmd> \| reid` | Stdin as first turn |
| `reid exec "<prompt>"` | Headless one-shot |
| `reid deepreid "<task>"` | Headless plan pipeline |
| `reid resume <session-id>` | Resume then TUI |
| `reid sessions` | List sessions |
| `reid config-show` / `tools` / `doctor` / `version` | Utility |

### Slash commands (highlights)

**Session**

| Command | Purpose |
|---|---|
| `/status` | Session summary |
| `/sessions` | List sessions (with message counts) |
| `/resume <id>` | Resume; type `/resume ` for a pick list; **re-prints conversation** |
| `/transcript [n]` | Show last n messages |
| `/rewind` | Drop last turn |
| `/compact [n\|--force]` | Summarize older turns |
| `/cost [reset]` | Session spend estimate |
| `/copy` | Last AI reply → clipboard |
| `/recap` / `/review <pr>` | Recap / PR review helpers |

**Config**

| Command | Purpose |
|---|---|
| `/model [list\|id]` | List or set model; **context meter updates for that model** |
| `/effort auto\|low\|medium\|high\|xhigh` | Reasoning effort (`auto` classifies the prompt) |
| `/stream auto\|on\|off` | Token streaming (default **auto**) |
| `/mode` / `/nyx` / `/permissions` / `/tools` | Policy and tools |

**Providers**

| Command | Purpose |
|---|---|
| `/providers` | List connected + built-in |
| `/connect` | Palette or CLI connect |
| `/use <name>` | Switch provider (aliases: `nvidia`, `opencode`, …) |
| `/models` | Models across providers |

---

## Providers

| Kind | Notes |
|---|---|
| `anthropic` | Messages API |
| `openai` | Chat Completions + **SSE streaming** |
| `openai-compatible` | NIM, Groq, xAI, **OpenCode Go**, vLLM, LM Studio, … + streaming |
| `ollama` | Local Ollama |

### Sign in with your subscription (OAuth)

`/connect` lists two OAuth shortcuts at the top — sign in with your existing
plan instead of pasting an API key:

| Entry | Flow | Notes |
|---|---|---|
| **Sign in with Claude (OAuth)** | Browser → **paste code** | Opens `claude.ai`; copy the code from the hosted callback page back into the palette. Token used as `Authorization: Bearer` + `anthropic-beta`. |
| **Sign in with Codex (OAuth)** | Browser → auto-caught | Opens `auth.openai.com`; a local server on `:1455` catches the redirect. Device-code flow also available for headless. |

Tokens are stored **encrypted** in `providers.db`. OAuth access tokens are
short-lived; re-run `/connect` if a session expires (auto-refresh on load is
planned).

### OpenCode Go

[OpenCode Go](https://opencode.ai/go) — Zen subscription for open coding models.

```powershell
$env:OPENCODE_API_KEY = "your-zen-key"   # or OPENCODE_GO_API_KEY
# optional: $env:OPENCODE_GO_MODEL = "glm-5.2"
reid
/use opencode
/model list
```

Or `/connect` → **OpenCode Go** → paste key.

A **single** catalog entry (`kind: opencode-go`) that auto-routes each request
by model: GLM/Kimi/DeepSeek/MiMo go over the OpenAI-compatible endpoint,
MiniMax/Qwen over the Anthropic Messages endpoint — one key, one entry.

| Catalog entry | Base origin | Routed backend | Example models |
|---|---|---|---|
| **OpenCode Go** | `https://opencode.ai/zen/go/v1` | OpenAI-compatible | `glm-5.2`, `kimi-k2.7-code`, `deepseek-v4-flash`, `mimo-v2.5` |
| **OpenCode Go** | `https://opencode.ai/zen/go/v1` | Anthropic Messages | `qwen3.7-plus`, `minimax-m2.7` |

Aliases: `opencode`, `opencode-go`, `zen-go`, `go`, `opencode-anthropic`, `zen-go-anthropic`.

HTTP clients send a normal **User-Agent** so Cloudflare does not block stdlib
`urllib` (403 HTML / error 1010).

### Context windows

The footer meter (`used/max`) updates when you change model or provider:

1. Provider `/models` metadata when present  
2. Known model-id table (e.g. **GLM-5.2 → 1M**, Claude → 200k)  
3. Session cache / id size tags / 128k default  

`/model glm-5.2` should show something like `1.4k/1.0M`, not a stale 200k seed.

### Streaming

| Mode | Behavior |
|---|---|
| `auto` (default) | Stream when the provider supports SSE |
| `on` | Prefer streaming |
| `off` | Wait for full reply |

---

## Tools

| Tool | Risk | Purpose |
|---|---|---|
| `read_file` | low | Read file text |
| `write_file` | medium | Create/overwrite |
| `patch_file` | medium | Unique substring replace |
| `list_dir` | low | Directory listing as **Name / Type** columns |
| `find_files` | low | Glob |
| `grep_files` | low | Regex search |
| `run_command` | high | Shell + policy (PowerShell on Windows; rewrites `head`/`tail`) |
| `set_context_window` | low | Agent can fix footer max context (`1M`, `200k`, …) |
| `list_provider_catalog` | low | Search known providers (OpenCode Go, NIM, …) |
| `list_connected_providers` | low | What’s registered in this session |
| `connect_provider` | high | Save API key + register (user approves; like `/connect`) |
| `use_provider` | low | Switch active provider (like `/use`) |
| `set_model` | low | Set session model id (like `/model`) |
| `disconnect_provider` | high | Remove a saved provider |
| `web_search` | high | DuckDuckGo (no API key) |
| `spawn_agent` | medium | Scoped child agent |

---

## Configuration

Precedence (low → high): defaults → `~/.reidcli/config.json` → project
`.reidx/config.json` → `settings.json` `reidx` block → environment.

### Storage (`~/.reidcli`)

```
~/.reidcli/
  settings.json          # auto-seeded; empty env keys ignored
  input_history          # ↑/↓ prompt history
  providers.db
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

Override root with `REIDX_STORAGE`. Legacy `.reidx` / AppData `Reid` migrate once.

### Environment variables

| Variable | Effect |
|---|---|
| `REIDX_STORAGE` | Storage root (default `~/.reidcli`) |
| `REIDX_PROVIDER` / `REIDX_WORKSPACE` / `REIDX_PERMISSION_MODE` | Defaults |
| `REIDX_LOG_LEVEL` / `REIDX_INSECURE` / `REIDX_COLOR` / `NO_COLOR` | Runtime |
| `REIDCHAT_SETTINGS` | Explicit settings path |
| `ANTHROPIC_*` / `OPENAI_*` | Auto-register those providers |
| `OPENCODE_API_KEY` or `OPENCODE_GO_API_KEY` | OpenCode Go auto-register |
| `OPENCODE_GO_MODEL` / `OPENCODE_GO_BASE_URL` | Go model / base override |

See `settings.example.json`.

---

## Permission modes

| Mode | Behavior |
|---|---|
| `strict` | Approve nearly everything |
| `balanced` | (Default) Medium/high prompt |
| `autonomous` | Low/medium free; high prompts |
| `custom` | Explicit allowlists only |

Path confinement + shell denylist apply in all modes.

---

## Sessions

- **Resume:** `/resume <id>` or `/resume ` (completion list). Restores transcript
  into model context **and** re-prints the conversation in the TUI.
- **Empty sessions** (0 messages) are starts that never finished a turn.
- **Compact:** `/compact` or auto near the model window limit.
- **Cost:** per-turn estimates + `/cost` (from public price table).

---

## Nyx / DeepReid / subagents

- **Nyx** — redteam persona (`--nyx` or `/nyx on`); tools/policy unchanged  
- **DeepReid** — Researcher → Planner → Critic; `reid deepreid` or `deepread` in TUI  
- **spawn_agent** — scoped child; live panel under the input box  

---

## Headless

```powershell
reid exec "list the current dir"
reid exec --nyx "recon plan for <authorized target>"
```

Exit `1` on provider errors. Approver auto-allows in exec mode.

---

## Development

```powershell
pytest
ruff check src tests
```

Coverage includes policy, tools, session, reasoning tags (incl. GLM parameter
blocks), soft provider errors, aliases, **context windows / bind-on-model**,
compact, cost, effort auto, settings seed, and **SSE streaming**.

```
src/reidx/
  app/  config/  diagnostics/
  session/  tasks/  goals/  workflows/
  policy/  provider/  provider_manager/  tools/
  runtime/   # agent, orchestrator, compact, cost, effort_auto, reasoning
  deepreid/  nyx/
  ui/        # TUI, slash commands, terminal_host
```

---

## What works now

- Full-width TUI: live terminal width, compact footer, input history (↑/↓)
- Streaming (`/stream auto`), drag-copy, `/copy` / Ctrl+Y
- OpenCode Go + NIM + Anthropic/OpenAI/Ollama; Cloudflare-safe User-Agent
- Auto context windows on `/model` / `/use` (GLM-5.2 → 1M, etc.)
- Soft HTTP errors; provider aliases; settings seed under `~/.reidcli`
- `/resume` pick list + visible transcript restore
- `list_dir` Name/Type listings; compact/cost/effort auto
- DeepReid, Nyx, workflows, goals, headless exec

## What is stubbed

- **MCP** full stdio/JSON-RPC  
- **Structured patch** / diff preview  
- **Anthropic native event-stream** (OpenAI-compatible stream is done)  
- **DeepReid Builder** role  
- Escape does not cancel DeepReid stages  

---

## License

MIT
