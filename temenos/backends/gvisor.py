"""GVisorBackend — the v1 isolation engine (verified by scripts/gvisor_spike.py).

Session contract (held-foreground pattern, since rootless can't `create`+`start`):
  open()  -> `[systemd-run --user --scope ...] runsc <run-globals> run -bundle B <cid>`
             held as a child process (init `sleep infinity`); poll until "running".
  exec()  -> `runsc <exec-globals> exec [-cwd][-env] <cid> -- <cmd>`.
  close() -> `runsc kill <cid> KILL`; reap held child; `runsc delete --force`; cleanup.

Key flags: `--overlay2=all:memory` (writes never touch the host — verified),
`--network=none` (v1), `--platform` auto-detected (kvm→systrap→ptrace), and a per-box
`systemd-run --user --scope` for enforced memory/pids limits (D6).
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import time
import uuid

from ..exceptions import BackendError
from ..policy import Policy
from ..result import ExecResult
from . import oci
from .base import Backend

log = logging.getLogger("temenos.gvisor")

_PLATFORMS = ("kvm", "systrap", "ptrace")
_START_TIMEOUT_S = 10.0


class GVisorBackend(Backend):
    name = "gvisor"
    _platform_cache: str | None = None

    def __init__(self, *, runsc: str = "runsc") -> None:
        self._runsc = runsc
        self._cid: str | None = None
        self._bundle: str | None = None
        self._state: str | None = None
        self._held: subprocess.Popen | None = None
        self._policy: Policy | None = None
        self._base_env: dict[str, str] = {}

    # -- availability / platform detection -------------------------------------------

    @classmethod
    def is_available(cls) -> bool:
        return shutil.which("runsc") is not None and cls.detect_platform() is not None

    @classmethod
    def detect_platform(cls, runsc: str = "runsc") -> str | None:
        """First gVisor platform that actually starts /bin/true (cached). Probe, don't
        trust `runsc help` — all platforms list even when they fail to start (e.g. kvm
        without /dev/kvm, systrap on WSL2)."""
        if cls._platform_cache is not None:
            return cls._platform_cache
        if shutil.which(runsc) is None:
            return None
        for plat in _PLATFORMS:
            state = tempfile.mkdtemp(prefix="temenos-plat-")
            bundle = tempfile.mkdtemp(prefix="temenos-platb-")
            try:
                oci.build_bundle(Policy(), bundle, init_cmd=("/bin/true",))
                g = [runsc, f"--root={state}", "--rootless", "--network=none",
                     "--ignore-cgroups", f"--platform={plat}"]
                r = subprocess.run(g + ["run", "-bundle", bundle, f"plat-{plat}"],
                                   capture_output=True, timeout=30)
                subprocess.run(g + ["delete", "--force", f"plat-{plat}"],
                               capture_output=True)
                if r.returncode == 0:
                    cls._platform_cache = plat
                    return plat
            except Exception:  # noqa: BLE001 — a broken platform shouldn't abort detection
                pass
            finally:
                shutil.rmtree(state, ignore_errors=True)
                shutil.rmtree(bundle, ignore_errors=True)
        return None

    # -- flag construction ------------------------------------------------------------

    def _net_mode(self) -> str:
        # v1 simple toggle: full passthrough (host) or isolated (none). No filtering.
        return "host" if (self._policy and self._policy.network) else "none"

    def _run_globals(self, platform: str) -> list[str]:
        # root:memory => root/system writes are ephemeral RAM (host untouched); volume
        # mounts (disk binds, tmpfs) keep their own semantics (disk = durable).
        return [self._runsc, f"--root={self._state}", "--rootless", f"--network={self._net_mode()}",
                "--ignore-cgroups", "--overlay2=root:memory", f"--platform={platform}"]

    def _ctl_globals(self) -> list[str]:
        # exec/state/kill/delete join the running sandbox; no --overlay2 needed.
        return [self._runsc, f"--root={self._state}", "--rootless", f"--network={self._net_mode()}",
                "--ignore-cgroups", f"--platform={self._platform_cache}"]

    @staticmethod
    def _scope_prefix(policy: Policy) -> list[str]:
        """Per-box systemd user scope for enforced MEMORY (D6, spike-verified). Empty if
        unavailable. Note: the scope bounds the whole sandbox host-side (sentry + gofer +
        guest), so we do NOT set TasksMax here — gVisor's sentry needs many host threads;
        guest process count is bounded inside the box via OCI RLIMIT_NPROC instead."""
        if shutil.which("systemd-run") is None or not os.environ.get("XDG_RUNTIME_DIR"):
            log.warning("systemd-run --user unavailable: per-box memory limits will NOT "
                        "be enforced (D6). Do not run adversarial multi-tenant.")
            return []
        return [
            "systemd-run", "--user", "--scope", "-q",
            "-p", f"MemoryMax={policy.max_memory_mb}M",
            "-p", "MemorySwapMax=0",
            "--",
        ]

    # -- lifecycle --------------------------------------------------------------------

    def open(self, policy: Policy, *, name: str, env: dict[str, str] | None = None) -> None:
        if self._held is not None:
            raise BackendError("backend already open")
        if policy.network:
            log.warning("box %s: network=host (full passthrough, NO firewalling) — the "
                        "box can reach localhost, the LAN, cloud metadata, and exfiltrate "
                        "anywhere. Operator opt-in only; unsafe for adversarial tenants.",
                        name)
        # read paths must already exist (host data we expose). write paths are durable
        # disk binds — created if missing (the box's output dir). Box-internal scratch
        # should use the always-present /tmp or a MemoryVolume.
        for path in policy.read:
            if not os.path.exists(path):
                raise BackendError(
                    f"read path does not exist on host: {path!r} "
                    "(read paths must be existing host paths; use /tmp for scratch)"
                )
        for path in policy.write:
            os.makedirs(path, exist_ok=True)
        platform = self.detect_platform(self._runsc)
        if platform is None:
            raise BackendError("no usable gVisor platform (need runsc + kvm/systrap/ptrace)")

        self._policy = policy
        self._base_env = dict(env or {})
        self._cid = name or f"temenos-{uuid.uuid4().hex[:12]}"
        self._state = tempfile.mkdtemp(prefix="temenos-state-")
        self._bundle = tempfile.mkdtemp(prefix="temenos-bundle-")
        # resolve a box image (runner-owned writable rootfs) if the policy names one
        image_rootfs = None
        if policy.image:
            from ..image import resolve
            image_rootfs = resolve(policy.image).rootfs
        # let each storage provider set up its backing (mkdir, download, …) before start
        for m in policy.mounts:
            m.provider.prepare(self._cid)
        oci.build_bundle(policy, self._bundle, env=env, image_rootfs=image_rootfs)

        run_cmd = (self._scope_prefix(policy)
                   + self._run_globals(platform)
                   + ["run", "-bundle", self._bundle, self._cid])
        self._held = subprocess.Popen(run_cmd, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.PIPE, text=True)
        if not self._wait_running():
            err = self._drain_held_stderr()
            self.close()
            raise BackendError(f"box failed to start: {err or 'held process exited'}")

    def _wait_running(self) -> bool:
        deadline = time.monotonic() + _START_TIMEOUT_S
        while time.monotonic() < deadline:
            if self._held and self._held.poll() is not None:
                return False
            r = subprocess.run(self._ctl_globals() + ["state", self._cid],
                               capture_output=True, text=True)
            if r.returncode == 0 and '"status": "running"' in r.stdout:
                return True
            time.sleep(0.1)
        return False

    def _drain_held_stderr(self) -> str:
        if self._held and self._held.stderr:
            try:
                return self._held.stderr.read().strip()[:500]
            except Exception:  # noqa: BLE001
                return ""
        return ""

    def _alive(self) -> bool:
        return self._held is not None and self._held.poll() is None

    def exec(
        self,
        cmd: list[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        stdin: bytes | None = None,
        timeout: float | None = None,
    ) -> ExecResult:
        if self._cid is None:
            raise BackendError("backend is not open")
        if not self._alive():
            raise BackendError("box is not running (held process exited — OOM-killed?)")

        argv = self._ctl_globals() + ["exec"]
        if cwd:
            argv += ["-cwd", cwd]
        merged_env = dict(self._base_env)
        if env:
            merged_env.update(env)
        for k, v in merged_env.items():
            argv += ["-env", f"{k}={v}"]
        argv += [self._cid, *cmd]

        started = time.monotonic()
        try:
            r = subprocess.run(argv, input=stdin, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            out = e.stdout or b""
            return self._result(out, b"timeout", 124, started)
        return self._result(r.stdout, r.stderr, r.returncode, started)

    def _result(self, out: bytes, err: bytes, code: int, started: float) -> ExecResult:
        limit = self._policy.max_output_bytes if self._policy else 10 * 1024 * 1024
        truncated = len(out) > limit
        if truncated:
            out = out[:limit]
        return ExecResult(
            stdout=out.decode("utf-8", "replace"),
            stderr=err.decode("utf-8", "replace"),
            exit_code=code,
            truncated=truncated,
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    def commit(self) -> None:
        """Persist provider-backed volumes (e.g. fsspec upload). Disk/memory are no-ops."""
        if self._policy and self._cid:
            for m in self._policy.mounts:
                m.provider.commit(self._cid)

    def close(self) -> None:
        if self._cid and self._state:
            ctl = self._ctl_globals()
            subprocess.run(ctl + ["kill", self._cid, "KILL"], capture_output=True)
            subprocess.run(ctl + ["delete", "--force", self._cid], capture_output=True)
        if self._policy and self._cid:
            for m in self._policy.mounts:
                try:
                    m.provider.cleanup(self._cid)
                except Exception:  # noqa: BLE001 — cleanup must not mask teardown
                    log.warning("storage cleanup failed for %s", m.target, exc_info=True)
        if self._held is not None:
            try:
                self._held.wait(timeout=10)
            except Exception:  # noqa: BLE001
                self._held.kill()
            self._held = None
        for d in (self._bundle, self._state):
            if d:
                shutil.rmtree(d, ignore_errors=True)
        self._bundle = self._state = self._cid = self._policy = None
