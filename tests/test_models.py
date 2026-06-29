import pytest

from runspool.models import NextStep, WorkflowDef


def test_first_and_next_step():
    wf = WorkflowDef("w", ["a", "b", "c"])
    assert wf.first_step() == "a"
    assert wf.next_step("a") == NextStep(step="b", done=False)
    assert wf.next_step("b") == NextStep(step="c", done=False)
    assert wf.next_step("c") == NextStep(step=None, done=True)


def test_empty_steps_rejected():
    with pytest.raises(ValueError):
        WorkflowDef("w", [])


def test_unknown_step_rejected():
    wf = WorkflowDef("w", ["a"])
    with pytest.raises(ValueError):
        wf.next_step("missing")
