# ReidX Phase 5 Plan

**Version:** 0.1
**Derived from:** `docs/reidx-architecture-audit.md`
**Scope:** correctness fixes + real resume + interaction upgrade.

This plan converts audit findings into ordered, verifiable work. It is split into 5a / 5b / 5c so each step can be tested independently.

---

## 1. Phase 5a — Correctness fixes (do first)

Every item here is a bug from audit §3. Small, isolated, verifiable.

### 5a.1 Single assistant message per provider turn
**File:** `src/reidx/runtime/agent.py`
**Bug:** audit §3.1 — double-append when provider returns text + tool_calls.
**Change:** append one `Message(role="assistant", content=resp.text, tool_calls=resp.tool_calls)` per turn. Drop the separate text-only append.

### 5a.2 REPL respects existing session state
**File:** `src/reidx/ui/repl.py`
**Bug:** audit §3.2 — `repl()` always calls `start_session`, clobbering resumed state.
**Change:** `repl(orchestrator)` starts a session only if `orchestrator.state is None`. If state exists (resume path), reuse it.

### 5a.3 Single source of truth for permission mode
**Files:** `src/reidx/runtime/orchestrator.py`, `src/reidx/runtime/state.py`, `src/reidx/runtime/agent.py`
**Bug:** audit §3.3 — `set_permission_mode` doesn't update the policy engine when state exists; `mode_override` duplicates session.permission_mode.
**Change:**
- Remove `mode_override` from `RuntimeState`.
- `Orchestrator.set_permission_mode` always calls `self.policy.set_mode(mode)` AND updates `session.permission_mode` AND persists.
- `Agent.run_turn` no longer mutates `self.policy.mode` — the orchestrator owns mode, the agent reads it.

### 5a.4 /tools reads from the orchestrator's registry
**Files:** `src/reidx/ui/commands.py`, `src/reidx/app/commands.py`
**Bug:** audit §3.4 — `/tools` and `reidx tools` build a fresh `default_registry()`.
**Change:** both read `orchestrator.tools.definitions()`.

### 5a.5 Task status reflects turn outcome
**File:** `src/reidx/runtime/orchestrator.py`
**Bug:** audit §3.6 — tasks always marked COMPLETED.
**Change:** derive status: if `final_text` starts with `[agent] step budget exhausted` or all tool calls failed → `TaskStatus.FAILED` with `error=final_text`. Otherwise COMPLETED.

### 5a.6 Remove dead shell no-op
**File:** `src/reidx/tools/shell_tool.py`
**Bug:** audit §3.7 — `ctx.policy.evaluate(ActionKind.SHELL_EXEC)` result discarded.
**Change:** delete the line.

### 5a.7 Collapse triple-gating to one site
**Files:** `src/reidx/tools/registry.py`, `src/reidx/tools/file_tools.py`, `src/reidx/tools/shell_tool.py`
**Bug:** audit §3.8 — tools gated 2-3 times.
**Change:**
- Registry gates the call by `tool.definition.risk` (coarse allow/deny/prompt). Use the tool's risk directly, not `ActionKind.TOOL_CALL`.
- File tools keep `check_path` (action-specific path confinement). Remove the redundant `evaluate()` inside `_safe_read`/`_safe_write` — `check_path` already calls `evaluate`.
- Shell tool keeps `check_command`.
- Remove unused `ToolContext.is_writable` (dead third path).

### 5a.8 Add focused tests
**New file:** `tests/test_policy.py`, `tests/test_tools.py`, `tests/test_session.py`, `tests/test_agent.py`
**Why:** audit §4.6 — zero tests; each §3 bug would have been caught by a 5-line test.
**Coverage:**
- policy: mode matrix, path confinement, command denylist
- tools: registry dispatch, unknown tool, file tool path safety
- session: create/get/list round-trip
- agent: StubProvider turn with tool call, step-budget exhaustion

---

## 2. Phase 5b — Real resume + transcript persistence

