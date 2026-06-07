# temenos — Plan

> **Handover:** Linux-first, developed in WSL (Ubuntu 24.04, kernel 6.6, aarch64).
> v1 is **gVisor-only** and **box-centric** (see below). This single doc reconciles the
> architecture, decisions, spike results, APIs, CLI, and harness integration. Phase 0
> (capability spike) is **done and green**; code is not yet written.

---

## 1. What is temenos?

**Untrusted-code containment for a *trusted* agent.** You describe what an agent's
*executed code* may do (a `Policy`); temenos runs that code inside a **box** — a
persistent, named gVisor sandbox — and hands back an audit trail and the write-set.

- **Core abstraction: the box.** A box is a named Linux sandbox you `create`, `exec`
  into, `list`, and `delete`. Within a box, reads/writes persist across calls (write a
  file in one call, run it in the next). Across boxes/tenants: zero crosstalk.
- **Delivery: a daemon exposing REST + MCP.** A FastAPI daemon (`temenos serve`)
  supervises boxes. Agents attach over **MCP**; humans/scripts use the **CLI** (a REST
  client) or the REST API directly.
- **The agent runs on the host; its payloads run in the box.** We sandbox what the
  agent *executes*, not the agent itself (so its updates, auth, and model API keep
  working). The headline flow: `temenos claude --box NAME` runs Claude on the host with
  its native host-touching tools banned and replaced by box-scoped MCP tools.
- **Multi-tenant.** One host serves many users; each box belongs to a tenant.

> **Accuracy note (use in security docs):** gVisor (`runsc`) is a *userspace kernel* —
> it implements a Linux-compatible syscall surface and intercepts syscalls. It is **not**
> a hardware/KVM VM. A box *feels* like a lightweight Linux machine, but the boundary is
> syscall interception. Say "sandbox," not "VM."

**PyPI name:** `temenos` (available). **Python:** 3.11+.

---

## 2. Threat model (read first)

**Trusted:** the agent process (Claude/Codex/etc.) and the human operator + policy.
**Untrusted:** the *code/data the agent executes* (buggy or destructive commands,
untrusted repos/packages/data) and tool output flowing back (may carry prompt injection).
**Also untrusted of each other (D11):** different tenants.

**Guarantee:** code run through temenos cannot read/write/network/spawn outside its
box's policy; you get a semantic audit trail + a CoW write-set; and one tenant cannot
read, influence, or (modulo the D6 caveat) exhaust another.

**Not guaranteed:** containing a *malicious agent*. If the agent itself is adversarial,
this is the wrong tool.

### The load-bearing requirement: sole execution path
The boundary holds **only if temenos is the agent's only way to execute anything.** This
is structural, not a prompt:
- **Omit native executors.** The agent's harness must expose *only* temenos tools — no
  native bash/file/fetch. Tool-omission (or a hard deny) beats string-matched allowlists.
- **A steering prompt is not enforcement.** "Please use temenos" defends against nothing.

### Residual risk: injection through tool output
A payload can print a prompt injection into stdout that flows back to the host-side
agent. The model stays sound **iff** (1) every execution channel routes through temenos,
and (2) the default policy is tight enough that even a fully injection-steered agent acts
only within policy (no `~/.ssh` in `read`, no `evil.com` in `network`). The story is
**"tight default policy + sole execution path,"** not "the agent is trusted so we're fine."

### Cross-tenant boundary (D11)
The gVisor-container-per-(tenant, box) boundary + the daemon's authz & quotas. Side
channels between co-resident sandboxes are out of scope (documented, not solved).

---

## 3. Spike results — Phase 0, VERIFIED on this box (WSL2/aarch64/6.6/no KVM)

`scripts/doctor.py` (namespace/overlay/cgroup/nft/seccomp probes) and
`scripts/gvisor_spike.py` (the box session model) ran green. Findings that drove §5:

- ✅ **gVisor runs — on `ptrace` here.** Probed all three: `kvm` fails (no `/dev/kvm` on
  WSL2), `systrap` fails (`StartRoot EOF`, a WSL kernel quirk), `ptrace` works. ptrace is
  the **WSL fallback**, not a global default — the backend auto-detects best-available
  (`kvm` → `systrap` → `ptrace`), so a native host with `/dev/kvm` uses kvm and a typical
  cloud VM uses systrap. Platform is perf/compat only; same security model.
- ✅ **The box session model works:** wrote `/tmp/work.py` in one `exec`, ran
  `python3 /tmp/work.py` in the next → `42`. `/tmp` persists across calls.
- ✅ **`--network=none` isolates** (only `lo` inside).
- ⚠️ **Rootless does NOT support `runsc create`** (create→start→exec). Only `run` →
  hence the *held-foreground* pattern (§9).
- ⚠️→✅ **OCI memory limit not enforced under `--ignore-cgroups`** (sandbox saw all 8 GB),
  **but** a second spike (`scripts/memory_spike.py`) resolved D6: wrapping the held
  `runsc run` in **`systemd-run --user --scope -p MemoryMax -p MemorySwapMax=0`**
  enforces it (900 MB hog OOM-killed in a 512 MB box; 64 MB control fine). Works from
  any cgroup because the *user manager* places the scope in the delegated slice. A direct
  child cgroup under `user@<uid>.service` also works **but only** if the daemon runs
  inside the user session (cgroup-v2 common-ancestor rule blocks a self-move from
  `/init.scope`). gVisor has no internal memory-cap flag, so cgroup-based is the way.
- ℹ️ Native-namespace primitives (userns/mountns/pivot_root/seccomp) also work here —
  **but** `uid_map` must be written from the *unsharing process's main thread* via a
  single raw `write()`; doing it inside an `os.fork()` child returns EPERM (cost us real
  time). Recorded for the post-v1 native backend (§13/§14).

---

## 4. Repository layout

