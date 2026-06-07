"""Leak-test — the spillover conformance gate (plan §10).

Each test asserts one containment property of a default-locked box. These are the rows a
harness config must pass to be "supported": no host write, host secrets invisible, no
network, no /proc escape, and (D6) enforced memory. They run against `Box` directly — the
real enforcement boundary that the MCP tools and `temenos exec` both funnel through.

gVisor-gated. The memory row is additionally gated on systemd-run (the D6 enforcer);
without it, limits are unenforced and the row is skipped with a clear reason (matching the
plan's "degrades to a warning" behavior).
"""
from __future__ import annotations

import shutil

import pytest

from temenos import Box, Policy
from temenos.backends.gvisor import GVisorBackend

pytestmark = pytest.mark.skipif(not GVisorBackend.is_available(), reason="no gVisor")


def test_cannot_write_host_system_dirs():
    with Box("leak-fs", Policy()) as box:
        assert not box.exec(["sh", "-c", "echo pwned >> /etc/passwd"]).ok   # /etc is ro
        assert not box.exec(["sh", "-c", "echo pwned > /usr/bin/x"]).ok      # /usr is ro


def test_host_files_outside_policy_are_invisible(tmp_path):
    secret = tmp_path / "id_rsa"
    secret.write_text("TOP-SECRET-KEY")
    with Box("leak-secret", Policy()) as box:                  # secret is NOT mounted
        assert not box.exec(["cat", str(secret)]).ok
        # and not reachable by pivoting through the init process's root
        assert "TOP-SECRET" not in box.exec(["cat", f"/proc/1/root{secret}"]).stdout


def test_no_network_by_default():
    with Box("leak-net", Policy(network=False)) as box:        # default: isolated netns
        r = box.exec(["python3", "-c",
                      "import socket; socket.setdefaulttimeout(3);"
                      "socket.create_connection(('1.1.1.1', 53))"])
        assert not r.ok                                         # no route off the box


def test_proc1_root_is_the_box_not_the_host():
    with Box("leak-proc", Policy()) as box:
        # /proc/1 is the box init; its root is the sandbox rootfs (writable scratch),
        # so writing there succeeds and stays in the box — it is NOT the host's /.
        assert box.exec(["sh", "-c", "echo ok > /proc/1/root/tmp/inbox"]).ok
        assert box.exec(["cat", "/proc/1/root/tmp/inbox"]).stdout.strip() == "ok"


@pytest.mark.skipif(shutil.which("systemd-run") is None,
                    reason="no systemd-run — memory limits unenforced (D6 degraded)")
def test_memory_cap_is_enforced():
    # 128 MiB cap; try to grab ~400 MiB → the scope's MemoryMax must OOM-kill it.
    with Box("leak-mem", Policy(max_memory_mb=128)) as box:
        r = box.exec(["python3", "-c", "bytearray(400 * 1024 * 1024)"], timeout=30)
        assert not r.ok                                        # killed, host stays up
