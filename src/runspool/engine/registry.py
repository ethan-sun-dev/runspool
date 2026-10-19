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
        if not getattr(step, "lazy", False):  # a lazy step is checked when it loads
            check_retry(step)
        self._steps[step.name] = step

    def no_retry(self, name: str) -> bool:
        """Whether the step must not be requeued automatically: it opted out of
        automatic retries (``retry = False``), or it cannot be answered for (unknown,
        or it cannot load), so a person decides rather than an unknown step rerun."""
        try:
            return getattr(self.get(name), "retry", True) is False
        except Exception:  # noqa: BLE001 - reported where the step is used
            return True

    def unregister(self, name: str) -> None:
        self._steps.pop(name, None)

    def get(self, name: str) -> Step:
        if name not in self._steps:
            raise KeyError(f"unregistered step: {name!r}")
        return self._steps[name]

    def has(self, name: str) -> bool:
        return name in self._steps

    def names(self) -> list[str]:
        return sorted(self._steps)


def check_retry(step: Step) -> None:
    """``retry`` must be a bool: ``0`` or ``None`` would silently mean "retry"."""
    value = getattr(step, "retry", True)
    if not isinstance(value, bool):
        raise TypeError(f"step {step.name!r}: retry must be True or False, not {value!r}")
