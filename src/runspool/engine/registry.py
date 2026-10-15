"""Step registry: register and resolve steps by name.

Adding a capability means registering a new step; the engine never changes.
"""

from __future__ import annotations

import threading

from runspool.engine.step import Step


class StepRegistry:
    """Steps by name. ``generation`` changes whenever a step is registered or
    unregistered (plugins can be reloaded at run time), so a reader that needs a
    consistent view takes a :meth:`snapshot` and checks the generation after."""

    def __init__(self) -> None:
        self._steps: dict[str, Step] = {}
        self._lock = threading.Lock()
        self._generation = 0

    @property
    def generation(self) -> int:
        return self._generation

    def snapshot(self) -> tuple[int, dict[str, Step]]:
        """The current generation and a copy of the steps, taken together."""
        with self._lock:
            return self._generation, dict(self._steps)

    def register(self, step: Step) -> None:
        with self._lock:
            if step.name in self._steps:
                raise ValueError(f"step already registered: {step.name!r}")
            self._steps[step.name] = step
            self._generation += 1

    def unregister(self, name: str) -> None:
        with self._lock:
            if self._steps.pop(name, None) is not None:
                self._generation += 1

    def get(self, name: str) -> Step:
        steps = self._steps
        if name not in steps:
            raise KeyError(f"unregistered step: {name!r}")
        return steps[name]

    def has(self, name: str) -> bool:
        return name in self._steps

    def names(self) -> list[str]:
        return sorted(self._steps)
