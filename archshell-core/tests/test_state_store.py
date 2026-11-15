"""Tests of the state store adapter: lock file and revision compare-and-swap."""

from __future__ import annotations

import pytest

from archshell.adapters.catalogs import render
from archshell.adapters.output_schemas import validate
from archshell.adapters.state_store import (
    CorruptState,
    LockHeld,
    RevisionConflict,
    StateStore,
    serialize,
)
from archshell.domain.workflow import FreshIds, Outcome, on_next, on_submit, Submission


def first_state():
    outcome = on_next(None, "probe", FreshIds("id-1", "n-1"), render=render)
    assert isinstance(outcome, Outcome)
    return outcome.state


def second_state(state):
    sub = Submission(
        "probe", state.action.action_id, state.revision, None, None, {"text": "ok"}
    )
    outcome = on_submit(state, sub, FreshIds("id-2", "n-2"), validate=validate, render=render)
    assert isinstance(outcome, Outcome)
    return outcome.state


def test_write_and_read_round_trip(tmp_path):
    store = StateStore(tmp_path)
    assert store.read("probe") is None
    state = first_state()
    with store.locked("probe"):
        store.write(state, expected_revision=0)
    assert store.read("probe") == state
    assert store.state_path("probe").read_bytes() == serialize(state)
    assert not store.lock_path("probe").exists()


def test_compare_and_swap_rejects_wrong_expected_revision(tmp_path):
    store = StateStore(tmp_path)
    state = first_state()
    with store.locked("probe"):
        store.write(state, expected_revision=0)
        before = store.state_path("probe").read_bytes()
        with pytest.raises(RevisionConflict):
            store.write(second_state(state), expected_revision=0)
        with pytest.raises(RevisionConflict):
            store.write(state, expected_revision=1)  # revision must increase
        assert store.state_path("probe").read_bytes() == before
        store.write(second_state(state), expected_revision=1)
    assert store.read("probe").revision == 2


def test_lock_is_exclusive_and_released(tmp_path):
    store = StateStore(tmp_path)
    with store.locked("probe"):
        assert store.lock_path("probe").exists()
        with pytest.raises(LockHeld):
            with store.locked("probe"):
                pass
        assert store.lock_path("probe").exists()
    assert not store.lock_path("probe").exists()


def test_lock_released_on_error(tmp_path):
    store = StateStore(tmp_path)
    with pytest.raises(RuntimeError):
        with store.locked("probe"):
            raise RuntimeError
    assert not store.lock_path("probe").exists()


@pytest.mark.parametrize("raw", [b"{", b"\xff", b"[]", b'{"schema":"archshell/state/v1"}'])
def test_corrupt_state(tmp_path, raw):
    store = StateStore(tmp_path)
    store.state_path("probe").parent.mkdir(parents=True)
    store.state_path("probe").write_bytes(raw)
    with pytest.raises(CorruptState):
        store.read("probe")


def test_state_for_another_feature_is_corrupt(tmp_path):
    store = StateStore(tmp_path)
    path = store.state_path("probe-report")
    path.parent.mkdir(parents=True)
    path.write_bytes(serialize(first_state()))
    with pytest.raises(CorruptState):
        store.read("probe-report")


def test_lock_retries_a_delete_pending_lock_file(tmp_path, delete_pending):
    # Windows: the previous holder's unlinked lock file is still "delete pending".
    store = StateStore(tmp_path)
    attempts = delete_pending("probe.lock", 3)
    with store.locked("probe"):
        assert store.lock_path("probe").exists()
    assert len(attempts) == 4
    assert not store.lock_path("probe").exists()


def test_lock_persistent_permission_error_is_not_lock_held(tmp_path, delete_pending):
    store = StateStore(tmp_path)
    attempts = delete_pending("probe.lock", 10**6)
    with pytest.raises(PermissionError):
        with store.locked("probe"):
            pass
    assert len(attempts) == 26  # 25 bounded retries, then the error


def test_lock_permission_error_is_not_retried_off_windows(tmp_path, delete_pending, monkeypatch):
    from archshell.adapters import files

    monkeypatch.setattr(files, "_WINDOWS", False)
    store = StateStore(tmp_path)
    attempts = delete_pending("probe.lock", 1)
    with pytest.raises(PermissionError):
        with store.locked("probe"):
            pass
    assert len(attempts) == 1
