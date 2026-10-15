"""RunSpool's plugin kernel: plugins, services, effects, events, composition.

The kernel knows nothing about tasks, steps or storage; everything RunSpool does
is provided by plugins mounted into it. See docs/design/0.2-plugin-architecture.md.
"""

from runspool.kernel.compose import Composition, Entry, Layer, apply_patches, compose, deep_merge
from runspool.kernel.errors import (
    ComposeError,
    InactiveEffectError,
    KernelError,
    PluginShapeError,
    ServiceConflict,
    ServiceNotInjected,
    StartupError,
)
from runspool.kernel.fiber import Context, Fiber, FiberState, Kernel
from runspool.kernel.loader import (
    BUNDLE_GROUP,
    PLUGIN_GROUP,
    Loader,
    PluginInfo,
    Profile,
    StartupReport,
    check_compat,
    compose_profile,
)
from runspool.kernel.plugin import ConfigError, External, Plugin, PluginMeta, config_schema

__all__ = [
    "BUNDLE_GROUP",
    "PLUGIN_GROUP",
    "ComposeError",
    "ConfigError",
    "Composition",
    "Context",
    "Entry",
    "External",
    "Fiber",
    "FiberState",
    "InactiveEffectError",
    "Kernel",
    "KernelError",
    "Layer",
    "Loader",
    "Plugin",
    "PluginInfo",
    "PluginMeta",
    "PluginShapeError",
    "Profile",
    "ServiceConflict",
    "ServiceNotInjected",
    "StartupError",
    "StartupReport",
    "apply_patches",
    "check_compat",
    "compose",
    "compose_profile",
    "config_schema",
    "deep_merge",
]
