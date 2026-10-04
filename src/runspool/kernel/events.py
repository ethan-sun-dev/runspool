"""Event bus with three dispatch modes.

* ``emit``      broadcast; return values ignored; a failing listener is logged and
                skipped, so observers can never break the emitter.
* ``bail``      call listeners in order; the first result that is not ``None`` or
                ``False`` wins and stops the chain. Exceptions propagate.
* ``waterfall`` around-middleware: each listener is called as ``listener(*args, next)``
                and decides whether to call ``next()``. Returning without calling it
                vetoes the rest of the chain. ``default()`` is the innermost result.

Registering a listener is an effect of the registering plugin's fiber, so it is
removed automatically when that plugin unloads.

Dispatch modes and ordering semantics adapted from Cordis (MIT, (c) Shigma); see
THIRD_PARTY_NOTICES.md.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:
    from runspool.kernel.fiber import Fiber

T = TypeVar("T")
log = logging.getLogger("runspool.kernel.events")


class _Listener:
    __slots__ = ("callback", "fiber")

    def __init__(self, callback: Callable[..., Any], fiber: Fiber) -> None:
        self.callback = callback
        self.fiber = fiber


class EventBus:
    def __init__(self) -> None:
        self._listeners: dict[str, list[_Listener]] = {}

    def on(
        self, fiber: Fiber, event: str, callback: Callable[..., Any], *, prepend: bool = False
    ) -> Callable[[], None]:
        listener = _Listener(callback, fiber)

        def register() -> Callable[[], None]:
            listeners = self._listeners.setdefault(event, [])
            if prepend:
                listeners.insert(0, listener)
            else:
                listeners.append(listener)

            def unregister() -> None:
                current = self._listeners.get(event, [])
                if listener in current:
                    current.remove(listener)

            return unregister

        return fiber.effect(register, label=f"on:{event}")

    def listeners(self, event: str) -> list[Callable[..., Any]]:
        return [entry.callback for entry in self._listeners.get(event, [])]

    def emit(self, event: str, *args: Any) -> None:
        for entry in list(self._listeners.get(event, [])):
            try:
                entry.callback(*args)
            except Exception:  # noqa: BLE001 - observers must not break the emitter
                log.exception("listener for %r in plugin %r failed", event, entry.fiber.name)

    def bail(self, event: str, *args: Any) -> Any:
        for entry in list(self._listeners.get(event, [])):
            result = entry.callback(*args)
            if result is not None and result is not False:
                return result
        return None

    def waterfall(self, event: str, *args: Any, default: Callable[[], T]) -> T:
        chain = list(self._listeners.get(event, []))

        def call(index: int) -> T:
            if index == len(chain):
                return default()
            return chain[index].callback(*args, lambda: call(index + 1))

        return call(0)
