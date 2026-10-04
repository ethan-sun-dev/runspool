"""Plugin lifecycle, services and contexts: the heart of the kernel.

Every loaded plugin runs in a :class:`Fiber`. A fiber:

* waits (PENDING) until every service it ``inject``s is *usable* (see below);
* then validates its config and calls ``apply`` (LOADING -> ACTIVE);
* records every registration made during ``apply`` (or later while active) as an
  *effect* with a disposer, and runs them in reverse order when it unloads; what
  ``apply`` itself returns runs last;
* rolls all of its effects back if ``apply`` raises (-> FAILED), without touching
  any other plugin;
* reloads automatically when the services it injects change: each provided service
  registration gets a serial number, the injected registrations are fingerprinted
  as an "epoch" string, and any change to it unloads the fiber and loads it again.
  A provider that reloads in place therefore reloads its dependents too, and a
  FAILED dependent is retried against the new registration.

A service is usable by a consumer only when its provider and every ancestor of the
provider are ACTIVE, except that a plugin and its own descendants may use what a
LOADING ancestor already provides. A provider whose ``apply`` is still running
never wakes outsiders early. When a provider unloads, its dependents are unloaded
first, while the provider's services are still in place.

Disposing a fiber that is mid-transition (from inside its own ``apply``, or from a
disposer while it unloads) is deferred until the transition ends, never lost.

The kernel is synchronous and single-threaded for lifecycle changes.

Lifecycle and epoch-based dependency algorithm adapted from Cordis
(MIT, (c) 2021-present Shigma); see THIRD_PARTY_NOTICES.md.
"""

from __future__ import annotations

import itertools
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from runspool.kernel.errors import (
    InactiveEffectError,
    KernelError,
    ServiceConflict,
    ServiceNotInjected,
)
from runspool.kernel.events import EventBus
from runspool.kernel.plugin import PluginSpec, normalize, validate_config

log = logging.getLogger("runspool.kernel")

Disposer = Callable[[], None]

# Attribute names a service may not use: they are Context's own API.
RESERVED_NAMES = frozenset(
    {"plugin", "provide", "get", "on", "emit", "bail", "waterfall", "effect", "logger", "fiber"}
)


class FiberState(StrEnum):
    PENDING = "pending"
    LOADING = "loading"
    ACTIVE = "active"
    FAILED = "failed"
    UNLOADING = "unloading"
    DISPOSED = "disposed"


class _Effect:
    """A registration and the disposers that undo it. Calling it disposes it, once."""

    __slots__ = ("fiber", "label", "_disposers", "_done")

    def __init__(self, fiber: Fiber, disposers: list[Disposer], label: str) -> None:
        self.fiber = fiber
        self.label = label
        self._disposers = disposers
        self._done = False

    def __call__(self) -> None:
        if self._done:
            return
        self._done = True
        if self in self.fiber._effects:
            self.fiber._effects.remove(self)
        for disposer in reversed(self._disposers):
            try:
                disposer()
            except Exception:  # noqa: BLE001 - teardown keeps going; failures are logged
                log.exception("disposer %r of plugin %r failed", self.label, self.fiber.name)


@dataclass(eq=False)
class _Impl:
    name: str
    value: Any
    fiber: Fiber
    serial: int