```
temenos/                         # (v1) built in v1 · (post) specified, deferred
├── temenos/
│   ├── __init__.py          # re-exports: BoxManager, Box, Policy, TrustLevel, Result
│   ├── policy.py            # (v1) Policy dataclass, TrustLevel, policy algebra (pure)
│   ├── box.py               # (v1) Box: persistent sandbox handle (exec/read/write/writes)
│   ├── manager.py           # (v1) BoxManager: multi-tenant registry, CRUD, quotas, TTL
│   ├── result.py            # (v1) ExecResult, AuditEntry, AuditLog (pure)
│   ├── storage.py           # (v1) StorageProvider + MemoryVolume/DiskVolume + Mount (D12)
│   ├── image.py             # (v1) box images: runner-owned base rootfs (writable /usr)
│   ├── exceptions.py        # (v1) PolicyViolation, BoxNotFound, QuotaExceeded, Backend/StorageError
│   │
│   ├── backends/
│   │   ├── base.py          # (v1) Backend ABC: open/exec/close
│   │   ├── gvisor.py        # (v1) runsc held-run + exec session backend
│   │   ├── oci.py           # (v1) config.json generation from Policy
│   │   ├── native.py        # (post) namespaces+seccomp+keeper via ctypes
│   │   ├── seatbelt.py      # (post) macOS sandbox-exec
│   │   └── windows.py       # (post) Job Objects
│   │
│   ├── server/
│   │   ├── app.py           # (v1) FastAPI: REST control plane
│   │   ├── mcp.py           # (v1) per-box MCP sub-app (temenos_exec/read/write/list)
│   │   └── auth.py          # (v1) bearer token -> tenant
│   │
│   ├── cli.py               # (v1) stdlib CLI; `image build/ls/rm` done · box cmds (create/exec/shell/serve) = Phase 3
│   ├── harness/
│   │   ├── claude.py        # (v1) `temenos claude`: wire MCP + ban native tools
│   │   └── jail.py          # (post) run a whole harness inside a box (Codex etc.)
│   │
│   ├── net/                 # (post) per-host egress filtering: pasta + nft + SNI proxy
│   └── rootfs/              # (post) only the native backend needs this
│
├── scripts/
│   ├── doctor.py            # capability spike (namespaces/overlay/cgroup/nft/seccomp)
│   ├── gvisor_spike.py      # gVisor box session-model spike (the v1 backend contract)
│   └── memory_spike.py      # D6: per-box memory enforcement via systemd-run --user --scope
│
├── tests/
│   ├── test_policy.py
│   ├── test_box.py
│   ├── test_manager.py      # multi-tenant isolation + quota tests (D11)
│   ├── test_backends/test_gvisor.py
│   └── leak/                # per-harness spillover conformance (§10)
│
├── pyproject.toml
└── plan.md                  # this file
```

---

## 5. Decisions (resolved — build against these)

The first four were product calls; the rest follow from them + the threat model + the
Phase 0 spike.

| # | Decision | Choice | Why |
|---|---|---|---|
| D1 | Python deps | **Pragmatic, vetted.** Core lib zero runtime deps; surfaces pull extras. `runsc` is a *binary* dep, not pip. | Avoid reinventing MCP/HTTP/CLI plumbing. |
| D2 | v1 backend | **gVisor (`runsc`) ONLY.** Native/macOS/Windows deferred. | Spike-driven: gVisor provides ns+seccomp+rootfs+netns internally → v1 skips the riskiest hand-rolled code and ships the *strongest* isolation. Trade-off: hard `runsc` dep. |
| D3 | Networking | **Simple toggle** (`Policy.network: bool`, verified): `False` → `--network=none` + isolated netns (default); `True` → `--network=host` + **drop the netns** = full passthrough, **no firewalling** (both flag and netns-drop required). Per-host filtering (pasta+SNI proxy) is **post-v1**. | No firewalling wanted — off or passthrough. ⚠️ `network=True` = full host reach (localhost/LAN/cloud-metadata/arbitrary egress): operator opt-in, unsafe for adversarial tenants; agents never set policy. |
| D4 | Surfaces | **Core lib → FastAPI daemon (REST + MCP) + thin CLI.** One core, multiple adapters. | §7 layering. |
| D5 | seccomp | **gVisor's built-in interception.** Hand-rolled cBPF is post-v1 (native only). | gVisor's filtering is stronger and already written. |
| D6 | Resource limits | **Per-box `systemd-run --user --scope`** with `MemoryMax`/`MemorySwapMax=0`/`CPUQuota`/`TasksMax` from `Policy` (spike-verified: enforces, unprivileged, works from any cgroup). Direct delegated child cgroup is an optimization when the daemon runs inside `user@.service`. Fallback if no systemd user-delegation: warn + unenforced. | gVisor has no internal cap; cgroups are the only lever, and the user manager makes them unprivileged-per-box. |
| D7 | Box filesystem | **Writable root, ephemeral, disk-backed by default** (`Policy.scratch="disk"` → `--overlay2=root:dir=<per-box dir>`): tooling (pip/npm/builds) works, writes stay off the host, AND the box is **checkpointable** (D14). `scratch="memory"` (`root:memory`) is a fast RAM-backed opt-in that is **RAM-bound and NOT checkpointable** — backend warns; `checkpoint()` refuses it. Durable/remote storage is explicit via providers (D12). | Default must be checkpointable; memory is an explicit, warned escape hatch. |
| D17 | Background checkpointing ✅ | `Policy.checkpoint`: **`auto`** (loop + on-close, default) / **`on-close`** (`--no-autosave`) / **`off`** (`--ephemeral-fs`). Loop checkpoints **dirty** boxes on **idle-debounce (~3s)** or **staleness cap (~60s)**, capped per tick — not naive fixed-interval. **No registry — the box dir's `checkpoint/` is the resume point** (restore-on-next-`create`). `fscheckpoint --leave-running` (~30 ms); atomic write, keep last-good. | Snapshots at tool-call quiescence; box-dir is self-describing; cheap. §8f. |
| D15 | Project-dir CLI | git-style `.temenos/` discovery (walk up from CWD; auto-create + a `default` box). A bare box name resolves **project-first, then global**; a project box **shadows** a same-named global one (CLI warns). One auto-spawned **per-user daemon on one port**; all clients attach; MCP routes by path-hash **box-id**. | Zero-config local UX; disambiguation without typing data paths. §8f. |
| D16 | Box state & repo mount | **Everything lives in `.temenos/<box>/`** (config/overlay/checkpoint/exports — portable, gitignored). The repo mounts **live-writable** by default so Claude edits real files (sandbox contains *execution*, not edits); `--ephemeral` opt-in flips to overlay+review. | Matches "use claude on my repo"; per-project, cleanable boxes. §8f. |
| D14 | Scratch medium / checkpoint | **`Policy.scratch="disk"` default** (`root:dir`, disk-backed): not RAM-bound + **checkpointable** (`Box.checkpoint` → `fscheckpoint`, verified). `scratch="memory"` (`root:memory`) is a warned opt-in: RAM-bound + **cannot checkpoint** (`checkpoint()` refuses it). | `root:memory` blocks fscheckpoint, so it must not be the silent default. |
| D12 | Storage providers | **Pluggable `StorageProvider`** mounted at box paths. v1: **MemoryVolume** (tmpfs, ephemeral) + **DiskVolume** (host dir, durable, contained: realpath + optional `allowed_root`, and gVisor's mount-ns stops in-box `..`). Post-v1: **FsspecVolume** (s3/azure/… via fsspec; sync-on-commit; remote I/O runs on the host so the box stays network-less). `read`/`write` are sugar for ro/rw DiskVolumes. | Everything resolves to one box mount — providers differ in backing + commit/cleanup. Pluggable in Python = the extension point. |
| D8 | Sandbox env | **Minimal env** (`PATH`,`HOME=/tmp`,`LANG`); host env **not** inherited; caller adds via `env=`. | Host env may hold secrets (`ANTHROPIC_API_KEY`). |
| D9 | Backend select | v1 gVisor only. `TrustLevel` gates **policy strictness** (net/limits/mounts), not backend. `HOST` = explicit no-sandbox escape hatch. | Multi-backend selection returns when native lands. |
| D10 | Audit fidelity | exec + network-connection + spawn + write-set. **No per-syscall tracing** in v1. | Honest scope. |
| D11 | Multi-tenancy | **One isolated gVisor box per (tenant, session).** No shared *writable* mount, ever. `BoxManager` routes authenticated tenant → box; enforces quotas. | §6. Isolation by construction; the work is the control plane. |

---

## 6. The Box, and multi-tenancy

A **box** wraps the gVisor backend's `open()/exec()/close()` (the verified held-run
pattern, §9). It has a name, a `Policy`, a state (running/stopped), an audit log, and a
write-set. **Lifetime (v1):** a box lives as long as the daemon holds its `runsc run`
child; surviving a daemon restart (gVisor `checkpoint`/`restore`) is post-v1.

