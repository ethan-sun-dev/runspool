"""Entry composition from ordered patch layers."""

from __future__ import annotations

import pytest

from runspool.kernel import ComposeError, Entry, Layer, apply_patches, compose, deep_merge
from runspool.kernel.compose import load_patch_file


def test_insert_then_modify_across_layers():
    result = compose(
        [
            Layer(
                "bundle:core",
                [{"insert": [{"id": "store", "plugin": "store-sqlite", "config": {"wal": True}}]}],
            ),
            Layer("profile", [{"id": "store", "config": {"path": "x.db"}}]),
        ]
    )
    assert result.entries == [
        Entry(id="store", plugin="store-sqlite", config={"wal": True, "path": "x.db"})
    ]
    assert result.warnings == []


def test_config_mappings_merge_deeply_and_lists_replace():
    base = {"a": {"b": 1, "c": [1, 2]}, "keep": True}
    override = {"a": {"c": [3]}, "new": 1}
    assert deep_merge(base, override) == {"a": {"b": 1, "c": [3]}, "keep": True, "new": 1}
    assert base == {"a": {"b": 1, "c": [1, 2]}, "keep": True}  # inputs untouched


def test_later_layer_wins_and_can_disable():
    result = compose(
        [
            Layer("b", [{"insert": [{"id": "archive", "plugin": "builtin-archive"}]}]),
            Layer("p", [{"id": "archive", "disabled": True}]),
        ]
    )
    assert result.entries[0].disabled is True
    assert result.enabled() == []


def test_unknown_id_is_a_warning_not_an_error():
    warnings: list[str] = []
    out = apply_patches([], [{"id": "ghost", "disabled": True}], source="p", warnings=warnings)
    assert out == []
    assert "ghost" in warnings[0]


def test_plugin_field_is_an_identity_assertion():
    entries = [Entry(id="store", plugin="store-sqlite")]
    warnings: list[str] = []
    out = apply_patches(
        entries, [{"id": "store", "plugin": "other", "disabled": True}], warnings=warnings
    )
    assert out[0].disabled is False
    assert "store-sqlite" in warnings[0]


def test_duplicate_insert_is_an_error():
    with pytest.raises(ComposeError, match="already defined"):
        compose(
            [
                Layer("a", [{"insert": [{"id": "x", "plugin": "p"}]}]),
                Layer("b", [{"insert": [{"id": "x", "plugin": "q"}]}]),
            ]
        )


@pytest.mark.parametrize(
    "patch, message",
    [
        ({"id": "x", "confg": {}}, "unknown keys"),
        ({"disabled": True}, "needs an id"),
        ({"insert": [{"id": "y"}]}, "needs a plugin"),
        ({"insert": [{"plugin": "p"}]}, "needs an id"),
        ({"insert": [], "id": "x"}, "cannot carry other keys"),
        ({"id": "x", "disabled": "yes"}, "true or false"),
    ],
)
def test_malformed_patches_are_rejected(patch, message):
    entries = [Entry(id="x", plugin="p")]
    with pytest.raises(ComposeError, match=message):
        apply_patches(entries, [patch])


def test_apply_patches_does_not_mutate_input():
    entries = [Entry(id="x", plugin="p", config={"a": 1})]
    apply_patches(entries, [{"id": "x", "config": {"a": 2}}])
    assert entries[0].config == {"a": 1}


def test_load_patch_file(tmp_path):
    path = tmp_path / "patch.yaml"
    path.write_text("- id: x\n  disabled: true\n", encoding="utf-8")
    assert load_patch_file(path) == [{"id": "x", "disabled": True}]
    empty = tmp_path / "empty.yaml"
    empty.write_text("", encoding="utf-8")
    assert load_patch_file(empty) == []
