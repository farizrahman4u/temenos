"""BoxManager — the multi-box registry the daemon owns (Layer 2½).

Boxes are keyed by a **stable id derived from their data dir's absolute path**
(`hash(realpath)`), so two repos' `default` boxes (or any same-named boxes) get distinct
ids without the user typing data paths — that's the disambiguation (D15). A box's data
dir holds everything for it (config + overlay + checkpoints — D16); the backend runs with
`work_dir` pointed at it.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import threading
import time

from .backends.gvisor import GVisorBackend
from .box import Box
from .exceptions import BoxNotFound
from .policy import Policy

log = logging.getLogger("temenos.manager")

CHECKPOINT_DIR = "checkpoint"   # <box data dir>/checkpoint — the box's own resume point


def box_id(data_dir: str) -> str:
    """Stable id for a box, from the absolute path of its data dir."""
    return hashlib.sha256(os.path.realpath(data_dir).encode()).hexdigest()[:16]


def _checkpointable(policy: Policy) -> bool:
    return policy.checkpoint != "off" and policy.scratch != "memory"


class BoxManager:
    def __init__(self) -> None:
        self._boxes: dict[str, Box] = {}
        self._dirs: dict[str, str] = {}
        self._loop_stop: threading.Event | None = None
        self._loop_thread: threading.Thread | None = None

    def create(self, data_dir: str, policy: Policy, *, name: str | None = None,
               env: dict[str, str] | None = None, restore_from: str | None = None,
               cwd: str | None = None) -> str:
        """Create+start a box at `data_dir` (or return the running one — idempotent).
        Returns the box id. Persists the policy to `<data_dir>/config.json`. `cwd` sets the
        box's default working dir for execs that don't pass one (e.g. an agent's MCP calls)."""
        os.makedirs(data_dir, exist_ok=True)
        bid = box_id(data_dir)
        existing = self._boxes.get(bid)
        if existing is not None:
            if existing.running:
                if cwd is not None:
                    existing.default_cwd = cwd   # keep an attached session's cwd current
                return bid                      # ensure-semantics: already up
            self._boxes.pop(bid, None)          # crashed/dead → recreate below
        with open(os.path.join(data_dir, "config.json"), "w") as f:
            json.dump(policy.to_dict(), f, indent=2)
        # resume from the box's own checkpoint (the box dir IS the registry — no separate
        # state file): if one exists and we persist this box, restore from it.
        if restore_from is None and _checkpointable(policy):
            cp = os.path.join(data_dir, CHECKPOINT_DIR)
            if os.path.isdir(cp) and os.listdir(cp):
                restore_from = cp
        box = Box(name or bid, policy, backend=GVisorBackend(work_dir=data_dir),
                  env=env, restore_from=restore_from, default_cwd=cwd)
        box.start()
        self._boxes[bid] = box
        self._dirs[bid] = os.path.realpath(data_dir)
        return bid

    # -- checkpointing ----------------------------------------------------------------

    def _checkpoint(self, bid: str) -> None:
        """Atomically refresh <box dir>/checkpoint from the live box (keeps it running).
        Writes to a temp dir then swaps, so a crash never corrupts the last good one."""
        box, data_dir = self._boxes[bid], self._dirs[bid]
        if not _checkpointable(box.policy) or not box.running:
            return
        new = os.path.join(data_dir, ".checkpoint.new")
        final = os.path.join(data_dir, CHECKPOINT_DIR)
        bak = os.path.join(data_dir, ".checkpoint.bak")
        shutil.rmtree(new, ignore_errors=True)
        box.checkpoint(new, leave_running=True)
        shutil.rmtree(bak, ignore_errors=True)
        if os.path.isdir(final):
            os.rename(final, bak)          # keep last-good until the swap completes
        os.rename(new, final)
        shutil.rmtree(bak, ignore_errors=True)

    # -- background checkpoint loop (D17) --------------------------------------------

    def start_checkpoint_loop(self, *, idle_debounce: float = 3.0, max_staleness: float = 60.0,
                              tick: float = 1.0, max_per_tick: int = 2) -> None:
        """Periodically checkpoint dirty `checkpoint='auto'` boxes (idle-debounce or
        staleness-cap; capped per tick to avoid an I/O herd). Idempotent."""
        if self._loop_thread is not None:
            return
        self._loop_stop = threading.Event()

        def run() -> None:
            while not self._loop_stop.wait(tick):  # type: ignore[union-attr]
                now = time.monotonic()
                done = 0
                for bid, box in list(self._boxes.items()):
                    if done >= max_per_tick:
                        break
                    if box.policy.checkpoint != "auto" or not _checkpointable(box.policy):
                        continue
                    if box.should_autocheckpoint(idle_debounce=idle_debounce,
                                                 max_staleness=max_staleness, now=now):
                        try:
                            self._checkpoint(bid)
                            done += 1
                        except Exception:  # noqa: BLE001 — a bad checkpoint shouldn't kill the loop
                            log.warning("autocheckpoint failed for %s", bid, exc_info=True)

        self._loop_thread = threading.Thread(target=run, name="temenos-checkpointer",
                                            daemon=True)
        self._loop_thread.start()

    def stop_checkpoint_loop(self) -> None:
        if self._loop_stop is not None:
            self._loop_stop.set()
        if self._loop_thread is not None:
            self._loop_thread.join(timeout=5)
        self._loop_stop = self._loop_thread = None

    def get(self, bid: str) -> Box:
        box = self._boxes.get(bid)
        if box is None:
            raise BoxNotFound(bid)
        return box

    def info(self, bid: str) -> dict:
        box = self.get(bid)
        return {"id": bid, "name": box.name, "dir": self._dirs[bid],
                "running": box.running, "scratch": box.policy.scratch,
                "image": box.policy.image, "network": box.policy.network}

    def list(self) -> list[dict]:
        return [self.info(bid) for bid in list(self._boxes)]

    def delete(self, bid: str) -> None:
        box = self._boxes.pop(bid, None)
        if box is None:
            raise BoxNotFound(bid)
        self._dirs.pop(bid, None)
        box.close()

    def shutdown(self) -> None:
        """Daemon teardown: stop the loop, **commit each box on close** (the only commit
        for checkpoint='on-close' boxes; a final one for 'auto'), then close."""
        self.stop_checkpoint_loop()
        for bid, box in list(self._boxes.items()):
            try:
                self._checkpoint(bid)            # no-op for checkpoint='off' / memory
            except Exception:  # noqa: BLE001
                log.warning("commit-on-close failed for %s", bid, exc_info=True)
            try:
                box.close()
            except Exception:  # noqa: BLE001 — best-effort teardown
                pass
        self._boxes.clear()
        self._dirs.clear()
