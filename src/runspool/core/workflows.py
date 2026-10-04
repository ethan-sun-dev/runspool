"""``workflows``: named workflow definitions (ordered step lists).

Definitions come from the profile's ``workflows`` setting. A plugin may contribute a
default with ``ctx.workflows.add_default(name, steps)``; the profile's own
definition of the same name always wins. Engine components still read workflows
through the config object, which this service keeps in sync.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from runspool.config import AppConfig, WorkflowConfig
from runspool.kernel import Plugin
from runspool.models import WorkflowDef


class WorkflowsService:
    def __init__(self, config: AppConfig) -> None:
        self._config = config

    def for_context(self, ctx: Any) -> _BoundWorkflows:
        return _BoundWorkflows(self, ctx)

    def get(self, name: str) -> WorkflowDef:
        return self._config.workflow(name)

    def names(self) -> list[str]:
        return list(self._config.workflows)


class _BoundWorkflows:
    def __init__(self, service: WorkflowsService, ctx: Any) -> None:
        self._service = service
        self._ctx = ctx

    def get(self, name: str) -> WorkflowDef:
        return self._service.get(name)

    def names(self) -> list[str]:
        return self._service.names()

    def add_default(self, name: str, steps: Sequence[str]) -> Callable[[], None]:
        workflows = self._service._config.workflows

        def register() -> Callable[[], None] | None:
            if name in workflows:
                return None  # the profile (or an earlier plugin) already defines it
            definition = WorkflowConfig(steps=list(steps))
            workflows[name] = definition

            def unregister() -> None:
                if workflows.get(name) is definition:
                    del workflows[name]

            return unregister

        return self._ctx.effect(register, label=f"workflow:{name}")


plugin = Plugin(
    name="workflows",
    apply=lambda ctx, config: ctx.provide("workflows", WorkflowsService(ctx.config)),
    inject=["config"],
)
