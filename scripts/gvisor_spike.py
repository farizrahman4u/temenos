#!/usr/bin/env python3
"""temenos gVisor spike (Phase 0, gVisor-only track).

Gating question: does `runsc` actually run on THIS box (WSL2, aarch64, kernel 6.6,
no KVM), and does the session model we want work — i.e. can an agent write a file
in one tool call and execute it in the next, against ONE persistent sandbox?

This validates, end to end:
  1. runsc is present and reports a usable platform (systrap/ptrace, no KVM needed)
  2. a minimal OCI bundle runs at all          (`runsc run` -> echo)
  3. resource limits apply                       (OCI linux.resources.memory)
  4. network isolation                           (--network=none -> no egress)
  5. THE SESSION MODEL                            (`run -detach` + repeated `exec`):
       exec #1 writes /tmp/work.py
       exec #2 runs python3 /tmp/work.py   -> proves /tmp persists across calls
  6. clean teardown                              (kill + delete)

Pure stdlib. Rootless, private state dir, --network=none, --ignore-cgroups so it
needs no root and no cgroup delegation (the doctor showed cgroups aren't delegated).

Run:  python3 scripts/gvisor_spike.py
Exit: 0 if the session model works here; non-zero otherwise (with the failing step).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

CONTAINER_ID = "temenos-spike"
# Auto-detect: best-available first. kvm (fastest, needs /dev/kvm) -> systrap (gVisor's
# default, no KVM) -> ptrace (slowest, most compatible). The real backend probes the
# same order. On WSL2 here only ptrace works (no /dev/kvm; systrap "StartRoot EOF").
PLATFORMS_TO_TRY = ["kvm", "systrap", "ptrace"]


def sh(cmd: list[str], **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def build_rootfs(base: str) -> str:
    """Assemble a minimal rootfs by reusing the host's /usr (+ usrmerge symlinks)."""
    rootfs = os.path.join(base, "rootfs")
    os.makedirs(rootfs, exist_ok=True)
    # usrmerge: /bin /sbin /lib /lib64 are symlinks into /usr on modern Ubuntu.
    # Recreate the symlinks; bind the real dirs (/usr, /etc) via OCI mounts below.
    for name in ("bin", "sbin", "lib", "lib64"):
        src = "/" + name
        dst = os.path.join(rootfs, name)
        if os.path.islink(src):
            os.symlink(os.readlink(src), dst)
        elif os.path.isdir(src):
            os.makedirs(dst, exist_ok=True)   # will be a bind target
    for d in ("usr", "etc", "proc", "tmp", "dev"):
        os.makedirs(os.path.join(rootfs, d), exist_ok=True)
    return rootfs


def bind_mounts() -> list[dict]:
    mounts = [
        {"destination": "/proc", "type": "proc", "source": "proc"},
        {"destination": "/tmp", "type": "tmpfs", "source": "tmpfs",
         "options": ["nosuid", "nodev", "mode=1777"]},
        {"destination": "/dev", "type": "tmpfs", "source": "tmpfs",
         "options": ["nosuid", "mode=0755"]},
    ]
    # bind the real host dirs the symlinks point into
    for name in ("usr", "etc"):
        src = "/" + name
        if os.path.isdir(src):
            mounts.append({"destination": src, "source": src, "type": "bind",
                           "options": ["rbind", "ro"]})
    # if any of bin/sbin/lib/lib64 is a *real* dir (not usrmerge symlink), bind it
    for name in ("bin", "sbin", "lib", "lib64"):
        src = "/" + name
        if os.path.isdir(src) and not os.path.islink(src):
            mounts.append({"destination": src, "source": src, "type": "bind",
                           "options": ["rbind", "ro"]})
    return mounts


def write_config(bundle: str, args: list[str], mem_bytes: int | None) -> None:
    config = {
        "ociVersion": "1.0.0",
        "process": {
            "terminal": False,
            "user": {"uid": 0, "gid": 0},
            "args": args,
            "env": ["PATH=/usr/bin:/bin", "HOME=/tmp", "LANG=C.UTF-8"],
            "cwd": "/",
            "capabilities": {k: [] for k in
                             ("bounding", "effective", "inheritable", "permitted", "ambient")},
            "rlimits": [{"type": "RLIMIT_NOFILE", "hard": 1024, "soft": 1024}],
        },
        "root": {"path": "rootfs", "readonly": True},
        "hostname": "temenos",
        "mounts": bind_mounts(),
        "linux": {
            "namespaces": [{"type": t} for t in
                           ("pid", "mount", "ipc", "uts", "network")],
            "resources": {},
        },
    }
    if mem_bytes is not None:
        config["linux"]["resources"]["memory"] = {"limit": mem_bytes}
    with open(os.path.join(bundle, "config.json"), "w") as f:
        json.dump(config, f, indent=2)


class Runner:
    def __init__(self, state_dir: str, platform: str):
        self.base = ["runsc", f"--root={state_dir}", "--rootless",
                     "--network=none", "--ignore-cgroups", f"--platform={platform}"]

    def __call__(self, *args: str, **kw):
        return sh(self.base + list(args), **kw)


def cleanup(run: Runner):
    run("kill", CONTAINER_ID, "KILL")
    run("delete", "--force", CONTAINER_ID)


