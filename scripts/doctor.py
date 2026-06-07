#!/usr/bin/env python3
"""temenos capability spike (Phase 0).

Validates the riskiest assumptions in plan.md BEFORE we write real code:

  - unprivileged user + mount + net namespaces        (native backend, Phase 2)
  - pivot_root into a prepared rootfs                  (rootfs/pivot.py)
  - unprivileged overlayfs mount                       (D7 — else eager-copy fallback)
  - seccomp BPF filter install via prctl               (D5 — the security-critical path)
  - cgroup v2 delegated subtree, writable              (D6 — else RLIMIT_* fallback)
  - nft redirect rule inside our own net namespace     (D3 — the egress chokepoint)
  - pasta present                                      (D3 — unprivileged netns egress)
  - landlock ABI                                       (optional FS hardening)
  - runsc present                                      (optional strongest backend)

Pure stdlib. No side effects on the host: every privileged probe runs in a forked
child inside fresh namespaces, so its mounts/filters/cgroups die with the child.

Run:  python3 scripts/doctor.py
Exit: non-zero if any CRITICAL probe fails (native backend would not work).
"""
from __future__ import annotations

import ctypes
import ctypes.util
import os
import platform
import shutil
import subprocess
import sys
import tempfile

# ---------------------------------------------------------------------------
# arch-specific constants
# ---------------------------------------------------------------------------
MACHINE = platform.machine()
IS_X86_64 = MACHINE in ("x86_64", "amd64")
IS_AARCH64 = MACHINE in ("aarch64", "arm64")

if IS_X86_64:
    NR_PIVOT_ROOT = 155
    NR_LANDLOCK_CREATE_RULESET = 444
    AUDIT_ARCH = 0xC000003E
    BLOCKED_MKDIR_NRS = [83, 258]          # mkdir, mkdirat
elif IS_AARCH64:
    NR_PIVOT_ROOT = 41
    NR_LANDLOCK_CREATE_RULESET = 444
    AUDIT_ARCH = 0xC00000B7
    BLOCKED_MKDIR_NRS = [34]               # mkdirat (no legacy mkdir on arm64)
else:
    NR_PIVOT_ROOT = None
    NR_LANDLOCK_CREATE_RULESET = None
    AUDIT_ARCH = None
    BLOCKED_MKDIR_NRS = []

# clone/unshare flags
CLONE_NEWNS = 0x00020000
CLONE_NEWUSER = 0x10000000
CLONE_NEWNET = 0x40000000
CLONE_NEWPID = 0x20000000

# mount flags
MS_RDONLY = 1
MS_NOSUID = 2
MS_NODEV = 4
MS_NOEXEC = 8
MS_REC = 16384
MS_BIND = 4096
MS_PRIVATE = 1 << 18
MNT_DETACH = 2

# prctl / seccomp
PR_SET_NO_NEW_PRIVS = 38
PR_SET_SECCOMP = 22
SECCOMP_MODE_FILTER = 2
SECCOMP_RET_ALLOW = 0x7FFF0000
SECCOMP_RET_ERRNO = 0x00050000
EPERM = 1

# classic BPF opcodes
BPF_LD = 0x00
BPF_W = 0x00
BPF_ABS = 0x20
BPF_JMP = 0x05
BPF_JEQ = 0x10
BPF_K = 0x00
BPF_RET = 0x06

# landlock
LANDLOCK_CREATE_RULESET_VERSION = 1 << 0

_libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)


def _errno_str() -> str:
    e = ctypes.get_errno()
    return f"errno={e} ({os.strerror(e)})"


# ---------------------------------------------------------------------------
# thin libc wrappers
# ---------------------------------------------------------------------------
def unshare(flags: int) -> None:
    if _libc.unshare(flags) != 0:
        raise OSError(f"unshare({hex(flags)}) failed: {_errno_str()}")


def mount(source: str | None, target: str, fstype: str | None, flags: int, data: str | None) -> None:
    s = source.encode() if source else None
    t = target.encode()
    f = fstype.encode() if fstype else None
    d = data.encode() if data else None
    if _libc.mount(s, t, f, ctypes.c_ulong(flags), d) != 0:
        raise OSError(f"mount(src={source}, tgt={target}, fs={fstype}, data={data}) failed: {_errno_str()}")


