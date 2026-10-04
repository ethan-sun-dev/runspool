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
    # True when termination was requested; a long step should check this and
    # return early. Pause is NOT signalled here: it is applied at the step
    # boundary, so a running step is always allowed to finish.
    should_stop: Callable[[], bool]
    # No argument / None = refresh heartbeat only; pass a progress string to
    # refresh the heartbeat and persist ``progress`` (throttled by the runner).
    heartbeat: Callable[..., None]
    # Which run of this step this is: 1 the first time, counting earlier runs that
    # deferred, failed or degraded. Lets a step give up after N transient failures
    # without keeping its own bookkeeping files.
    attempt: int = 1


@dataclass(frozen=True)
class StepResult:
    """Result of a step run: an optional message and task field updates.

    ``updates`` may only set non-lifecycle columns (name, priority, max_retries,
    progress); status, step and lock fields belong to the state machine, so any
    other key fails the step instead of silently corrupting state.

    ``degraded`` marks a best-effort step that returned without doing its job (an
    input was missing, an optional upload failed). The run is recorded as degraded
    and the task, once every step has run, ends ``partially_completed`` instead of
    ``completed``. ``message`` is kept as the run's note.
    """

    message: str = ""
    updates: dict[str, Any] = field(default_factory=dict)
    degraded: bool = False


class Step(ABC):
    """A workflow step: an adapter over some capability.

    Subclasses must set the class attribute ``name`` and implement ``run``.
    ``when`` defaults to always-true; override it to declare a conditional skip
    (for example, a publish step that only runs when explicitly enabled).
    """

    name: str
    # True for a step whose effects leave RunSpool (publishing, uploading, sending a
    # message, creating a draft on a platform). Such a step runs only after a human
    # approves that attempt (``runspool approve``); see runspool.engine.gate.
    side_effect: bool = False

    def when(self, task: dict[str, Any], config: Any) -> bool:
        return True

    @abstractmethod
    def run(self, ctx: StepContext) -> StepResult: ...


class StepDeferred(Exception):
    """Step not ready yet: stay on the current step and retry later.

    Raising this does not count as a failure and does not advance the workflow.
    Use it for steps that wait on an external precondition (a file to appear, a
    time window, a manual hand-off). ``reason`` is recorded in the task's events
    (so ``status``/``logs`` show why it waits). With ``delay_seconds`` > 0 the task
    is not picked up again before that delay passes (``runspool wake`` cuts it short);
    with 0 it is retried on the next scheduling round.
    """

    def __init__(self, reason: str = "not ready, will retry", *, delay_seconds: int = 0) -> None:
        super().__init__(reason)
        self.reason = reason
        self.delay_seconds = delay_seconds
