"""Plugin shapes and their normalization.

A plugin is any of:

* a :class:`Plugin` value,
* an object (or module) with an ``apply(ctx, config)`` attribute,
* a class, instantiated as ``cls(ctx, config)``,
* a plain function ``fn(ctx, config)``.

Optional metadata, read from the object: ``name``, ``Config`` (a pydantic model
used to validate and default the config before ``apply``) and ``inject`` (names
of services that must be active before the plugin loads).

Optional descriptive metadata, for catalogs, settings forms and ``doctor``; the
kernel itself never acts on it, and a plugin without it works as before:

* ``category`` — a free-form group name (``"publish"``, ``"storage"`` ...);
* ``description`` — one line saying what the plugin does;
* ``external`` — what it needs outside Python, as :class:`External` values (or
  mappings with the same keys). ``doctor`` checks ``kind="binary"`` entries are on
  ``PATH``; other kinds are listed, not checked;
* ``hosts`` — free-form labels for the environments it is meant to run under
  (an application's own names; RunSpool does not interpret them).

The config model's JSON Schema is available through :func:`config_schema`.

``apply`` may return a disposer (a callable), or an object with a ``dispose()``
method (a class plugin's instance); either is run when the plugin unloads.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from runspool.kernel.errors import PluginShapeError


@dataclass(frozen=True)
class External:
    """Something outside Python a plugin needs at run time.

    ``kind="binary"``: a program that must be on ``PATH`` (``doctor`` checks it).
    Any other kind (``"service"``, ``"account"`` ...) is descriptive only.
    ``hint`` says how to get it.
    """

    kind: str
    name: str
    hint: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "name": self.name, "hint": self.hint}


@dataclass(frozen=True)
class PluginMeta:
    """Descriptive metadata; see the module docstring."""

    category: str | None = None
    description: str | None = None
    external: tuple[External, ...] = ()
    hosts: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "description": self.description,
            "external": [e.to_dict() for e in self.external],
            "hosts": list(self.hosts),
        }


@dataclass(frozen=True)
class Plugin:
    """Explicit plugin declaration; the recommended shape for packaged plugins."""

    name: str
    apply: Callable[..., Any]
    Config: Any = None
    inject: Sequence[str] = ()
    category: str | None = None
    description: str | None = None
    external: Sequence[External | Mapping[str, str]] = ()
    hosts: Sequence[str] = ()


@dataclass(frozen=True)
class PluginSpec:
    """The normalized form the kernel works with."""

    name: str
    apply: Callable[[Any, Any], Any]
    config_model: Any
    inject: tuple[str, ...]
    meta: PluginMeta = PluginMeta()


def normalize(obj: Any) -> PluginSpec:
    """Return the :class:`PluginSpec` for any supported plugin shape."""
    if isinstance(obj, PluginSpec):
        return obj
    if isinstance(obj, Plugin):
        return PluginSpec(obj.name, obj.apply, obj.Config, tuple(obj.inject), _meta(obj))
    if inspect.isclass(obj):
        cls = obj
        return PluginSpec(
            _name(cls, cls.__name__),
            lambda ctx, config: cls(ctx, config),
            getattr(cls, "Config", None),
            _inject(cls),
            _meta(cls),
        )
    apply = getattr(obj, "apply", None)
    if callable(apply) and not inspect.isfunction(obj):
        fallback = getattr(obj, "__name__", None) or type(obj).__name__
        return PluginSpec(
            _name(obj, fallback), apply, getattr(obj, "Config", None), _inject(obj), _meta(obj)
        )
    if callable(obj):
        return PluginSpec(
            _name(obj, getattr(obj, "__name__", "plugin")),
            obj,
            getattr(obj, "Config", None),
            _inject(obj),
            _meta(obj),
        )
    raise PluginShapeError(
        f"{obj!r} is not a plugin: expected a Plugin, a class, a function, "
        "or an object with apply(ctx, config)"
    )


def config_schema(model: Any) -> dict[str, Any] | None:
    """The JSON Schema of a plugin's config model, or ``None`` when it has none (no
    model, or a model that is not pydantic)."""
    if model is None or not hasattr(model, "model_json_schema"):
        return None
    return model.model_json_schema()


class ConfigError(ValueError):
    """A plugin's config is invalid. The message names fields, never their values:
    a config may hold a secret pasted where a name belongs."""


def validate_config(model: Any, raw: Any) -> Any:
    """Validate ``raw`` against ``model`` (pydantic or any callable); pass through if no model."""
    if model is None:
        return raw
    if hasattr(model, "model_validate"):
        try:
            return model.model_validate({} if raw is None else raw)
        except Exception as exc:
            errors = getattr(exc, "errors", None)
            if not callable(errors):
                raise
            details = "; ".join(
                f"{'.'.join(str(p) for p in e.get('loc', ())) or '(config)'}: {e.get('msg')}"
                for e in errors(include_input=False, include_url=False)
            )
            raise ConfigError(f"invalid config: {details}") from None
    return model(raw)


def _name(obj: Any, fallback: str) -> str:
    name = getattr(obj, "name", None)
    return name if isinstance(name, str) and name else fallback


def _inject(obj: Any) -> tuple[str, ...]:
    inject = getattr(obj, "inject", ())
    if isinstance(inject, str):
        raise PluginShapeError(f"{obj!r}: inject must be a sequence of names, not a string")
    return tuple(inject or ())


def _meta(obj: Any) -> PluginMeta:
    category = getattr(obj, "category", None)
    description = getattr(obj, "description", None)
    for key, value in (("category", category), ("description", description)):
        if value is not None and not isinstance(value, str):
            raise PluginShapeError(f"{obj!r}: {key} must be a string")
    hosts = getattr(obj, "hosts", ()) or ()
    if isinstance(hosts, str) or not all(isinstance(h, str) for h in hosts):
        raise PluginShapeError(f"{obj!r}: hosts must be a sequence of names, not a string")
    external = getattr(obj, "external", ()) or ()
    if isinstance(external, (str, Mapping)):
        raise PluginShapeError(f"{obj!r}: external must be a sequence of External values")
    externals = tuple(_external(obj, e) for e in external)
    return PluginMeta(category, description, externals, tuple(hosts))


def _external(obj: Any, item: Any) -> External:
    if isinstance(item, External):
        return item
    if isinstance(item, Mapping):
        try:
            return External(str(item["kind"]), str(item["name"]), str(item.get("hint", "")))
        except KeyError as exc:
            raise PluginShapeError(f"{obj!r}: external entry {item!r} lacks {exc}") from None
    raise PluginShapeError(f"{obj!r}: external entries must be External values or mappings")
