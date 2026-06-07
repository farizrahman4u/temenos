# temenos — macOS feasibility

> Companion to `plan.md`. Question answered: **can temenos run on macOS, and how?**
> Verdict: **yes.** Two viable routes, very different effort/guarantee trade-offs. The
> recommended one preserves the v1 security model *unchanged* and is mostly packaging.
> No code in this repo has been changed to produce this doc.

---

## 0. TL;DR

| Route | Effort | Isolation | Preserves v1 guarantees? | When |
|---|---|---|---|---|
| **A. gVisor inside a Linux VM** (Lima / Apple VZ) | **Low** (packaging + plumbing, ~no core code) | **Identical to v1** (it *is* v1, in a guest) | **Yes — all of them** | Recommended default |
| **B. Native Seatbelt backend** (`sandbox-exec`) | High (new backend + storage rework) | **Partial** (matches plan §11 "Seatbelt" column) | **No** — loses CoW write-set, images, enforced limits, structural multi-tenancy | Single-user local convenience only |
| **C. microVM-per-box** (Virtualization.framework directly) | Very high | Strongest (real HW VM) | Re-implements the backend against a VM agent | Out of scope; that's a different product |

**The load-bearing insight:** temenos is *already* shipped on a Linux VM — WSL2 is a Linux
VM on Windows. macOS via a Linux VM (Route A) is the same architecture transplanted, so
the box model, gVisor, overlays, checkpoint, systemd-scope limits, and the leak-test all
keep working byte-for-byte. The only macOS-specific work is *getting a Linux kernel under
the daemon* and *bridging the host-side agent to the in-guest daemon*.

---

## 1. Why native gVisor is impossible on macOS

`runsc` is a userspace re-implementation of the **Linux** syscall ABI; it intercepts
Linux syscalls (via ptrace/systrap/KVM) and services them in its sentry. There is no
macOS/XNU port and there cannot be a trivial one — the whole point is Linux-compatibility.
Every v1 guarantee in `plan.md` flows from gVisor (namespaces, seccomp interception,
rootfs, netns, overlay, fscheckpoint). So on macOS gVisor can only live **inside a Linux
guest**. That single fact forks the design into Routes A vs B.

---

## 2. Route A — gVisor inside a managed Linux VM (recommended)

### 2.1 Shape
Run a small, long-lived Linux VM on the Mac driven by Apple **Virtualization.framework**
(VZ). We drive VZ **ourselves** via a bundled signed helper (see §2.7) rather than asking
the user to install Lima/Colima/Docker/UTM — the VM is an implementation detail the package
provisions automatically. Inside the guest, run **the existing, unmodified temenos stack**
— `temenos serve` (daemon + `BoxManager` + `GVisorBackend`).
The Mac host runs only:
- the **CLI** (a thin REST client — already designed to talk to a daemon over a port), and
- `temenos claude`'s **Claude process** (Claude already runs "on the host" by design; the
  host here is macOS, and it reaches the in-guest daemon's MCP endpoint over a forwarded
  port — exactly the `http://127.0.0.1:PORT/mcp/<id>` wiring in §8e, just port-forwarded).

This is **the same topology as WSL2 today** (agent on the outer OS, gVisor in the Linux
kernel), so the threat model, §11 security table, and the §10 leak-test all carry over
intact.

### 2.2 What gVisor platform runs in the guest?
Apple VZ Linux guests do **not** expose `/dev/kvm` (no nested virt), so — exactly like
WSL2 — `detect_platform()` will skip `kvm`. It will try `systrap` then fall back to
`ptrace`. `ptrace` is the guaranteed-working floor (verified on WSL2); `systrap` *may*
work on the VZ kernel (needs a one-line probe — `is_available()` already does this, no code
change, just a per-host result). Security model is identical across platforms (§3 of plan).

### 2.3 The two genuinely macOS-specific pieces of work
Both are plumbing, not core logic:

1. **Repo file sharing host→guest.** `temenos claude` mounts the repo live-writable so
   Claude edits real files (D16). On macOS the repo lives on the Mac FS; the guest must see
   it. Lima/VZ provide **virtiofs** (or 9p) shares — mount the Mac home/repo into the guest,
   then the daemon bind-mounts *that guest path* into the box. So the chain is:
   `macOS repo → virtiofs → guest path → gVisor bind`. Two consequences:
   - Path translation: the CLI/`temenos claude` resolves a **guest-side** path for the
     repo, not the macOS path. A small host↔guest path-map (`/Users/me/x` ⇄
     `/Users/me/x` if Lima mirrors home, or a configured prefix).
   - virtiofs write semantics + perf: writes are real and live (good — matches D16
     live-writable intent), but virtiofs is slower than a native FS for heavy build trees;
     document it. CoW/`--ephemeral` review still works because the overlay is *inside*
     gVisor in the guest, independent of how the lower got there.

