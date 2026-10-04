"""``config-steps``: steps declared in the profile's ``steps:`` map.

Keeps the 0.1 way of adding a custom step working::

    plugin_paths: [steps]
    steps:
      greet:
        import: "my_steps:GreetStep"

Each step is registered lazily under its key: the name is reserved at boot (so a
clash with a built-in step fails this plugin right away), but the module is only
imported when the step is first needed. Read-only commands such as ``status``
therefore never import step code. ``run``, ``daemon`` and ``doctor`` import them
all up front and report any that fail.
"""

from __future__ import annotations

from runspool.kernel import Plugin
from runspool.registry_builder import ensure_plugin_paths, load_step


def _apply(ctx, config) -> None:
    settings = ctx.config
    if not settings.steps:
        return
    ensure_plugin_paths(settings.resolved_plugin_paths())
    for key, entry in settings.steps.items():
        ctx.steps.register_lazy(key, lambda target=entry.import_target: load_step(target))


plugin = Plugin(name="config-steps", apply=_apply, inject=["config", "steps"])
