"""The _resume_skip channel: a frozen, last-write-wins skip snapshot."""
from sqrlly.runtime.state import REDUCERS, WorkflowState, make_initial_state


def test_resume_skip_absent_on_fresh_run():
    # Fresh runs must not carry a skip set — absent => guards skip nothing.
    assert "_resume_skip" not in make_initial_state()


def test_resume_skip_is_declared_but_has_no_reducer():
    # Declared so LangGraph keeps the seeded key; no reducer => last-write-wins
    # (it must never accumulate via set-union like completed_nodes).
    assert "_resume_skip" in WorkflowState.__annotations__
    assert "_resume_skip" not in REDUCERS


def test_make_initial_state_accepts_override():
    # Tests/CLI may seed it explicitly; overrides flow through.
    st = make_initial_state(_resume_skip={"a", "b"})
    assert st["_resume_skip"] == {"a", "b"}
