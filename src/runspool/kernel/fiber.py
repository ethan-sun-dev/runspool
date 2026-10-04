"""Plugin lifecycle, services and contexts: the heart of the kernel.

Every loaded plugin runs in a :class:`Fiber`. A fiber:

* waits (PENDING) until every service it ``inject``s is provided by an ACTIVE fiber;
* then validates its config and calls ``apply`` (LOADING -> ACTIVE);
* records every registration made during ``apply`` (or later while active) as an
  *effect* with a disposer, and runs them in reverse order when it unloads;
* rolls all of its effects back if ``apply`` raises (-> FAILED), without touching
  any other plugin;
* reloads automatically when the identity of its providers changes: the set of
  provider fibers is fingerprinted as an "epoch" string, and any change to it
  unloads the fiber and loads it again against the new providers.

Providers count only once ACTIVE, so a provider whose ``apply`` is still running
never wakes its dependents early. When a provider unloads, its dependents are
unloaded first, while the provider's services are still usable.

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
        """Injected services that have no ACTIVE provider right now."""
        return [name for name in sorted(self.inject) if not self._kernel._usable(name, self)]

    def wait(self) -> Fiber:
        """Raise this fiber's load error if it FAILED; return the fiber otherwise."""
        if self.error is not None and self.state is FiberState.FAILED:
            raise self.error
        return self

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
        result = body()
        if result is None:
            disposers: list[Disposer] = []
        elif callable(result):
            disposers = [result]
        elif isinstance(result, Iterable):
            disposers = list(result)
        else:
            raise KernelError(f"effect {label!r} returned {result!r}, not a disposer")
        effect = _Effect(self, disposers, label)
        self._effects.append(effect)
        return effect

    def _accepts_effects(self) -> bool:
        if self._disposed or self._phase is FiberState.UNLOADING:
            return False
        return self._phase is FiberState.LOADING or self._loaded_epoch is not None

    # -- lifecycle ----------------------------------------------------------------

    def restart(self) -> None:
        """Unload (if loaded) and load again; also retries a FAILED fiber."""
        if self._disposed:
            return
        if self._loaded_epoch is not None:
            self._unload()
        self._failed_epoch = None
        self._reconcile()

    def dispose(self) -> None:
        """Unload and permanently remove this fiber (and every child plugin)."""
        if self._disposed:
            return
        if self._phase is FiberState.LOADING:
            raise KernelError(f"plugin {self.name!r} cannot be disposed from inside its apply")
        if self._loaded_epoch is not None:
            self._unload()
        self._disposed = True
        self._kernel._fibers.remove(self)
        if self._parent_effect is not None:
            self._parent_effect()
        self._kernel.events.emit("internal/status", self)

    def _epoch(self) -> str | None:
        parts = []
        for name in sorted(self.inject):
            impl = self._kernel._impls.get(name)
            if impl is None or not self._kernel._usable(name, self):
                return None
            parts.append(f"{name}={impl.fiber.uid}")
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
            while True:
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
            return
        self._loaded_epoch = epoch
        self._phase = None
        self._kernel.events.emit("internal/status", self)
        self._kernel._notify(self._kernel._names_provided_by(self))

    def _unload(self) -> None:
        self._phase = FiberState.UNLOADING
        # Dependents unload first, while this fiber's services are still in place.
        self._kernel._notify(self._kernel._names_provided_by(self))
        self._run_disposers()
        self._store = {}
        self._loaded_epoch = None
        self._phase = None
        self._kernel.events.emit("internal/status", self)

    def _run_disposers(self) -> None:
        while self._effects:
            self._effects[-1]()


class Context:
    """What a plugin sees: its services, events, and registration API.

    Reading ``ctx.<name>`` returns a service the plugin declared in ``inject`` (or
    one its own fiber, or an ancestor's, provides). Anything else raises
    :class:`ServiceNotInjected`, so a plugin cannot reach a service it did not
    declare. Use :meth:`get` for an optional dependency.
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
        fiber: Fiber | None = self.fiber
        while fiber is not None:
            if name in fiber._store:
                return fiber._store[name]
            impl = self._kernel._impls.get(name)
            if impl is not None and impl.fiber is fiber:
                return impl.value
            fiber = fiber.parent
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
        """Return a service if it is currently provided by an ACTIVE fiber, else ``None``."""
        impl = self._kernel._impls.get(name)
        if impl is None or not self._kernel._usable(name, self.fiber):
            return None
        return impl.value

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
        """Unload every plugin, newest first."""
        self.root_fiber._run_disposers()

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

        def register() -> Disposer:
            impl = _Impl(name, value, fiber)
            self._impls[name] = impl
            if fiber.state is FiberState.ACTIVE:
                self._notify({name})

            def unregister() -> None:
                if self._impls.get(name) is impl:
                    del self._impls[name]
                    self._notify({name})

            return unregister

        return fiber.effect(register, label=f"provide:{name}")

    def _usable(self, name: str, consumer: Fiber) -> bool:
        impl = self._impls.get(name)
        if impl is None:
            return False
        if impl.fiber.state is FiberState.ACTIVE:
            return True
        # A plugin (and its children) may use what it provides while it is loading.
        ancestor: Fiber | None = consumer
        while ancestor is not None:
            if ancestor is impl.fiber and impl.fiber.state is FiberState.LOADING:
                return True
            ancestor = ancestor.parent
        return False

    def _names_provided_by(self, fiber: Fiber) -> set[str]:
        return {name for name, impl in self._impls.items() if impl.fiber is fiber}

    def _notify(self, names: set[str]) -> None:
        if not names:
            return
        for fiber in list(self._fibers):
            if fiber.inject & names:
                fiber._reconcile()
