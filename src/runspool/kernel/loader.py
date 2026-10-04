"""Discover plugins and bundles, check compatibility, mount entries, audit startup.

Plugins and bundles are found through package entry points:

* ``runspool.plugins``  name -> plugin object (see :mod:`runspool.kernel.plugin`)
* ``runspool.bundles``  name -> a list of patches, or a callable returning one

An entry's ``plugin`` is either an entry-point name or a ``module:attr`` reference
(handy for plugins that live next to a project's config rather than in a package).

A plugin distributed as a package declares the RunSpool versions it supports in
its own requirements (``Requires-Dist: runspool>=0.2,<0.3``). The loader checks
that range against the running RunSpool before importing the plugin; an
incompatible entry is skipped unless the profile allows that exact
``name@version``. This is a compatibility check, not a security boundary.

After mounting, :meth:`Loader.audit` reports every entry that is not active and
why (missing services, a load error, a skipped entry). A missing provider would
otherwise leave a plugin silently PENDING forever.

Compatibility gating and the startup audit follow DeepSeek Harness's app-boot
design (MIT, (c) 2026 DeepSeek); see THIRD_PARTY_NOTICES.md.
"""

from __future__ import annotations

import importlib
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from typing import Any

import yaml
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from runspool.kernel.compose import Composition, Entry, Layer, compose, load_patch_file
from runspool.kernel.errors import ComposeError, StartupError
from runspool.kernel.fiber import Fiber, FiberState, Kernel

PLUGIN_GROUP = "runspool.plugins"
BUNDLE_GROUP = "runspool.bundles"
RUNTIME_DIST = "runspool"


def discover(group: str) -> dict[str, metadata.EntryPoint]:
    """Entry points of ``group``, by name."""
    return {ep.name: ep for ep in metadata.entry_points(group=group)}


def runtime_version() -> str:
    try:
        return metadata.version(RUNTIME_DIST)
    except metadata.PackageNotFoundError:  # running from a source tree without install
        return "0.0.0"


def check_compat(
    dist_name: str,
    dist_version: str,
    requires: Iterable[str] | None,
    runtime: str,
    allow: Collection[str] = (),
) -> str | None:
    """Return why ``dist_name`` is incompatible with RunSpool ``runtime``, or ``None``.

    Only an explicit requirement on ``runspool`` is checked; a package that does not
    declare one is accepted. ``allow`` holds exact ``name@version`` exemptions.
    """
    try:
        runtime_v = Version(runtime)
    except InvalidVersion:
        return None
    for line in requires or ():
        try:
            req = Requirement(line)
        except InvalidRequirement:
            continue
        if canonicalize_name(req.name) != RUNTIME_DIST:
            continue
        if req.marker is not None and not req.marker.evaluate({"extra": ""}):
            continue
        if req.specifier.contains(runtime_v, prereleases=True):
            return None
        if f"{dist_name}@{dist_version}" in allow:
            return None
        return f"{dist_name} {dist_version} requires runspool{req.specifier}, running {runtime}"
    return None


@dataclass
class EntryStatus:
    id: str
    plugin: str
    state: str  # a FiberState value, or "skipped"
    missing: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass
class StartupReport:
    entries: list[EntryStatus]

    def by_state(self, state: str) -> list[EntryStatus]:
        return [status for status in self.entries if status.state == state]

    def not_active(self) -> list[EntryStatus]:
        return [status for status in self.entries if status.state != FiberState.ACTIVE.value]

    def lines(self) -> list[str]:
        """Human-readable problems, one per entry that is not active."""
        out = []
        for status in self.not_active():
            if status.state == FiberState.PENDING.value:
                out.append(f"{status.id}: pending, waiting for {', '.join(status.missing) or '?'}")
            else:
                out.append(f"{status.id}: {status.state}: {status.error}")
        return out


