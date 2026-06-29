"""Build the step registry: built-in steps plus dynamically imported plugins."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

from runspool.builtin_steps import register_builtins
from runspool.config import AppConfig
from runspool.engine.registry import StepRegistry
from runspool.engine.step import Step


class StepLoadError(Exception):
    """A configured custom step could not be loaded."""


def load_step(import_target: str) -> Step:
    """Import and instantiate a step from a ``"module.path:ClassName"`` target."""
    module_name, sep, class_name = import_target.partition(":")
    if not sep or not module_name or not class_name:
        raise StepLoadError(
            f"invalid import target {import_target!r}; expected 'module.path:ClassName'"
        )
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise StepLoadError(f"cannot import module {module_name!r}: {exc}") from exc
    try:
        cls = getattr(module, class_name)
    except AttributeError as exc:
        raise StepLoadError(
            f"module {module_name!r} has no attribute {class_name!r}"
        ) from exc
    try:
        instance = cls()
    except Exception as exc:  # noqa: BLE001 - surface any constructor failure clearly
        raise StepLoadError(f"failed to construct {import_target!r}: {exc}") from exc
    if not isinstance(instance, Step):
        raise StepLoadError(f"{import_target!r} is not a runspool Step subclass")
    return instance


def _ensure_plugin_paths(paths: list[Path]) -> None:
    for path in paths:
        s = str(path)
        if s not in sys.path:
            sys.path.insert(0, s)


def build_registry(config: AppConfig) -> StepRegistry:
    """Return a registry with all built-in steps and any configured plugins.

    Plugin step names must match their registry key in ``config.steps`` so that
    workflows referencing the name resolve unambiguously.
    """
    registry = StepRegistry()
    register_builtins(registry)

    if config.steps:
        _ensure_plugin_paths(config.resolved_plugin_paths())
        for key, plugin in config.steps.items():
            step = load_step(plugin.import_target)
            if step.name != key:
                raise StepLoadError(
                    f"plugin key {key!r} does not match step name {step.name!r} "
                    f"(from {plugin.import_target!r})"
                )
            registry.register(step)
    return registry
