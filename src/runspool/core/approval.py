"""``approval``: who may approve steps with side effects, and how they hear about it.

The rule that a step with ``side_effect = True`` needs approval is enforced by the
runtime's gate, not here: disabling or replacing this plugin can make approval
impossible (such steps are then refused) but never unnecessary.

Policy (plugin config ``policy``):

* ``ask``   (default) the task waits in ``awaiting_approval`` until someone runs
            ``runspool approve <id>`` or ``runspool reject <id>``;
* ``never`` refuse such steps outright, e.g. for unattended CI runs.

When a task starts waiting, the service emits ``approval/request`` with the task,
so a notification plugin can tell someone (by message, mail, ...).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel

from runspool.kernel import Plugin


class ApprovalConfig(BaseModel):
    policy: Literal["ask", "never"] = "ask"


class ApprovalService:
    def __init__(self, ctx: Any, policy: str) -> None:
        self._ctx = ctx
        self.policy = policy

    def requested(self, task: dict[str, Any]) -> None:
        """A task started waiting for approval: tell whoever listens."""
        self._ctx.emit("approval/request", task)


plugin = Plugin(
    name="approval",
    apply=lambda ctx, config: ctx.provide("approval", ApprovalService(ctx, config.policy)),
    Config=ApprovalConfig,
)
