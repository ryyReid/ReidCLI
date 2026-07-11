# ReidX `/goal` Build Plan

## Source Inputs

- `../deep-research-report.md`: goal decomposition research across AI planning, psychology, project management, education, and behavior change.
- Existing ReidX architecture:
  - `src/reidx/tasks/`: per-session task persistence and task status state machine.
  - `src/reidx/runtime/orchestrator.py`: session/task/agent coordination.
  - `src/reidx/ui/commands.py`: slash command routing and help/completion source.
  - `src/reidx/ui/render.py`: Rich rendering helpers.
  - `src/reidx/ui/app.py`: full-screen TUI, slash command dispatch, completion menu.
  - `src/reidx/session/store.py`: per-session persistence layout.

## Product Intent

Add a first-class `/goal` surface for long-horizon work in ReidX.

The feature should not be a thin alias for `/tasks`. A goal represents a durable desired end state with success evidence, constraints, child goals or work packages, dependency ordering, progress assessment, and revision history. Tasks remain execution records for individual agent turns; goals organize why those turns exist and how progress is judged.

The central product idea is that `/goal` should turn decomposition into a living control system, not a saved checklist. A serious goal needs:

- A clear parent outcome.
- Observable evidence that proves the outcome is achieved.
- Subgoals or work packages that cover the scope.
- Explicit ordering and dependencies.
- Constraints and resource assumptions.
- Feedback loops that reveal whether the hierarchy is still useful.
- Revision history when the plan changes.

In practice, this means `/goal` should eventually represent a goal like this:

```text
Goal: Ship authentication safely

Outcome:
Users can sign up, log in, log out, and recover sessions reliably.

Evidence:
- Auth API tests pass
- Login/logout works in the app flow
- Session persistence is verified
- Failure states are documented
- Rollback path exists

Subgoals:
1. Define auth contract
2. Implement storage/session model
3. Add login/logout commands
4. Add tests
5. Validate security failure cases
6. Document usage

Dependencies:
Storage model before login commands.
Tests before completion.
Docs after command behavior settles.

Review:
Reassess if provider/session architecture changes.
```

V1 does not need to automate all of this, but the data model and command design should preserve these concepts from the start.

## Research-Derived Design Principles

1. Goals need observable success evidence, not just prose.
2. Decomposition should stop when the next layer is testable, controllable, and worth tracking.
3. Child goals should obey a practical "100 percent rule": together they cover the parent scope without hiding large "other" buckets.
4. Milestones/evidence are distinct from tasks; completing activity is not the same as achieving the outcome.
5. Ordering and dependencies should be explicit so the agent can identify blocked, ready, and parallelizable work.
6. Goals should support revision, because the hierarchy is a hypothesis that may be wrong.
7. The system should guard against over-decomposition and vague subgoals.

## V1 Scope

V1 should be persistent, inspectable, and manually operable from the TUI. It should not try to become a full autonomous project manager yet.

Ship:

- Per-session `goals.json`.
- Goal models with hierarchy, evidence, constraints, dependencies, status, notes, and revision history.
- Slash command group `/goal`.
- Rendering for goal list/detail/tree.
- Orchestrator helpers for goal store access.
- Optional linking from created tasks to an active goal via `Task.meta`.
- Tests for model/store/command behavior.
- README update.

Do not ship in v1:

- Automatic LLM decomposition.
- Scheduling/background reminders.
- Multi-session/global goal dashboards.
- DeepReid Builder role integration.
- External issue tracker sync.

## Data Model

Create `src/reidx/goals/models.py`.

Recommended enums:

- `GoalStatus`: `draft`, `active`, `blocked`, `completed`, `abandoned`.
- `GoalNodeKind`: `goal`, `subgoal`, `milestone`, `habit`, `task_ref`.

Recommended models:

```python
class GoalEvidence(BaseModel):
    description: str
    satisfied: bool = False
    note: str = ""

class GoalConstraint(BaseModel):
    description: str
    kind: str = "general"  # time|cost|policy|resource|scope|general

class GoalRevision(BaseModel):
    at: datetime
    note: str

class GoalNode(BaseModel):
    id: str
    title: str
    kind: GoalNodeKind = GoalNodeKind.SUBGOAL
    status: GoalStatus = GoalStatus.DRAFT
    parent_id: str | None = None
    depends_on: list[str] = []
    evidence: list[GoalEvidence] = []
    constraints: list[GoalConstraint] = []
    owner: str = "user"
    notes: list[str] = []
    task_ids: list[str] = []
    created_at: datetime
    updated_at: datetime

class Goal(BaseModel):
    id: str
    session_id: str
    title: str
    status: GoalStatus = GoalStatus.DRAFT
    outcome: str = ""
    evidence: list[GoalEvidence] = []
    constraints: list[GoalConstraint] = []
    nodes: list[GoalNode] = []
    active_node_id: str | None = None
    revisions: list[GoalRevision] = []
    created_at: datetime
    updated_at: datetime
```

Keep v1 intentionally explicit and JSON-friendly. Avoid storing generated prose plans as the source of truth; prose can live in notes or revisions, while status/evidence/dependencies stay structured.

## Persistence

Create `src/reidx/goals/store.py`, mirroring `TaskStore`.

Path:

```text
~/.reidx/sessions/<session-id>/goals.json
```

Shape:

```json
{
  "active_goal_id": "abc123",
  "goals": []
}
```

Store methods:

- `create(title: str, outcome: str = "") -> Goal`
- `get(goal_id: str) -> Goal | None`
- `list() -> list[Goal]`
- `set_active(goal_id: str | None) -> Goal | None`
- `update(goal: Goal) -> Goal`
- `update_status(goal_id: str, status: GoalStatus, note: str = "") -> Goal | None`
- `add_evidence(goal_id: str, description: str) -> Goal | None`
- `satisfy_evidence(goal_id: str, evidence_index: int, note: str = "") -> Goal | None`
- `add_node(goal_id: str, title: str, kind: GoalNodeKind, parent_id: str | None = None, depends_on: list[str] | None = None) -> GoalNode | None`
- `update_node_status(goal_id: str, node_id: str, status: GoalStatus, note: str = "") -> Goal | None`
- `add_note(goal_id: str, note: str, node_id: str | None = None) -> Goal | None`

Implementation detail: preserve insertion order in JSON for readable diffs, like `TaskStore` already does.

## Orchestrator Integration

Update `src/reidx/runtime/orchestrator.py`.

Add:

```python
from reidx.goals.store import GoalStore

def goal_store(self) -> GoalStore:
    if self.state is None:
        raise RuntimeError("no active session")
    return GoalStore(self.config.storage_root or (Path.home() / ".reidx"), self.state.session.id)

def list_goals(self) -> list[Goal]:
    if self.state is None:
        return []
    return self.goal_store().list()
```

When `submit_task()` creates a task, attach active goal metadata if one exists:

```python
active_goal = self.goal_store().active()
if active_goal:
    task.meta["goal_id"] = active_goal.id
    task.meta["goal_title"] = active_goal.title
```

This needs either a `TaskStore.create(..., meta=None)` extension or a follow-up `update()` method on `TaskStore`. Prefer adding `meta` to `TaskStore.create()` because it is a small compatible API extension.

Do not inject goal state into every model prompt in v1. Add that later once goal summarization is deliberate, token-bounded, and tested.

## Slash Commands

Add `/goal` to `SLASH_COMMANDS` in `src/reidx/ui/commands.py`:

```text
/goal <new|list|show|active|evidence|add|done|block|revise|delete> ...
```

Add `GOAL_SUBCOMMANDS` for completion, matching the existing workflow pattern.

V1 command surface:

- `/goal` or `/goal show`: show active goal.
- `/goal new <title>`: create a draft goal and make it active.
- `/goal outcome <text>`: set active goal outcome criterion.
- `/goal evidence add <text>`: add success evidence to active goal.
- `/goal evidence done <index> [note]`: mark evidence satisfied.
- `/goal add <title>`: add a child subgoal under the active goal.
- `/goal milestone <title>`: add a milestone node.
- `/goal list`: list goals in the current session.
- `/goal show [id]`: detailed view with evidence, child nodes, status, and revision notes.
- `/goal active <id>`: switch active goal.
- `/goal done [id]`: complete goal or node when evidence is satisfied.
- `/goal block [id] <reason>`: mark blocked and append a revision/note.
- `/goal revise <note>`: record a revision note without changing structure.
- `/goal abandon [id] <reason>`: abandon goal.

Validation rules:

- Refuse `/goal done` if no evidence exists unless `--force` is added later.
- Warn, but do not block, if evidence exists but not all evidence is satisfied.
- Refuse child nodes with a missing parent.
- Refuse dependency IDs that are not in the same goal.
- Keep messages terse and TUI-friendly.

## Rendering

Update `src/reidx/ui/render.py`.

Add:

- `print_goals(goals: list[Goal], active_goal_id: str | None)`
- `print_goal(goal: Goal)`
- `print_goal_tree(goal: Goal)`

List view columns:

- active marker
- id
- status
- title
- evidence progress, for example `2/4`
- child node progress, for example `3/8`

Detail view:

- title/outcome/status
- evidence checklist
- constraints
- tree of nodes with status and dependencies
- latest revision notes

Use existing Rich table/panel style and `STATUS_STYLE` where possible.

## TUI Completion

Update `src/reidx/ui/app.py`.

Current completion special-cases `/workflow `. Add the same pattern for `/goal `:

```python
if text.startswith("/goal "):
    prefix = text[len("/goal "):]
    if " " in prefix:
        return
    for name, args, desc in GOAL_SUBCOMMANDS:
        ...
```

Import `GOAL_SUBCOMMANDS` from `ui.commands`.

## README Updates

Update:

- Slash command table: add `/goal`.
- Sessions and persistence: add `goals.json`.
- What works now: only after implementation is complete.

Avoid promising autonomous goal decomposition until it exists.

## Test Plan

Add `tests/test_goals.py`.

Cover:

- Create/list/get/update round trip.
- Active goal persistence.
- Evidence add/satisfy.
- Child node add/status update.
- Invalid dependency rejected or handled cleanly.
- Goal completion behavior with unsatisfied evidence.

Extend existing tests:

- `tests/test_session.py`: session layout can coexist with `goals.json`.
- Command handler tests if a command-test module is added; otherwise test store/orchestrator directly.
- `tests/test_agent.py` or a new orchestrator test: submitted tasks inherit active goal metadata.

Manual smoke:

```powershell
reidx
/goal new Ship /goal v1
/goal outcome A session can persist and inspect structured goals
/goal evidence add goals.json is written under the session
/goal add Add goal models and store
/goal show
/goal list
```

## Build Sequence

1. Add `goals` package with models and store.
2. Add goal store tests and make them pass.
3. Add orchestrator `goal_store()` and `list_goals()` helpers.
4. Extend `TaskStore.create()` with optional `meta`, then link new tasks to the active goal.
5. Add render helpers for goal list/detail/tree.
6. Add `/goal` command handling and validation.
7. Add `/goal` completion support.
8. Update README persistence and slash command documentation.
9. Run `pytest` and `ruff check src tests`.

## V2 Options

After v1 is stable:

- `/goal decompose`: use the active provider to propose subgoals from the design canvas.
- `/goal assess`: score alignment, completeness, reachability, measurability, granularity, and robustness.
- DeepReid integration: let DeepReid output seed a draft goal hierarchy.
- Prompt context: add a compact active-goal summary to `Agent` context only when a goal is active.
- Cross-session goals: promote selected goals to workspace/global scope.
- Review cadence: `/goal review` to surface stale, blocked, or evidence-light goals.