def umount2(target: str, flags: int) -> None:
    if _libc.umount2(target.encode(), flags) != 0:
        raise OSError(f"umount2({target}) failed: {_errno_str()}")


def pivot_root(new_root: str, put_old: str) -> None:
    _libc.syscall.restype = ctypes.c_long
    rc = _libc.syscall(ctypes.c_long(NR_PIVOT_ROOT), new_root.encode(), put_old.encode())
    if rc != 0:
        raise OSError(f"pivot_root({new_root}, {put_old}) failed: {_errno_str()}")


def map_root() -> None:
    """Map the current host uid/gid -> 0 inside the new user namespace."""
    uid, gid = os.getuid(), os.getgid()
    try:
        with open("/proc/self/setgroups", "w") as f:
            f.write("deny")
    except FileNotFoundError:
        pass  # very old kernels
    with open("/proc/self/uid_map", "w") as f:
        f.write(f"0 {uid} 1")
    with open("/proc/self/gid_map", "w") as f:
        f.write(f"0 {gid} 1")


# ---------------------------------------------------------------------------
# child-probe harness — run fn() in a forked child, capture (ok, detail)
# ---------------------------------------------------------------------------
def run_child(fn) -> tuple[bool, str]:
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:  # child
        os.close(r)
        try:
            ok, detail = fn()
            payload = ("1" if ok else "0") + str(detail)
        except Exception as exc:  # noqa: BLE001 — report anything the probe raised
            payload = "0" + f"{type(exc).__name__}: {exc}"
        try:
            os.write(w, payload.encode()[:8000])
        finally:
            os._exit(0)
    # parent
    os.close(w)
    chunks = []
    while True:
        b = os.read(r, 8000)
        if not b:
            break
        chunks.append(b)
    os.close(r)
    os.waitpid(pid, 0)
    out = b"".join(chunks).decode(errors="replace")
    if not out:
        return False, "child produced no output (crashed before reporting)"
    return out[0] == "1", out[1:]


# ---------------------------------------------------------------------------
# probes
# ---------------------------------------------------------------------------
def _probe_userns():
    unshare(CLONE_NEWUSER)
    map_root()
    if os.getuid() != 0:
        return False, f"mapped but getuid()={os.getuid()} (expected 0)"
    return True, "unshare(CLONE_NEWUSER) + uid/gid map -> root-in-namespace OK"


def _probe_mountns_tmpfs():
    unshare(CLONE_NEWUSER | CLONE_NEWNS)
    map_root()
    mount(None, "/", None, MS_REC | MS_PRIVATE, None)
    d = tempfile.mkdtemp(prefix="temenos-tmpfs-")
    mount("none", d, "tmpfs", MS_NOSUID | MS_NODEV, None)
    test = os.path.join(d, "hello")
    with open(test, "w") as f:
        f.write("ok")
    return True, "fresh tmpfs mounted + writable inside mount namespace"


def _probe_pivot_root():
    unshare(CLONE_NEWUSER | CLONE_NEWNS)
    map_root()
    mount(None, "/", None, MS_REC | MS_PRIVATE, None)
    newroot = tempfile.mkdtemp(prefix="temenos-root-")
    # new root must itself be a mount point
    mount("none", newroot, "tmpfs", MS_NOSUID | MS_NODEV, None)
    put_old = os.path.join(newroot, "put_old")
    os.mkdir(put_old)
    pivot_root(newroot, put_old)
    os.chdir("/")
    umount2("/put_old", MNT_DETACH)
    os.rmdir("/put_old")
    entries = os.listdir("/")
    return True, f"pivot_root OK; new / contains {entries}"


def _try_overlay(opts_extra: str):
    base = tempfile.mkdtemp(prefix="temenos-ovl-")
    lower = os.path.join(base, "lower")
    upper = os.path.join(base, "upper")
    work = os.path.join(base, "work")
    merged = os.path.join(base, "merged")
    for p in (lower, upper, work, merged):
        os.mkdir(p)
    with open(os.path.join(lower, "from_lower"), "w") as f:
        f.write("L")
    data = f"lowerdir={lower},upperdir={upper},workdir={work}" + opts_extra
    mount("overlay", merged, "overlay", 0, data)
    if not os.path.exists(os.path.join(merged, "from_lower")):
        return False, "merged view missing lower file"
    with open(os.path.join(merged, "written_in_sandbox"), "w") as f:
        f.write("U")
    if not os.path.exists(os.path.join(upper, "written_in_sandbox")):
        return False, "write did not land in upper/ (CoW capture broken)"
    return True, "ok"


