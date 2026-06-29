"""Step registry: register and resolve steps by name.

Adding a capability means registering a new step; the engine never changes.
"""

from __future__ import annotations

from runspool.engine.step import Step


class StepRegistry:
    def __init__(self) -> None:
        self._steps: dict[str, Step] = {}

    def register(self, step: Step) -> None:
        if step.name in self._steps:
            raise ValueError(f"step already registered: {step.name!r}")
        self._steps[step.name] = step

    def get(self, name: str) -> Step:
        if name not in self._steps:
            raise KeyError(f"unregistered step: {name!r}")
        return self._steps[name]

    def has(self, name: str) -> bool:
        return name in self._steps

    def names(self) -> list[str]:
        return sorted(self._steps)
