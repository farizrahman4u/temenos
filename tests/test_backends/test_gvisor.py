"""Phase 2 — gVisor backend + Box integration tests.

These drive a real `runsc`, so they skip when gVisor isn't available. Each test stands up
a real box; they're slower than the pure-Python suite.
"""
from __future__ import annotations

import pytest

from temenos import Box, DiskVolume, MemoryVolume, Mount, Policy
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


def test_missing_read_path_gives_clear_error():
    box = Box("t-missing", Policy(read=["/no/such/path/xyzzy"]))
    with pytest.raises(BackendError, match="does not exist on host"):
        box.start()


def test_two_boxes_are_isolated():
    with _box("t-iso-a") as a, _box("t-iso-b") as b:
        a.write_file("/tmp/secret", "from-a")
        # b must not see a's /tmp
        assert b.exec(["cat", "/tmp/secret"]).exit_code != 0


def test_disk_write_persists_to_host(tmp_path):
    # write= is the Disk provider: durable, writes land on the host dir
    hostfile = tmp_path / "f.txt"
    hostfile.write_text("ORIGINAL")
    with _box("t-disk", write=[str(tmp_path)]) as box:
        box.write_file(str(hostfile), "BOX_WROTE")
        assert box.read_file(str(hostfile)) == "BOX_WROTE"
    assert hostfile.read_text() == "BOX_WROTE"                # durable on disk


def test_memory_volume_is_writable_and_offhost():
    # a MemoryVolume is ephemeral RAM at the target; nothing exists on the host for it
    pol = Policy(mounts=[Mount("/scratch", MemoryVolume(), mode="rw")])
    with Box("t-mem", pol) as box:
        r = box.exec(["sh", "-c", "echo hi > /scratch/x && cat /scratch/x"])
        assert r.ok and r.stdout.strip() == "hi"


def test_disk_volume_remapped_target_persists(tmp_path):
    # explicit Disk mount at a remapped path; writes persist to the host dir
    pol = Policy(mounts=[Mount("/data", DiskVolume(str(tmp_path)), mode="rw")])
    with Box("t-diskvol", pol) as box:
        box.write_file("/data/out.txt", "payload")
    assert (tmp_path / "out.txt").read_text() == "payload"


def test_root_writes_stay_ephemeral():
    # root:memory — writing outside any volume is ephemeral and never hits the host
    with _box("t-rootmem") as box:
        assert box.exec(["sh", "-c", "echo x > /var_test_file 2>/dev/null; "
                                     "mkdir -p /scratchdir && echo ok > /scratchdir/f "
                                     "&& cat /scratchdir/f"]).stdout.strip() == "ok"


def test_read_only_path_is_visible():
    # a read mount is readable inside the box
    with _box("t-ro", read=["/etc"]) as box:
        assert box.exec(["test", "-f", "/etc/hostname"]).ok or \
               box.exec(["test", "-d", "/etc"]).ok


def test_exit_code_propagates():
    with _box("t-exit") as box:
        assert box.exec(["sh", "-c", "exit 7"]).exit_code == 7


def test_default_box_is_checkpointable(tmp_path):
    # scratch defaults to 'disk' → fscheckpoint captures the filesystem
    import os
    with _box("t-ckpt") as box:
        box.write_file("/tmp/marker", "x")
        dest = str(tmp_path / "ckpt")
        box.checkpoint(dest)
        assert os.path.getsize(os.path.join(dest, "multitar.img")) > 0
        assert box.running and box.exec(["true"]).ok    # leave_running default — box survives


def test_memory_scratch_box_cannot_checkpoint(tmp_path):
    from temenos.exceptions import BackendError
    with Box("t-mem-ckpt", Policy(scratch="memory")) as box:
        with pytest.raises(BackendError, match="memory"):
            box.checkpoint(str(tmp_path / "ck"))


def test_checkpoint_then_restore_roundtrip(tmp_path):
    dest = str(tmp_path / "ckpt")
    with _box("t-ck-save") as box:
        box.exec(["mkdir", "-p", "/opt/saved"])
        box.write_file("/opt/saved/marker", "RESTORED_OK")
        box.checkpoint(dest)
    # a fresh box (same policy) restored from the checkpoint has the saved filesystem
    with Box("t-ck-restore", Policy(), restore_from=dest) as box2:
        assert box2.read_file("/opt/saved/marker") == "RESTORED_OK"


def test_restore_from_missing_dir_errors(tmp_path):
    from temenos.exceptions import BackendError
    box = Box("t-ck-bad", Policy(), restore_from=str(tmp_path / "nope"))
    with pytest.raises(BackendError, match="checkpoint dir not found"):
        box.start()


def _ifaces(box):
    r = box.exec(["sh", "-c", "awk -F: 'NR>2{print $1}' /proc/net/dev | tr -d ' '"])
    return {ln for ln in r.stdout.split() if ln}


def test_network_off_is_isolated():
    with _box("t-net-off", network=False) as box:  # explicit isolation (default is on)
        assert _ifaces(box) == {"lo"}              # only loopback, no egress path


def test_network_host_passthrough_exposes_host_ifaces():
    with Box("t-net-host", Policy(network=True)) as box:
        assert _ifaces(box) - {"lo"}               # host interfaces (eth0, …) present