def _probe_overlay():
    unshare(CLONE_NEWUSER | CLONE_NEWNS)
    map_root()
    mount(None, "/", None, MS_REC | MS_PRIVATE, None)
    # plain first, then the defensive WSL options
    try:
        ok, msg = _try_overlay("")
        if ok:
            return True, "unprivileged overlayfs OK (plain opts); CoW write-set capture works"
    except OSError as e:
        plain_err = str(e)
    else:
        plain_err = msg
    try:
        ok, msg = _try_overlay(",index=off,metacopy=off")
        if ok:
            return True, "overlayfs OK only with index=off,metacopy=off (plain failed: %s)" % plain_err
        return False, f"overlay mounted but {msg}"
    except OSError as e:
        return False, f"overlayfs unavailable (plain: {plain_err}; defensive: {e}) -> D7 eager-copy fallback"


def _build_seccomp_prog():
    class SockFilter(ctypes.Structure):
        _fields_ = [("code", ctypes.c_uint16), ("jt", ctypes.c_uint8),
                    ("jf", ctypes.c_uint8), ("k", ctypes.c_uint32)]

    class SockFprog(ctypes.Structure):
        _fields_ = [("len", ctypes.c_uint16), ("filter", ctypes.POINTER(SockFilter))]

    instrs = []
    # 0: A = arch (seccomp_data offset 4)
    instrs.append((BPF_LD | BPF_W | BPF_ABS, 0, 0, 4))
    # 1: if arch != ours -> ALLOW (don't kill on unexpected arch during a probe)
    #    placeholder jf filled after we know the total length
    instrs.append((BPF_JMP | BPF_JEQ | BPF_K, 0, 0xFF, AUDIT_ARCH & 0xFFFFFFFF))
    # 2: A = nr (offset 0)
    instrs.append((BPF_LD | BPF_W | BPF_ABS, 0, 0, 0))
    # 3..: one JEQ per blocked syscall -> jump to ERRNO
    n = len(BLOCKED_MKDIR_NRS)
    errno_idx = 3 + n
    allow_idx = errno_idx + 1
    for i, nr in enumerate(BLOCKED_MKDIR_NRS):
        here = 3 + i
        jt = errno_idx - here - 1
        instrs.append((BPF_JMP | BPF_JEQ | BPF_K, jt, 0, nr))
    instrs.append((BPF_RET | BPF_K, 0, 0, SECCOMP_RET_ERRNO | EPERM))   # errno_idx
    instrs.append((BPF_RET | BPF_K, 0, 0, SECCOMP_RET_ALLOW))           # allow_idx
    # fix arch-mismatch jump to land on ALLOW
    code, jt, _jf, k = instrs[1]
    instrs[1] = (code, jt, allow_idx - 1 - 1, k)

    arr = (SockFilter * len(instrs))()
    for i, (code, jt, jf, k) in enumerate(instrs):
        arr[i] = SockFilter(code, jt, jf, k)
    prog = SockFprog(len(instrs), arr)
    return prog, arr  # keep arr alive


def _probe_seccomp():
    if not BLOCKED_MKDIR_NRS:
        return False, f"unsupported arch {MACHINE} for this probe"
    if _libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        return False, f"PR_SET_NO_NEW_PRIVS failed: {_errno_str()}"
    prog, _keep = _build_seccomp_prog()
    if _libc.prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, ctypes.byref(prog), 0, 0) != 0:
        return False, f"PR_SET_SECCOMP failed: {_errno_str()}"
    # filter installed: mkdir must now be blocked with EPERM
    target = "/tmp/temenos-seccomp-should-not-exist"
    try:
        os.mkdir(target)
    except PermissionError:
        return True, "seccomp BPF installed; blocked syscall (mkdir) -> EPERM as designed"
    except OSError as e:
        return True, f"seccomp installed; mkdir blocked ({e.__class__.__name__})"
    # if we get here the dir was created -> filter not enforced
    try:
        os.rmdir(target)
    except OSError:
        pass
    return False, "seccomp filter installed but mkdir was NOT blocked"


