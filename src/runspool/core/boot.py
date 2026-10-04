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
from runspool.core.bundles import BUILTIN_STEPS, CORE, CORE_IDS, DEFAULT_BUNDLES
from runspool.kernel import (
    BUNDLE_GROUP,
    Composition,
    Kernel,
    Layer,
    Loader,
    Profile,
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
    config = AppConfig.model_validate(profile.settings)
    config.base_dir = path.resolve().parent
    return profile, config


def boot(config_path: Path | str, *, overlays: Iterable[Path] = ()) -> Booted:
    profile, config = load_profile(config_path)
    kernel = Kernel()
    loader = Loader(kernel, allow=profile.allow)
    kernel.root.provide("config", config)
    kernel.root.provide("startup", loader)

    bundles = {**discover(BUNDLE_GROUP), **STATIC_BUNDLES}
    layers = bundle_layers(profile.bundles or DEFAULT_BUNDLES, bundles)
    layers.append(Layer("boot:config-steps", CONFIG_STEPS_LAYER))
    layers.append(Layer(f"profile:{profile.path}", profile.patch))
    for overlay in overlays:
        layers.append(Layer(f"overlay:{overlay}", load_patch_file(overlay)))
    composition = compose(layers)
    for warning in composition.warnings:
        log.warning(warning)

    loader.mount(composition.enabled())
    enabled = {entry.id for entry in composition.enabled()}
    required = [entry_id for entry_id in CORE_IDS if entry_id in enabled]
    required += [entry_id for entry_id in profile.required if entry_id not in required]
    report = loader.audit(required=required)
    return Booted(kernel, loader, config, profile, composition, report)
