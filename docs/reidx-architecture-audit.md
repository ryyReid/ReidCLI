# ReidX Architecture Audit

**Version:** 0.1
**Audited:** the scaffold under `src/reidx/` against `reidx-build-plan.md` and `agent-first-cli-spec.md`.
**Stance:** critique over praise. File-aware. Concrete.

---

## 1. Verdict

The scaffold is **credibly shaped but not yet trustworthy**. The layering, composition point, and persistence layout are right. What is wrong is a handful of **real correctness bugs** hiding behind clean module boundaries, plus the usual first-pass thinness in provider, integration, and UX layers.

The most important finding: **resume is broken**, and **the agent loop corrupts its own message history** on any turn that returns both text and tool calls. These are not polish issues; they are runtime-coherence bugs.

---

## 2. What is strong and should remain

| Area | Evidence | Why it stays |
|---|---|---|
| **Layering / dependency direction** | `config → policy.models (leaf) → session/tasks → provider/tools → runtime → ui/app` | No cycles. UI depends on runtime, never the reverse. This is the shape `reidx-build-plan.md` §7 asks for. |
| **Orchestrator as composition root** | `runtime/orchestrator.py:27` is the only site that instantiates `PolicyEngine`, `SessionStore`, `Agent` together | UI (`ui/repl.py`) and automation (`automation/exec.py`) both call into it. One behavior stack, not two. |
| **Structured persistence layout** | `session/store.py:1` docstring; per-session `meta.json` / `transcript.jsonl` / `tasks.json` / `events.jsonl` | Inspectable, replayable, migratable. Better than most prototypes. |
| **Policy engine does real work** | `policy/engine.py:42` `evaluate()`, `:61` `check_path()`, `:81` `check_command()` | Modes + path confinement + command allowlist/denylist. Not hand-wavy. |
| **StubProvider** | `provider/stub.py` | Runtime is exercisable end-to-end with no API keys. This is why the scaffold is testable today. |
| **ToolResult structured failure** | `tools/base.py:30` + `tools/registry.py:47` catches exceptions → `ToolResult.fail` | Tools cannot crash the runtime. Failures become model-readable text. |
| **Config merge precedence** | `config/loader.py:69` defaults < global < project < env | Correct precedence, env wins, deep-merge is right. |

Do not rewrite these. Build on them.

---

## 3. Correctness bugs (must fix)

These are wrong, not thin. Each is concrete and reproducible.

### 3.1 Agent loop double-appends assistant messages
**File:** `runtime/agent.py:86-94`

When a provider returns both `text` AND `tool_calls` (the normal OpenAI/Anthropic shape), the loop:
1. line 87: appends `Message(role="assistant", content=resp.text)` (text-only)
2. line 94: appends a SECOND `Message(role="assistant", content=resp.text, tool_calls=resp.tool_calls)`

The transcript now has two assistant messages for one provider turn. On the next loop iteration both are sent back to the provider, corrupting history. The stub provider masks this because its tool-call turn has empty text, but any real provider will break.

**Fix:** append exactly one assistant message per turn, carrying both `content` and `tool_calls`.

### 3.2 Resume is broken — repl() always starts a fresh session
**File:** `ui/repl.py:27` + `app/commands.py:58-67`

`app/commands.py` `resume` calls `orch.resume_session(id)` (sets `orch.state`), then calls `repl(orch)`. But `repl()`'s first line is `orchestrator.start_session(title="interactive")`, which **overwrites `orch.state` with a brand-new session**. The resumed state is discarded immediately. `/resume` from inside the REPL has the same problem: `commands.py:60` calls `resume_session` but the next prompt still runs under whatever session the REPL started with, because `resume_session` replaces `state` but the REPL loop never re-checks.

**Fix:** `repl()` must accept an already-resumed orchestrator and not start a new session when `state` is present. `resume_session` must reload transcript into `state.messages` (see 3.5).

### 3.3 /mode drifts between session and policy engine
**File:** `runtime/orchestrator.py:111-118` + `ui/commands.py:73-77`