class Fiber:
    """One loaded plugin: its context, config, state and effects."""

    def __init__(
        self, kernel: Kernel, parent: Fiber | None, spec: PluginSpec, raw_config: Any, name: str
    ) -> None:
        self._kernel = kernel
        self.parent = parent
        self.spec = spec
        self.name = name
        self.uid: int = next(kernel._uids)
        self.raw_config = raw_config
        self.config: Any = None
        self.inject = frozenset(spec.inject)
        self.error: BaseException | None = None
        self.ctx = Context(kernel, self)
        self._effects: list[_Effect] = []
        self._store: dict[str, Any] = {}
        self._loaded_epoch: str | None = None
        self._failed_epoch: str | None = None
        self._phase: FiberState | None = None  # LOADING / UNLOADING while transitioning
        self._reconciling = False
        self._dirty = False
        self._disposed = False
        self._dispose_requested = False
        self._parent_effect: _Effect | None = None

    def __repr__(self) -> str:
        return f"<Fiber {self.name} #{self.uid} {self.state.value}>"

    # -- state --------------------------------------------------------------------

    @property
    def state(self) -> FiberState:
        if self._disposed:
            return FiberState.DISPOSED
        if self._phase is not None:
            return self._phase
        if self._loaded_epoch is not None:
            return FiberState.ACTIVE
        if self.error is not None:
            return FiberState.FAILED
        return FiberState.PENDING

    def missing_services(self) -> list[str]:
        """Injected services that are not usable right now."""
        return [name for name in sorted(self.inject) if not self._kernel._usable(name, self)]

    def wait(self) -> Fiber:
        """Raise this fiber's load error if it FAILED; return the fiber otherwise."""
        if self.error is not None and self.state is FiberState.FAILED:
            raise self.error
        return self

    def ancestors(self) -> Iterable[Fiber]:
        """This fiber, its parent, and so on up to (and including) the root."""
        fiber: Fiber | None = self
        while fiber is not None:
            yield fiber
            fiber = fiber.parent

    # -- effects ------------------------------------------------------------------

    def effect(self, body: Callable[[], Any], label: str = "effect") -> Disposer:
        """Run ``body`` now and remember how to undo it.

        ``body`` returns ``None``, a disposer, or an iterable of disposers. The
        returned callable disposes the effect early; otherwise it is disposed when
        the fiber unloads.
        """
        if not self._accepts_effects():
            raise InactiveEffectError(
                f"plugin {self.name!r} is {self.state.value}; it cannot register {label!r}"
            )
        disposers = _disposers_of(body(), label)
        effect = _Effect(self, disposers, label)
        if not self._accepts_effects():
            # The body itself tore this fiber down; undo it now rather than leak it.
            effect()
            raise InactiveEffectError(
                f"plugin {self.name!r} became {self.state.value} while registering {label!r}"
            )
        self._effects.append(effect)
        return effect

    def _accepts_effects(self) -> bool:
        if self._disposed or self._phase is FiberState.UNLOADING:
            return False
        return self._phase is FiberState.LOADING or self._loaded_epoch is not None

    # -- lifecycle ----------------------------------------------------------------

    def restart(self) -> None:
        """Unload (if loaded) and load again; also retries a FAILED fiber."""
        if self._disposed or self._phase is not None:
            return
        if self._loaded_epoch is not None:
            self._unload()
        self._failed_epoch = None
        self._reconcile()

    def dispose(self) -> None:
        """Unload and permanently remove this fiber (and every child plugin).

        Called mid-transition (from inside its own ``apply``, or from one of its
        disposers), the dispose happens as soon as that transition ends.
        """
        if self._disposed:
            return
        if self._phase is not None:
            self._dispose_requested = True
            return
        if self._loaded_epoch is not None:
            self._unload()  # finalizes the dispose itself if one is requested meanwhile
            if self._disposed:
                return
        self._finalize_dispose()

    def _finalize_dispose(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        self._dispose_requested = False
        if self in self._kernel._fibers:
            self._kernel._fibers.remove(self)
        if self._parent_effect is not None:
            self._parent_effect()
        self._kernel.events.emit("internal/status", self)

    def _epoch(self) -> str | None:
        parts = []
        for name in sorted(self.inject):
            if not self._kernel._usable(name, self):
                return None
            parts.append(f"{name}={self._kernel._impls[name].serial}")
        return ";".join(parts)

    def _reconcile(self) -> None:
        if self._disposed:
            return
        if self._reconciling:
            # A notification arrived mid-transition; re-check when the transition ends.
            self._dirty = True
            return
        self._reconciling = True
        try:
            while not self._disposed:
                self._dirty = False
                desired = self._epoch()
                if self._loaded_epoch is not None and desired != self._loaded_epoch:
                    self._unload()
                if (
                    desired is not None
                    and self._loaded_epoch is None
                    and desired != self._failed_epoch
                    and not self._disposed
                ):
                    self._load(desired)
                if not self._dirty:
                    break
        finally:
            self._reconciling = False

    def _load(self, epoch: str) -> None:
        self._phase = FiberState.LOADING
        self.error = None
        self._failed_epoch = None
        self._store = {name: self._kernel._impls[name].value for name in self.inject}
        # What apply returns tears the plugin itself down, so it must run *after* every
        # effect apply registered (children, provides, listeners). Reserve its slot
        # first; reverse-order disposal then runs it last.
        own: list[Disposer] = []
        self._effects.append(_Effect(self, own, "apply"))
        try:
            self.config = validate_config(self.spec.config_model, self.raw_config)
            result = self.spec.apply(self.ctx, self.config)
            if result is not None:
                dispose = getattr(result, "dispose", None)
                if callable(dispose):
                    own.append(dispose)
                elif callable(result):
                    own.append(result)
        except Exception as exc:  # noqa: BLE001 - a plugin failure is contained to its fiber
            log.error("plugin %r failed to load: %s", self.name, exc)
            self.error = exc
            self._failed_epoch = epoch
            self._phase = FiberState.UNLOADING
            self._run_disposers()
            self._store = {}
            self._phase = None
            self._kernel.events.emit("internal/status", self)
            if self._dispose_requested:
                self._finalize_dispose()
            return
        self._loaded_epoch = epoch
        self._phase = None
        self._kernel.events.emit("internal/status", self)
        if self._dispose_requested:
            self._unload()
            return
        self._kernel._notify(self._kernel._names_provided_within(self))

    def _unload(self) -> None:
        if self._phase is FiberState.UNLOADING or self._loaded_epoch is None:
            return
        self._phase = FiberState.UNLOADING
        # Dependents unload first, while this fiber's services are still in place.
        self._kernel._notify(self._kernel._names_provided_within(self))
        self._run_disposers()
        self._store = {}
        self._loaded_epoch = None
        self._phase = None
        self._kernel.events.emit("internal/status", self)
        if self._dispose_requested:
            self._finalize_dispose()

    def _run_disposers(self) -> None:
        while self._effects:
            self._effects[-1]()


def _disposers_of(result: Any, label: str) -> list[Disposer]:
    if result is None:
        return []
    if callable(result):
        return [result]
    if isinstance(result, Iterable) and not isinstance(result, (str, bytes)):
        disposers = list(result)
        if all(callable(item) for item in disposers):
            return disposers
    raise KernelError(f"effect {label!r} returned {result!r}, not a disposer or disposers")


class Context:
    """What a plugin sees: its services, events, and registration API.

    Reading ``ctx.<name>`` returns a service the plugin declared in ``inject``, or
    one that this plugin or one of its (non-root) ancestors provides. Anything else
    raises :class:`ServiceNotInjected`, so a plugin cannot reach a service it did
    not declare. Use :meth:`get` for an optional dependency.

    A service object with a ``for_context(ctx)`` method is *context-bound*: each
    plugin receives ``service.for_context(its_ctx)`` instead of the shared object.
    That is how ``ctx.steps.register(step)`` can record the registration as an
    effect of the calling plugin, undone automatically when it unloads.
    """

    __slots__ = ("_kernel", "fiber")

    def __init__(self, kernel: Kernel, fiber: Fiber) -> None:
        self._kernel = kernel
        self.fiber = fiber

    def __repr__(self) -> str:
        return f"<Context of {self.fiber.name}>"

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        if name in self.fiber._store:
            return self._bind(self.fiber._store[name])
        impl = self._kernel._impls.get(name)
        if impl is not None:
            for fiber in self.fiber.ancestors():
                if impl.fiber is fiber:
                    return self._bind(impl.value)
                if fiber.parent is self._kernel.root_fiber:
                    break  # the root's services must be injected like anyone else's
        raise ServiceNotInjected(
            f"plugin {self.fiber.name!r} reads service {name!r} without declaring it in inject"
        )

    @property
    def logger(self) -> logging.Logger:
        return logging.getLogger(f"runspool.plugin.{self.fiber.name}")

    def plugin(self, plugin: Any, config: Any = None, *, name: str | None = None) -> Fiber:
        """Load a child plugin. It is disposed together with this plugin."""
        return self._kernel._mount(self.fiber, plugin, config, name)

    def provide(self, name: str, value: Any) -> Disposer:
        """Provide a service under ``name`` for as long as this plugin is loaded."""
        return self._kernel._provide(self.fiber, name, value)

    def get(self, name: str) -> Any:
        """Return a service if it is usable right now, else ``None``. No inject needed."""
        if not self._kernel._usable(name, self.fiber):
            return None
        return self._bind(self._kernel._impls[name].value)

    def _bind(self, value: Any) -> Any:
        binder = getattr(value, "for_context", None)
        return binder(self) if callable(binder) else value

    def effect(self, body: Callable[[], Any], label: str = "effect") -> Disposer:
        return self.fiber.effect(body, label)

    def on(self, event: str, callback: Callable[..., Any], *, prepend: bool = False) -> Disposer:
        return self._kernel.events.on(self.fiber, event, callback, prepend=prepend)

    def emit(self, event: str, *args: Any) -> None:
        self._kernel.events.emit(event, *args)

    def bail(self, event: str, *args: Any) -> Any:
        return self._kernel.events.bail(event, *args)

    def waterfall(self, event: str, *args: Any, default: Callable[[], Any]) -> Any:
        return self._kernel.events.waterfall(event, *args, default=default)


class Kernel:
    """The plugin runtime: a root context, the services table and the event bus."""

    def __init__(self) -> None:
        self._uids = itertools.count(1)
        self._serials = itertools.count(1)
        self._impls: dict[str, _Impl] = {}
        self._fibers: list[Fiber] = []
        self.events = EventBus()
        self.root_fiber = Fiber(self, None, normalize(lambda ctx, config: None), None, "root")
        self.root_fiber._loaded_epoch = ""  # the root is always active
        self.root = self.root_fiber.ctx

    @property
    def fibers(self) -> list[Fiber]:
        """Every live plugin fiber, in load order (the root excluded)."""
        return list(self._fibers)

    def dispose(self) -> None:
        """Unload every plugin, newest first. Plugins still loading finish, then go."""
        root = self.root_fiber
        root._phase = FiberState.UNLOADING
        try:
            root._run_disposers()
        finally:
            root._phase = None

    # -- internals used by Fiber / Context ----------------------------------------

    def _mount(self, parent: Fiber, plugin: Any, config: Any, name: str | None) -> Fiber:
        if not parent._accepts_effects():
            raise InactiveEffectError(
                f"plugin {parent.name!r} is {parent.state.value}; it cannot load child plugins"
            )
        spec = normalize(plugin)
        child = Fiber(self, parent, spec, config, name or spec.name)
        self._fibers.append(child)
        child._parent_effect = parent.effect(lambda: child.dispose, label=f"plugin:{child.name}")
        child._reconcile()
        return child

    def _provide(self, fiber: Fiber, name: str, value: Any) -> Disposer:
        if name in RESERVED_NAMES or name.startswith("_"):
            raise KernelError(f"{name!r} cannot be used as a service name")
        existing = self._impls.get(name)
        if existing is not None:
            raise ServiceConflict(
                f"service {name!r} is already provided by plugin {existing.fiber.name!r}"
            )
        impl = _Impl(name, value, fiber, next(self._serials))

        def register() -> Disposer:
            self._impls[name] = impl

            def unregister() -> None:
                if self._impls.get(name) is impl:
                    del self._impls[name]
                    self._notify({name})

            return unregister

        undo = fiber.effect(register, label=f"provide:{name}")
        # Notify only once the registration is recorded, so a dependent that tears the
        # provider down in response also tears this service down.
        self._notify({name})
        return undo

    def _usable(self, name: str, consumer: Fiber) -> bool:
        impl = self._impls.get(name)
        if impl is None:
            return False
        lineage = set(consumer.ancestors())
        for fiber in impl.fiber.ancestors():
            state = fiber.state
            if state is FiberState.ACTIVE:
                continue
            if state is FiberState.LOADING and fiber in lineage:
                continue  # a plugin may use what its own loading ancestor provides
            return False
        return True

    def _names_provided_within(self, fiber: Fiber) -> set[str]:
        """Services provided by ``fiber`` or any of its descendants."""
        return {
            name
            for name, impl in self._impls.items()
            if any(ancestor is fiber for ancestor in impl.fiber.ancestors())
        }

    def _notify(self, names: set[str]) -> None:
        if not names:
            return
        for fiber in list(self._fibers):
            if fiber.inject & names:
                fiber._reconcile()
