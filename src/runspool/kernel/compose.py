"""Compose the plugin entry list from ordered patch layers.

An *entry* is one plugin to mount: ``{id, plugin, config, disabled}``. ``id`` is its
stable identity; ``plugin`` names the plugin (an entry-point name or ``module:attr``).

A *layer* is a list of patches, applied in order over the entries built so far:

* ``{insert: [entry, ...]}``  appends new entries (a duplicate id is an error);
* ``{id: x, ...fields}``      modifies entry ``x``:
    - ``config``   deep-merged: mappings merge key by key, anything else (lists
                   included) replaces the old value;
    - ``disabled`` replaces the flag (there is no "remove"; disable instead);
    - ``plugin``   is an identity assertion: if it differs from the entry's plugin
                   the patch is skipped with a warning.

  A patch for an unknown id is skipped with a warning, so a layer written for one
  distribution does not break another.

Layers are applied in the order given: bundle layers (in profile order), then the
profile's own patch, then the user's, then command-line overlays. Later wins.

Patch-layer algorithm adapted from Cordis's include loader (MIT, (c) 2021-present
Shigma) and DeepSeek Harness's modifications to it (MIT, (c) 2026 DeepSeek); see
THIRD_PARTY_NOTICES.md. The deep-merge rule for ``config`` is RunSpool's own.
"""

from __future__ import annotations

import copy
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from runspool.kernel.errors import ComposeError

_ENTRY_KEYS = frozenset({"id", "plugin", "config", "disabled"})
_MODIFY_KEYS = frozenset({"id", "plugin", "config", "disabled"})


@dataclass
class Entry:
    id: str
    plugin: str
    config: Any = None
    disabled: bool = False


@dataclass
class Layer:
    source: str
    patches: list[Mapping[str, Any]]


@dataclass
class Composition:
    entries: list[Entry]
    warnings: list[str] = field(default_factory=list)

    def enabled(self) -> list[Entry]:
        return [entry for entry in self.entries if not entry.disabled]


def compose(layers: Iterable[Layer]) -> Composition:
    """Apply every layer in order, starting from no entries."""
    entries: list[Entry] = []
    warnings: list[str] = []
    for layer in layers:
        entries = apply_patches(entries, layer.patches, source=layer.source, warnings=warnings)
    return Composition(entries, warnings)


def apply_patches(
    entries: list[Entry],
    patches: Iterable[Mapping[str, Any]],
    *,
    source: str = "<patch>",
    warnings: list[str] | None = None,
) -> list[Entry]:
    """Return a new entry list with ``patches`` applied; the input is not modified."""
    warnings = warnings if warnings is not None else []
    result = [copy.deepcopy(entry) for entry in entries]
    index = {entry.id: entry for entry in result}
    for number, patch in enumerate(patches, start=1):
        where = f"{source} patch #{number}"
        if not isinstance(patch, Mapping):
            raise ComposeError(f"{where}: expected a mapping, got {type(patch).__name__}")
        if "insert" in patch:
            if set(patch) != {"insert"}:
                raise ComposeError(f"{where}: an insert patch cannot carry other keys")
            for raw in _as_list(patch["insert"], where):
                entry = _entry(raw, where)
                if entry.id in index:
                    raise ComposeError(f"{where}: entry {entry.id!r} is already defined")
                result.append(entry)
                index[entry.id] = entry
            continue
        unknown = set(patch) - _MODIFY_KEYS
        if unknown:
            raise ComposeError(f"{where}: unknown keys {sorted(unknown)}")
        entry_id = patch.get("id")
        if not isinstance(entry_id, str) or not entry_id:
            raise ComposeError(f"{where}: a modify patch needs an id")
        target = index.get(entry_id)
        if target is None:
            warnings.append(f"{where}: no entry {entry_id!r}; patch skipped")
            continue
        if "plugin" in patch and patch["plugin"] != target.plugin:
            warnings.append(
                f"{where}: entry {entry_id!r} is plugin {target.plugin!r}, "
                f"not {patch['plugin']!r}; patch skipped"
            )
            continue
        if "config" in patch:
            target.config = deep_merge(target.config, patch["config"])
        if "disabled" in patch:
            target.disabled = _flag(patch["disabled"], where)
    return result


def deep_merge(base: Any, override: Any) -> Any:
    """Merge mappings key by key; any non-mapping ``override`` replaces ``base``."""
    if isinstance(base, Mapping) and isinstance(override, Mapping):
        merged = {key: copy.deepcopy(value) for key, value in base.items()}
        for key, value in override.items():
            merged[key] = deep_merge(merged.get(key), value)
        return merged
    return copy.deepcopy(override)


def load_patch_file(path: Path | str) -> list[Mapping[str, Any]]:
    """Read a YAML list of patches; an empty file is an empty layer."""
    path = Path(path)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if data is None:
        return []
    return _as_list(data, str(path))


def _entry(raw: Any, where: str) -> Entry:
    if not isinstance(raw, Mapping):
        raise ComposeError(f"{where}: an inserted entry must be a mapping")
    unknown = set(raw) - _ENTRY_KEYS
    if unknown:
        raise ComposeError(f"{where}: unknown entry keys {sorted(unknown)}")
    entry_id, plugin = raw.get("id"), raw.get("plugin")
    if not isinstance(entry_id, str) or not entry_id:
        raise ComposeError(f"{where}: an inserted entry needs an id")
    if not isinstance(plugin, str) or not plugin:
        raise ComposeError(f"{where}: entry {entry_id!r} needs a plugin")
    return Entry(
        id=entry_id,
        plugin=plugin,
        config=copy.deepcopy(raw.get("config")),
        disabled=_flag(raw.get("disabled", False), where),
    )


def _as_list(value: Any, where: str) -> list[Any]:
    if not isinstance(value, list):
        raise ComposeError(f"{where}: expected a list")
    return value


def _flag(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        raise ComposeError(f"{where}: disabled must be true or false")
    return value