`set_permission_mode` has split behavior:
- When `state is None`: mutates `config.policy.default_mode` + `self.policy.set_mode(mode)`. Consistent.
- When `state` exists: sets `state.mode_override` + `session.permission_mode` + persists — but **does NOT call `self.policy.set_mode(mode)`**.

So with an active session, `orchestrator.policy.mode` is stale. `commands.py:75` (`/mode` with no arg) reads `orchestrator.policy.mode.value` and prints a mode that does not reflect the override the user just set. The agent loop patches this at `agent.py:78-79` by re-applying `state.mode_override` to the policy engine during the turn — but only during the turn. Outside turns, the policy engine is wrong.

**Fix:** `set_permission_mode` should always call `self.policy.set_mode(mode)`. Drop the `mode_override` indirection on `RuntimeState` — the session's `permission_mode` and the policy engine's `mode` should be a single source of truth, kept in sync by the orchestrator.

### 3.4 /tools shows the wrong registry
**File:** `ui/commands.py:79`

`/tools` calls `default_registry()` — a **fresh** registry — instead of the orchestrator's `orchestrator.tools`. Any future config-driven tool registration will be invisible to `/tools`. Same bug in `app/commands.py:88` (the `tools` Typer command).

**Fix:** read from the orchestrator's registry.

### 3.5 Resume does not restore transcript/context
**File:** `runtime/orchestrator.py:60-68`

`resume_session` creates `RuntimeState(session=session)` with `messages=[]`. The `transcript.jsonl` on disk is never read back. The spec (`agent-first-cli-spec.md` §5.2) requires resume to carry "some or all of its history and context intact." Today resume gives you a fresh conversation wearing an old session's name.

**Fix:** persist messages incrementally to `transcript.jsonl` as they are produced (not just a turn summary), and have `resume_session` reload them into `state.messages`.

### 3.6 Tasks marked COMPLETED even on failure
**File:** `runtime/orchestrator.py:102`

`store.update_status(task.id, TaskStatus.COMPLETED, ...)` runs unconditionally. If the agent exhausted its step budget, if every tool errored, or if `final_text` is the "[agent] step budget exhausted" message, the task still says COMPLETED. The task state machine (`tasks/models.py`) has FAILED and BLOCKED statuses that are never used.

**Fix:** derive task status from the turn outcome. Step-budget exhaustion or all-tools-failed → FAILED with the final text as `error`. Otherwise COMPLETED.

### 3.7 Shell tool has dead no-op policy call
**File:** `tools/shell_tool.py` (the line after approval resolution)

After `check_command` and the approver gate, the shell tool calls `ctx.policy.evaluate(ActionKind.SHELL_EXEC)` and throws away the result. It is a no-op that reads as if it does something. Remove it.

### 3.8 Triple-gating of tool actions
**Files:** `tools/registry.py:38`, `tools/file_tools.py` `_safe_read`/`_safe_write`, `tools/shell_tool.py` `check_command`

A file write is gated three times: (1) registry evaluates `TOOL_CALL` with the tool's risk, (2) `WriteFileTool.execute` calls `_safe_write` → `check_path` → `evaluate(FILE_WRITE)`, (3) `ToolContext.is_writable` is a third path check that nothing calls. The registry gate uses `ActionKind.TOOL_CALL` (classified MEDIUM) with the tool's own risk — but `TOOL_CALL` is a generic class, while the tool-specific action kind (`FILE_WRITE`, `SHELL_EXEC`) is what the policy engine actually understands for path/command checks.

**Fix:** pick one gating site. The registry should gate by `tool.definition.risk` only (approve/deny the call); the tool should do the action-specific checks (`check_path`, `check_command`) internally. Remove the registry's `TOOL_CALL` evaluation, or keep it as a coarse gate and remove the per-tool re-evaluation. Do not do both.

---

## 4. Structurally risky (not bugs, but future pain)

### 4.1 Transcript persistence is summary-only, not restorable
**File:** `runtime/orchestrator.py:96-97`

