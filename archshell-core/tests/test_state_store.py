"""Tests of the state store adapter: lock file and revision compare-and-swap."""

from __future__ import annotations

import errno
import os
import subprocess
import sys
import time

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
    assert lock_is_free(store)


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


def lock_is_free(store, feature="probe"):
    try:
        with store.locked(feature):
            return True
    except LockHeld:
        return False


def test_lock_is_exclusive_and_released(tmp_path):
    store = StateStore(tmp_path)
    with store.locked("probe"):
        assert store.lock_path("probe").exists()
        with pytest.raises(LockHeld):
            with store.locked("probe"):
                pass
    assert lock_is_free(store)


def test_lock_released_on_error(tmp_path):
    store = StateStore(tmp_path)
    with pytest.raises(RuntimeError):
        with store.locked("probe"):
            raise RuntimeError
    assert lock_is_free(store)


def test_lock_file_left_without_a_holder_is_free(tmp_path):
    # A killed engine, or an older engine version, left the file behind.
    store = StateStore(tmp_path)
    path = store.lock_path("probe")
    path.parent.mkdir(parents=True)
    path.write_bytes(b"12345\n")
    with store.locked("probe"):
        pass
    assert path.read_bytes() == f"{os.getpid()}\n".encode("ascii")


def test_lock_of_a_killed_holder_is_recovered(tmp_path):
    store = StateStore(tmp_path)
    holder = subprocess.Popen(
        [
            sys.executable, "-c",
            "import os, sys, time\n"
            "from pathlib import Path\n"
            "from archshell.adapters.state_store import StateStore\n"
            "with StateStore(Path(sys.argv[1])).locked('probe'):\n"
            "    print('held', os.getpid(), flush=True)\n"
            "    time.sleep(60)\n",
            str(tmp_path),
        ],
        stdout=subprocess.PIPE,
    )
    try:
        word, pid = holder.stdout.readline().split()
        assert word == b"held"
        assert not lock_is_free(store)
        # The pid is readable while the lock is held (Windows locks are mandatory).
        assert store.lock_path("probe").read_bytes() == pid + b"\n"
    finally:
        holder.kill()
        holder.wait()
        holder.stdout.close()
    assert store.lock_path("probe").exists()
    # The OS frees a dead process's locks soon after exit, not necessarily at once.
    deadline = time.monotonic() + 5
    while not lock_is_free(store):
        assert time.monotonic() < deadline, "lock of a killed holder was never released"
        time.sleep(0.05)


def test_lock_os_error_is_raised_not_lock_held(tmp_path, monkeypatch):
    from archshell.adapters import state_store

    def broken(*args):
        raise OSError(errno.EIO, "I/O error")

    if os.name == "nt":
        monkeypatch.setattr(state_store.msvcrt, "locking", broken)
    else:
        monkeypatch.setattr(state_store.fcntl, "flock", broken)
    store = StateStore(tmp_path)
    with pytest.raises(OSError) as raised:
        with store.locked("probe"):
            pass
    assert not isinstance(raised.value, LockHeld)
    assert raised.value.errno == errno.EIO


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