2. **Port bridging for the daemon.** The single per-user daemon (D15) binds
   `127.0.0.1:PORT` *in the guest*; the macOS-side CLI and Claude need to reach it. Lima
   auto-forwards guest→host ports; with raw VZ you add a forward. The "connect-or-spawn"
   logic (flock + readiness) needs a macOS-aware variant: on Mac, "spawn the daemon" means
   "ensure the VM is up and the in-guest daemon is running," not "fork a local process."

### 2.4 What does NOT change
- `Policy`, `ExecResult`, `exceptions`, `box.py`, `storage.py`, `image.py`, `oci.py`,
  `backends/gvisor.py`, `BoxManager`, MCP/REST — **all unchanged**. They run in the guest.
- D6 memory limits: the guest is a normal systemd Linux, so `systemd-run --user --scope`
  works (give the guest a user session / `loginctl enable-linger`). Limits are enforced
  *within the VM's* RAM budget — set the VM's memory ceiling sensibly.
- Checkpoint/restore, images (`download`/`mmdebstrap`), network toggle — all as-is.

### 2.5 Cost / honest downsides
- A VM is heavier than a native sandbox: boot time, a few hundred MB–GB of guest RAM,
  background `virtiofsd`. For a dev laptop this is the Docker-Desktop tax, broadly accepted.
