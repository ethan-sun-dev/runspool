"""The pre-execute gate: whether a claimed step may run now.

Before a step runs, the runner asks the gate. The answer is one of a closed set:

* :class:`Allow`  run the step;
* :class:`Deny`   do not run it; the task needs attention (``manual_required``);
* :class:`Defer`  not now; try again later (like ``StepDeferred``);
* :class:`Ask`    a human must approve it first (``awaiting_approval``).

Anything else is treated as :class:`Deny`. An approval is a *grant* for one attempt
of one step: a retry or a re-run after a crash asks again.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from runspool.engine.step import Step


@dataclass(frozen=True)
class Allow:
    pass


@dataclass(frozen=True)
class Deny:
    reason: str


@dataclass(frozen=True)
class Defer:
    reason: str
    delay_seconds: int = 0


@dataclass(frozen=True)
class Ask:
    reason: str


Decision = Allow | Deny | Defer | Ask
DECISIONS = (Allow, Deny, Defer, Ask)


@dataclass(frozen=True)
class GateRequest:
    """What a gate policy sees: the task, the step about to run, and which attempt."""

    task: dict[str, Any]
    step: Step
    attempt: int

    @property
    def granted(self) -> bool:
        """Whether a human approved exactly this attempt of this step."""
        return self.task.get("approval_grant") == grant_value(self.step.name, self.attempt)


def asked_value(step: str, attempt: int) -> str:
    return f"asked:{step}:{attempt}"


def grant_value(step: str, attempt: int) -> str:
    return f"granted:{step}:{attempt}"
