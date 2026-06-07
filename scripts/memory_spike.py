#!/usr/bin/env python3
"""temenos memory-enforcement spike (D6).

The gVisor box spike proved isolation works but memory is NOT enforced under rootless
`--ignore-cgroups` (a 256 MB box saw all 8 GB). This spike tests how to actually cap a
box's memory unprivileged, by running a memory hog inside a limited box and checking it
gets OOM-killed rather than succeeding.

Mechanisms tested:
  M0 baseline   : runsc --ignore-cgroups + OCI memory limit only   (expect: NOT enforced)
  M1 systemd-run: wrap held `runsc run` in `systemd-run --user --scope -p MemoryMax=...`
                  (user manager places the scope in the delegated slice — works from
                   anywhere, incl. /init.scope)
  M2 direct cg  : mkdir a child cgroup under user@<uid>.service, set memory.max, move the
                  held runsc into it  (works only if we can join the delegated slice —
                  the cgroup-v2 common-ancestor rule may block a self-move from /init.scope)

Method per mechanism: start a MEM_MB-limited box, run a CONTROL_MB alloc (must succeed)
then a HOG_MB alloc (must fail if enforced). enforced = control_ok and not hog_ok.

Pure stdlib. Reuses scripts/gvisor_spike.py for the rootfs/config/run plumbing.
Run: python3 scripts/memory_spike.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gvisor_spike as g   # build_rootfs, write_config

MEM_MB = 512               # box memory cap
CONTROL_MB = 64            # must succeed under the cap
HOG_MB = 900               # must fail if the cap is enforced
UID = os.getuid()
USER_SLICE = f"/sys/fs/cgroup/user.slice/user-{UID}.slice/user@{UID}.service"


def alloc_cmd(mb: int) -> list[str]:
    # bytearray(N) zero-fills => actually commits N bytes of RSS
    return ["/usr/bin/python3", "-c",
            f"b=bytearray({mb}*1024*1024); print('ALLOC_OK', len(b))"]


def sh(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def make_bundle() -> str:
    b = tempfile.mkdtemp(prefix="temenos-mem-bundle-")
    g.build_rootfs(b)
    g.write_config(b, ["/bin/sleep", "300"], mem_bytes=MEM_MB * 1024**2)
    return b


def wait_running(globals_, cid, popen) -> bool:
    for _ in range(60):
        if popen.poll() is not None:
            return False
        st = sh(globals_ + ["state", cid])
        if st.returncode == 0 and '"status": "running"' in st.stdout:
            return True
        time.sleep(0.1)
    return False


def test_box(name: str, held_cmd: list[str], globals_: list[str], cid: str,
             setup_err: str = "") -> tuple[str, str]:
    """Returns (status, detail). status in PASS/FAIL/INFO."""
    if setup_err:
        return "INFO", setup_err
    popen = subprocess.Popen(held_cmd, stdout=subprocess.DEVNULL,
                             stderr=subprocess.PIPE, text=True)
    try:
        if not wait_running(globals_, cid, popen):
            err = (popen.stderr.read() if popen.stderr else "")[:300]
            return "INFO", f"box did not come up: {err.strip() or 'held process exited'}"
        ctl = sh(globals_ + ["exec", cid] + alloc_cmd(CONTROL_MB))
        ctl_ok = ctl.returncode == 0 and "ALLOC_OK" in ctl.stdout
        if not ctl_ok:
            return "INFO", (f"control {CONTROL_MB}MB alloc failed (cap too low / sentry "
                            f"overhead?) rc={ctl.returncode}: {(ctl.stdout+ctl.stderr).strip()[:200]}")
        hog = sh(globals_ + ["exec", cid] + alloc_cmd(HOG_MB))
        hog_ok = hog.returncode == 0 and "ALLOC_OK" in hog.stdout
        enforced = ctl_ok and not hog_ok
        detail = (f"control {CONTROL_MB}MB ok; hog {HOG_MB}MB "
                  f"{'BLOCKED rc=%d' % hog.returncode if not hog_ok else 'SUCCEEDED (rc=0)'}")
        return ("PASS" if enforced else "FAIL"), detail
    finally:
        sh(globals_ + ["kill", cid, "KILL"])
        sh(globals_ + ["delete", "--force", cid])
        try:
            popen.wait(timeout=10)
        except Exception:
            popen.kill()


def detect_platform() -> str:
    """First gVisor platform that actually starts /bin/true: kvm -> systrap -> ptrace."""
    for p in ("kvm", "systrap", "ptrace"):
        s = tempfile.mkdtemp(prefix="temenos-mem-plat-"); b = tempfile.mkdtemp(prefix="temenos-mem-pb-")
        try:
            g.build_rootfs(b); g.write_config(b, ["/bin/true"], mem_bytes=256 * 1024**2)
            ok = sh(["runsc", f"--root={s}", "--rootless", "--network=none",
                     "--ignore-cgroups", f"--platform={p}", "run", "-bundle", b, f"plat-{p}"])
            sh(["runsc", f"--root={s}", "delete", "--force", f"plat-{p}"])
            if ok.returncode == 0:
                return p
        finally:
            shutil.rmtree(s, ignore_errors=True); shutil.rmtree(b, ignore_errors=True)
    raise RuntimeError("no working gVisor platform (tried kvm/systrap/ptrace)")


PLATFORM = None            # set in main() via detect_platform()


def base_globals(state: str) -> list[str]:
    return ["runsc", f"--root={state}", "--rootless", "--network=none",
            "--ignore-cgroups", f"--platform={PLATFORM}"]


def m0_baseline():
    s = tempfile.mkdtemp(prefix="temenos-mem-s0-"); b = make_bundle()
    G = base_globals(s); cid = "mem-m0"
    held = G + ["run", "-bundle", b, cid]
    try:
        return test_box("M0", held, G, cid)
    finally:
        shutil.rmtree(s, ignore_errors=True)


def m1_systemd_run():
    if not shutil.which("systemd-run"):
        return "INFO", "systemd-run not present"
    s = tempfile.mkdtemp(prefix="temenos-mem-s1-"); b = make_bundle()
    G = base_globals(s); cid = "mem-m1"
    held = (["systemd-run", "--user", "--scope", "-q",
             "-p", f"MemoryMax={MEM_MB}M", "-p", "MemorySwapMax=0", "--"]
            + G + ["run", "-bundle", b, cid])
    try:
        return test_box("M1", held, G, cid)
    finally:
        shutil.rmtree(s, ignore_errors=True)


def m2_direct_cgroup():
    if not os.path.isdir(USER_SLICE):
        return "INFO", f"no delegated user slice at {USER_SLICE}"
    cg = os.path.join(USER_SLICE, "temenos-mem-spike")
    try:
        os.makedirs(cg, exist_ok=True)
    except OSError as e:
        return "INFO", f"cannot mkdir child cgroup ({e}) — slice not writable here"
    try:
        try:
            with open(os.path.join(cg, "memory.max"), "w") as f:
                f.write(str(MEM_MB * 1024**2))
            with open(os.path.join(cg, "memory.swap.max"), "w") as f:
                f.write("0")
        except OSError as e:
            return "INFO", f"cannot set memory.max ({e}) — memory not delegated to this subtree"
        s = tempfile.mkdtemp(prefix="temenos-mem-s2-"); b = make_bundle()
        G = base_globals(s); cid = "mem-m2"
        # held process self-moves into the child cgroup, then execs runsc
        wrapper = (f'echo $$ > "{cg}/cgroup.procs" 2>/tmp/m2move.err || '
                   f'{{ echo CGMOVE_FAIL >&2; cat /tmp/m2move.err >&2; exit 97; }}; exec "$@"')
        held = ["sh", "-c", wrapper, "_"] + G + ["run", "-bundle", b, cid]
        try:
            status, detail = test_box("M2", held, G, cid)
            if "did not come up" in detail and "CGMOVE_FAIL" in detail:
                return "INFO", ("self-move into delegated cgroup blocked from /init.scope "
                                "(cgroup-v2 common-ancestor rule) — works when the daemon "
                                "already runs inside user@.service")
            return status, detail
        finally:
            shutil.rmtree(s, ignore_errors=True)
    finally:
        try:
            os.rmdir(cg)
        except OSError:
            pass


def main() -> int:
    print("temenos memory-enforcement spike (D6)\n")
    global PLATFORM
    PLATFORM = detect_platform()
    where = open("/proc/self/cgroup").read().strip()
    print(f"this process cgroup: {where}")
    print(f"gVisor platform: {PLATFORM}")
    print(f"limit={MEM_MB}MB  control={CONTROL_MB}MB  hog={HOG_MB}MB\n")

    results = []
    for name, fn, expect in [
        ("M0 baseline (--ignore-cgroups + OCI limit)", m0_baseline, "expected NOT enforced"),
        ("M1 systemd-run --user --scope MemoryMax", m1_systemd_run, "expected enforced"),
        ("M2 direct delegated child cgroup", m2_direct_cgroup, "enforced if joinable"),
    ]:
        try:
            status, detail = fn()
        except Exception as exc:  # noqa: BLE001
            status, detail = "INFO", f"spike error: {type(exc).__name__}: {exc}"
        results.append((name, status, detail))
        verdict = {"PASS": "ENFORCED", "FAIL": "not enforced", "INFO": "n/a"}[status]
        print(f"  [{status}] {name}  ({expect})")
        print(f"         -> {verdict}: {detail}")

    print("\n" + "-" * 70)
    winners = [n for n, s, _ in results if s == "PASS"]
    if winners:
        print(f"RESULT: memory IS enforceable here via: {', '.join(w.split(' (')[0] for w in winners)}")
        print("=> wire the winning mechanism into the gVisor backend for D6.")
        return 0
    print("RESULT: no tested mechanism enforced memory on this box. Investigate "
          "cgroup delegation / swap accounting before relying on per-box memory caps.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
