"""Goal store: per-session goal state persisted to goals.json."""
from __future__ import annotations

from pathlib import Path

from reidx.diagnostics.logger import get_logger
from reidx.goals.models import Goal, GoalEvidence, GoalNode, GoalNodeKind, GoalStatus

log = get_logger("reidx.goals")


class GoalStore:
    def __init__(self, storage_root: Path, session_id: str) -> None:
        self.path = storage_root / "sessions" / session_id / "goals.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.session_id = session_id

    def _read_payload(self) -> tuple[dict[str, Goal], str | None]:
        if not self.path.exists():
            return {}, None
        import json

        text = self.path.read_text(encoding="utf-8").strip()
        if not text:
            return {}, None

        raw = json.loads(text)
        goals: dict[str, Goal] = {}
        for item in raw.get("goals", []):
            goal = Goal.model_validate(item)
            goals[goal.id] = goal
        active_id = raw.get("active_goal_id")
        if active_id not in goals:
            active_id = None
        return goals, active_id

    def _write_payload(self, goals: dict[str, Goal], active_goal_id: str | None) -> None:
        import json

        payload = {
            "active_goal_id": active_goal_id if active_goal_id in goals else None,
            "goals": [goal.model_dump(mode="json") for goal in goals.values()],
        }
        self.path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

    def create(self, title: str, outcome: str = "", *, make_active: bool = True) -> Goal:
        goals, active_id = self._read_payload()
        goal = Goal(
            session_id=self.session_id,
            title=title,
            outcome=outcome,
            status=GoalStatus.ACTIVE if make_active else GoalStatus.DRAFT,
        )
        goals[goal.id] = goal
        self._write_payload(goals, goal.id if make_active else active_id)
        log.debug("created goal %s: %s", goal.id, title)
        return goal

    def get(self, goal_id: str) -> Goal | None:
        goals, _active_id = self._read_payload()
        return goals.get(goal_id)

    def list(self) -> list[Goal]:
        goals, _active_id = self._read_payload()
        return list(goals.values())

    def active_id(self) -> str | None:
        _goals, active_id = self._read_payload()
        return active_id

    def active(self) -> Goal | None:
        goals, active_id = self._read_payload()
        return goals.get(active_id or "")

    def set_active(self, goal_id: str | None) -> Goal | None:
        goals, _active_id = self._read_payload()
        if goal_id is None:
            self._write_payload(goals, None)
            return None
        goal = goals.get(goal_id)
        if goal is None:
            return None
        if goal.status is GoalStatus.DRAFT:
            goal.status = GoalStatus.ACTIVE
            goal.touch()
        self._write_payload(goals, goal.id)
        return goal

    def update(self, goal: Goal) -> Goal:
        goals, active_id = self._read_payload()
        if goal.id not in goals:
            raise KeyError(f"goal {goal.id} not found")
        goal.touch()
        goals[goal.id] = goal
        self._write_payload(goals, active_id)
        return goal

    def delete(self, goal_id: str) -> bool:
        goals, active_id = self._read_payload()
        if goal_id not in goals:
            return False
        del goals[goal_id]
        if active_id == goal_id:
            active_id = None
        self._write_payload(goals, active_id)
        return True

    def update_status(
        self, goal_id: str, status: GoalStatus, note: str = "", *, node_id: str | None = None
    ) -> Goal | None:
        goals, active_id = self._read_payload()
        goal = goals.get(goal_id)
        if goal is None:
            return None
        if node_id:
            node = self._find_node(goal, node_id)
            if node is None:
                return None
            node.status = status
            if note:
                node.notes.append(note)
            node.touch()
        else:
            goal.status = status
            goal.add_revision(note)
        goal.touch()
        goals[goal.id] = goal
        self._write_payload(goals, active_id)
        return goal

    def set_outcome(self, goal_id: str, outcome: str) -> Goal | None:
        goals, active_id = self._read_payload()
        goal = goals.get(goal_id)
        if goal is None:
            return None
        goal.outcome = outcome
        goal.add_revision("outcome updated")
        goal.touch()
        goals[goal.id] = goal
        self._write_payload(goals, active_id)
        return goal

    def add_evidence(self, goal_id: str, description: str) -> Goal | None:
        goals, active_id = self._read_payload()
        goal = goals.get(goal_id)
        if goal is None:
            return None
        goal.evidence.append(GoalEvidence(description=description))
        goal.touch()
        goals[goal.id] = goal
        self._write_payload(goals, active_id)
        return goal

    def satisfy_evidence(self, goal_id: str, evidence_index: int, note: str = "") -> Goal | None:
        goals, active_id = self._read_payload()
        goal = goals.get(goal_id)
        if goal is None or evidence_index < 0 or evidence_index >= len(goal.evidence):
            return None
        goal.evidence[evidence_index].satisfy(note)
        goal.touch()
        goals[goal.id] = goal
        self._write_payload(goals, active_id)
        return goal

    def add_node(
        self,
        goal_id: str,
        title: str,
        kind: GoalNodeKind = GoalNodeKind.SUBGOAL,
        *,
        parent_id: str | None = None,
        depends_on: list[str] | None = None,
    ) -> GoalNode | None:
        goals, active_id = self._read_payload()
        goal = goals.get(goal_id)
        if goal is None:
            return None
        depends = depends_on or []
        node_ids = {node.id for node in goal.nodes}
        if parent_id is not None and parent_id not in node_ids:
            return None
        if any(dep not in node_ids for dep in depends):
            return None
        node = GoalNode(title=title, kind=kind, parent_id=parent_id, depends_on=depends)
        goal.nodes.append(node)
        goal.touch()
        goals[goal.id] = goal
        self._write_payload(goals, active_id)
        return node

    def add_note(self, goal_id: str, note: str, *, node_id: str | None = None) -> Goal | None:
        goals, active_id = self._read_payload()
        goal = goals.get(goal_id)
        if goal is None:
            return None
        if node_id:
            node = self._find_node(goal, node_id)
            if node is None:
                return None
            node.notes.append(note)
            node.touch()
        else:
            goal.add_revision(note)
        goal.touch()
        goals[goal.id] = goal
        self._write_payload(goals, active_id)
        return goal

    def add_task_link(self, goal_id: str, task_id: str, *, node_id: str | None = None) -> Goal | None:
        goals, active_id = self._read_payload()
        goal = goals.get(goal_id)
        if goal is None:
            return None
        if task_id not in goal.task_ids:
            goal.task_ids.append(task_id)
        if node_id:
            node = self._find_node(goal, node_id)
            if node is None:
                return None
            if task_id not in node.task_ids:
                node.task_ids.append(task_id)
            node.touch()
        goal.touch()
        goals[goal.id] = goal
        self._write_payload(goals, active_id)
        return goal

    @staticmethod
    def _find_node(goal: Goal, node_id: str) -> GoalNode | None:
        for node in goal.nodes:
            if node.id == node_id:
                return node
        return None