The transcript entry is a single `{"user": ..., "assistant": ..., "tools": [...]}` blob per turn. It is human-readable and useful for replay viewing, but it is **not a restorable message list**. The actual `state.messages` (system + user + assistant + tool messages with tool_call_ids) is never persisted. This is the root cause of 3.5.

**Fix:** write each message to `transcript.jsonl` as a separate event (role, content, tool_calls, tool_call_id) as it is appended. The turn summary can stay as an `events.jsonl` entry. Then resume reads `transcript.jsonl` back into `Message` objects.

### 4.2 Policy engine is shared and mutable across turns
**File:** `runtime/agent.py:77-79, 109-110`

The agent saves `previous_mode`, sets the override, and restores in `finally`. This is fragile: the `PolicyEngine` is a long-lived object on the orchestrator, and the agent reaches up and mutates it. If anything else reads `policy.mode` during a turn (e.g. a future background worker), it sees the override. Once subagents land (`reidx-build-plan.md` Phase 9), this pattern breaks completely — two agents cannot share one mutable engine.

**Fix:** the policy engine should take a mode at evaluation time, or the orchestrator should own mode selection and pass the effective mode into the agent per-turn. The agent should not mutate shared state.

### 4.3 Provider contract has no error/retry/usage surface
**File:** `provider/base.py:40-50`

`chat()` returns `ProviderResponse` or raises. There is no:
- structured error type (rate limit, auth, network, context overflow)
- retry/backoff hook
- capability discovery (does this model support tool calls? streaming?)
- streaming surface
- cumulative usage accounting (Usage is per-response, never aggregated)