### 5b.1 Incremental message persistence
**File:** `src/reidx/runtime/orchestrator.py` + `src/reidx/session/store.py`
**Bug:** audit §4.1 — only a turn summary is persisted, not restorable messages.
**Change:** add `SessionStore.append_message(session_id, message)` that writes one JSONL line per `Message` (role, content, tool_calls, tool_call_id). The orchestrator calls it as messages are appended during a turn. Keep the turn-summary event in `events.jsonl`.

### 5b.2 Resume restores messages
**File:** `src/reidx/runtime/orchestrator.py`
**Bug:** audit §3.5 — `resume_session` creates empty `messages`.
**Change:** `resume_session` reads `transcript.jsonl` back into `Message` objects and loads them into `state.messages`. Cap at a configurable max (default 100 most-recent) to bound context.

### 5b.3 /transcript command
**File:** `src/reidx/ui/commands.py`
**Change:** `/transcript [n]` prints the last n message exchanges for the active session from `state.messages`.

---

## 3. Phase 5c — Interaction upgrade

### 5c.1 Persistent status line
**File:** `src/reidx/ui/render.py` + `src/reidx/ui/repl.py`
**Change:** after each turn and before each prompt, print a one-line status: `session <id> | <mode> | <model> | <workspace> | <task-count> tasks`. Reuse `render.status_line` but make it the prompt prefix.

### 5c.2 /sessions with freshness + task counts
**File:** `src/reidx/ui/render.py` + `src/reidx/ui/commands.py`
**Change:** `print_sessions` adds columns: `updated` (relative), `tasks` (count from TaskStore), `tools` (count of tool calls from events.jsonl tail).

### 5c.3 /tasks with status filtering
**File:** `src/reidx/ui/commands.py`
**Change:** `/tasks [status]` filters by status. Default shows all. Add a summary line: `N pending · N active · N completed · N failed`.

### 5c.4 /permissions view
**File:** `src/reidx/ui/commands.py` + `src/reidx/ui/render.py`
**Change:** `/permissions` shows: current mode, blocked commands, allowed commands, writable roots, read-only paths, shell timeout. Read from `orchestrator.policy` + `orchestrator.config.policy`.

### 5c.5 Approval UX with context
**File:** `src/reidx/ui/repl.py`
**Change:** `_make_approver` shows the prompt text with risk context and offers `[y]es / [n]o / [a]lways-this-session`. `always` flips the policy mode to AUTONOMOUS for the session (with a confirmation). Default stays `n`.

### 5c.6 /rewind stub
**File:** `src/reidx/ui/commands.py` + `src/reidx/runtime/orchestrator.py`
**Change:** `/rewind` drops the last turn from `state.messages` (pop back to the last `user` message) and persists. Mark as stub for deeper rewind later.

---

## 4. Acceptance criteria

Phase 5 is done when:
- `reidx exec "list dir"` still works and produces exactly one assistant message per turn (verifiable in transcript.jsonl).
- `reidx resume <id>` followed by a prompt continues the prior conversation (the agent sees prior messages).
- `/mode strict` then `/mode` (no arg) prints `strict`.
- `/tools` matches the tools the orchestrator actually has.
- A failed turn (step budget) marks the task FAILED, not COMPLETED.
- `pytest` passes with the new test suite.
- `/status`, `/sessions`, `/tasks`, `/permissions`, `/transcript` all work and show real data.
- `ruff check` is clean.

---

## 5. Out of scope for Phase 5

- Real provider implementation (Phase 6).
- MCP stdio bridge (Phase 6).
- Diff preview / structured patch (Phase 6).
- Background/scheduled tasks (Phase 7).
- Context summarization (Phase 7).
- Subagents (Phase 9).

---

## 6. Implementation order

1. 5a.1 (agent loop) — unblocks real providers.
2. 5a.3 (mode source of truth) — unblocks 5c.4.
3. 5a.7 (gating collapse) — cleanup.
4. 5a.4, 5a.5, 5a.6 — small fixes.
5. 5a.2 — repl respects state.
6. 5a.8 — tests for everything above.
7. 5b.1 + 5b.2 — real resume.
8. 5b.3 — /transcript.
9. 5c.1–5c.6 — UX.
10. Verify against §4 acceptance criteria.
