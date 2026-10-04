"""``steps``: the step registry. Plugins contribute steps with ``ctx.steps.register``.

The service is context-bound: a registration is an effect of the registering
plugin and is undone when that plugin unloads. A duplicate step name fails the
registering plugin, leaving the first registration in place.

``register_lazy(name, loader)`` reserves a name now and imports the step on first
use, so commands that only read task state never import step code. ``resolve_all``
imports every lazy step and reports the ones that fail; ``run``, ``daemon`` and
``doctor`` call it before relying on the registry.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from runspool.engine.registry import StepRegistry
from runspool.engine.step import Step, StepContext, StepResult
from runspool.kernel import Plugin
from runspool.registry_builder import StepLoadError


class LazyStep(Step):
    """A step known by name whose implementation is imported on first use."""

    def __init__(self, name: str, loader: Callable[[], Step]) -> None:
        self.name = name
        self._loader = loader
        self._step: Step | None = None
        self._error: Exception | None = None

    def resolve(self) -> Step:
        if self._step is None and self._error is None:
            try:
                step = self._loader()
                if not isinstance(step, Step):
                    raise StepLoadError(f"{step!r} is not a runspool Step")
                if step.name != self.name:
                    raise StepLoadError(
                        f"step key {self.name!r} does not match step name {step.name!r}"
                    )
                self._step = step
            except Exception as exc:  # noqa: BLE001 - kept and re-raised on every use
                self._error = exc
        if self._error is not None:
            raise self._error
        assert self._step is not None
        return self._step

    def when(self, task: dict[str, Any], config: Any) -> bool:
        return self.resolve().when(task, config)

    def run(self, ctx: StepContext) -> StepResult:
        return self.resolve().run(ctx)


class StepsService:
    def __init__(self) -> None:
        self.registry = StepRegistry()

    def for_context(self, ctx: Any) -> _BoundSteps:
        return _BoundSteps(self, ctx)

    def names(self) -> list[str]:
        return self.registry.names()

    def has(self, name: str) -> bool:
        return self.registry.has(name)

    def resolve_all(self) -> list[tuple[str, Exception]]:
        """Import every lazy step; return ``(name, error)`` for those that fail."""
        failures = []
        for name in self.registry.names():
            step = self.registry.get(name)
            if isinstance(step, LazyStep):
                try:
                    step.resolve()
                except Exception as exc:  # noqa: BLE001 - reported, not raised
                    failures.append((name, exc))
        return failures


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

    def resolve_all(self) -> list[tuple[str, Exception]]:
        return self._service.resolve_all()

    def register_lazy(self, name: str, loader: Callable[[], Step]) -> Callable[[], None]:
        """Reserve ``name`` now; import the step with ``loader()`` on first use."""
        return self.register(LazyStep(name, loader))

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
