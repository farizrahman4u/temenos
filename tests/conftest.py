"""Shared fixtures + a safety net that reaps leaked gVisor sandboxes.

Box-creating tests run a real `runsc`. A held `runsc run` (plus its `runsc-sandbox` /
`runsc-gofer` children) is a child of whatever started it — the spawned daemon, or a direct
`Box`. If that owner dies ungracefully (a pytest timeout, Ctrl-C mid-test, a SIGKILL'd
daemon), the sandbox is orphaned and lingers, accumulating across runs.

Two defenses:
  * `stop_daemon` — graceful teardown the daemon fixtures use: delete every box (tearing
    down its sandbox while the daemon is alive and responsive), then stop the process and
    wait for it to actually exit (SIGKILL fallback).
  * `_reap_orphan_sandboxes` — a session-scoped autouse net: every test sandbox roots its
    state under pytest's tmp tree, so at session end we SIGKILL any `runsc*` whose `--root`
    points there. Catches whatever escaped per-test teardown, including leftovers from
    earlier interrupted sessions.
"""
from __future__ import annotations

import os
import signal
import subprocess
import time

import pytest


def _kill_and_wait(pid: int, timeout: float = 5.0) -> None:
    """SIGTERM `pid`, wait up to `timeout` for it to exit, then SIGKILL."""
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _terminate_daemon(client) -> None:
    """Stop the spawned per-user daemon recorded in the daemon-info file. Deletes its boxes
    first (so their sandboxes are torn down by the live daemon, not orphaned by the kill),
    then stops the process and waits for it to exit."""
    info = client.read_info()
    if not info:
        return
    cl = client.connect()
    if cl is not None:
        try:
            for box in cl.list_boxes():
                try:
                    cl.delete_box(box["id"])
                except Exception:  # noqa: BLE001 — best-effort teardown
                    pass
        except Exception:  # noqa: BLE001
            pass
    pid = info.get("pid")
    if pid:
        _kill_and_wait(pid)


@pytest.fixture
def stop_daemon():
    """Yields a callable the daemon fixtures invoke in teardown to stop the daemon cleanly."""
    from temenos.server import client
    return lambda: _terminate_daemon(client)


def _runsc_pids_under(prefix: str) -> list[int]:
    """PIDs of every `runsc*` process whose `--root=` value lives under `prefix`."""
    try:
        out = subprocess.run(["pgrep", "-af", "runsc"],
                             capture_output=True, text=True).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    pids: list[int] = []
    for line in out.splitlines():
        parts = line.split()
        if not parts or not parts[0].isdigit():
            continue
        for tok in parts[1:]:
            if tok.startswith("--root=") and tok[len("--root="):].startswith(prefix):
                pids.append(int(parts[0]))
                break
    return pids


@pytest.fixture(scope="session", autouse=True)
def _reap_orphan_sandboxes(tmp_path_factory):
    # Match the per-user base (…/pytest-of-<user>), not just this session's dir, so the net
    # also clears sandboxes leaked by earlier interrupted runs.
    prefix = str(tmp_path_factory.getbasetemp().parent)
    yield
    pids = _runsc_pids_under(prefix)
    if not pids:
        return
    for pid in pids:                       # SIGKILL: orphaned sandbox/gofer ignore SIGTERM
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
