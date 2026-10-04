"""Example RunSpool plugin: a creator/content pipeline that builds a multi-platform
DRAFT package and a publish checklist from raw materials. It never publishes.

Mount it in a profile::

    patch:
      - insert: [{id: creator, plugin: example-creator}]

and run the ``creator_publishing`` workflow it contributes.
"""

from __future__ import annotations

from runspool.kernel import Plugin
from runspool_example_creator.steps import (
    CollectMaterialsStep,
    CreatePublishChecklistStep,
    DraftArticleStep,
    ExtractHighlightsStep,
    RenderPlatformPackageStep,
)

STEPS = (
    CollectMaterialsStep,
    ExtractHighlightsStep,
    DraftArticleStep,
    RenderPlatformPackageStep,
    CreatePublishChecklistStep,
)
WORKFLOW = [cls.name for cls in STEPS] + ["archive"]


def apply(ctx, config) -> None:
    for cls in STEPS:
        ctx.steps.register(cls())
    ctx.workflows.add_default("creator_publishing", WORKFLOW)


plugin = Plugin(name="example-creator", apply=apply, inject=["steps", "workflows"])
