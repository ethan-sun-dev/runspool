"""Plugin shapes and their normalization.

A plugin is any of:

* a :class:`Plugin` value,
* an object (or module) with an ``apply(ctx, config)`` attribute,
* a class, instantiated as ``cls(ctx, config)``,
* a plain function ``fn(ctx, config)``.

Optional metadata, read from the object: ``name``, ``Config`` (a pydantic model
used to validate and default the config before ``apply``) and ``inject`` (names
of services that must be active before the plugin loads).

``apply`` may return a disposer (a callable), or an object with a ``dispose()``
method (a class plugin's instance); either is run when the plugin unloads.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from runspool.kernel.errors import PluginShapeError


@dataclass(frozen=True)
class Plugin:
    """Explicit plugin declaration; the recommended shape for packaged plugins."""

    name: str
    apply: Callable[..., Any]
    Config: Any = None
    inject: Sequence[str] = ()


@dataclass(frozen=True)
class PluginSpec:
    """The normalized form the kernel works with."""

    name: str
    apply: Callable[[Any, Any], Any]
    config_model: Any
    inject: tuple[str, ...]


def normalize(obj: Any) -> PluginSpec:
    """Return the :class:`PluginSpec` for any supported plugin shape."""
    if isinstance(obj, PluginSpec):
        return obj
    if isinstance(obj, Plugin):
        return PluginSpec(obj.name, obj.apply, obj.Config, tuple(obj.inject))
    if inspect.isclass(obj):
        cls = obj
        return PluginSpec(
            _name(cls, cls.__name__),
            lambda ctx, config: cls(ctx, config),
            getattr(cls, "Config", None),
            _inject(cls),
        )
    apply = getattr(obj, "apply", None)
    if callable(apply) and not inspect.isfunction(obj):
        fallback = getattr(obj, "__name__", None) or type(obj).__name__
        return PluginSpec(_name(obj, fallback), apply, getattr(obj, "Config", None), _inject(obj))
    if callable(obj):
        return PluginSpec(
            _name(obj, getattr(obj, "__name__", "plugin")),
            obj,
            getattr(obj, "Config", None),
            _inject(obj),
        )
    raise PluginShapeError(
        f"{obj!r} is not a plugin: expected a Plugin, a class, a function, "
        "or an object with apply(ctx, config)"
    )


def validate_config(model: Any, raw: Any) -> Any:
    """Validate ``raw`` against ``model`` (pydantic or any callable); pass through if no model."""
    if model is None:
        return raw
    if hasattr(model, "model_validate"):
        return model.model_validate({} if raw is None else raw)
    return model(raw)


def _name(obj: Any, fallback: str) -> str:
    name = getattr(obj, "name", None)
    return name if isinstance(name, str) and name else fallback


def _inject(obj: Any) -> tuple[str, ...]:
    inject = getattr(obj, "inject", ())
    if isinstance(inject, str):
        raise PluginShapeError(f"{obj!r}: inject must be a sequence of names, not a string")
    return tuple(inject or ())