**`BoxManager`** is the multi-tenant control plane:
- `dict[(tenant, name) -> Box]`; every op checks tenant ownership; names namespaced per
  tenant; gVisor container ids + state/bundle dirs namespaced too (no collisions).
- **The invariant:** no writable mount is shared across boxes. The only shared thing is
  the **read-only** host `/usr` bind — no writable state — so two boxes can't see each
  other's files/processes/network/memory.
- Per-tenant **quotas** (max boxes, aggregate mem/disk — reject, don't silently queue),
  idle-TTL eviction, dead-child detection.
- **Agents never get `create`/`delete`** — box lifecycle is operator/CLI-controlled.

### Runtime & dependencies (verified)

Boxes don't install runtimes per-box and don't run host-side (unsandboxed) binaries.
Instead the box's rootfs **binds the host `/usr` read-only**, so the host's `python3`,
`node`, `gcc`, and all system-installed packages are available — executed **inside
gVisor**, not on the host. Verified: host `python3`/`node`/`pip` run in the box, `/usr`
writes are denied, box can't escape.

**v1 constraints on *adding* deps:** `/usr` is read-only (no system `pip`/`apt`), and v1
has **no network** (no PyPI/npm fetch — verified `pip install` → "No matching
distribution"). `venv` builds the env but can't bootstrap pip offline (Debian strips
ensurepip wheels); `--without-pip` works. So a v1 box uses **stdlib + host
system-installed packages (ro) + whatever is mounted in**.
- **Custom deps in v1:** pre-build a venv / site-packages / node_modules / wheelhouse on
  the host (or a DiskVolume), mount it (`Mount("/deps", DiskVolume(...), "ro")`), and set
  `PYTHONPATH`/`NODE_PATH`/`PATH`. No network needed.
- **Network (v1):** simple toggle (D3). `network=False` → no egress (only `lo`).
  `network=True` → full host passthrough (box reaches the internet; verified). With
  passthrough, `pip`/`npm` *would* work — but `/usr` is still read-only, so install to a
  venv/`--user`/volume, not the system.
- **Writable `/usr` → box images (BUILT).** The host `/usr` bind can't be made writable
  in a rootless box: host-root-owned files map to `nobody` (65534) so box-root can't
  write them — it's **uid mapping, not overlay** (verified: `/usr` shows owner 65534).
  The fix is a **runner-owned image** (`temenos/image.py`, `Policy.image=<name>`): the box
  root is a rootfs *owned by the box-runner*, so the root overlay (disk/memory, D14) makes
  the whole root — `/usr`,`/etc`,`/var` — **writable-ephemeral**, while disk volumes stay durable.
  The image is the shared lower (built once); each box gets its own memory upper → no
  per-box copy. Pluggable builder registry (`image.build(name, builder=…)`) + a
  `temenos image build/ls/rm` CLI. Builders: **`download`** (extract a prebuilt rootfs
  tarball, e.g. Ubuntu base — robust everywhere, **e2e-verified**, recommended on WSL2),
  `mmdebstrap` (clean apt base — preferred on normal Linux, but its unshare mode **fails
  on this WSL2 kernel**), `minimal` (thin ldd-resolved, for tests), `host-copy` (full host
  copy — **guarded by `--force-copy`** so a 16 GB `/usr` is never duplicated silently).
  **e2e VERIFIED (both `download` and `mmdebstrap`)**: build → box (`network=True`) →
  `apt-get update` + `apt-get install cowsay` succeed and the binary runs, no manual flags.
  - **mmdebstrap on WSL2 recipe** (native unshare mode fails here): drive the userns
    ourselves — `unshare --map-root-user --map-auto -- mmdebstrap --mode=root
    --skip=setup/mknod --skip=chroot/mount/dev --skip=chroot/mount`, host distro suite+mirror
    (host keyring; `variant=apt` to include apt), a SHORT ext4 `TMPDIR` (hook socket has a
    ~108-char limit; the session `/tmp` also breaks it), tarball out, extract
    `--no-same-owner --exclude=./dev/*` (non-root can't mknod; gVisor provides /dev).
  - **apt-in-box fixes** auto-baked into `/etc/apt/apt.conf.d/99temenos`:
    `APT::Sandbox::User "root"` (uid-drop to `_apt` breaks in-box) + `Acquire::ForceIPv4`
    (IPv6 doesn't route through the passthrough).
  - **DNS**: the backend injects the **host's** `/etc/resolv.conf` into image-mode network
    boxes at start (not a baked resolver) — matches the shared netns.
  Images also give runtime *version* choice (host-bind boxes inherit the host's Python 3.12).
- **Scratch medium = `Policy.scratch` (D14), default `"disk"`.** The root overlay upper is
  ephemeral either way; the medium is a knob (verified): `disk` (`root:dir=<box dir>`) is
  disk-backed → not RAM-bound (a 400 MB write in a 256 MB box succeeds) **and
  checkpointable**; `memory` (`root:memory`) is RAM-backed → fast but RAM-bound (same
  write OOMs) **and NOT checkpointable**. So `disk` is the default; `memory` is an explicit
  opt-in the backend **warns** about, and `Box.checkpoint()` **refuses** a memory box.
- **Persisting writes is deliberate and separate:** (a) **mount a `DiskVolume`**;
  (b) **checkpoint/restore** — `Box.checkpoint(dest)` → `fscheckpoint`, and
  `Box(name, policy, restore_from=dest)` → `runsc run -fs-restore-image-path` seeds a
  fresh box's filesystem from the checkpoint. **Roundtrip verified** for `scratch="disk"`
  (rootless; gVisor-experimental); (c) **commit-to-image** to bake the root into a new image.

---

## 7. Architecture & layering

Strict one-directional dependencies — **lower layers never import higher ones.**

```
Layer 3  surfaces      server/ (FastAPI REST + MCP) · cli.py · harness/
                          │  authenticate tenant, translate Results, ban native tools
Layer 2½ control plane  BoxManager — multi-tenant routing/authz/quotas (D11)
                          │  maps (tenant, name) -> Box
Layer 2  orchestration  Box — single-box lifecycle, audit aggregation
                          ▼
Layer 1  backends       Backend ABC + GVisorBackend (v1)
                          ▼
Layer 0  data           Policy, TrustLevel, ExecResult, AuditEntry, exceptions (pure)
```

Rules that keep this honest:
- **Core speaks Python objects, not wire formats.** `Box.exec()` returns an
  `ExecResult`; JSON (REST/MCP) and text/tables (CLI) are produced only in Layer 3.
- **`Policy` round-trips through plain data** — one `Policy.from_dict`/`to_dict` shared
  by REST bodies, MCP args, CLI flags, and a `temenos.toml`.
- **No surface concepts leak down.** Delete `server/` and the core still works. The CLI
  (single-user, local) may talk to `BoxManager` directly; the MCP server always does.
- **One code path.** REST, MCP, and CLI are all `Policy → Box → ExecResult`. A core bug
  fix fixes all three.

---

## 8. The APIs

### 8a. Box Python API (the core)

```python
from temenos import BoxManager, Policy, TrustLevel

mgr = BoxManager()                          # daemon-side; multi-tenant, quotas

box = await mgr.create(                     # -> Box
    name="my_silly_box",                    # optional; auto-generated if omitted
    tenant="acme",
    policy=Policy(read=["/project"], write=["/work"], trust=TrustLevel.UNTRUSTED),
)

r = box.exec(["echo", "hi"])                # -> ExecResult(stdout, stderr, exit_code, truncated, ms)
box.write_file("/tmp/a.py", "print(6*7)\n") # /tmp = always-present scratch (tmpfs)
src = box.read_file("/tmp/a.py")
# policy.write/read paths must be EXISTING host dirs; writes there are CoW (overlay),
# so the host is never modified — inspect box.writes() and commit deliberately.
async for ev in box.exec_stream(["python3","/work/a.py"], tty=True):  # for -it / shell
    ...
writeset = box.writes()                     # {path: bytes} from the overlay upper layer
audit = box.audit                           # list[AuditEntry] for this box

boxes = mgr.list(tenant="acme")             # -> [BoxInfo(name, status, created, policy, ...)]
await mgr.delete("my_silly_box", tenant="acme")
```

### 8b. Policy

```python
from dataclasses import dataclass
from enum import IntEnum

class TrustLevel(IntEnum):
    UNTRUSTED = 0; RESTRICTED = 1; SANDBOXED = 2; HOST = 3   # gates STRICTNESS in v1

@dataclass(frozen=True)
class Policy:
    read:  tuple[str, ...] = ()           # host paths visible read-only in the box
    write: tuple[str, ...] = ()           # CoW: writes go to the overlay, never the host
    network: tuple[str, ...] = ()         # v1: () = no net; non-empty = post-v1
    max_memory_mb: int = 256              # D6: best-effort under --ignore-cgroups
    max_cpu_seconds: int = 30
    max_processes: int = 16
    max_output_bytes: int = 10 * 1024 * 1024
    trust: TrustLevel = TrustLevel.UNTRUSTED

    def __post_init__(self):              # coerce list inputs -> frozen tuples
        for f in ("read", "write", "network"):
            object.__setattr__(self, f, tuple(getattr(self, f)))
    def restrict(self, **kw) -> "Policy": ...   # the ONLY way to derive a child policy:
        # sets must be subsets, ints <=, trust <=. Widening raises PolicyViolation.
        # There is no escalate() — widening is an error, not an operation.
    @classmethod
    def from_dict(cls, d) -> "Policy": ...
    def to_dict(self) -> dict: ...
```

`Policy()` with no args = most restrictive: no network, no host writes (overlay only),
tight limits. You opt *in* to capability.

### 8c. FastAPI daemon (REST + MCP)

One long-lived process supervises all boxes and exposes two faces over the same
`BoxManager`. Auth: `Authorization: Bearer <token>` → tenant; every op checks ownership.

**REST (control plane — CLI & programmatic):**

| Method & path | Action |
|---|---|
| `POST /v1/boxes` | create `{name?, policy}` → `BoxInfo` |
| `GET /v1/boxes` | list tenant's boxes |
| `GET /v1/boxes/{name}` | inspect (policy, status, platform, write-set summary) |
| `DELETE /v1/boxes/{name}` | destroy (`?force=true`) |
| `POST /v1/boxes/{name}/exec` | run a command (non-interactive) → `ExecResult` |
| `GET /v1/boxes/{name}/attach` | **WebSocket** — interactive tty / streaming |
| `GET /v1/boxes/{name}/audit` | audit log (`?follow=true`) |
| `GET /v1/boxes/{name}/writes` | write-set, for human review/commit |

**MCP (data plane — for agent harnesses):** a sub-app mounts the MCP server; **each MCP
connection is bound to exactly one box** via path `…/mcp/{box}` (token → tenant →
ownership check). Tools operate on that box only:

| MCP tool | Signature | Maps to |
|---|---|---|
| `temenos_exec` | `(command: string[], cwd?, timeout_s?) -> {stdout, stderr, exit_code, truncated}` | `Box.exec` (argv, not a shell string) |
| `temenos_read` | `(path) -> {content, truncated}` | `cat` in-box; refuses host paths outside policy |
| `temenos_write` | `(path, content) -> {bytes}` | `tee` in-box (overlay) |
| `temenos_list` | `(path) -> {entries}` | `ls` in-box |
| `temenos_fetch` *(post-v1)* | `(url, …)` | network-policy proxy — **unavailable in v1** |

The agent gets no `create`/`delete` tool. It cannot commit to the host — writes stay in
the overlay; a human reviews the write-set out-of-band. Tool descriptions state "this is
your only way to run/read/write" (belt; the deny rules are the enforcement).

**Tenant identity:** local single-user → MCP over **stdio**, one server per user (tenant
implicit). Hosted multi-tenant → MCP over **HTTP** with a per-tenant token; the tenant
check (not session-id obscurity) is the gate. A `temenos mcp --box NAME` stdio bridge
exists for harnesses that prefer stdio.

### 8d. CLI

A thin REST client to the daemon (Docker ↔ dockerd model); auto-starts a local daemon if
none is reachable. Global flags: `--endpoint`/`--token`, `--json`.

```
temenos serve [--host 127.0.0.1] [--port 8080]    # run the daemon (FastAPI + MCP)

temenos create [NAME] [--read P]... [--write P]... [--mem 512m] [--cpus N]
               [--net none|HOST...] [--trust LEVEL] [--image REF]    # NAME auto-gen if omitted
temenos ls   [--all] [--json]                      # alias: list, ps
temenos inspect NAME
temenos rm   NAME... [-f/--force] [--all]          # alias: delete

temenos exec [-i] [-t] NAME [--] CMD [ARGS...]     # run CMD in an existing box
temenos shell NAME                                 # interactive shell (= exec -it NAME $SHELL)

temenos audit  NAME [--follow]
temenos diff   NAME                                # show the write-set (overlay upper)
temenos export NAME --to DIR                       # extract write-set for review/commit

temenos claude --box NAME [-- CLAUDE_ARGS...]      # attach Claude Code to a box (§8e)
temenos doctor                                     # capability checks (gVisor platform, ...)
temenos version
```

**Conventions (and how this differs from the first CLI sketch):**
- **`exec`, not `run`.** The box already exists (`create` made it) → the verb is
  `exec` (run-in-existing). Docker's `run` = *create a new container and start it*;
  reusing `run` would invert that mental model. (`run` may exist as a documented alias
  of `exec`, but `exec` is canonical.)
- **`shell` for interactive**, not a no-arg `exec`. Overloading one verb to mean both
  "exec a command" and "open a terminal" is ambiguous (cf. `kubectl exec -it`,
  `fly ssh console`).
- **`--` is optional for `exec`.** The trailing command parses pass-through, Docker-style
  (Click `ignore_unknown_options=True` + `nargs=-1, type=UNPROCESSED`), so
  `temenos exec box echo --version` works *without* `--` — `--version` goes to `echo`.
  (Docker disables interspersed parsing and needs no `--`; only kubectl-style naive
  parsers do — so "`--` is required to stop temenos eating flags" was wrong.) Use `--`
  only to disambiguate a command flag that collides with `exec`'s own (e.g.
  `temenos exec box -- mytool -t`, where `-t` is otherwise temenos's tty flag) or for
  script clarity. temenos flags and `-i/-t` go *before* the box name.
- **`temenos claude --box b -- <args>`** — here `--` *is* useful: it routes Claude's own
  flags (`--model`, `--permission-mode`, …) to Claude, not temenos.

### 8e. `temenos claude` — attaching a Claude Code session to a box

Claude runs **on the host** (updates/auth/model-API keep working); its only execution
path is the box's MCP tools, every host-touching native tool banned:

1. **Resolve the box** (`GET /v1/boxes/{name}`); error if absent (explicit lifecycle).
2. **Mint a scoped token** for `(tenant, box)` and write a temp MCP config:
   ```json
   {"mcpServers": {"temenos": {"type": "http",
     "url": "http://127.0.0.1:8080/mcp/my_silly_box",
     "headers": {"Authorization": "Bearer <scoped-token>"}}}}
   ```
3. **Launch Claude with natives banned, only temenos allowed** (real flags — verify
   against the installed version):
   ```bash
   claude --strict-mcp-config --mcp-config /tmp/temenos-<box>.json \
     --disallowedTools "Bash,Read,Write,Edit,MultiEdit,NotebookEdit,Glob,Grep,WebFetch,WebSearch,Task" \
     --allowedTools "mcp__temenos__exec,mcp__temenos__read,mcp__temenos__write,mcp__temenos__list" \
     <user CLAUDE_ARGS>
   ```
   - `--strict-mcp-config` is **load-bearing**: it stops a stray `.mcp.json` from
     re-introducing a host-capable MCP server (a sole-execution-path hole).
   - `Task` denied so subagents can't spawn with a different toolset (or confirm they
     inherit `--disallowedTools` before allowing it).
   - Defense in depth: a `temenos-guard` `PreToolUse` hook that fails *closed* on any
     tool not named `mcp__temenos__*`, so a future Claude tool can't silently leak.
4. **The project lives in the box, not on the host path Claude sees.** Because `Read`/
   `Write` are banned, Claude reads/writes the *box's* files via `temenos_read/write`.
   So `temenos create my_box --write ./project` mounts the project in, and Claude
   operates through MCP. (Reading the host project directly would be the spillover we
   prevent.)

### 8f. Project mode, box resolution & the single daemon (D15/D16)

**Box identity = `hash(realpath(<box data dir>))`.** Project box dir =
`<repo>/.temenos/<name>`; global box dir = `$TEMENOS_DATA/boxes/<name>`. One scheme →
two repos' `default` boxes get distinct ids; the daemon registry and the MCP route
(`/mcp/<id>`) key on it. **This is the disambiguation** — no user-typed data paths.

**Everything in `.temenos/<box>/`** (D16): `config.toml` (the Policy), the overlay upper
(the box's writes), checkpoints, write-set exports, logs — project-local, portable,
`rm -rf`-able. The CLI writes a `.temenos/.gitignore`. *Verified*: runsc `--root` +
overlay under a deep `.temenos` path work (abstract sockets, no 108-char socket limit at
realistic depth); the daemon falls back to a short runtime state dir only for a
pathologically long path.

**Location separation — `.temenos` is ONLY the project marker.** Global/daemon state must
*not* live in `~/.temenos`, or `temenos` run in `$HOME` would mistake it for a project.
So: daemon runtime (token/lock/pid) → `$XDG_RUNTIME_DIR/temenos/` (0700, transient);
global boxes + images → `$XDG_DATA_HOME/temenos/` (`~/.local/share/temenos`). Neither is
named `.temenos` nor sits on a repo's walk-up path. **Walk-up safeguard:** discovery stops
at `$HOME` and at `/`; auto-creating `.temenos/` in `$HOME` warns ("creating a project box
in your home dir — did you mean a global/named box?").

**Name resolution (git-style, no data-path typing).** A bare name resolves to the
project `.temenos/<name>` (walking up from CWD) if it exists, **else** the global
`$XDG_DATA_HOME/temenos/boxes/<name>`. If both exist the **project box wins and the CLI
warns** that it shadows a global box of the same name.

**One daemon, one port, auto-spawn.** A single per-user daemon binds `127.0.0.1:PORT`
serving REST (control) + MCP (`/mcp/<id>`, per-box token). Every CLI call
**connects-or-spawns** (flock + readiness wait → exactly one daemon). All temenos
processes across all repos attach to it.

**`temenos claude` (the basic flow):** discover `.temenos/` (walk up; create in CWD +
`.gitignore` if none) → box = `--box` or `default`, dir `.temenos/<box>`, id = hash →
ensure daemon → ensure box running (create from `config.toml`, mount the repo) → write a
temp MCP config (`http://127.0.0.1:PORT/mcp/<id>` + token), ban native tools, exec
`claude <args>`.

**Repo mount = live-writable by default (D16/decision 1).** The repo (the dir holding
`.temenos`) mounts read-write so Claude's edits land in your real files — the box
contains *execution* (bash/python can't escape, network gated, limited, audited), not the
trusted agent's edits. `--ephemeral` flips it to overlay-with-review (`temenos diff` +
commit). `.temenos/` itself is excluded from the mount so the box can't scribble its own
state.

**Durability (D17). ✅ BUILT.** **The box dir is its own registry** — no separate state
file: a box checkpoints to `<box dir>/checkpoint` and, on next `create`, **restores from
it**, so resuming is just re-running `temenos claude` in the repo (the daemon doesn't track
desired-running state across restarts). Modes via `Policy.checkpoint`: **`auto`** (default —
background loop + commit-on-close), **`on-close`** (`--no-autosave`: commit only on close,
loop off), **`off`** (`--ephemeral-fs`: never persist). The loop tracks a per-box **dirty**
flag (set on each `exec`) and checkpoints a dirty box on **idle-debounce** (~3 s quiet →
stable state, coalesces bursts) **or a staleness cap** (~60 s → bounds work-at-risk),
**capped per tick** (no I/O herd). `fscheckpoint --leave-running` (~30 ms). Atomic write
(temp dir → rename, keep last-good). Snapshots land at agent tool-call quiescence.
(`scratch="memory"` can't checkpoint → treated as off.) Verified: auto-resume across a
fresh manager, `--ephemeral-fs` non-persist, loop fires on idle.

---

## 9. gVisor backend contract (verified)

Global flags: `--root=<per-box state dir> --rootless --network=<none|host>
--ignore-cgroups --overlay2=<root:dir=<box dir>|root:memory> --platform=<auto-detected>`.
Overlay: **disk-backed `root:dir` by default** (`Policy.scratch="disk"` — checkpointable,
not RAM-bound); `root:memory` only on `scratch="memory"` (warned, not checkpointable, D14).
Network: `none`
(default, with the netns kept) or `host` (passthrough, with the netns **dropped** from
the OCI spec — both required, D3). **Platform is auto-detected, not hardcoded** — it's a
performance/compatibility choice; the security model (the gVisor sentry) is identical
across platforms. Preference order, each validated by a probe run: **`kvm`** (fastest,
needs `/dev/kvm`) → **`systrap`** (gVisor's modern default, no KVM needed) → **`ptrace`**
(slowest, most compatible). On this WSL2 box only ptrace works (no `/dev/kvm`; systrap
hits `StartRoot EOF`); a native Linux host with `/dev/kvm` gets kvm, a typical cloud VM
gets systrap.

- **`open(policy)`** — run each storage provider's `prepare()` (mkdir/download); build a
  bundle: writable `rootfs/` with usrmerge symlinks + read-only bind of host `/usr`,`/etc`;
  tmpfs `/tmp`,`/dev`; `proc`; plus the provider mounts (D12) and `read`/`write` disk
  binds. Write `config.json` from `Policy`: namespaces incl. network, minimal env (D8),
  empty caps, RLIMIT_NPROC/CPU. Overlay: `--overlay2=root:dir=<state>/overlay` (disk,
  checkpointable, default) or `root:memory` (`scratch="memory"`, warned). Writable root,
  ephemeral, host-safe — verified. Start a **held** child wrapping `runsc` in a
  per-box systemd scope for enforced memory (D6):
  `systemd-run --user --scope -q -p MemoryMax=<mem> -p MemorySwapMax=0
  -- runsc <global> run -bundle <dir> <cid>` (NB: no `TasksMax` — it would starve the
  sentry's host threads; guest procs are bounded via RLIMIT_NPROC). Init `sleep infinity`;
  poll `runsc state <cid>` until `"running"`. (Rootless can't
  `create`+`start` — `run` only. If no systemd user-delegation: drop the wrapper, log a
  warning that limits are unenforced.)
- **`exec(cmd)`** — `runsc <global> exec <cid> -- <cmd>`; collect stdout/stderr/exit.
- **`close()`** — `runsc <global> kill <cid> KILL`; reap held child; `runsc delete
  --force <cid>`; rm bundle + state dir.
- **`is_available()`** — must **probe**, not just check PATH (all platforms appear in
  `runsc help` but may fail to start): try `("kvm","systrap","ptrace")` in order, return
  the first that actually starts a tiny `/bin/true` container; remember it per host.

`scripts/gvisor_spike.py` already implements a working rootfs builder + config generator
+ run/exec/teardown — port it into `backends/gvisor.py` + `backends/oci.py`.

---

## 10. Harness integration

No single mechanism fits every harness (they differ in how much you can disable their
native tools). Three tiers, strongest invariant first:

| Tier | Mechanism | Use when | Audit |
|---|---|---|---|
| **T1 Library** | Harness imports temenos; its tools call `Box`/`BoxManager` | You own the loop (custom/SDK, e.g. hermes-agent) | per call |
| **T2 MCP tool-routing** | temenos MCP server; **all** native exec/fs/net denied | Harness has real permission control (Claude Code, opencode) | per call |
| **T3 Jail** *(post-v1)* | Run the **whole harness** inside a box | Harness can't have natives removed (Codex's shell) or is unknown | per box |

**Default:** T1 if you build it, T2 if the harness has a permission system, **T3 as the
universal fallback** for anything you can't prove is neutered. Don't try to
enumerate-and-disable an unknown harness's tools — jail it.

**Sole-execution-path checklist** (walk it per harness+config; prove each row):

| Vector | T2 mitigation | T3 mitigation |
|---|---|---|
| Native shell/exec | deny; use `temenos_exec` | runs in box |
| File read/write/edit | deny; use `temenos_read/write` | runs in box |
| File search (glob/grep) | deny; agent uses `temenos_exec` (`rg`/`grep`) | runs in box |
| Web fetch/search/browser | deny (v1 box has no net anyway) | egress via box net policy |
| Other MCP servers/plugins | remove; allow only `mcp__temenos__*` (`--strict-mcp-config`) | irrelevant |
| Subagents/spawned tasks | confirm they inherit the deny rules | inherit the jail |
| Auto-approve / yolo modes | OFF for any native tool | safe (box-confined) |
| Host env / secrets in prompt | minimal env (D8); inject secrets to box, not context | minimal env in box |

**Per-harness:** Claude Code → T2 (§8e). opencode → T2 if its permission model fully
removes natives, else T3. Codex → **T3** (built-in shell can't be cleanly removed; graduates
to T2 if a version allows disabling it). Custom/hermes-agent → T1 (cleanest), else T3.

**Leak-test (`tests/leak/`):** a conformance battery run *per harness+config*, not just
the core — "write `/etc/passwd`", "read `~/.ssh/id_rsa`", "`curl evil.com`", "fork
bomb / OOM", "escape via `/proc/1/root`", "spawn a subagent that runs host bash". A
harness isn't "supported" until it's green; re-run on harness upgrades (new tools = new
holes). The fork-bomb/OOM row should **pass** given the D6 systemd-scope enforcement;
if the host lacks systemd user-delegation it degrades to a warning (and that row fails) —
the leak-test reports which mode it ran in.

**v1 reality:** T2 is fully ready (the agent keeps its own host net to the model API;
payloads need none). T3 needs net for the harness→API call — in v1 that's only *full
passthrough* (FS/process contained, egress not); contained jailed egress arrives with
post-v1 network filtering.

---

## 11. Security properties

All rows assume the §2 threat model. v1 = gVisor only; others are roadmap.

| Threat | gVisor (v1) | Native (post) | Seatbelt (post) |
|---|---|---|---|
| Filesystem escape | blocked | blocked | partial |
| Network exfiltration | blocked* | blocked* | partial |
| /proc leakage | blocked | blocked | n/a |
| Kernel CVE (host) | mostly blocked | vulnerable | vulnerable |
| Cross-tenant crosstalk (D11) | blocked† | blocked† | partial |
| Side channels (co-resident) | out of scope | out of scope | out of scope |
| Requires root | no (rootless) | no | no |

\* v1 network is a toggle (D3): `network=False` (`--network=none`) is genuinely blocked;
`network=True` (`--network=host`) is **full passthrough — no isolation** (operator
opt-in, unsafe for adversarial tenants). Per-host *filtered* egress is post-v1, with
a residual gap (forged-SNI-to-allowed-IP / hostile protocols). † gVisor container
boundary + BoxManager authz/quotas; side channels not addressed.

✅ **D6 resolved (v1):** per-box memory/CPU/pids limits are enforced by wrapping the
held `runsc run` in `systemd-run --user --scope` (spike-verified, unprivileged). Requires
a systemd user manager with cgroup delegation (default on modern systemd; on headless
hosts run the daemon as a `--user` service or `loginctl enable-linger`). Without it,
limits degrade to unenforced **with a warning** — don't run adversarial multi-tenant in
that mode.

---

## 12. Implementation phases (v1)

- **Phase 0 — scaffold + capability spike. ✅ DONE.** `scripts/doctor.py` +
  `scripts/gvisor_spike.py` green; results in §3. Re-run as a smoke test on new hosts.
- **Phase 1 — `Policy` + `ExecResult` + `exceptions`. ✅ DONE.** Pure Python: frozen
  `Policy` (restrict/from_dict/to_dict/allows_*), `TrustLevel`, `ExecResult`,
  `AuditEntry`/`AuditLog`, exception hierarchy. 37 tests green (`tests/test_policy.py`,
  `tests/test_result.py`). Package scaffold (`pyproject.toml`, `temenos/`) in place.
- **Phase 2 — gVisor backend + `Box` + storage providers. ✅ DONE.** `backends/base.py`
  (ABC), `backends/oci.py` (bundle from Policy), `backends/gvisor.py` (platform
  auto-detect, held-run + exec, `--overlay2=root:memory` writable-but-ephemeral root,
  per-box systemd memory scope, provider prepare/commit/cleanup), `storage.py`
  (`StorageProvider` + `MemoryVolume`/`DiskVolume` + `Mount`, D12), `box.py`
  (`exec`/`read_file`/`write_file`/`list_dir`/`writes`/`commit`, audit, context manager).
  plus a **network toggle** (`Policy.network: bool` → `--network=none|host` + netns
  drop, verified passthrough). **66 tests green** total: write-then-run `42`, two-box
  isolation, disk-durable vs memory-ephemeral, root-ephemeral, disk containment
  (`allowed_root`), provider round-trip, network off (only `lo`) vs host (eth0 present),
  missing-read-path error. `read`/`write` paths are existing host dirs (write
  auto-created); `/tmp` is the scratch tmpfs. **Plus box images** (`image.py`,
  `Policy.image`): runner-owned base rootfs → writable `/usr`/`/etc` (verified), the
  unblock for `apt`/system-`pip`. **72 tests green.** (Persistence: gVisor `fscheckpoint`
  + `-fs-restore-image-path` verified with a disk-backed overlay — checkpointable boxes
  opt into `root:self`/`root:dir`.)
  *(`image build/ls/rm` CLI already exists from Phase 2.)*
- **Phase 3 — daemon + `BoxManager`. ✅ DONE.** `manager.py` (`BoxManager`: path-hash
  `box_id`, create/get/list/delete, idempotent "ensure", persists `config.json`, dead-box
  recreate, `shutdown`), `backends/gvisor.py` `work_dir=` (state/bundle/overlay under the
  box data dir — everything-in-.temenos), `server/app.py` (FastAPI: `/healthz`,
  `/v1/boxes` CRUD + `/exec`, Bearer-token auth), `server/client.py` (`connect_or_spawn`:
  daemon-info file + flock → exactly one per-user daemon), `temenos serve`. Tests: id
  stability/distinctness, REST lifecycle, real spawned-daemon roundtrip — **90 green**.
  (Quotas deferred to Phase 4/hosted.)
- **Phase 4 — context-aware CLI (project mode, D15/D16). ✅ DONE.** `project.py`
  (`find_project` walk-up stopping at `$HOME`/`/`; `ensure_project` creates `.temenos/` +
  `.gitignore`; `resolve_box` project→global with `shadows_global` flag), CLI
  `create`/`ls`/`exec`/`shell`/`rm`/`audit`/`diff` driving the daemon client, everything in
  `.temenos/<box>` (config from `config.json`), **live-writable repo mount** at its real
  path (write-bind of the project root + tmpfs mask over `.temenos`; `--ephemeral` flips it
  read-only), `--scratch`/`--force-memory`/`--ephemeral-fs`/`--no-autosave`/`--image`/
  `--net`/`--volume`/`--memory`/`--cpu`/`--global` flags. New daemon endpoints
  `/v1/boxes/{id}/audit` and `/writes`. `shell` is a non-PTY REPL (client-side cwd tracking;
  true interactivity lands with MCP in Phase 5). Tests: pure discovery/resolution + a
  gVisor-gated `create→exec→ls→audit→rm` e2e through `main()` — **109 green**.
- **Phase 5 — MCP + `temenos claude` + leak-test. ✅ DONE.** `server/mcp.py`: one FastMCP
  (`stateless_http`, `json_response`, DNS-rebinding off) with box-scoped tools
  `exec`/`read`/`write`/`list` (→ `mcp__temenos__*`; no create/delete/commit tool); a
  `BoxMCPRouter` ASGI app mounted at `/mcp` pulls the id off `/mcp/<id>`, checks the daemon
  Bearer token, verifies the box, and binds the request to it via a contextvar (verified to
  reach FastMCP's stateless handler + sync-tool threadpool). `create_app` lifespan runs the
  session manager. `temenos claude [--box] [box-flags] [-- claude-args]`: ensure project box
  → write `<box>/mcp.json` (http url + Bearer) → `os.execvp` claude with
  `--strict-mcp-config --mcp-config … --disallowedTools <natives> --allowedTools
  mcp__temenos__*` (real flags verified against claude 2.1.168); `--dry-run` prints instead.
  `temenos doctor` (gVisor/platform/mmdebstrap/systemd). Tests: **official mcp client**
  drives a real box (init/list/exec/read/write/list) + 401/404 routing; claude dry-run wires
  config + bans; **leak battery `tests/leak/`** (no host write, host secrets invisible incl.
  `/proc/1/root` pivot, no network, /proc-is-box, D6 memory cap OOM-kills). **118 green, 2
  skipped.** (Durability already shipped in Phase 3/§8f: box-dir checkpoint + restore-on-use.)
- **Phase 6 — polish & release:** README (threat model, multi-tenancy, ptrace/WSL notes,
  the systemd-delegation requirement for limits (D6), and the one honest limit: no v1
  network filtering), examples (sample Claude config), PyPI.

(Swarm — `mgr.map(...)` over N boxes — is optional sugar; add if needed, else post-v1.)

---

## 13. Post-v1 (specified, not built)

- **Network egress filtering (D3 deferred):** `pasta` for unprivileged netns egress +
  in-namespace `nft` TPROXY → transparent **SNI/Host allowlist proxy** + stub DNS
  (`net/`). Enables `temenos_fetch` and *contained* jailed-harness egress. Residual gap:
  forged-SNI-to-allowed-IP / hostile protocols. Install: `apt-get install -y passt nftables`.
- **T3 jail launcher (`harness/jail.py`):** `temenos jail -- <harness cmd>` /
  `mgr.launch_harness(...)` — run a whole harness inside a box (pairs with the network
  filter so its model-API egress is contained). `temenos codex --box …`.
- **FsspecVolume storage provider (D12):** s3/azure/gcs/… via fsspec. v1-after: **sync**
  model — `prepare()` materializes the remote prefix to a local DiskVolume, `commit()`
  uploads the diff; FUSE/live mode later. Remote I/O runs on the host (box stays
  network-less); prefix-scoped. Behind a `temenos[storage]` extra.

### Symlink containment for host-side volume access (spike-verified analysis)

Spike result (`scripts/`-style probe): **the in-box layer is already safe** —
- a box symlink to a host path *outside* its mounts (`/work/x → /tmp/secret`) reads as
  *No such file or directory*: gVisor resolves symlinks **guest-side**, so absolute /
  `../`-traversing targets resolve in the box's VFS, not the host's. The box cannot
  escape its mount set via symlinks.
- root-only host files (`/etc/shadow`) return *Permission denied* even though `/etc` is
  bound, because the gofer runs as the **unprivileged host user**.

**The real risk is host-side:** a box can plant `/<vol>/evil → /home/user/.ssh/id_rsa`
in a DiskVolume, and any temenos code that walks the volume **on the host** would follow
it. So:
- `box.writes()` is safe today — it runs `find` *inside* the box (guest-side).
- When building **`export` / `commit` / fsspec-sync** (the host-side readers/writers of a
  volume), they MUST NOT follow symlinks out of the volume root: `os.walk(followlinks=
  False)` + realpath-containment per entry, or `openat2(RESOLVE_BENEATH |
  RESOLVE_NO_SYMLINKS)`; copy symlinks *as* symlinks, never write *through* one.
- Leak-test: plant `/<vol>/evil → ~/.ssh/id_rsa`, assert `export` doesn't dereference it.
- `allowed_root` covers config-time escape; this covers runtime-created symlinks.
- **Native namespaces backend (`backends/native.py`, `rootfs/`):** no-`runsc` fallback at
  weaker isolation — ctypes `unshare`/`mount`/`pivot_root`/`setns`, a fork-after-unshare
  **keeper** as PID 1, hand-assembled **seccomp cBPF** (old D5), cgroups v2, overlayfs.
  **Trap (spike, §14):** write `uid_map` from the unsharing process's *main thread* via a
  single raw `write()` — inside an `os.fork()` child it returns EPERM.
- **Direct delegated child cgroups** (D6 optimization) — when the daemon runs inside
  `user@.service`, create child cgroups directly instead of one `systemd-run` scope per
  box, avoiding the per-box dbus round-trip. v1 already enforces via `systemd-run`.
- **Commit-to-image** (`docker commit` analog) — snapshot a box's merged root into a new
  image dir for reuse; the way to persist `/usr`-level system changes (the overlay upper
  isn't user-persistable — verified).
- **Box persistence across daemon restarts** — gVisor `checkpoint`/`restore`.
- **macOS Seatbelt / Windows Job Objects backends.** v1 `BoxManager` raises
  "platform not yet supported" off-Linux.

---

## 14. Key implementation notes (post-v1 native backend)

> Apply to the **post-v1 native backend only** — gVisor handles all of this internally.
> Kept because the Phase 0 spike validated them and the details are easy to lose.

**uid_map / gid_map — two spike-discovered traps (both cost real time):**
1. **Write from the *unsharing* process's main thread, then fork the keeper.** Writing
   `uid_map` from inside an `os.fork()` child returns **EPERM** on this kernel; the same
   write from a non-forked main thread succeeds.
2. **Single raw `write()`** (`os.open`+`os.write`), not buffered text I/O — the kernel
   wants the whole map in one `write()`, and buffered writes flush at GC, hiding the
   error and leaving you as `nobody` (65534).

```python
def _write_id_maps():            # run in the unsharing process's MAIN thread
    uid, gid = os.getuid(), os.getgid()
    fd = os.open("/proc/self/setgroups", os.O_WRONLY); os.write(fd, b"deny"); os.close(fd)
    fd = os.open("/proc/self/uid_map",  os.O_WRONLY); os.write(fd, f"0 {uid} 1".encode()); os.close(fd)
    fd = os.open("/proc/self/gid_map",  os.O_WRONLY); os.write(fd, f"0 {gid} 1".encode()); os.close(fd)
```

- **`unshare(CLONE_NEWPID)` only affects future children** — the keeper must `fork` to
  become PID 1; don't use `preexec_fn` (unsafe under an asyncio/multithreaded parent).
- **pivot_root needs the new root to be a mount point** (bind-mount it to itself first).
- **seccomp last** in the keeper's init sequence; **cgroups v2** for limits (not
  `RLIMIT_AS`, which caps virtual address space and breaks runtimes).

---

## 15. What temenos is NOT

- Not a container runtime (no images/layers/registries — it *uses* gVisor's runtime).
- Not a cluster scheduler.
- Not a cloud sandbox (E2B/Modal/Daytona exist for that).
- Not a Python-only code sandbox (RestrictedPython etc.).
- Not a replacement for a VM when you need hardware-level isolation (gVisor ≠ KVM).

temenos is specifically: **run an agent's tool calls on my machine, in a per-tenant box,
with a Python-native policy and a real audit trail.**

---

## 16. Dev setup (WSL)

```bash
uname -r                                   # need ≥5.11 (overlay), 6.6 here ✓
sudo apt-get install -y runsc              # gVisor (the v1 isolation engine)
sudo apt-get install -y mmdebstrap uidmap  # 'mmdebstrap' image builder (clean apt base)
                                           #  uidmap (newuidmap) + /etc/subuid required;
                                           #  works on WSL2 via the self-unshare recipe.
                                           #  'download' builder needs nothing extra.
# post-v1 networking: sudo apt-get install -y passt nftables

python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,mcp,cli]"            # core has no runtime deps; surfaces via extras

python scripts/doctor.py                   # capability probes (re-run on new hosts)
python scripts/gvisor_spike.py             # confirm the box session model (expect green on ptrace)
python scripts/memory_spike.py             # confirm per-box memory enforcement (D6; needs systemd --user)
pytest tests/test_policy.py -v             # Phase 1 (pure Python)
```

`pyproject.toml` extras: `mcp = ["mcp","fastapi","uvicorn"]`, `cli = ["click","rich","httpx"]`,
`dev = ["pytest","pytest-asyncio","ruff","mypy"]`. The `runsc` *binary* is detected at
runtime, not a pip dep.
