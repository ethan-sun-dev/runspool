"""Child tasks a step spawns (``StepResult.spawn``): fan one task out into several.

A step returns ``StepResult(spawn=[ChildTask(input, workflow, ...)])``. When the
step succeeds, the state machine creates every child in the same transaction that
advances the parent, so a crash leaves either both (the parent advanced and its
children exist) or neither (the step runs again and spawns them then): a re-run
after a crash never creates duplicates.

Children are independent tasks: ``parent_task_id`` points at the parent, and the
parent does not wait for them (it goes on to its next step, or finishes). Each
child starts queued on the first step of its workflow.

``key`` (optional) names a child within its parent: a later run of a step that
spawns the same key again (after ``set-step`` back, say) does not create a second
one. Children without a key are created every time the step succeeds.

Nothing is spawned when the step fails, defers, or is terminated at its boundary,
and a spawn that does not validate fails the step: nothing is created.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from runspool.persistence.repository import dump_metadata


@dataclass(frozen=True)
class ChildTask:
    """A child task to create. ``metadata`` must be a JSON object."""

    input: str
    workflow: str
    name: str | None = None
    metadata: Mapping[str, Any] | None = None
    key: str | None = None


@dataclass(frozen=True)
class PlannedChild:
    """A validated child, with what creating it needs. ``metadata_json`` is its
    metadata serialized exactly as it is stored (checked when planned)."""

    input: str
    workflow: str
    first_step: str
    max_retries: int
    name: str | None
    metadata: dict[str, Any] | None
    key: str | None
    metadata_json: str | None = None


class SpawnPlan:
    """The children a successful step creates; ``created`` / ``existing`` are filled
    with task ids when they are written (``existing``: keys spawned before)."""

    def __init__(self, children: Sequence[PlannedChild], *, step: str, step_run_id: int | None):
        self.children = tuple(children)
        self.step = step
        self.step_run_id = step_run_id
        self.created: list[int] = []
        self.existing: list[int] = []


def plan_children(spawn: Sequence[Any], config: Any) -> tuple[PlannedChild, ...]:
    """Validate ``StepResult.spawn`` against ``config``; raise ValueError naming the
    child by position (never its input) if anything is wrong. Only a list or tuple
    is a spawn: an empty one spawns nothing; anything else (``{}``, ``0``, ``""``,
    ``None``) is an error, even when it is empty or falsy."""
    if not isinstance(spawn, (list, tuple)):
        raise ValueError("spawn must be a list of ChildTask")
    planned: list[PlannedChild] = []
    keys: set[str] = set()
    for n, child in enumerate(spawn, 1):
        where = f"child {n}"
        if not isinstance(child, ChildTask):
            raise ValueError(f"{where}: not a ChildTask")
        if not isinstance(child.input, str) or not child.input:
            raise ValueError(f"{where}: input must be a non-empty string")
        if not isinstance(child.workflow, str):
            raise ValueError(f"{where}: workflow must be a string")
        try:
            workflow = config.workflow(child.workflow)
        except KeyError:
            raise ValueError(f"{where}: workflow {child.workflow!r} is not defined") from None
        if child.name is not None and not isinstance(child.name, str):
            raise ValueError(f"{where}: name must be a string")
        metadata = metadata_json = None
        if child.metadata is not None:
            if not isinstance(child.metadata, Mapping):
                raise ValueError(f"{where}: metadata must be a JSON object")
            metadata = dict(child.metadata)
            try:  # the very serialization the store uses (keys sorted, so all strings)
                metadata_json = dump_metadata(metadata)
            except (TypeError, ValueError):
                raise ValueError(f"{where}: metadata must be a JSON object") from None
        if child.key is not None:
            if not isinstance(child.key, str) or not child.key:
                raise ValueError(f"{where}: key must be a non-empty string")
            if child.key in keys:
                raise ValueError(f"{where}: key {child.key!r} is used twice")
            keys.add(child.key)
        planned.append(
            PlannedChild(
                input=child.input,
                workflow=child.workflow,
                first_step=workflow.first_step(),
                max_retries=config.scheduler.max_retries,
                name=child.name,
                metadata=metadata,
                key=child.key,
                metadata_json=metadata_json,
            )
        )
    return tuple(planned)
