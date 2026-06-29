"""Step abstraction: the single contract every capability adapter implements.

A step is a small, self-contained unit of work. It receives a ``StepContext``
(the task row, the resolved config, a stop check, and a heartbeat callback) and
returns a ``StepResult`` (an optional message plus field updates to persist).
Steps never touch the database directly; they read the task, do their work,
write artifacts to the filesystem, and report back through the result.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class StepContext:
    """Runtime context handed to a step."""

    task: dict[str, Any]
    config: Any
    should_stop: Callable[[], bool]
    # No argument / None = refresh heartbeat only; pass a progress string to
    # refresh the heartbeat and persist ``progress`` (throttled by the runner).
    heartbeat: Callable[..., None]


@dataclass(frozen=True)
class StepResult:
    """Result of a step run: an optional message and task field updates.

    ``updates`` must use whitelisted task columns (see the repository); any other
    key is treated as a step failure so the task never silently corrupts state.
    """

    message: str = ""
    updates: dict[str, Any] = field(default_factory=dict)


class Step(ABC):
    """A workflow step: an adapter over some capability.

    Subclasses must set the class attribute ``name`` and implement ``run``.
    ``when`` defaults to always-true; override it to declare a conditional skip
    (for example, a publish step that only runs when explicitly enabled).
    """

    name: str

    def when(self, task: dict[str, Any], config: Any) -> bool:
        return True

    @abstractmethod
    def run(self, ctx: StepContext) -> StepResult: ...


class StepDeferred(Exception):
    """Step not ready yet: stay on the current step and retry next tick.

    Raising this does not count as a failure and does not advance the workflow.
    Use it for steps that wait on an external precondition (a file to appear, a
    time window, a manual hand-off).
    """
