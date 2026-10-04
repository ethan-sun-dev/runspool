"""Boot RunSpool from a profile: compose the plugin entries and mount them.

A profile is the config file (``config.yaml`` / ``runspool.yaml``). Its ``bundles``,
``required``, ``allow`` and ``patch`` keys drive composition; every other key is an
engine setting validated as :class:`runspool.config.AppConfig` and provided to
plugins as the ``config`` service. A profile without ``bundles`` gets the default
``core`` + ``builtin-steps``, so a 0.1-style config file boots unchanged.

Layer order: bundles (profile order) -> the profile's ``steps:`` map (as the
``config-steps`` entry, after every bundle so built-in steps register first) ->
the profile's own patch -> command-line overlays.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from runspool.config import AppConfig
from runspool.core.bundles import (
    BUILTIN_STEPS,
    CORE,
    CORE_SERVICES,
    DEFAULT_BUNDLES,
    LOCKED_CORE_IDS,
)
from runspool.kernel import (
    BUNDLE_GROUP,
    Composition,
    Kernel,
    Layer,
    Loader,
    Profile,
    StartupError,
    StartupReport,
    compose,
)
from runspool.kernel.compose import load_patch_file
from runspool.kernel.loader import bundle_layers, discover

log = logging.getLogger("runspool.boot")

CONFIG_STEPS_LAYER = [
    {"insert": [{"id": "config-steps", "plugin": "runspool.core.config_steps:plugin"}]}
]


class _StaticBundle:
    """A bundle shipped inside runspool itself (no entry-point lookup needed)."""

    dist = None

    def __init__(self, name: str, patches: list[Mapping[str, Any]]) -> None:
        self.name = name
        self._patches = patches

    def load(self) -> list[Mapping[str, Any]]:
        return self._patches


STATIC_BUNDLES = {
    "core": _StaticBundle("core", CORE),
    "builtin-steps": _StaticBundle("builtin-steps", BUILTIN_STEPS),
}


class StartupInfo:
    """The ``startup`` service: the mount report and the profile's warnings."""

    def __init__(self, loader: Loader) -> None:
        self.loader = loader
        self.warnings: list[str] = []

    def report(self) -> StartupReport:
        return self.loader.report()


@dataclass
class Booted:
    kernel: Kernel
    loader: Loader
    config: AppConfig
    profile: Profile
    composition: Composition
    report: StartupReport

    def service(self, name: str) -> Any:
        value = self.kernel.root.get(name)
        if value is None:
            raise LookupError(f"service {name!r} is not available")
        return value


def load_profile(path: Path | str) -> tuple[Profile, AppConfig]:
    path = Path(path)
    profile = Profile.load(path)
    base_dir = path.resolve().parent
    settings = dict(profile.settings)
    root = settings.get("workspace_root")
    if isinstance(root, str) and root and not Path(root).expanduser().is_absolute():
        # Relative to the profile, like plugin_paths: the same profile must mean the
        # same workspace whichever directory a command or the daemon starts in.
        settings["workspace_root"] = str(base_dir / root)
    config = AppConfig.model_validate(settings)
    config.base_dir = base_dir
    return profile, config


def boot(config_path: Path | str, *, overlays: Iterable[Path] = ()) -> Booted:
    profile, config = load_profile(config_path)
    kernel = Kernel()
    loader = Loader(kernel, allow=profile.allow)
    startup = StartupInfo(loader)
    kernel.root.provide("config", config)
    kernel.root.provide("startup", startup)

    known = set(AppConfig.model_fields) - {"base_dir"}
    for key in sorted(set(profile.settings) - known):
        startup.warnings.append(f"unknown setting {key!r} in {profile.path} is ignored")

    discovered = discover(BUNDLE_GROUP)
    for name in sorted(set(discovered) & set(STATIC_BUNDLES)):
        startup.warnings.append(f"installed bundle {name!r} is ignored: the name is RunSpool's own")
    bundles = {**discovered, **STATIC_BUNDLES}
    layers = bundle_layers(profile.bundles or DEFAULT_BUNDLES, bundles)
    layers.append(Layer("boot:config-steps", CONFIG_STEPS_LAYER))
    layers.append(Layer(f"profile:{profile.path}", profile.patch))
    for overlay in overlays:
        layers.append(Layer(f"overlay:{overlay}", load_patch_file(overlay)))
    composition = compose(layers)
    startup.warnings.extend(composition.warnings)
    for warning in startup.warnings:
        log.warning(warning)

    # The state machine, step registry, workflows, runtime and doctor are core: they
    # carry the engine's invariants and cannot be disabled or replaced. (The store is
    # a seam: it may be replaced, but something must provide it.)
    enabled = {entry.id for entry in composition.enabled()}
    locked_off = [entry_id for entry_id in LOCKED_CORE_IDS if entry_id not in enabled]
    if locked_off:
        raise StartupError(
            f"core entries {', '.join(locked_off)} are disabled or missing; they cannot be "
            "turned off (is the 'core' bundle listed in the profile?)",
            StartupReport([]),
        )

    loader.mount(composition.enabled())
    required = list(LOCKED_CORE_IDS)
    required += [entry_id for entry_id in profile.required if entry_id not in required]
    report = loader.audit(required=required)
    absent = [name for name in CORE_SERVICES if kernel.root.get(name) is None]
    if absent:
        raise StartupError(f"no active plugin provides {', '.join(absent)}", report)
    return Booted(kernel, loader, config, profile, composition, report)
