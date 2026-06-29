"""Domain model: fixed semantics use enums, dynamic workflow steps use strings."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class TaskStatus(StrEnum):
    """Overall scheduling state of a task."""

    QUEUED = "queued"
    RUNNING = "running"
    PAUSE_PENDING = "pause_pending"
    PAUSED = "paused"
    TERMINATED = "terminated"
    FAILED = "failed"
    MANUAL_REQUIRED = "manual_required"
    COMPLETED = "completed"


# Terminal states never scheduled again.
TERMINAL_STATUSES = frozenset({TaskStatus.COMPLETED, TaskStatus.TERMINATED})


class EventType(StrEnum):
    """Task event types recorded in the event log."""

    CREATED = "created"
    CLAIMED = "claimed"
    STEP_COMPLETED = "step_completed"
    STEP_FAILED = "step_failed"
    PAUSE_REQUESTED = "pause_requested"
    PAUSED = "paused"
    RESUMED = "resumed"
    TERMINATE_REQUESTED = "terminate_requested"
    TERMINATED = "terminated"
    RETRY = "retry"
    MANUAL_REQUIRED = "manual_required"
    COMPLETED = "completed"
    FIELD_SET = "field_set"
    RECLAIMED = "reclaimed"
    DEFERRED = "deferred"


@dataclass(frozen=True)
class NextStep:
    """Result of advancing a workflow: the next step name, or end-of-workflow."""

    step: str | None
    done: bool


@dataclass(frozen=True)
class WorkflowDef:
    """A declarative workflow: an ordered tuple of step names."""

    name: str
    steps: tuple[str, ...]

    def __init__(self, name: str, steps: list[str] | tuple[str, ...]) -> None:
        if not steps:
            raise ValueError(f"workflow {name!r} must define at least one step")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "steps", tuple(steps))

    def first_step(self) -> str:
        return self.steps[0]

    def next_step(self, current: str) -> NextStep:
        if current not in self.steps:
            raise ValueError(f"step {current!r} is not part of workflow {self.name!r}")
        idx = self.steps.index(current)
        if idx + 1 >= len(self.steps):
            return NextStep(step=None, done=True)
        return NextStep(step=self.steps[idx + 1], done=False)
