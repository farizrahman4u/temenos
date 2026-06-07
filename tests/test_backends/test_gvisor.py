"""Phase 2 — gVisor backend + Box integration tests.

These drive a real `runsc`, so they skip when gVisor isn't available. Each test stands up
a real box; they're slower than the pure-Python suite.
"""
from __future__ import annotations

import pytest

from temenos import Box, Policy, TrustLevel
from temenos.backends.gvisor import GVisorBackend
from temenos.exceptions import BackendError

pytestmark = pytest.mark.skipif(
    not GVisorBackend.is_available(),
    reason="gVisor (runsc) with a usable platform not available",
)


def _box(name, **policy_kw):
    return Box(name, Policy(**policy_kw))


def test_platform_detected():
    assert GVisorBackend.detect_platform() in ("kvm", "systrap", "ptrace")


def test_exec_basic():
    with _box("t-echo") as box:
        r = box.exec(["echo", "hi"])
        assert r.ok and r.stdout.strip() == "hi"


def test_write_then_run_persists_across_calls():
    # the headline session guarantee: write in one call, run in the next (scratch = /tmp)
    with _box("t-session") as box:
        box.write_file("/tmp/a.py", "print(6*7)\n")
        r = box.exec(["python3", "/tmp/a.py"])
        assert r.ok and r.stdout.strip() == "42"


def test_read_write_round_trip():
    with _box("t-rw") as box:
        box.write_file("/tmp/note.txt", "hello box")
        assert box.read_file("/tmp/note.txt") == "hello box"


def test_writes_manifest_lists_written_file(tmp_path):
    # writes() reports files under policy.write paths (an existing host dir, CoW)
    with _box("t-writes", write=[str(tmp_path)]) as box:
        target = str(tmp_path / "x.txt")
        box.write_file(target, "x")
        assert target in box.writes()


def test_missing_policy_path_gives_clear_error():
    box = Box("t-missing", Policy(write=["/work"]))   # /work doesn't exist on host
    with pytest.raises(BackendError, match="does not exist on host"):
        box.start()


def test_two_boxes_are_isolated():
    with _box("t-iso-a") as a, _box("t-iso-b") as b:
        a.write_file("/tmp/secret", "from-a")
        # b must not see a's /tmp
        assert b.exec(["cat", "/tmp/secret"]).exit_code != 0


def test_writes_do_not_touch_host(tmp_path):
    # the ephemeral-by-default guarantee: a writable host bind stays unchanged
    hostfile = tmp_path / "f.txt"
    hostfile.write_text("ORIGINAL")
    with _box("t-ephemeral", write=[str(tmp_path)]) as box:
        box.write_file(str(hostfile), "BOX_WROTE")
        assert box.read_file(str(hostfile)) == "BOX_WROTE"   # box sees its write
    assert hostfile.read_text() == "ORIGINAL"                 # host is untouched


def test_read_only_path_is_visible():
    # a read mount is readable inside the box
    with _box("t-ro", read=["/etc"]) as box:
        assert box.exec(["test", "-f", "/etc/hostname"]).ok or \
               box.exec(["test", "-d", "/etc"]).ok


def test_exit_code_propagates():
    with _box("t-exit") as box:
        assert box.exec(["sh", "-c", "exit 7"]).exit_code == 7


def test_network_policy_rejected_in_v1():
    box = Box("t-net", Policy(network=["example.com"], trust=TrustLevel.RESTRICTED))
    with pytest.raises(BackendError, match="network"):
        box.start()
