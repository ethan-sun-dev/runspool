"""``config-steps``: steps declared in the profile's ``steps:`` map.

Keeps the 0.1 way of adding a custom step working::

    plugin_paths: [steps]
    steps:
      greet:
        import: "my_steps:GreetStep"

A step that cannot be imported, or whose ``name`` differs from its key, fails this
plugin; ``runspool doctor`` and ``runspool run`` report why.
"""

from __future__ import annotations

from runspool.kernel import Plugin
from runspool.registry_builder import StepLoadError, ensure_plugin_paths, load_step


def _apply(ctx, config) -> None:
    settings = ctx.config
    if not settings.steps:
        return
    ensure_plugin_paths(settings.resolved_plugin_paths())
    for key, entry in settings.steps.items():
        step = load_step(entry.import_target)
        if step.name != key:
            raise StepLoadError(
                f"plugin key {key!r} does not match step name {step.name!r} "
                f"(from {entry.import_target!r})"
            )
        ctx.steps.register(step)


plugin = Plugin(name="config-steps", apply=_apply, inject=["config", "steps"])
