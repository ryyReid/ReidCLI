"""Orchestrator: ties session + tasks + agent into a coherent runtime.

Owns the lifetime of a session's runtime: starting, submitting tasks, resuming,
and persisting transcript/events. The orchestrator is the single place where
session, task, agent, and policy meet — UI and automation layers call into it.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from reidx.config.models import Config
from reidx.diagnostics.logger import get_logger
from reidx.goals import Goal, GoalStore
from reidx.nyx import NYX_SYSTEM_PROMPT
from reidx.policy.engine import PolicyEngine
from reidx.provider.base import BaseProvider
from reidx.provider.registry import ProviderRegistry
from reidx.runtime.agent import BASE_SYSTEM_PROMPT, PROVIDER_ERROR_PREFIX, Agent
from reidx.runtime.compact import (
    AUTO_COMPACT_RATIO,
    DEFAULT_KEEP_USER_TURNS,
    CompactResult,
    compact_messages,
    estimate_tokens,
    should_auto_compact,
)
from reidx.runtime.cost import CostLedger
from reidx.runtime.state import RuntimeState
from reidx.session.models import Session, SessionStatus
from reidx.session.store import SessionStore
from reidx.tasks.models import TaskStatus
from reidx.tasks.store import TaskStore
from reidx.tools.base import Approver
from reidx.tools.registry import ToolRegistry
from reidx.workflows.store import WorkflowStore

log = get_logger("reidx.runtime")


class Orchestrator:
    def __init__(
        self,
        config: Config,
        provider: BaseProvider,
        tools: ToolRegistry,
        providers: ProviderRegistry | None = None,
        provider_name: str | None = None,
    ) -> None:
        self.config = config
        self.provider = provider
        self.tools = tools
        self.providers = providers
        # Registry key for the active provider ("NVIDIA NIM"), not provider.name
        # which is often the generic kind ("openai-compatible").
        self.provider_name = provider_name or getattr(provider, "name", "stub")
        self.policy = PolicyEngine(config)
        from reidx.config.storage import storage_root as default_storage_root

        root = config.storage_root or default_storage_root()
        self.session_store = SessionStore(root)
        self.workflow_store = WorkflowStore(root)
        # Subagent runtime (spawn_agent tool + TUI panel subscribe to this).
        # Imported lazily to avoid an import cycle: tools/spawn_agent constructs
        # a child Agent using the registries held here.
        from reidx.runtime.subagent import SubagentManager  # noqa: PLC0415
        self.subagents = SubagentManager()
        self.agent = Agent(provider, tools, self.policy, context_extras={"orchestrator": self})
        self.state: RuntimeState | None = None
        self.nyx_enabled = False

    def use_provider(self, name: str) -> BaseProvider:
        """Session-scoped provider swap. Rebuilds the Agent so subsequent turns
        use the new backend. Also remembers the choice as default_provider so
        the next launch does not fall back to offline stub-v0."""
        if self.providers is None:
            raise RuntimeError("no provider registry attached")
        resolved = self.providers.resolve(name) or name
        provider = self.providers.get(resolved)
        self.provider = provider
        self.provider_name = resolved
        self.config.default_provider = resolved
        self.agent = Agent(
            provider,
            self.tools,
            self.policy,
            base_system_prompt=NYX_SYSTEM_PROMPT if self.nyx_enabled else BASE_SYSTEM_PROMPT,
            context_extras={"orchestrator": self},
        )
        if self.state is not None:
            self.state.session.provider = resolved
            prov_cfg = self.config.providers.get(resolved)
            if prov_cfg and prov_cfg.default_model:
                self.state.session.model = prov_cfg.default_model
            elif getattr(provider, "default_model", ""):
                self.state.session.model = provider.default_model
            else:
                self.state.session.model = ""
            self.session_store.update(self.state.session)
        self._persist_default_provider(resolved)
        # Auto-update context meter for the active model (known table + cache).
        try:
            from reidx.provider.context_windows import bind_model_context

            mid = ""
            if self.state is not None:
                mid = self.state.session.model or ""
            mid = mid or getattr(provider, "default_model", "") or ""
            if mid and self.state is not None:
                window = bind_model_context(mid, provider, network=False)
                self.state.session.context_window = window
                self.session_store.update(self.state.session)
        except Exception:  # noqa: BLE001
            pass
        return provider

    def _persist_default_provider(self, name: str) -> None:
        """Write reidx.default_provider so restarts keep the last /use choice."""
        try:
            import json

            from reidx.config.settings import ensure_user_settings, settings_path

            path = settings_path()
            if not path.exists():
                path = ensure_user_settings()
            data = json.loads(path.read_text(encoding="utf-8"))
            reidx = data.setdefault("reidx", {})
            reidx["default_provider"] = name
            path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            log.debug("could not persist default_provider: %s", exc)

    def start_session(self, title: str = "") -> Session:
        workspace = (self.config.workspace_root or Path.cwd()).resolve()
        # Use the *active* registry provider, not a stale config default that
        # may still say "stub" while self.provider is already NVIDIA/Claude.
        pname = self.provider_name or self.config.default_provider or "stub"
        if self.providers is not None:
            resolved = self.providers.resolve(pname)
            if resolved:
                pname = resolved
        session = Session(
            title=title or "untitled",
            workspace=workspace,
            provider=pname,
            model=self._default_model(pname),
            permission_mode=self.config.policy.default_mode,
        )
        self.session_store.create(session)
        self.state = RuntimeState(session=session)
        self._init_session_extras()
        # Auto-set context window from model id / known table (no network hang).
        try:
            from reidx.provider.context_windows import bind_model_context

            if session.model:
                session.context_window = bind_model_context(
                    session.model, self.provider, network=False
                )
                self.session_store.update(session)
        except Exception:  # noqa: BLE001
            pass
        self.session_store.event_log(session.id).write(
            "session_start",
            {
                "title": session.title,
                "provider": pname,
                "model": session.model,
                "context_window": session.context_window,
            },
        )
        return session

    def _init_session_extras(self) -> None:
        """Wire the per-session cost ledger (JSONL under the session dir)."""
        if self.state is None:
            return
        sid = self.state.session.id
        cost_path = self.session_store.session_dir(sid) / "costs.jsonl"
        self.state.costs = CostLedger(path=cost_path)

    def _default_model(self, provider_name: str | None = None) -> str:
        pname = provider_name or self.provider_name or self.config.default_provider
        prov = self.config.providers.get(pname or "")
        if prov and prov.default_model:
            return prov.default_model
        live = getattr(self.provider, "default_model", "") or ""
        if live:
            return live
        # Offline stub only — never invent stub-v0 for a real provider.
        if (pname or "") == "stub" or getattr(self.provider, "name", "") == "stub":
            return "stub-v0"
        return ""

    def resume_session(self, session_id: str) -> Session:
        session = self.session_store.get(session_id)
        if session is None:
            raise KeyError(f"session {session_id} not found")
        self.session_store.set_status(session.id, SessionStatus.ACTIVE)
        self.state = RuntimeState(session=session)
        self.policy.set_mode(session.permission_mode)
        # Restore prior transcript into in-memory state for real continuation.
        self.state.messages = self.session_store.read_messages(session.id)
        self._init_session_extras()
        self.session_store.event_log(session.id).write("session_resume", {"messages": len(self.state.messages)})
        return session

    def task_store(self) -> TaskStore:
        if self.state is None:
            raise RuntimeError("no active session")
        from reidx.config.storage import storage_root as default_storage_root

        root = self.config.storage_root or default_storage_root()
        return TaskStore(root, self.state.session.id)

    def goal_store(self) -> GoalStore:
        if self.state is None:
            raise RuntimeError("no active session")
        from reidx.config.storage import storage_root as default_storage_root

        root = self.config.storage_root or default_storage_root()
        return GoalStore(root, self.state.session.id)

    def submit_task(
        self,
        user_input: str,
        *,
        approver: Approver | None = None,
        title: str | None = None,
        cancel: Callable[[], bool] | None = None,
        on_text_delta: Callable[[str], None] | None = None,
        on_status: Callable[[str], None] | None = None,
    ) -> dict:
        """Run one user turn against the agent, tracking it as a Task.

        `cancel`, if given, is forwarded to `Agent.run_turn` and polled at
        safe points so a user-triggered stop (e.g. Escape in the TUI) ends
        the turn with whatever partial answer/tool results it already has,
        instead of running to completion.

        `on_text_delta` receives streamed tokens when `/stream` is auto/on and
        the provider supports SSE streaming.
        """
        if self.state is None:
            raise RuntimeError("no active session; call start_session first")
        store = self.task_store()
        active_goal = self.goal_store().active()
        task_meta = {}
        if active_goal is not None:
            task_meta = {
                "goal_id": active_goal.id,
                "goal_title": active_goal.title,
            }
            if active_goal.active_node_id:
                task_meta["goal_node_id"] = active_goal.active_node_id
        task = store.create(title or user_input[:60], meta=task_meta)
        if active_goal is not None:
            self.goal_store().add_task_link(
                active_goal.id,
                task.id,
                node_id=active_goal.active_node_id,
            )
        store.update_status(task.id, TaskStatus.ACTIVE)
        self.state.active_task_id = task.id

        # Auto-compact when the transcript is crowding the model window.
        # This rewrites conversation history — it is not the status-bar % meter.
        auto_compact: CompactResult | None = None
        from reidx.provider.context_windows import context_window_for as _ctx_win

        window = _ctx_win(
            self.state.session.model or "",
            session_window=self.state.session.context_window,
        )
        if should_auto_compact(self.state.messages, context_window=window):
            auto_compact = self.compact_context(keep_user_turns=DEFAULT_KEEP_USER_TURNS)
            if auto_compact.method != "skipped":
                log.info(
                    "auto-compacted context: %s → %s msgs (%s)",
                    auto_compact.before_count,
                    auto_compact.after_count,
                    auto_compact.method,
                )

        # Record message count so we can persist only the new messages from this turn.
        pre_turn_count = len(self.state.messages)
        writable_roots = [r.resolve() for r in self.config.policy.additional_writable_roots]
        final_text, tool_log = self.agent.run_turn(
            self.state,
            user_input,
            writable_roots=writable_roots,
            approver=approver,
            cancel=cancel,
            on_text_delta=on_text_delta,
            on_status=on_status,
        )

        # Persist new messages incrementally for restorable resume.
        sid = self.state.session.id
        for msg in self.state.messages[pre_turn_count:]:
            self.session_store.append_message(sid, msg)

        # Cost tracking from this turn's usage (last provider.chat call).
        cost_event = None
        pt = self.state.last_usage_prompt_tokens
        ct = self.state.last_usage_completion_tokens
        if pt or ct:
            cost_event = self.state.costs.record(
                provider=self.state.session.provider,
                model=self.state.session.model or "",
                prompt_tokens=pt,
                completion_tokens=ct,
                task_id=task.id,
            )

        # Turn summary in events.jsonl (human-readable, separate from restorable transcript).
        self.session_store.event_log(sid).write(
            "task_complete",
            {
                "task_id": task.id,
                "tools": len(tool_log),
                "cost_usd": cost_event.cost_usd if cost_event else 0,
            },
        )

        # Derive task status from the turn outcome.
        cancelled = final_text.startswith("[cancelled by user]")
        exhausted = final_text.startswith("[agent] step budget exhausted")
        provider_failed = final_text.startswith(PROVIDER_ERROR_PREFIX)
        all_tools_failed = bool(tool_log) and not any(entry["ok"] for entry in tool_log)
        if cancelled:
            store.update_status(task.id, TaskStatus.SKIPPED, summary=final_text[:200])
        elif exhausted or all_tools_failed or provider_failed:
            store.update_status(task.id, TaskStatus.FAILED, error=final_text[:200])
        else:
            store.update_status(task.id, TaskStatus.COMPLETED, summary=final_text[:200])
        self.session_store.update(self.state.session)
        display_text = (
            final_text[len(PROVIDER_ERROR_PREFIX) :] if provider_failed else final_text
        )
        result = {
            "task_id": task.id,
            "text": display_text,
            "tools": tool_log,
            "thinking": self.state.last_thinking,
            "error": provider_failed,
        }
        if auto_compact is not None and auto_compact.method != "skipped":
            result["compacted"] = {
                "before": auto_compact.before_count,
                "after": auto_compact.after_count,
                "method": auto_compact.method,
                "before_tokens": auto_compact.before_tokens,
                "after_tokens": auto_compact.after_tokens,
            }
        if cost_event is not None:
            result["cost"] = {
                "turn_usd": cost_event.cost_usd,
                "session_usd": self.state.costs.total_usd,
                "priced": cost_event.priced,
                "prompt_tokens": cost_event.prompt_tokens,
                "completion_tokens": cost_event.completion_tokens,
            }
        return result

    def compact_context(
        self,
        *,
        keep_user_turns: int = DEFAULT_KEEP_USER_TURNS,
        force: bool = False,
        prefer_llm: bool = True,
    ) -> CompactResult:
        """Compact the active session transcript (manual `/compact` or auto).

        Replaces older turns with a summary message, keeps recent user turns,
        and rewrites transcript.jsonl. Separate from the footer token gauge.
        """
        if self.state is None:
            raise RuntimeError("no active session; call start_session first")
        new_msgs, result = compact_messages(
            self.state.messages,
            provider=self.provider,
            model=self.state.session.model,
            keep_user_turns=keep_user_turns,
            force=force,
            prefer_llm=prefer_llm,
        )
        if result.method == "skipped":
            return result
        self.state.messages = new_msgs
        # Usage estimate after compact so the footer reflects the shrink.
        self.state.last_usage_prompt_tokens = result.after_tokens
        sid = self.state.session.id
        self.session_store.rewrite_messages(sid, new_msgs)
        self.session_store.event_log(sid).write(
            "context_compact",
            {
                "before": result.before_count,
                "after": result.after_count,
                "before_tokens": result.before_tokens,
                "after_tokens": result.after_tokens,
                "method": result.method,
                "keep_user_turns": keep_user_turns,
            },
        )
        self.session_store.update(self.state.session)
        return result

    def context_stats(self) -> dict:
        """Token / window stats for the active session (status bar + /compact)."""
        if self.state is None:
            return {"messages": 0, "tokens": 0, "window": 0, "ratio": 0.0}
        msgs = self.state.messages
        from reidx.provider.context_windows import context_window_for as _ctx_win

        tokens = self.state.last_usage_prompt_tokens or estimate_tokens(msgs)
        window = _ctx_win(
            self.state.session.model or "",
            session_window=self.state.session.context_window,
        )
        ratio = (tokens / window) if window else 0.0
        return {
            "messages": len(msgs),
            "tokens": tokens,
            "window": window,
            "ratio": ratio,
            "auto_threshold": AUTO_COMPACT_RATIO,
        }

    def list_tasks(self) -> list:
        if self.state is None:
            return []
        return self.task_store().list()

    def list_goals(self) -> list[Goal]:
        if self.state is None:
            return []
        return self.goal_store().list()

    def set_nyx(self, enabled: bool) -> None:
        """Toggle Nyx (redteam/offensive-security) persona. Rebuilds the Agent
        with the swapped system prompt; tool registry and policy engine are
        untouched — those are the actual safety boundary, not the prompt."""
        self.nyx_enabled = enabled
        self.agent = Agent(
            self.provider,
            self.tools,
            self.policy,
            base_system_prompt=NYX_SYSTEM_PROMPT if enabled else BASE_SYSTEM_PROMPT,
            context_extras={"orchestrator": self},
        )

    def set_permission_mode(self, mode) -> None:  # type: ignore[no-untyped-def]
        # Single source of truth: update policy engine + session + persist.
        self.policy.set_mode(mode)
        if self.state is None:
            self.config.policy.default_mode = mode
            return
        self.state.session.permission_mode = mode
        self.session_store.update(self.state.session)

    def rewind(self) -> None:
        """Drop messages back to before the last user turn (stub for deeper rewind later)."""
        if self.state is None or not self.state.messages:
            return
        msgs = self.state.messages
        # Find the last user message and drop from there onward.
        for i in range(len(msgs) - 1, -1, -1):
            if msgs[i].role == "user":
                del msgs[i:]
                break
        sid = self.state.session.id
        self.session_store.rewrite_messages(sid, msgs)
        self.session_store.event_log(sid).write("rewind", {"remaining": len(msgs)})
