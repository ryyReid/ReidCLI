"""Goal store tests."""
from __future__ import annotations

from pathlib import Path

from reidx.config.models import default_config
from reidx.goals.models import GoalNodeKind, GoalStatus
from reidx.goals.store import GoalStore
from reidx.provider.stub import StubProvider
from reidx.runtime.orchestrator import Orchestrator
from reidx.tools import default_registry
from reidx.ui.commands import handle


def test_goal_create_get_list_and_active(tmp_path: Path) -> None:
    store = GoalStore(tmp_path, "session-1")
    goal = store.create("Ship /goal")

    assert goal.status is GoalStatus.ACTIVE
    assert store.active_id() == goal.id
    assert store.active().title == "Ship /goal"
    assert store.get(goal.id).title == "Ship /goal"
    assert [g.id for g in store.list()] == [goal.id]


def test_goal_evidence_round_trip(tmp_path: Path) -> None:
    store = GoalStore(tmp_path, "session-1")
    goal = store.create("Ship /goal")

    store.add_evidence(goal.id, "goals.json is persisted")
    updated = store.satisfy_evidence(goal.id, 0, "verified")

    assert updated is not None
    assert updated.evidence[0].description == "goals.json is persisted"
    assert updated.evidence[0].satisfied
    assert updated.evidence[0].note == "verified"
    assert store.get(goal.id).evidence[0].satisfied


def test_goal_nodes_and_dependencies(tmp_path: Path) -> None:
    store = GoalStore(tmp_path, "session-1")
    goal = store.create("Ship /goal")

    models = store.add_node(goal.id, "Add models", GoalNodeKind.SUBGOAL)
    render = store.add_node(
        goal.id,
        "Render goals",
        GoalNodeKind.MILESTONE,
        depends_on=[models.id],
    )

    assert models is not None
    assert render is not None
    assert render.depends_on == [models.id]

    updated = store.update_status(goal.id, GoalStatus.COMPLETED, "done", node_id=models.id)
    assert updated is not None
    assert updated.nodes[0].status is GoalStatus.COMPLETED


def test_goal_invalid_dependency_is_rejected(tmp_path: Path) -> None:
    store = GoalStore(tmp_path, "session-1")
    goal = store.create("Ship /goal")

    node = store.add_node(goal.id, "Broken", depends_on=["missing"])

    assert node is None
    assert store.get(goal.id).nodes == []


def test_goal_status_revision_and_task_link(tmp_path: Path) -> None:
    store = GoalStore(tmp_path, "session-1")
    goal = store.create("Ship /goal")

    store.update_status(goal.id, GoalStatus.BLOCKED, "waiting on API")
    store.add_task_link(goal.id, "task-1")
    updated = store.get(goal.id)

    assert updated.status is GoalStatus.BLOCKED
    assert updated.revisions[-1].note == "waiting on API"
    assert updated.task_ids == ["task-1"]


def test_orchestrator_task_links_to_active_goal(tmp_path: Path) -> None:
    cfg = default_config()
    cfg.workspace_root = tmp_path
    cfg.storage_root = tmp_path
    orch = Orchestrator(cfg, StubProvider(), default_registry())
    orch.start_session("test")

    goal = orch.goal_store().create("Ship /goal")
    result = orch.submit_task("hello")
    task = orch.task_store().get(result["task_id"])
    linked_goal = orch.goal_store().get(goal.id)

    assert task.meta["goal_id"] == goal.id
    assert task.meta["goal_title"] == "Ship /goal"
    assert linked_goal.task_ids == [task.id]


def test_goal_slash_commands_mutate_store(tmp_path: Path) -> None:
    cfg = default_config()
    cfg.workspace_root = tmp_path
    cfg.storage_root = tmp_path
    orch = Orchestrator(cfg, StubProvider(), default_registry())
    orch.start_session("test")

    assert handle(orch, "/goal new Ship /goal") == "continue"
    assert handle(orch, "/goal outcome Structured goals persist") == "continue"
    assert handle(orch, "/goal evidence add goals.json exists") == "continue"
    assert handle(orch, "/goal evidence done 1 verified") == "continue"

    goal = orch.goal_store().active()
    assert goal.title == "Ship /goal"
    assert goal.outcome == "Structured goals persist"
    assert goal.evidence[0].satisfied
    assert goal.evidence[0].note == "verified"


def test_goal_free_text_creates_goal(tmp_path: Path) -> None:
    cfg = default_config()
    cfg.workspace_root = tmp_path
    cfg.storage_root = tmp_path
    orch = Orchestrator(cfg, StubProvider(), default_registry())
    orch.start_session("test")

    assert handle(orch, "/goal make me a report of ReidX") == "continue"

    goal = orch.goal_store().active()
    assert goal is not None
    assert goal.title == "make me a report of ReidX"