- Two memory ceilings to reason about (per-box `MemoryMax` *inside* the VM's total).
- File-share perf for huge repos/builds (virtiofs); mitigate by keeping build scratch on a
  guest-local DiskVolume rather than the shared mount.
- VM lifecycle is a new operational surface (start/stop/upgrade the guest image).

### 2.6 Effort estimate
- A `temenos` "VM provider" shim (start/ensure VM, run daemon in guest, forward port,
  translate paths) — a few hundred LoC of host-side glue + a guest provisioning script.
- `temenos doctor` gains a macOS branch: detect Lima/VZ, VM up, daemon reachable, share
  mounted, gVisor platform in guest.
- **No change to the security-critical core.** This is why it's the recommended route.

### 2.7 Zero-touch bootstrap from host Python (no manual VM install)

**Goal:** `pip install temenos` → first `temenos claude` *just works*, with no
Homebrew, no VM app, no `sudo`, no Xcode/CLT, no Gatekeeper dialog. This is achievable.

> **No `[macos]` extra.** Extras are for *optional features* the user opts into (like the
> existing `[mcp]`/`[cli]`/`[dev]`). Platform support is not optional — on a Mac the VZ
> backend is the only way temenos runs — so it must be implicit, selected by pip's two
> platform mechanisms, not a string the user has to remember:
> - **Environment markers** pick per-platform *dependencies* automatically, e.g.
>   `pyobjc-framework-Virtualization; sys_platform == "darwin"` (Mac gets it, Linux skips).
> - **Wheel-tag selection** picks the right *build of temenos itself*: ship a
>   `macosx_13_0_arm64` wheel that bundles the small signed helper (§2.7.2) as package
>   data (the helper from step 2 below); Linux gets the `manylinux`/`py3-none-any` wheel
>   without it. (Gotcha: pip can drop
>   the exec bit on bundled binaries — `chmod +x` on first use; the code signature survives
>   zipping.)
> - The **guest image** (hundreds of MB) is in *neither* — PyPI file-size limits make
>   bundling it abusive and it'd bloat every install, so it's always a first-run download
>   (step 3). Extras would only make sense if a *Linux* user wanted to pull Mac machinery,
>   which markers already prevent.

The chain, all orchestrated from host Python:

1. **VMM = Virtualization.framework directly (unprivileged).** VZ is built into macOS 13+,
   boots a Linux kernel + rootfs, and runs **without root**. We do **not** shell out to
   Lima/Colima or require any pre-installed VM manager.

2. **A bundled, pre-signed helper drives VZ — we do NOT call VZ from Python directly.**
   *The one hard constraint:* VZ refuses to run unless the calling process is code-signed
   with the **`com.apple.security.virtualization`** entitlement. Stock `python3` is not
   entitled, and signing the interpreter is fragile (shared/symlinked, breaks venvs). So
   ship a tiny entitled helper and `subprocess`-drive it. Two sourcing options:
   - **Reuse [`vfkit`](https://github.com/crc-org/vfkit)** — the notarized
     Virtualization.framework CLI from the podman/crc project (virtiofs + vsock + NAT +
     Rosetta, MIT). Zero Apple-Developer cost to us; it's already signed+notarized.
   - **Ship our own minimal helper**, signed+notarized at *release* time under an Apple
     Developer ID ($99/yr). This is a **build-time publisher cost, never a user action.**

3. **Assets download on first run (a fetch, not an "install").** A temenos-built guest
   image — Linux kernel + rootfs with `runsc` + systemd + temenos prebaked — pulled from a
   pinned URL + checksum into `~/.local/share/temenos/vm/` (a few hundred MB, once). Built
   in CI; the package only references it.

4. **No Gatekeeper prompt.** Files fetched via Python `urllib` do **not** receive the
   `com.apple.quarantine` xattr (only browser-class downloaders apply it), so a properly
   signed helper executes silently. Notarized → clean regardless.

5. **Wire-up:** boot VZ unprivileged → virtiofs-share the repo/home → vsock (or a forwarded
   TCP port) for the in-guest daemon → host CLI / `temenos claude` connect over it. The
   "connect-or-spawn" daemon logic (D15) gets a macOS arm: *spawn* = "ensure VM up + daemon
   up," then connect to the forwarded port.

**Honest constraints / spike list (this project is spike-driven — verify before relying):**
- **macOS 13+ on Apple Silicon** is the clean target (virtiofs needs 12+, Rosetta-in-VM
  needs 13+). Intel VZ works but is secondary.
- **The entitlement is the load-bearing risk.** Confirm that a *downloaded* helper carrying
  `com.apple.security.virtualization` runs without a dialog — easy with a notarized vfkit;
  needs a spike if we try **ad-hoc** signing to skip the Developer ID (ad-hoc entitlement
  honoring across machines is the uncertain bit; doing it *at the user's machine* would
  require `codesign`, which can trigger a CLT-install prompt = a user action, so avoid it —
  pre-sign at build instead).
- **No `/dev/kvm` in the guest** (no nested virt) → gVisor uses `systrap`/`ptrace`, same as
  WSL2. `ptrace` is the guaranteed floor.
- **Two memory ceilings**: per-box `MemoryMax` (D6) lives inside the VM's total RAM budget;
  size the VM sensibly and document it.
- First-run download latency; the VM is a background process to lifecycle (start/stop/GC).

**Net:** fully automatable from Python. The only thing outside the package's control is the
macOS version/arch floor and a **one-time, publisher-side** notarized helper (or reusing
vfkit's). The *user* installs nothing beyond `pip install`.

---

## 3. Route B — native Seatbelt backend (`backends/seatbelt.py`)

### 3.1 Shape
Implement the `Backend` ABC against macOS **Seatbelt** via `sandbox-exec` (SBPL profile
generated from `Policy`) — or `sandbox_init` through `ctypes` (private API). The persistent
"box" = a long-lived shell launched under a sandbox profile; `exec()` feeds commands into
it (mirror of the gVisor held-run pattern, minus the container). `is_available()` returns
true on Darwin with `sandbox-exec` present.

The `Backend` ABC fits this cleanly (`open/exec/close/name/is_available`), and `Policy` is
pure data, so wiring a second backend is architecturally supported. **But** several v1
guarantees do not survive the translation:

### 3.2 What breaks, and why
| v1 guarantee | Seatbelt reality |
|---|---|
| **CoW write-set** (`box.writes()`, `temenos diff`, `--ephemeral`) | Seatbelt **filters**, it does not **remap**. A `write` path is allowed → writes hit the **real host FS**. There is no overlay capturing a write-set. You'd have to fake CoW (redirect into a per-box dir), which SBPL can't express. **This guarantee is lost** unless re-engineered out-of-band. |
| **Box images / writable `/usr` / `apt`** (`Policy.image`) | No separate rootfs — the box sees the host's `/usr`. `image` is **unsupported**; no `download`/`mmdebstrap` story. |
| **Enforced memory/CPU/pids (D6)** | No cgroups. Only `setrlimit` (RLIMIT_AS breaks runtimes; RLIMIT_CPU is coarse) and `taskpolicy`. OOM-killing a 900 MB hog in a 512 MB box is **not achievable robustly**. Fork-bomb containment is weak. |
| **Structural multi-tenancy (D11)** | All sandboxed procs share the host kernel, host FS, host PID space. Isolation is *filter-based per process*, not the "no shared writable mount, ever" structural invariant. The §11 table already grades Seatbelt **"partial"** for cross-tenant. |
| **Kernel-CVE blast radius** | §11 grades Seatbelt **"vulnerable"** — a syscall filter, not a second kernel. |
| **checkpoint/restore** | No equivalent. Unsupported. |

What Seatbelt *does* do well: per-path read/write rules, process-exec control, and
network rules (SBPL `network-outbound` with `remote` host/port matching is actually
*more* expressive than v1's all-or-nothing toggle). For a **single-user, non-adversarial,
local dev** use ("keep this agent off my `~/.ssh` and `~/Documents`"), it's a legitimate
convenience backend.

### 3.3 Architectural leak to note
Storage is mildly OCI-coupled: `StorageProvider.oci_mount()` returns an **OCI** mount dict,
and `oci.py` builds an OCI bundle. A Seatbelt backend wouldn't use OCI bundles — it'd
translate `Policy.read/write/mounts` into **SBPL path rules**. So Route B needs either a
second translation method on providers (e.g. `seatbelt_rules()`) or a backend that reads
provider fields (`kind`, `host_dir`, …) directly. Not fatal, but it shows the storage
layer currently assumes OCI; generalizing it is part of Route B's cost.

### 3.4 `sandbox-exec` is deprecated
Apple marks `sandbox-exec` deprecated (the underlying `sandbox_init` is private). It still
ships and is used widely (Chrome, Bazel, Nix, Claude Code's own sandbox), but it's a
stability risk to build a *product* backend on. Acceptable for an opt-in convenience tier.

### 3.5 Verdict on B
Possible and a clean ABC fit, but it delivers a **materially weaker** product that
contradicts temenos's headline guarantees (CoW write-set + structural isolation +
enforced limits). Ship it, if at all, as an explicitly-labeled `TrustLevel`-gated
"partial / single-user" backend that the leak-test reports as degraded — **never** for
adversarial multi-tenant. The §11 "Seatbelt (post)" column is already an honest preview of
what it can and can't do.

---

## 4. Route C — microVM per box (for completeness)

Boot a lightweight Linux microVM per box directly on Virtualization.framework (à la
`krunvm`/Firecracker). Strongest isolation (real HW-assisted VM, beats gVisor on kernel
CVEs), but you'd re-implement the entire exec/mount/overlay/audit plumbing against an
in-guest agent instead of `runsc` — effectively a different product (a local E2B/Modal).
Out of scope for "support temenos on Mac." If maximal isolation is ever the goal, Route A
already gives you "gVisor *inside* a VM" (defense in depth) at a fraction of the effort.

---

## 5. Recommendation

1. **Ship Route A.** It's the WSL2 architecture on a Mac: a managed Linux VM (Lima or a
   bundled Apple-VZ guest) running the **unmodified** temenos daemon. Mac-side work is a VM
   provider shim (ensure-VM + port-forward + path-map) and a `doctor` branch. **Zero change
   to the security-critical core; all v1 guarantees intact.**
2. **Treat Route B (Seatbelt) as an optional, clearly-degraded convenience backend** for
   single-user local use, if there's demand for a no-VM experience — and only after the
   storage layer's OCI-coupling is generalized. Gate it so adversarial/multi-tenant configs
   refuse it, and have the leak-test report the degraded rows.
3. Keep `BoxManager`'s "platform not yet supported off-Linux" as the honest default until
   Route A's shim lands.

### Suggested phasing (additive to plan.md §12)
- **mac-P0 (spike):** confirm the §2.7 bootstrap — a downloaded, entitled helper (vfkit or
  ours) boots a VZ Linux guest unprivileged with no Gatekeeper/CLT prompt; runsc runs in it.
- **mac-P1:** self-provisioning VZ driver (download helper + guest image, boot, vsock/port
  forward) — **no user VM install**; `temenos serve` runs in guest; CLI/`claude` reach it
  via the forwarded port; macOS "connect-or-spawn" arm. `doctor` macOS checks. (Route A MVP.)
- **mac-P2:** repo file-share + host↔guest path translation for `temenos claude`
  live-writable mounts; document virtiofs perf + scratch-on-guest-disk guidance.
- **mac-P3 (optional):** `backends/seatbelt.py` convenience backend + storage-layer
  de-OCI-ification + degraded leak-test reporting.