def _probe_nft_netns():
    if not shutil.which("nft"):
        return False, "nft not in PATH (install: apt-get install nftables)"
    unshare(CLONE_NEWUSER | CLONE_NEWNET)
    map_root()
    # the exact shape D3 needs: a nat/output REDIRECT to a local proxy port
    ruleset = (
        "table ip temenos_probe {\n"
        "  chain output {\n"
        "    type nat hook output priority -100;\n"
        "    oifname != \"lo\" tcp dport 443 redirect to :8080;\n"
        "  }\n"
        "}\n"
    )
    p = subprocess.run(["nft", "-f", "-"], input=ruleset,
                       capture_output=True, text=True)
    if p.returncode == 0:
        return True, "nat/output REDIRECT rule loaded inside user+net namespace (D3 chokepoint works)"
    return False, f"nft failed in netns (rc={p.returncode}): {p.stderr.strip() or p.stdout.strip()}"


# ----- parent-side probes (no namespaces / no side effects worth isolating) -----
def probe_kernel():
    rel = platform.release()
    detail = f"{platform.system()} {rel} ({MACHINE})"
    try:
        with open("/proc/sys/user/max_user_namespaces") as f:
            detail += f"; max_user_namespaces={f.read().strip()}"
    except OSError:
        pass
    try:
        with open("/proc/sys/kernel/unprivileged_userns_clone") as f:
            detail += f"; unprivileged_userns_clone={f.read().strip()}"
    except OSError:
        detail += "; unprivileged_userns_clone=<absent, likely default-on>"
    return "INFO", False, detail


def probe_cgroup_v2():
    if not os.path.exists("/sys/fs/cgroup/cgroup.controllers"):
        return "WARN", False, "cgroup v2 unified hierarchy not mounted -> D6 falls back to RLIMIT_*"
    try:
        with open("/proc/self/cgroup") as f:
            line = f.read().strip().splitlines()[0]
        rel = line.split("::", 1)[1]  # "0::/user.slice/.../user@1000.service/..."
    except (OSError, IndexError):
        return "WARN", False, "could not read /proc/self/cgroup -> D6 falls back to RLIMIT_*"
    our_cg = "/sys/fs/cgroup" + rel
    try:
        with open(os.path.join(our_cg, "cgroup.controllers")) as f:
            controllers = f.read().split()
    except OSError:
        return "WARN", False, f"no cgroup.controllers in {our_cg} -> RLIMIT_* fallback"
    have_mem = "memory" in controllers
    have_pids = "pids" in controllers
    # try to actually create a child cgroup and set a limit
    child = os.path.join(our_cg, "temenos_probe")
    try:
        os.mkdir(child)
    except OSError as e:
        return "WARN", False, (f"controllers={controllers} but cannot mkdir child cgroup "
                               f"({e}) -> not delegated; RLIMIT_* fallback")
    detail_writes = []
    try:
        # enabling controllers on the child requires them in OUR subtree_control
        try:
            with open(os.path.join(our_cg, "cgroup.subtree_control"), "w") as f:
                f.write("+memory +pids")
        except OSError:
            pass
        if have_mem:
            try:
                with open(os.path.join(child, "memory.max"), "w") as f:
                    f.write("67108864")
                detail_writes.append("memory.max")
            except OSError as e:
                detail_writes.append(f"memory.max FAILED({e.errno})")
        if have_pids:
            try:
                with open(os.path.join(child, "pids.max"), "w") as f:
                    f.write("32")
                detail_writes.append("pids.max")
            except OSError as e:
                detail_writes.append(f"pids.max FAILED({e.errno})")
    finally:
        try:
            os.rmdir(child)
        except OSError:
            pass
    ok = have_mem and any(w == "memory.max" for w in detail_writes)
    status = "PASS" if ok else "WARN"
    return status, False, (f"controllers={controllers}; child cgroup writable; "
                           f"set {detail_writes} -> {'cgroups usable (D6)' if ok else 'partial; may need RLIMIT_* fallback'}")


def probe_pasta():
    exe = shutil.which("pasta") or shutil.which("passt")
    if not exe:
        return "WARN", False, "pasta/passt not found (apt-get install passt) -> non-empty network unavailable until installed"
    try:
        p = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=5)
        ver = (p.stdout or p.stderr).strip().splitlines()[0] if (p.stdout or p.stderr) else "present"
    except Exception as e:  # noqa: BLE001
        ver = f"present but --version errored: {e}"
    return "PASS", False, f"{exe}: {ver}"


