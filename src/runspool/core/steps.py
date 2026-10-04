"""``steps``: the step registry. Plugins contribute steps with ``ctx.steps.register``.

The service is context-bound: a registration is an effect of the registering
plugin and is undone when that plugin unloads. A duplicate step name fails the
registering plugin, leaving the first registration in place.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from runspool.engine.registry import StepRegistry
from runspool.engine.step import Step
from runspool.kernel import Plugin


class StepsService:
    def __init__(self) -> None:
        self.registry = StepRegistry()

    def for_context(self, ctx: Any) -> _BoundSteps:
        return _BoundSteps(self, ctx)

    def names(self) -> list[str]:
        return self.registry.names()

    def has(self, name: str) -> bool:
        return self.registry.has(name)


class _BoundSteps:
    def __init__(self, service: StepsService, ctx: Any) -> None:
        self._service = service
        self._ctx = ctx

    @property
    def registry(self) -> StepRegistry:
        return self._service.registry

    def names(self) -> list[str]:
        return self._service.names()

    def has(self, name: str) -> bool:
        return self._service.has(name)

    def register(self, step: Step | Callable[[], Step]) -> Callable[[], None]:
        """Register a step (or a zero-argument factory returning one)."""
        instance = step if isinstance(step, Step) else step()
        if not isinstance(instance, Step):
            raise TypeError(f"{instance!r} is not a runspool Step")
        registry = self._service.registry

        def register() -> Callable[[], None]:
            registry.register(instance)
            return lambda: registry.unregister(instance.name)

        return self._ctx.effect(register, label=f"step:{instance.name}")


plugin = Plugin(name="steps", apply=lambda ctx, config: ctx.provide("steps", StepsService()))