class Loader:
    """Mounts composed entries into a kernel and reports on the result."""

    def __init__(
        self,
        kernel: Kernel,
        *,
        plugins: Mapping[str, metadata.EntryPoint] | None = None,
        runtime: str | None = None,
        allow: Collection[str] = (),
    ) -> None:
        self.kernel = kernel
        self._plugins = dict(plugins) if plugins is not None else discover(PLUGIN_GROUP)
        self._runtime = runtime or runtime_version()
        self._allow = set(allow)
        self._mounted: dict[str, tuple[Entry, Fiber | None, str | None]] = {}

    def mount(self, entries: Iterable[Entry]) -> StartupReport:
        """Mount every enabled entry under the kernel root, in order."""
        for entry in entries:
            if entry.disabled:
                continue
            if entry.id in self._mounted:
                raise ComposeError(f"entry {entry.id!r} is already mounted")
            try:
                plugin = self._resolve(entry.plugin)
            except _Skip as skip:
                self._mounted[entry.id] = (entry, None, str(skip))
                continue
            fiber = self.kernel.root.plugin(plugin, entry.config, name=entry.id)
            self._mounted[entry.id] = (entry, fiber, None)
        return self.report()

    def report(self) -> StartupReport:
        statuses = []
        for entry, fiber, skipped in self._mounted.values():
            if fiber is None:
                statuses.append(EntryStatus(entry.id, entry.plugin, "skipped", error=skipped))
                continue
            state = fiber.state
            statuses.append(
                EntryStatus(
                    entry.id,
                    entry.plugin,
                    state.value,
                    missing=fiber.missing_services() if state is FiberState.PENDING else [],
                    error=None if fiber.error is None else _describe(fiber.error),
                )
            )
        return StartupReport(statuses)

    def audit(self, required: Collection[str] = ()) -> StartupReport:
        """Raise :class:`StartupError` if any required entry is not active."""
        report = self.report()
        problems = [
            line
            for status, line in zip(report.not_active(), report.lines(), strict=True)
            if status.id in required
        ]
        problems += [
            f"{entry_id}: required but not mounted (disabled or absent)"
            for entry_id in required
            if entry_id not in self._mounted
        ]
        if problems:
            message = "required plugins are not active:\n  " + "\n  ".join(problems)
            raise StartupError(message, report)
        return report

    def _resolve(self, ref: str) -> Any:
        if ":" in ref:
            module_name, _, attr = ref.partition(":")
            try:
                obj: Any = importlib.import_module(module_name)
                for part in attr.split("."):
                    obj = getattr(obj, part)
            except (ImportError, AttributeError) as exc:
                raise _Skip(f"cannot import {ref!r}: {exc}") from exc
            return obj
        ep = self._plugins.get(ref)
        if ep is None:
            raise _Skip(f"no installed plugin named {ref!r} (entry point group {PLUGIN_GROUP})")
        dist = getattr(ep, "dist", None)
        if dist is not None:
            reason = check_compat(
                dist.metadata["Name"], dist.version, dist.requires, self._runtime, self._allow
            )
            if reason:
                raise _Skip(f"incompatible: {reason}")
        try:
            return ep.load()
        except Exception as exc:  # noqa: BLE001 - a broken package must not stop startup
            raise _Skip(f"cannot load plugin {ref!r}: {_describe(exc)}") from exc


def bundle_layers(
    names: Iterable[str], bundles: Mapping[str, metadata.EntryPoint] | None = None
) -> list[Layer]:
    """Load the patch layer of each named bundle, in the given order."""
    found = dict(bundles) if bundles is not None else discover(BUNDLE_GROUP)
    layers = []
    for name in names:
        ep = found.get(name)
        if ep is None:
            raise ComposeError(f"no installed bundle named {name!r} (group {BUNDLE_GROUP})")
        value: Any = ep.load()
        patches = value() if callable(value) else value
        if not isinstance(patches, list):
            raise ComposeError(f"bundle {name!r} must provide a list of patches")
        layers.append(Layer(f"bundle:{name}", patches))
    return layers


_PROFILE_KEYS = frozenset({"bundles", "required", "allow", "patch"})


@dataclass
class Profile:
    """An application's ``runspool.yaml``: which bundles, which entries are required,
    its own patch layer, and every other top-level key as ``settings``."""

    path: Path | None = None
    bundles: list[str] = field(default_factory=list)
    required: list[str] = field(default_factory=list)
    allow: list[str] = field(default_factory=list)
    patch: list[Mapping[str, Any]] = field(default_factory=list)
    settings: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | str) -> Profile:
        path = Path(path)
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, Mapping):
            raise ComposeError(f"{path}: a profile must be a mapping")
        for key in ("bundles", "required", "allow", "patch"):
            if not isinstance(data.get(key, []), list):
                raise ComposeError(f"{path}: {key} must be a list")
        return cls(
            path=path,
            bundles=list(data.get("bundles", [])),
            required=list(data.get("required", [])),
            allow=list(data.get("allow", [])),
            patch=list(data.get("patch", [])),
            settings={k: v for k, v in data.items() if k not in _PROFILE_KEYS},
        )


def compose_profile(
    profile: Profile,
    *,
    bundles: Mapping[str, metadata.EntryPoint] | None = None,
    user_patch: Path | None = None,
    overlays: Iterable[Path] = (),
) -> Composition:
    """Bundle layers (profile order) -> profile patch -> user patch -> overlays."""
    layers = bundle_layers(profile.bundles, bundles)
    layers.append(Layer(f"profile:{profile.path or '<memory>'}", profile.patch))
    if user_patch is not None and user_patch.exists():
        layers.append(Layer(f"user:{user_patch}", load_patch_file(user_patch)))
    for overlay in overlays:
        layers.append(Layer(f"overlay:{overlay}", load_patch_file(overlay)))
    return compose(layers)


class _Skip(Exception):
    pass


def _describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"