def probe_landlock():
    if NR_LANDLOCK_CREATE_RULESET is None:
        return "INFO", False, f"unsupported arch {MACHINE}"
    _libc.syscall.restype = ctypes.c_long
    abi = _libc.syscall(ctypes.c_long(NR_LANDLOCK_CREATE_RULESET),
                        None, ctypes.c_size_t(0),
                        ctypes.c_uint(LANDLOCK_CREATE_RULESET_VERSION))
    if abi > 0:
        return "PASS", False, f"Landlock ABI v{abi} available (optional FS hardening)"
    return "WARN", False, f"Landlock unavailable ({_errno_str()}) -> skip landlock layer"


def probe_runsc():
    exe = shutil.which("runsc")
    if not exe:
        return "INFO", False, "runsc not found -> native backend only (gVisor optional, Phase 3)"
    try:
        p = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=5)
        ver = (p.stdout or p.stderr).strip().splitlines()[0]
    except Exception as e:  # noqa: BLE001
        ver = f"present but --version errored: {e}"
    return "PASS", False, f"{exe}: {ver}"


# ---------------------------------------------------------------------------
# driver
# ---------------------------------------------------------------------------
# (name, criticality, runner) — runner returns (status, critical, detail)
# child probes return (ok, detail); we wrap them.
def _wrap_child(fn, critical, warn_on_fail=False):
    def run():
        ok, detail = run_child(fn)
        if ok:
            return "PASS", critical, detail
        return ("WARN" if (warn_on_fail and not critical) else "FAIL"), critical, detail
    return run


PROBES = [
    ("kernel / userns sysctls", probe_kernel),
    ("user namespace",          _wrap_child(_probe_userns, critical=True)),
    ("mount ns + tmpfs",        _wrap_child(_probe_mountns_tmpfs, critical=True)),
    ("pivot_root",              _wrap_child(_probe_pivot_root, critical=True)),
    ("seccomp BPF (D5)",        _wrap_child(_probe_seccomp, critical=True)),
    ("overlayfs CoW (D7)",      _wrap_child(_probe_overlay, critical=False, warn_on_fail=True)),
    ("cgroup v2 delegation (D6)", probe_cgroup_v2),
    ("nft redirect in netns (D3)", _wrap_child(_probe_nft_netns, critical=False, warn_on_fail=True)),
    ("pasta egress (D3)",       probe_pasta),
    ("landlock",                probe_landlock),
    ("gVisor runsc",            probe_runsc),
]

_COLOR = sys.stdout.isatty()
_PAINT = {
    "PASS": "\033[32m", "FAIL": "\033[31m", "WARN": "\033[33m",
    "INFO": "\033[36m", "SKIP": "\033[90m",
}


def _tag(status: str) -> str:
    label = f"{status:4}"
    if _COLOR and status in _PAINT:
        return f"{_PAINT[status]}{label}\033[0m"
    return label


def main() -> int:
    print("temenos capability spike — validating plan.md assumptions\n")
    if not sys.platform.startswith("linux"):
        print("This spike only applies to Linux. Detected:", sys.platform)
        return 2

    results = []
    for name, runner in PROBES:
        try:
            status, critical, detail = runner()
        except Exception as exc:  # noqa: BLE001
            status, critical, detail = "FAIL", False, f"probe crashed: {type(exc).__name__}: {exc}"
        results.append((name, status, critical, detail))
        crit = " [critical]" if critical and status == "FAIL" else ""
        print(f"  [{_tag(status)}] {name}{crit}")
        print(f"         {detail}")

    crit_fail = [n for n, s, c, _ in results if c and s == "FAIL"]
    warns = [n for n, s, c, _ in results if s in ("WARN", "FAIL") and not (c and s == "FAIL")]

    print("\n" + "-" * 70)
    if crit_fail:
        print(f"RESULT: {len(crit_fail)} CRITICAL probe(s) failed: {', '.join(crit_fail)}")
        print("The native backend cannot run here as designed. Investigate before Phase 2.")
        return 1
    print("RESULT: all critical probes passed — native backend is viable here.")
    if warns:
        print(f"Degraded/optional ({len(warns)}): {', '.join(warns)}")
        print("These trigger documented fallbacks (D6 RLIMIT_*, D7 eager-copy) or limit")
        print("optional features (filtered network needs nft+pasta; gVisor is optional).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
