<div align="center">

# temenos

**A secure runtime for AI agents.** 🏛️

*Your agent runs on the host — the code it executes runs in a gVisor box.*

[![PyPI](https://img.shields.io/pypi/v/temenos.svg)](https://pypi.org/project/temenos/)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://github.com/farizrahman4u/temenos/blob/main/LICENSE)
[![Built on gVisor + MCP](https://img.shields.io/badge/built%20on-gVisor%20%2B%20MCP-8a2be2.svg)](#-how-it-works)
[![leak-tested](https://img.shields.io/badge/containment-leak--tested-2ea44f.svg)](#-threat-model--honest-limits)

</div>

```bash
cd ~/code/my-repo
temenos claude        # Claude runs on the host; everything it *executes* runs in a box
```

That one command keeps Claude Code where it works best — on the host, with its auth,
updates, and model API intact — while **banning every native host-touching tool** (`Bash`,
`Read`, `Write`, `Edit`, `WebFetch`, …) and routing its only execution path through a
**box**: a rootless [gVisor](https://gvisor.dev) sandbox with a small, Python-native policy.

A shell that tries to `rm -rf ~`, read `~/.ssh/id_rsa`, or `curl evil.com` is contained —
not because the model promised to behave, but because the sandbox boundary won't let it. The
agent is trusted; the *code it runs* is not. 🛡️

**That's one box. temenos is built to run a thousand.** The same engine that wraps a single
agent on your laptop is a multi-box control plane — the point isn't sandboxing *one* helpful
assistant, it's containment for when **human approval stops scaling** (see ["one box → a
swarm → a platform"](#-one-box--a-swarm--a-platform) below).

> *temenos* (τέμενος): a bounded precinct — a space set apart with a clear edge.

---

## 🪜 One box → a swarm → a platform

temenos reveals as much complexity as you bring to it. Same `Policy → Box` core at every
rung; you just point more agents at more boxes.

### Rung 1 — one box (start here)

```bash
temenos claude            # attach Claude to a box in this repo; natives banned
```

The approachable entry point: a single agent, your repo mounted live, a box underneath. Great
for trying temenos, demos, and "let it run unattended on this branch overnight." This is the
showcase — but it's the *first* rung, not the destination.

### Rung 2 — a swarm (the point)

When you fan a task across **dozens of agents in parallel**, approving each tool call by hand
is a non-starter — so you run them in **allow-all ("yolo") mode**. That's exactly where
containment-by-construction earns its keep: an agent can yolo all it wants because **there's
nothing dangerous to allow** — every action it takes structurally lands in a policy'd box.

```python
from temenos import Policy
from temenos.manager import BoxManager

mgr = BoxManager()
policy = Policy(network=False, write=["/scratch"])      # secure by default; opt into capability

# one contained box per agent — the same boundary, fifty times over
ids = [mgr.create(f"/srv/boxes/agent-{i}", policy, name=f"agent-{i}") for i in range(50)]

for bid in ids:
    box = mgr.get(bid)
    box.write_file("/scratch/task.py", "...")
    print(box.exec(["python3", "/scratch/task.py"]).stdout)   # ExecResult: stdout/exit/ms

mgr.shutdown()    # checkpoints (if enabled) + tears down the whole fleet
```

gVisor is the density sweet spot a swarm needs: a VM per agent is too heavy, a plain container
is a weaker boundary, a **box per agent is cheap *and* strong**. (Fan-out sugar — `mgr.map(...)`
over N boxes — is on the [roadmap](#-status); the explicit loop above works today.)

### Rung 3 — a platform (the ceiling)

The same registry that runs your local swarm is the **multi-tenant control plane**. One
per-user daemon supervises every box, exposes a REST control plane (the CLI) and a **per-box
MCP data plane** (`/mcp/<box-id>`, scoped token) for the agents. A "tenant" and an "agent" are
the same abstraction — so "run my swarm" and "run many customers' agents on untrusted code"
are **the same build**, not two products.

```bash
temenos serve --port 8839     # the daemon: REST control + per-box MCP, supervising N boxes
```

Per-tenant authz and quotas are the platform-tier work in progress (see [Status](#-status));
the box-per-owner isolation invariant — **no writable mount is ever shared across boxes** — is
in place today.

## ✨ Highlights

- 🏛️ **Agent on the host, execution in a box.** No broken updates, no API keys plumbed into a
  container, no re-auth. Only the code the agent *runs* is sandboxed.
- 🐝 **Built for fleets, not just one helper.** One box or a thousand — the multi-box
  `BoxManager` is the same code path whether it's your overnight swarm or a multi-tenant
  platform. Allow-all is safe because containment is structural.
- 🔒 **Real isolation, not a syscall allowlist.** gVisor is a userspace kernel — the host
  filesystem is invisible beyond what policy mounts, network is off by default, and most
  kernel-CVE surface is intercepted before it reaches the host.
- 🚫 **Sole-execution-path, enforced.** `temenos claude` denies native tools and exposes only
  `mcp__temenos__exec/read/write/list` over MCP, with `--strict-mcp-config` so a stray
  `.mcp.json` can't re-open a host-capable server.
- 📦 **Boxes are first-class.** Named, persistent, checkpointed, inspectable —
  `temenos exec`, `temenos shell`, `temenos diff`, `temenos audit`. Everything lives in a
  `.temenos/<box>/` you can `rm -rf`.
- 💾 **Durable by default.** Background checkpoint (gVisor `fscheckpoint`, ~30 ms) + restore
  on next use — re-run `temenos claude` in a repo and you resume where you left off.
- 🐍 **A clean core API.** `Policy → Box → ExecResult`. The CLI and MCP server are thin layers
  over the same `Box` you can use directly from Python. Core has **zero runtime deps**.
- 🧪 **Leak-tested.** A containment battery (`tests/leak/`) is the acceptance gate: no host
  write, host secrets invisible, no network, `/proc` escape blocked, memory cap OOM-kills.

## 🤔 What it is

A runtime that gives **trusted agents** an *untrusted-code execution surface* — whether
that's one agent or a swarm of them. You point a harness (Claude Code today; any MCP-capable
agent in principle) at a **box** and remove its host-touching tools. The agent keeps editing
your real files and calling its model — but every `bash`/`python`/file/network action it takes
happens inside gVisor, under a policy you set, observable and reversible. Run that for one
repo, or run it fifty times in parallel under one daemon — same boundary either way.

## 🚫 What it is NOT

| Not… | Because |
|---|---|
| A Docker / container runtime | It doesn't package or ship services. It wraps gVisor to confine an agent's *execution* and mounts your real repo live — the unit is a task, not an image. |
| A VM-per-task sandbox | The agent stays on the **host** (auth, updates, model API intact). Spinning a VM per task throws all that away; temenos boxes only what runs. |
| A seccomp / AppArmor filter | gVisor is a full userspace kernel, not a syscall allowlist bolted onto the host kernel — a categorically larger isolation boundary. |
| A defense against a malicious *agent* | The threat model trusts the agent binary. temenos contains the untrusted **code the agent runs**, not the agent itself. |
| A network firewall | v1 network is a toggle: **off** (isolated) or **full passthrough** (no filtering). Filtered per-host egress is post-v1 — and the load-bearing gap for adversarial fleets (see limits). |

## ⚖️ How it compares

| | temenos | Docker container | VM per task | firejail / bubblewrap | prompt guardrails |
|---|---|---|---|---|---|
| **Isolation boundary** | userspace kernel (gVisor) | shared host kernel + ns | hardware | shared kernel + seccomp/ns | none |
| **Agent stays on host** (auth/updates intact) | ✅ | ⚠️ (boxed → loses host context) | ❌ | ⚠️ partial | ✅ |
| **Sole-execution-path for an agent** | ✅ built-in (deny natives + MCP) | 🔧 DIY | 🔧 DIY | 🔧 DIY | ❌ (trust the model) |
| **Fleet control plane** (N boxes, one daemon) | ✅ `BoxManager` | 🔧 DIY (compose/k8s) | 🔧 DIY | ❌ | ❌ |
| **Kernel-CVE surface** | low | high | low | high | n/a |
| **Per-task object** (named, checkpointed, inspectable) | ✅ | ✅ (containers) | ⚠️ heavy | ❌ | ❌ |
| **Setup per task** | low (rootless, a box dir) | medium | high | low | none |

**In short:** containers and VMs isolate *whole programs you ship*; firejail filters syscalls
on the *host* kernel; prompt-level guardrails ask nicely. temenos isolates *the code trusted
agents run*, keeps the agents on the host, and makes each box a first-class, inspectable object
you can run one of — or a fleet of. It builds on [gVisor](https://gvisor.dev) and the
[Model Context Protocol](https://modelcontextprotocol.io). 🙂

## 🧩 How it works

```
   you ──► claude (host)            (×N agents, in a swarm)
              │  native tools BANNED (--disallowedTools, --strict-mcp-config)
              │  only mcp__temenos__* ALLOWED
              ▼
        temenos daemon  ──HTTP /mcp/<box-id>──►  Box (gVisor / runsc)
        (one per user,                            • host /usr,/etc bound read-only
         supervises every box)                    • repo mounted (live-writable by default)
                                                  • network off · mem/cpu/pid capped
                                                  • writes land in an overlay
```

A **box** = a `Policy` + a gVisor runtime + a data dir. **One daemon per user** auto-spawns on
first use and supervises *every* box, serving a REST control plane (the CLI) and a per-box MCP
data plane (the agents). Boxes are keyed by the hash of their data dir, so two repos' `default`
boxes — or fifty swarm agents — never collide. For the full design, decisions, and verification
log, see [`plan.md`](https://github.com/farizrahman4u/temenos/blob/main/plan.md).

## 📦 Install

temenos is **Linux + gVisor** for v1; a macOS (Seatbelt) backend is designed — see
[`macos_plan.md`](https://github.com/farizrahman4u/temenos/blob/main/macos_plan.md).

**1. gVisor (`runsc`)** — the sandbox. ([official guide](https://gvisor.dev/docs/user_guide/install/))

```bash
ARCH=$(uname -m)
wget https://storage.googleapis.com/gvisor/releases/release/latest/${ARCH}/runsc
chmod +x runsc && sudo mv runsc /usr/local/bin/
```

**2. temenos**

```bash
pip install "temenos[all]"        # daemon + MCP + CLI
# or from a checkout:
git clone https://github.com/farizrahman4u/temenos && cd temenos
pip install -e ".[all,dev]"
```

The core library has **zero runtime deps**; `[all]` pulls FastAPI/uvicorn/mcp/httpx for the
daemon and CLI. The bare `temenos image …` commands work without extras.

**3. (optional) mmdebstrap** — to build clean box base images (so boxes can `apt`/`pip`/`npm`
install into a writable system). Without it you boot against the host's read-only `/usr`.

```bash
sudo apt-get install mmdebstrap
```

**4. Check your host:**

```bash
$ temenos doctor
gVisor (runsc):     yes
  platform:         ptrace          # kvm on bare metal, systrap on most VMs, ptrace on WSL2
mmdebstrap:         yes
systemd-run:        yes             # required to ENFORCE memory/cpu limits (see Limits)
```

## 🚀 Quickstart

### Rung 1 — the project flow (git-style, one box)

```bash
cd ~/code/my-repo

temenos create                       # makes .temenos/default in this repo (+ .gitignore)
temenos exec default -- python3 -c "print(6*7)"
temenos shell default                # a minimal REPL inside the box
temenos ls                           # boxes the daemon is running
temenos audit default                # what ran in the box
temenos diff default                 # files under the box's write paths
temenos rm default                   # stop + delete the box
```

A bare box name resolves **project-first** (`.temenos/<name>`, walking up from CWD), then
**global** (`~/.local/share/temenos/boxes/<name>`); a project box shadows a global one of the
same name (with a warning).

**Attach Claude Code to a box:**

```bash
temenos claude                       # box 'default' in this repo
temenos claude --box review --net    # a separate box, network on
temenos claude --dry-run             # print the exact claude invocation, don't launch
temenos claude -- --model opus       # args after `--` go to claude
```

The repo mounts **live-writable**, so the agent's edits land in your real files — the sandbox
contains *execution*, not the trusted agent's edits. `--ephemeral` flips the repo to read-only.

### Rung 2 — a swarm (the Python core)

The CLI and MCP server are thin layers over the same `Box`/`BoxManager` you can drive directly:

```python
from temenos import Box, Policy
from temenos.manager import BoxManager

# one box, directly
with Box("demo", Policy(network=False, write=["/home/me/out"])) as box:
    box.write_file("/home/me/out/run.py", "print(6 * 7)\n")
    r = box.exec(["python3", "/home/me/out/run.py"])
    print(r.stdout, r.exit_code)        # "42\n", 0
    box.exec(["cat", "/etc/shadow"]).ok # -> False  (host invisible)

# a fleet, via the registry the daemon owns
mgr = BoxManager()
ids = [mgr.create(f"/srv/boxes/agent-{i}", Policy()) for i in range(50)]
for bid in ids:
    print(mgr.get(bid).exec(["echo", "hi"]).stdout.strip())
mgr.shutdown()
```

`Policy` is frozen and **secure by default** (`Policy()` = no network, no host writes, tight
limits). `restrict()` derives child policies that can only *narrow* — widening raises
`PolicyViolation`. A runnable version is in [`examples/python_api.py`](https://github.com/farizrahman4u/temenos/blob/main/examples/python_api.py).

## 🧠 The one design decision

**The agent runs on the host; only what it *executes* runs in a box.** That single split is
what makes temenos both usable and safe: the agent keeps its identity, updates, and model
access (so it actually works), while every command it issues crosses a hard sandbox edge
(so it can't hurt you). Everything else — the MCP data plane, the banned-natives wiring, the
checkpointing box, the multi-box registry — exists to make that split airtight and the "code"
the agent runs the **sole execution path**. And because that boundary is *structural*, not a
promise, the split holds identically whether you supervise one agent by hand or run a hundred
in allow-all mode: the box is the enforcement, not the human.

## 🗂️ CLI reference

| Command | What it does |
|---|---|
| `temenos doctor` | gVisor/platform/mmdebstrap/systemd capability check |
| `temenos image build NAME [--from mmdebstrap\|minimal\|host-copy\|download]` | build a box base image |
| `temenos image ls` · `rm NAME` | list / remove images |
| `temenos serve [--port]` | run the per-user daemon (auto-spawned otherwise) |
| `temenos create [NAME] [flags]` | create/ensure a box in this project |
| `temenos ls` | list running boxes (project boxes marked) |
| `temenos exec NAME -- CMD…` | run a command in a box |
| `temenos shell NAME` | minimal REPL in a box |
| `temenos rm NAME [--keep-data]` | stop + delete a box |
| `temenos audit NAME` · `diff NAME` | audit log / write-set manifest |
| `temenos claude [--box N] [flags] [-- claude-args]` | attach Claude with natives banned |
| `temenos version` | print version |

**Box-creation flags** (on `create` and `claude`): `--image NAME`, `--net`,
`--scratch disk\|memory`, `--force-memory`, `--ephemeral-fs` (never checkpoint),
`--no-autosave` (checkpoint only on close), `--ephemeral` (repo read-only),
`--volume HOST:TARGET[:ro\|rw]`, `--memory MB`, `--cpu SECONDS`, `--global`.

## 🛡️ Threat model & honest limits

The **agent is trusted** (you installed it; it authenticates as you; it isn't trying to
escape). The **code it runs is untrusted** — model-authored shell/python that may be buggy,
prompt-injected, or hostile. temenos's job is the *sole-execution-path* guarantee: every bit
of that code goes through a box, and a box can't touch the host beyond its policy. That
guarantee is what lets you take humans out of the loop at fleet scale.

| Property | Status (v1, gVisor) |
|---|---|
| Filesystem escape | **blocked** — host invisible beyond policy mounts; `/proc/1/root` is the box |
| Host writes outside policy | **blocked** — `/usr`,`/etc` read-only; writes go to an overlay |
| Network exfiltration | **blocked** when `network=off` (isolated netns) |
| Cross-box crosstalk | **blocked** — no writable mount is ever shared between boxes |
| Kernel-CVE surface | **mostly blocked** — gVisor intercepts syscalls in userspace |
| Memory/CPU/pid exhaustion | **enforced** via a per-box `systemd` scope (needs delegation — below) |

**Limits you should know about:**

- **Network is a toggle, not a firewall.** `--net` is **full host passthrough — no filtering**
  (localhost, LAN, cloud metadata, arbitrary egress). This is the **load-bearing gap for
  adversarial fleets**: a swarm of network-on boxes is an exfiltration surface multiplied by N.
  Run swarm boxes `network=off` where you can; filtered per-host egress is post-v1.
- **Resource limits need systemd user-cgroup delegation.** Without it, limits **degrade to
  unenforced with a warning** (`temenos doctor` shows the mode) — don't run adversarial work
  there.
- **Per-tenant authz/quotas are in progress.** The box-per-owner isolation invariant holds
  today; tenant-scoped tokens and aggregate quotas are the platform-tier roadmap.
- **WSL2 uses the `ptrace` platform** (no `/dev/kvm`). Slower, but the security model — the
  gVisor sentry — is identical to kvm/systrap.
- **Side channels** between co-resident boxes are out of scope for v1.
- **Not a defense against a malicious agent binary** — see the threat model.

Run `tests/leak/` against your host and re-run it when your harness upgrades (new tools are
new holes). A config isn't "supported" until it's green.

## 🏗️ Architecture

```
Layer 3  surfaces      server/ (FastAPI REST + per-box MCP) · cli.py
Layer 2½ registry      manager.py (BoxManager: ids, fleet lifecycle, checkpoint loop)
Layer 2  box           box.py (exec/read/write/list, audit, checkpoint)
Layer 1  backend       backends/ (gVisor: OCI bundle, held-run+exec, overlay, systemd scope)
Layer 0  data          policy.py · result.py · storage.py · exceptions.py  (pure, no OS calls)
```

`BoxManager` (Layer 2½) is the hinge: it's the local-swarm registry *and* the multi-tenant
control plane — one piece of code, two reach. Lower layers never import higher ones; REST, MCP,
and the CLI are all the same `Policy → Box → ExecResult` path. Delete `server/` and the core
still works.

## 🧪 Development

```bash
pip install -e ".[all,dev]"
PYTHONPATH=. pytest                       # full suite
PYTHONPATH=. pytest tests/leak/ -v        # the containment gate (needs gVisor)
TEMENOS_NET_TESTS=1 pytest tests/test_image_mmdebstrap.py   # opt-in network e2e
```

Tests that need gVisor / mmdebstrap / network are gated and skip cleanly without them.

## 📍 Status

**Pre-1.0** (`0.1.0`). v1 is feature-complete and leak-tested on Linux + gVisor; the API may
still shift before 1.0. Roadmap, ordered by where the value is:

1. **Fleet fan-out ergonomics** — `mgr.map(...)` over N boxes, batch lifecycle, aggregate audit.
2. **Filtered network egress** — per-host SNI/allowlist proxy, so swarm boxes get *contained*
   network instead of all-or-nothing (the biggest gap for adversarial fleets).
3. **Per-tenant authz & quotas** — tenant-scoped tokens, aggregate caps + backpressure.
4. **macOS (Seatbelt) backend** — see [`macos_plan.md`](https://github.com/farizrahman4u/temenos/blob/main/macos_plan.md).
5. True diff-vs-original, an interactive PTY/attach, persisted audit logs.

## 🙏 Credits

temenos stands on:

- [**gVisor**](https://gvisor.dev) — the userspace kernel that is the actual sandbox.
- [**Model Context Protocol**](https://modelcontextprotocol.io) — the agent-facing tool plane.

temenos's contribution is the *composition*: trusted agents on the host, untrusted-code boxes
underneath, one daemon that scales it from a single repo to a fleet, and the wiring that makes
each box the sole execution path.

## 📄 License

[Apache-2.0](https://github.com/farizrahman4u/temenos/blob/main/LICENSE) © temenos contributors