The agent loop has no try/except around `provider.chat()` — a provider exception crashes the turn (caught only by the REPL's broad `except` in `ui/repl.py:50`). For exec mode, a provider crash prints a traceback.

**Fix (Phase 6+):** define `ProviderError` with categories, wrap `chat()` in a retry layer, accumulate `Usage` into `RuntimeState`, and add an optional `stream()` method to `BaseProvider`.

### 4.4 EventLog is not concurrency-safe
**File:** `diagnostics/logger.py:18`

`EventLog.write` opens, writes, closes per call. If a future UI drains events on a background thread (the hollowgreen prototype did exactly this), interleaved writes will corrupt JSONL lines. No lock.

**Fix:** add a `threading.Lock` per `EventLog` instance, or open a file handle and guard writes. Cheap to do now.

### 4.5 Config save writes API keys in plaintext
**File:** `config/loader.py:82-88`

`save_global` writes the full config including `ProviderConfig.api_key` (a `SecretStr`) to `~/.reidx/config.json` in plaintext. `SecretStr` only protects in-memory display, not serialization via `model_dump_json`. The `config-show` command excludes it for display, but the saved file leaks it.

**Fix:** `save_global`/`save_project` should exclude `api_key` by default, or write to a separate `secrets.json` with restrictive permissions. Document the choice.

### 4.6 No tests
**File:** `pyproject.toml` lists `pytest` in dev deps; `tests/` does not exist.

For a "serious foundation I can continue building on," zero tests is a risk. The policy engine, tool registry, session/task stores, and agent loop are all pure-Python and trivially testable. Each bug in §3 would have been caught by a 5-line test.

**Fix (Phase 5):** add `tests/` with focused tests for policy engine, tool dispatch, session round-trip, and agent loop (using StubProvider).

---

## 5. What is thin (expected for first pass, upgrade later)

| Layer | Current state | Next upgrade |
|---|---|---|
| **Provider** | `StubProvider` only; synchronous, no retry/usage/streaming | Real OpenAI/Anthropic client with retry, structured errors, usage aggregation |
| **MCP** | `integrations/mcp.py` has config schema + lifecycle stubs | stdio subprocess + JSON-RPC initialize/tools/call (Phase 6) |
| **Patch tool** | `tools/file_tools.py:PatchFileTool` single exact-match replace | Diff preview, multi-hunk, structured edits, rollback (Phase 5/6) |
| **UX** | `ui/repl.py` is a 57-line REPL; `ui/render.py` is minimal | Status line, session/task/permission views, approval UX, transcript browser (Phase 5) |
| **Diagnostics** | `EventLog` + stdlib logger | Structured logs, tool timings, token usage, `doctor` depth, trace mode |
| **Automation** | `automation/exec.py` one-shot | Scheduling, background tasks, machine-readable output, exit-code semantics (Phase 7) |

---

## 6. Likely future failure points

1. **Real provider will expose 3.1 immediately.** The double-append bug is invisible with the stub because the stub's tool-call turn has empty text. The first real provider call that returns text + tool_calls together will corrupt history and the model will behave erratically. Fix 3.1 before any real provider work.
2. **Resume will be the first user-facing complaint.** A user who runs `reidx resume <id>` expecting continuation will get a blank session. Fix 3.2 + 3.5 + 4.1 together.
3. **Subagents will break 4.2.** The shared-mutable-policy-engine pattern cannot survive a second agent. Resolve before Phase 9.
4. **No concurrency story.** `EventLog`, `SessionStore`, and `TaskStore` all do bare file IO. Background tasks (Phase 7) will race. Add locks or a single-threaded IO owner.
5. **Context will grow unbounded.** `state.messages` accumulates forever. There is no summarization, truncation, or context-budget. Long sessions will hit provider token limits and fail opaquely. Needs a context-management strategy before long-session use.

---

## 7. Recommended next 3 implementation phases

### Phase 5a — Correctness fixes (do first, small, verifiable)
- Fix 3.1 double-append (one assistant message per turn)
- Fix 3.2 resume REPL clobber (repl respects existing state)
- Fix 3.3 /mode drift (single source of truth for mode)
- Fix 3.4 /tools shows orchestrator registry
- Fix 3.6 task status reflects outcome
- Fix 3.7 remove dead shell no-op
- Fix 3.8 collapse triple-gating to one site
- Add `tests/` with focused unit tests for policy, tools, session, agent loop

### Phase 5b — Real resume + transcript persistence
- Fix 4.1 write messages to `transcript.jsonl` incrementally
- Fix 3.5 `resume_session` reloads messages into `state.messages`
- Add `/transcript` command to view a session's history

### Phase 5c — Interaction upgrade (the UX work)
- Persistent status line (session id, mode, model, workspace, task count)
- `/sessions` with freshness + task counts
- `/tasks` with status filtering
- `/permissions` showing current mode + effective gates
- Approval UX with context (what command, what path, what risk)
- `/transcript` browsing
- `/rewind` stub (drop last turn from state + persist)

### Phase 6 — Provider + integration operationalization
- Real OpenAI or Anthropic provider with retry, structured errors, usage aggregation
- `ProviderError` taxonomy
- MCP stdio bridge (replacing the stub)
- Diff preview + structured patch tool

### Phase 7 — Automation + hardening
- `exec` with machine-readable output (`--json`)
- Exit code semantics mapped to task status
- Context management (summarization/truncation)
- Concurrency locks on stores
- Trace mode + `doctor` depth

---

## 8. What not to do yet

- Do not add a second provider before fixing 3.1 (you will chase phantom bugs).
- Do not build subagents before fixing 4.2.
- Do not add background/scheduled tasks before adding locks to stores (4.4).
- Do not polish the TUI visually before the status line and approval UX are real (5c).
- Do not add MCP servers before the provider layer has structured errors (a brittle MCP bridge + a brittle provider = unrecoverable runs).

---

## 9. Summary

The scaffold's **shape** is right. Its **wiring** has real bugs. The order is:

1. Fix the correctness bugs in §3 (small, verifiable, unblocks everything).
2. Make resume actually resume (§4.1, §3.5).
3. Then do the UX upgrade (§5c).
4. Only then touch providers, MCP, or automation.

The scaffold is worth building on. It is not yet safe to build on without the §3 fixes.
