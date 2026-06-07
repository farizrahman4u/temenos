# temenos

**Untrusted-code containment for a *trusted* agent.** temenos runs an AI coding agent
(Claude Code, etc.) on the host — where its auth, updates, and model API keep working —
but confines every command, file op, and network call the agent makes into a named
**box**: a rootless [gVisor](https://gvisor.dev) sandbox with a small, Python-native
policy. The agent is trusted; the *code it runs* is not.

> *temenos* (τέμενος): a bounded precinct — a space set apart with a clear edge.

```bash
cd ~/code/my-repo
temenos claude            # Claude runs on the host; its bash/python/file tools run in a box
```

Claude's native host-touching tools (`Bash`, `Read`, `Write`, `Edit`, `WebFetch`, …) are
**banned**; its only execution path is the box's MCP tools (`mcp__temenos__exec/read/write/list`).
A shell that tries to write `/etc/passwd`, read `~/.ssh/id_rsa`, or `curl` the network is
contained — not by trusting the model, but by the sandbox boundary.

---

## Why

Agent harnesses run model-authored code with the *user's* full privileges. The usual
answers are coarse (a whole VM/container per task) or fragile (prompt-level "please don't").
temenos takes the middle path:

- **Keep the agent on the host** — no broken updates, no API-key plumbing into a container,
  no re-auth. We assume the agent itself isn't malicious (see the threat model).
- **Contain what it *executes*.** Bash/python run inside gVisor (a userspace kernel), with
  the host filesystem invisible except what policy mounts, network off by default, and
  per-box memory/CPU/pid limits.
- **Make it a first-class object.** A *box* is named, persistent, checkpointed, and
  inspectable — `temenos exec`, `temenos diff`, `temenos audit` — not a throwaway.

## Threat model (one paragraph)

The **agent is trusted** (you installed it, it authenticates as you, it isn't trying to
escape). The **code the agent runs is untrusted** — model-authored shell/python that may be
buggy, prompt-injected, or hostile. temenos's job is the *sole-execution-path* guarantee:
every bit of that code goes through a box, and a box cannot touch the host beyond its
policy. It is **not** a defense against a malicious agent binary, and v1 does not isolate
co-resident side channels.

---

## How it works

```
   you ──► claude (host)
              │  native tools BANNED (--disallowedTools, --strict-mcp-config)
              │  only mcp__temenos__* ALLOWED
              ▼
        temenos daemon  ──HTTP /mcp/<box-id>──►  Box (gVisor / runsc)
        (one per user)                            • host /usr,/etc bound read-only
                                                  • repo mounted (live-writable by default)
                                                  • network off · mem/cpu/pid capped
                                                  • writes land in an overlay
```

- A **box** = `Policy` + a gVisor runtime + a data dir. Everything for it lives in
  `.temenos/<box>/` (config, overlay, checkpoint) — portable and `rm -rf`-able.
- **One daemon per user** auto-spawns on first use and serves both a REST control plane
  (the CLI) and a per-box MCP data plane (the agent). Boxes are keyed by the hash of their
  data dir, so two repos' `default` boxes never collide.
- **Durable by default:** a box checkpoints in the background (gVisor `fscheckpoint`,
  ~30 ms) and on close, then **restores on next use** — re-running `temenos claude` in a
  repo resumes where you left off. (`--no-autosave` / `--ephemeral-fs` opt out.)

For the full design, decisions, and verification log, see [`plan.md`](plan.md).

---

## Install

temenos is **Linux + gVisor** for v1 (macOS is planned — see `macos_plan.md`).

**1. gVisor (`runsc`)** — the sandbox. ([official guide](https://gvisor.dev/docs/user_guide/install/))

```bash
# example; prefer the official installer for your distro
ARCH=$(uname -m)
wget https://storage.googleapis.com/gvisor/releases/release/latest/${ARCH}/runsc
chmod +x runsc && sudo mv runsc /usr/local/bin/
```

**2. temenos**

```bash
pip install "temenos[all]"        # daemon + MCP + CLI
# or, from a checkout:
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

---

## Quickstart

### The project flow (git-style)

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
**global** (`~/.local/share/temenos/boxes/<name>`); a project box shadows a global one of
the same name (with a warning).

### Attach Claude Code to a box

```bash
temenos claude                              # box 'default' in this repo
temenos claude --box review --net           # a separate box, network on
temenos claude --dry-run                     # print the exact claude invocation, don't launch
temenos claude -- --model opus               # args after `--` go to claude
```

This writes a scoped MCP config, then launches `claude` with native tools denied and only
`mcp__temenos__*` allowed (`--strict-mcp-config` so a stray `.mcp.json` can't re-open a
host-capable server). The repo mounts **live-writable** so the agent's edits land in your
real files — the sandbox contains *execution*, not the trusted agent's edits. `--ephemeral`
flips the repo to read-only.

### The Python API (the core; CLI/MCP are thin layers over it)

```python
from temenos import Box, Policy

with Box("demo", Policy(network=False, write=["/home/me/out"])) as box:
    box.write_file("/home/me/out/run.py", "print(6 * 7)\n")
    r = box.exec(["python3", "/home/me/out/run.py"])
    print(r.stdout, r.exit_code)        # "42\n", 0
```

`Policy` is frozen and **secure by default** (`Policy()` = no network, no host writes, tight
limits). `restrict()` derives child policies that can only *narrow* — widening raises
`PolicyViolation`.

---

## CLI reference

| Command | What it does |
|---|---|
| `temenos doctor` | gVisor/platform/mmdebstrap/systemd capability check |
| `temenos image build NAME [--from mmdebstrap\|minimal\|host-copy\|download]` | build a box base image |
| `temenos image ls` / `rm NAME` | list / remove images |
| `temenos serve [--port]` | run the per-user daemon (auto-spawned otherwise) |
| `temenos create [NAME] [flags]` | create/ensure a box in this project |
| `temenos ls` | list running boxes (project boxes marked) |
| `temenos exec NAME -- CMD…` | run a command in a box |
| `temenos shell NAME` | minimal REPL in a box |
| `temenos rm NAME [--keep-data]` | stop + delete a box |
| `temenos audit NAME` / `diff NAME` | audit log / write-set manifest |
| `temenos claude [--box N] [flags] [-- claude-args]` | attach Claude with natives banned |
| `temenos version` | print version |

**Box-creation flags** (on `create` and `claude`): `--image NAME`, `--net`,
`--scratch disk\|memory`, `--force-memory`, `--ephemeral-fs` (never checkpoint),
`--no-autosave` (checkpoint only on close), `--ephemeral` (repo read-only),
`--volume HOST:TARGET[:ro\|rw]`, `--memory MB`, `--cpu SECONDS`, `--global`.

---

## Security properties & honest limits

Under the threat model above (v1 = gVisor):

| Property | Status |
|---|---|
| Filesystem escape | **blocked** — host invisible beyond policy mounts; `/proc/1/root` is the box |
| Host writes outside policy | **blocked** — `/usr`,`/etc` read-only; writes go to an overlay |
| Network exfiltration | **blocked** when `network=off` (isolated netns) |
| Kernel-CVE surface | **mostly blocked** — gVisor intercepts syscalls in userspace |
| Memory/CPU/pid exhaustion | **enforced** via a per-box `systemd` scope (needs delegation — below) |
| Cross-tenant crosstalk | gVisor boundary + per-box ids + daemon authz |

**Limits you should know about:**

- **Network is a toggle, not a firewall.** `network=off` is genuinely isolated; `--net`
  (`network=on`) is **full host passthrough — no filtering** (localhost, LAN, cloud
  metadata, arbitrary egress). It's an operator opt-in, unsafe for adversarial multi-tenant
  use. *Filtered* egress is post-v1.
- **Resource limits need systemd user-cgroup delegation.** temenos wraps each box in
  `systemd-run --user --scope` to enforce memory/CPU/pids. On hosts without delegation,
  limits **degrade to unenforced with a warning** (`temenos doctor` shows the mode) — don't
  run adversarial workloads there.
- **WSL2 uses the `ptrace` platform** (no `/dev/kvm`; `systrap` fails). It's slower but the
  security model — the gVisor sentry — is identical to kvm/systrap.
- **Side channels** between co-resident boxes are out of scope for v1.

The leak-test battery (`tests/leak/`) is the acceptance gate for these properties; run it on
your host (and re-run when your agent harness upgrades — new tools are new holes).

---

## Architecture

```
Layer 3  surfaces      server/ (FastAPI REST + per-box MCP) · cli.py
Layer 2½ registry      manager.py (BoxManager: ids, lifecycle, checkpoint loop)
Layer 2  box           box.py (exec/read/write/list, audit, checkpoint)
Layer 1  backend       backends/ (gVisor: OCI bundle, held-run+exec, overlay, systemd scope)
Layer 0  data          policy.py · result.py · storage.py · exceptions.py  (pure, no OS calls)
```

Lower layers never import higher ones; REST, MCP, and the CLI are all the same
`Policy → Box → ExecResult` path. Delete `server/` and the core still works.

## Development

```bash
pip install -e ".[all,dev]"
PYTHONPATH=. pytest                       # full suite
PYTHONPATH=. pytest tests/leak/ -v        # the containment gate (needs gVisor)
TEMENOS_NET_TESTS=1 pytest tests/test_image_mmdebstrap.py   # opt-in network e2e
```

Tests that need gVisor / mmdebstrap / network are gated and skip cleanly without them.

## License

Apache-2.0. See [`LICENSE`](LICENSE).
