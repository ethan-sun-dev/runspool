"""``steps``: the step registry. Plugins contribute steps with ``ctx.steps.register``.

The service is context-bound: a registration is an effect of the registering
plugin and is undone when that plugin unloads. A duplicate step name fails the
registering plugin, leaving the first registration in place.

``register_lazy(name, loader)`` reserves a name now and imports the step on first
use, so commands that only read task state never import step code. ``resolve_all``
imports every lazy step and reports the ones that fail; ``run``, ``daemon`` and
``doctor`` call it before relying on the registry.

Once the application has started, registering or unregistering a step is a change
to the loaded plugins and goes through the kernel's change guard, which refuses
it: the plugin set is fixed once started (see ``runspool.core.boot``).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from runspool.engine.artifacts import (
    ArtifactTypeError,
    ChainReport,
    Contract,
    check_workflow,
    check_workflows,
    contract_of,
)
from runspool.engine.registry import StepRegistry, check_retry
from runspool.engine.step import Step, StepContext, StepResult
from runspool.kernel import Plugin
from runspool.registry_builder import StepLoadError


class LazyStep(Step):
    """A step known by name whose implementation is imported on first use."""

    lazy = True  # the registry checks a lazy step when it loads, not when registered

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
                try:
                    check_retry(step)
                except TypeError as exc:
                    raise StepLoadError(str(exc)) from None
                self._step = step
            except Exception as exc:  # noqa: BLE001 - kept and re-raised on every use
                self._error = exc
        if self._error is not None:
            raise self._error
        assert self._step is not None
        return self._step

    @property
    def side_effect(self) -> bool:  # type: ignore[override]
        # Must come from the real step: a lazy proxy answering False would let a step
        # with side effects skip approval.
        return bool(getattr(self.resolve(), "side_effect", False))

    @property
    def retry(self) -> bool:  # type: ignore[override]
        # Must come from the real step: a lazy proxy answering True would let a step
        # that opted out of automatic retries be run again after it raised.
        return getattr(self.resolve(), "retry", True) is not False

    @property
    def requires(self) -> Any:  # type: ignore[override]
        return getattr(self.resolve(), "requires", None)

    @property
    def provides(self) -> Any:  # type: ignore[override]
        return getattr(self.resolve(), "provides", None)

    @property
    def external(self) -> bool:  # type: ignore[override]
        return bool(getattr(self.resolve(), "external", False))

    @property
    def max_attempts(self) -> Any:
        return getattr(self.resolve(), "max_attempts", None)

    def request(self, ctx: StepContext) -> Any:
        return self.resolve().request(ctx)  # type: ignore[attr-defined]

    def validate(self, ctx: StepContext, submission: Any) -> StepResult:
        return self.resolve().validate(ctx, submission)  # type: ignore[attr-defined]

    def when(self, task: dict[str, Any], config: Any) -> bool:
        return self.resolve().when(task, config)

    def run(self, ctx: StepContext) -> StepResult:
        return self.resolve().run(ctx)


class StepsService:
    def __init__(self) -> None:
        self.registry = StepRegistry()
        self.guards: list[Callable[[Any], str | None]] = []
        # Whether each step has side effects, recorded when it is registered (or, for
        # a lazy step, when it is first resolved) so that nothing changing the step
        # object later can take a step out of approval.
        self._side_effects: dict[str, bool] = {}

    def side_effect_of(self, name: str) -> bool:
        if name not in self._side_effects:
            self._side_effects[name] = bool(getattr(self.registry.get(name), "side_effect", False))
        return self._side_effects[name]

    def for_context(self, ctx: Any) -> _BoundSteps:
        return _BoundSteps(self, ctx)

    def names(self) -> list[str]:
        return self.registry.names()

    def has(self, name: str) -> bool:
        return self.registry.has(name)

    def contract_for(self, name: str) -> Contract | None:
        """The artifact contract of the step registered under ``name``; ``None`` if
        it is not registered or fails to load (other checks report those). Raises
        ArtifactTypeError when its declarations are malformed or cannot be read."""
        if not self.registry.has(name):
            return None
        return self._contract_of(name, self.registry.get(name))

    def _contract_of(self, name: str, step: Step) -> Contract | None:
        if isinstance(step, LazyStep):
            try:
                step = step.resolve()
            except Exception:  # noqa: BLE001 - a step that fails to load is reported elsewhere
                return None
        try:
            return contract_of(step)
        except ArtifactTypeError:
            raise
        except Exception as exc:  # noqa: BLE001 - e.g. a declaration property that raises
            raise ArtifactTypeError(
                f"step {name!r}: cannot read its declarations ({type(exc).__name__})"
            ) from None

    def check_contracts(self, workflows: Mapping[str, Any]) -> ChainReport:
        """Check the artifact contracts of ``workflows`` (``{name: WorkflowConfig}``)
        against the registered steps."""
        return check_workflows(workflows, self.contract_for)

    def check_contract(
        self, name: str, workflow: Any, *, running: Step | None = None
    ) -> ChainReport:
        """Check the artifact contracts of one workflow (a ``WorkflowConfig``).

        ``running``: the step object about to run, used for its own name instead of
        the registered one, so the check judges what runs. The other steps are the
        registered ones."""

        def lookup(step_name: str) -> Contract | None:
            if running is not None and step_name == running.name:
                return self._contract_of(step_name, running)
            return self.contract_for(step_name)

        return check_workflow(name, workflow, lookup)

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

    def check_contracts(self, workflows: Mapping[str, Any]) -> ChainReport:
        return self._service.check_contracts(workflows)

    def check_contract(
        self, name: str, workflow: Any, *, running: Step | None = None
    ) -> ChainReport:
        return self._service.check_contract(name, workflow, running=running)

    def side_effect_of(self, name: str) -> bool:
        return self._service.side_effect_of(name)

    @property
    def guards(self) -> list[Callable[[Any], str | None]]:
        return list(self._service.guards)

    def guard(self, check: Callable[[Any], str | None]) -> Callable[[], None]:
        """Add a final check run before every step: return a reason to refuse it,
        or ``None``. A guard can only refuse, never allow, so the order plugins
        register in can never turn a refusal into permission."""
        guards = self._service.guards

        def register() -> Callable[[], None]:
            guards.append(check)
            return lambda: guards.remove(check) if check in guards else None

        return self._ctx.effect(register, label="step-guard")

    def register_lazy(self, name: str, loader: Callable[[], Step]) -> Callable[[], None]:
        """Reserve ``name`` now; import the step with ``loader()`` on first use."""
        return self.register(LazyStep(name, loader))

    def register(self, step: Step | Callable[[], Step]) -> Callable[[], None]:
        """Register a step (or a zero-argument factory returning one)."""
        instance = step if isinstance(step, Step) else step()
        if not isinstance(instance, Step):
            raise TypeError(f"{instance!r} is not a runspool Step")
        registry = self._service.registry
        side_effects = self._service._side_effects
        fiber = self._ctx.fiber
        fiber.check_change(f"register step {instance.name!r}")

        def register() -> Callable[[], None]:
            registry.register(instance)
            if not isinstance(instance, LazyStep):
                side_effects[instance.name] = bool(getattr(instance, "side_effect", False))

            def unregister() -> None:
                registry.unregister(instance.name)
                side_effects.pop(instance.name, None)

            return unregister

        return fiber.effect(
            register, label=f"step:{instance.name}", change=f"unregister step {instance.name!r}"
        )


plugin = Plugin(name="steps", apply=lambda ctx, config: ctx.provide("steps", StepsService()))