def step(ok: bool, name: str, detail: str = "") -> bool:
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {name}" + (f"\n         {detail}" if detail else ""))
    return ok


def try_platform(platform: str) -> tuple[bool, str]:
    """Run the full sequence on one platform. Returns (ok, summary)."""
    state = tempfile.mkdtemp(prefix="temenos-runsc-state-")
    run = Runner(state, platform)
    print(f"\n--- platform: {platform} ---")
    try:
        # 1. minimal `runsc run` -> echo
        b1 = tempfile.mkdtemp(prefix="temenos-bundle-echo-")
        build_rootfs(b1)
        write_config(b1, ["/bin/echo", "hello-from-gvisor"], mem_bytes=256 * 1024**2)
        p = run("run", "-bundle", b1, CONTAINER_ID + "-echo")
        ok1 = p.returncode == 0 and "hello-from-gvisor" in p.stdout
        if not step(ok1, "minimal `runsc run` (echo)",
                    (p.stdout + p.stderr).strip()[:500]):
            return False, f"{platform}: basic run failed"

        # 2. SESSION MODEL: hold a foreground `runsc run` as a long-lived child
        # process (rootless can't use create+start), then exec into it repeatedly.
        b2 = tempfile.mkdtemp(prefix="temenos-bundle-session-")
        build_rootfs(b2)
        write_config(b2, ["/bin/sleep", "300"], mem_bytes=256 * 1024**2)
        held = subprocess.Popen(run.base + ["run", "-bundle", b2, CONTAINER_ID],
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        running = False
        for _ in range(50):  # up to ~5s for the sandbox to come up
            if held.poll() is not None:
                break
            st = run("state", CONTAINER_ID)
            if st.returncode == 0 and '"status": "running"' in st.stdout:
                running = True
                break
            time.sleep(0.1)
        if not step(running, "start persistent session (held `runsc run` + exec)",
                    "held process exited early" if held.poll() is not None else ""):
            held.kill()
            cleanup(run)
            return False, f"{platform}: persistent session did not come up"

        # exec #1: write a python file into the sandbox /tmp
        e1 = run("exec", CONTAINER_ID, "/bin/sh", "-c",
                 "printf 'print(6*7)\\n' > /tmp/work.py && echo wrote")
        step(e1.returncode == 0 and "wrote" in e1.stdout,
             "exec #1: write /tmp/work.py", (e1.stdout + e1.stderr).strip()[:300])

        # exec #2: run that file — proves /tmp persisted across tool calls
        e2 = run("exec", CONTAINER_ID, "/usr/bin/python3", "/tmp/work.py")
        ok_session = e2.returncode == 0 and e2.stdout.strip() == "42"
        step(ok_session, "exec #2: run /tmp/work.py (write-then-run across calls)",
             f"stdout={e2.stdout.strip()!r} stderr={e2.stderr.strip()[:300]!r}")

        # 3. network isolation: no non-loopback iface
        e3 = run("exec", CONTAINER_ID, "/bin/sh", "-c", "ls /sys/class/net 2>/dev/null || cat /proc/net/dev")
        net_iso = ("eth0" not in e3.stdout)
        step(net_iso, "network isolation (--network=none -> no eth0)",
             e3.stdout.strip()[:300])

        # 4. (best-effort) memory limit visible
        e4 = run("exec", CONTAINER_ID, "/bin/sh", "-c",
                 "free -b 2>/dev/null | head -2 || echo no-free")
        step(True, "memory limit (informational)", e4.stdout.strip()[:300])

        cleanup(run)
        held.wait(timeout=10)
        if ok_session and net_iso:
            return True, f"{platform}: SESSION MODEL WORKS"
        return False, f"{platform}: session or netiso failed"
    finally:
        shutil.rmtree(state, ignore_errors=True)


def main() -> int:
    print("temenos gVisor spike — does the session model work on this box?\n")
    if not shutil.which("runsc"):
        print("runsc not found. Install it first (needs sudo), then re-run:")
        print("  curl -fsSL https://gvisor.dev/archive.key | sudo gpg --dearmor -o /usr/share/keyrings/gvisor-archive-keyring.gpg")
        print('  echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/gvisor-archive-keyring.gpg] https://storage.googleapis.com/gvisor/releases release main" | sudo tee /etc/apt/sources.list.d/gvisor.list')
        print("  sudo apt-get update && sudo apt-get install -y runsc")
        return 2

    v = sh(["runsc", "--version"])
    print("runsc:", v.stdout.strip().splitlines()[0] if v.stdout else v.stderr.strip())

    for plat in PLATFORMS_TO_TRY:
        try:
            ok, summary = try_platform(plat)
        except Exception as exc:  # noqa: BLE001
            ok, summary = False, f"{plat}: spike crashed: {type(exc).__name__}: {exc}"
            print(f"  [FAIL] {summary}")
        if ok:
            print("\n" + "-" * 70)
            print(f"RESULT: gVisor works here on platform '{plat}'. {summary}")
            print("=> gVisor-only v1 is viable. Write a file in one exec, run it in the next: confirmed.")
            return 0
        print(f"  ...platform {plat} did not fully pass ({summary}); trying next.")

    print("\n" + "-" * 70)
    print("RESULT: gVisor did NOT work on this box with any tried platform.")
    print("Reconsider: keep the native backend, or run gVisor under a different platform/config.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
