"""Phase 3 — BoxManager registry + path-hash ids."""
from __future__ import annotations

import os
import time

import pytest

from temenos import Box, Policy
from temenos.backends.gvisor import GVisorBackend
from temenos.exceptions import BoxNotFound
from temenos.manager import BoxManager, box_id

gvisor = pytest.mark.skipif(not GVisorBackend.is_available(), reason="no gVisor")


# -- pure: id scheme (no runsc) -------------------------------------------------------

def test_box_id_is_stable_and_path_based(tmp_path):
    d = str(tmp_path / "boxA")
    assert box_id(d) == box_id(d)                      # stable
    assert len(box_id(d)) == 16

def test_same_name_different_repos_get_distinct_ids(tmp_path):
    a = tmp_path / "repoA" / ".temenos" / "default"
    b = tmp_path / "repoB" / ".temenos" / "default"
    a.mkdir(parents=True); b.mkdir(parents=True)
    assert box_id(str(a)) != box_id(str(b))            # the disambiguation (D15)


# -- registry lifecycle (needs runsc) -------------------------------------------------

def test_should_autocheckpoint_heuristic():
    box = Box("x", Policy())                       # backend constructed, not started — pure
    assert not box.should_autocheckpoint(idle_debounce=1, max_staleness=10)  # not dirty
    now = time.monotonic()
    box._dirty, box._dirty_since, box._last_activity = True, now - 20, now
    assert box.should_autocheckpoint(idle_debounce=1, max_staleness=10)      # staleness cap
    box._dirty_since, box._last_activity = now, now - 5
    assert box.should_autocheckpoint(idle_debounce=1, max_staleness=10)      # idle-debounce
    box._last_activity = now
    assert not box.should_autocheckpoint(idle_debounce=1, max_staleness=10)  # busy, not stale


@gvisor
def test_manager_create_exec_list_delete(tmp_path):
    mgr = BoxManager()
    try:
        d = str(tmp_path / "boxA")
        bid = mgr.create(d, Policy())
        assert bid == box_id(d)
        assert mgr.get(bid).exec(["echo", "hi"]).stdout.strip() == "hi"
        assert os.path.exists(os.path.join(d, "config.json"))   # everything in data dir
        assert any(b["id"] == bid and b["running"] for b in mgr.list())
        assert mgr.create(d, Policy()) == bid                   # idempotent (ensure)
        mgr.delete(bid)
        with pytest.raises(BoxNotFound):
            mgr.get(bid)
    finally:
        mgr.shutdown()


@gvisor
def test_box_resumes_from_its_dir_after_shutdown(tmp_path):
    d = str(tmp_path / "box")
    m1 = BoxManager()
    bid = m1.create(d, Policy())                  # checkpoint='auto'
    m1.get(bid).exec(["mkdir", "-p", "/opt/s"])
    m1.get(bid).write_file("/opt/s/marker", "RESUMED")
    m1.shutdown()                                  # commit-on-close → <d>/checkpoint
    assert os.path.isdir(os.path.join(d, "checkpoint"))
    m2 = BoxManager()                              # fresh manager = daemon restart
    bid2 = m2.create(d, Policy())                  # the box dir IS the registry → restores
    try:
        assert m2.get(bid2).read_file("/opt/s/marker") == "RESUMED"
    finally:
        m2.shutdown()


@gvisor
def test_ephemeral_fs_box_does_not_persist(tmp_path):
    d = str(tmp_path / "box")
    m1 = BoxManager()
    bid = m1.create(d, Policy(checkpoint="off"))   # --ephemeral-fs
    m1.get(bid).exec(["mkdir", "-p", "/opt/s"])
    m1.get(bid).write_file("/opt/s/marker", "gone")
    m1.shutdown()
    assert not os.path.exists(os.path.join(d, "checkpoint"))   # never committed
    m2 = BoxManager()
    bid2 = m2.create(d, Policy(checkpoint="off"))
    try:
        assert m2.get(bid2).exec(["cat", "/opt/s/marker"]).exit_code != 0   # not restored
    finally:
        m2.shutdown()


@gvisor
def test_checkpoint_loop_persists_dirty_box(tmp_path):
    d = str(tmp_path / "box")
    m = BoxManager()
    bid = m.create(d, Policy())
    m.start_checkpoint_loop(idle_debounce=0.2, max_staleness=5, tick=0.2, max_per_tick=4)
    try:
        m.get(bid).exec(["mkdir", "-p", "/opt/s"])
        m.get(bid).write_file("/opt/s/m", "y")
        assert m.get(bid).dirty
        time.sleep(1.2)                             # > idle_debounce + a couple ticks
        assert os.path.isdir(os.path.join(d, "checkpoint"))
        assert not m.get(bid).dirty                 # loop checkpointed it → clean
    finally:
        m.shutdown()
