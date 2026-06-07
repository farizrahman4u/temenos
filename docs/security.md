# Security model

## Threat model

The **agent is trusted** — you installed it, it authenticates as you, it isn't trying to
escape the sandbox. The **code the agent runs is untrusted** — model-authored shell/python
that may be buggy, prompt-injected, or outright hostile. temenos's job is the
**sole-execution-path guarantee**: every bit of that code goes through a box, and a box can't
touch the host beyond its policy.

It is explicitly **not**:
- a defense against a malicious *agent binary* (that's outside the trust boundary);
- a network firewall (v1 network is all-or-nothing — see [below](#network));
- a defense against co-resident **side channels** (out of scope for v1).

## What gVisor enforces (v1)

| Property | Status |
|---|---|
| Filesystem escape | **blocked** — the host is invisible beyond policy mounts; `/proc/1/root` is the box, not the host `/` |
| Host writes outside policy | **blocked** — `/usr`,`/etc` are read-only; writes go to the box overlay |
| Network exfiltration | **blocked with `--no-net`** (isolated netns) — but network is **on by default** |
| Kernel-CVE surface | **mostly blocked** — gVisor is a userspace kernel; syscalls are serviced by the sentry, not the host kernel |
| Memory / CPU / pid exhaustion | **enforced** via a per-box `systemd --user` scope (requires delegation — see limits) |
| Cross-box crosstalk | **blocked** — no writable mount is shared between boxes; each has its own overlay and id |

gVisor's **platform** (`kvm`/`systrap`/`ptrace`) is auto-detected and is a
performance/compat choice only — the security boundary (the sentry) is identical across them.
On WSL2 that's `ptrace` (no `/dev/kvm`); a bare-metal host gets `kvm`.

## Honest limits

### Network
Network is **on by default** in v1, and it's a **toggle, not a firewall**: the default is full
host passthrough — localhost, LAN, cloud metadata, arbitrary egress — with no filtering.
`--no-net` (`network=False`) fully isolates a box (empty netns). This is the **load-bearing
gap for adversarial or fleet workloads**: N network-on boxes are an exfiltration surface ×N.
Run untrusted/multi-tenant boxes with `--no-net`; *filtered* per-host egress is post-v1.

### Resource limits need systemd delegation
Memory/CPU/pid caps are enforced by wrapping each box in `systemd-run --user --scope`. This
needs a systemd user manager with cgroup delegation (default on modern desktop systemd; on
headless hosts, run the daemon as a `--user` service or `loginctl enable-linger`). Without it,
limits **degrade to unenforced, with a warning** — `temenos doctor` reports the mode. Don't
run adversarial workloads in the degraded mode.

### Secrets / environment
The host environment is **not** inherited into a box (a minimal env is set), so host secrets
(`ANTHROPIC_API_KEY`, etc.) don't leak into untrusted code. Inject what a box genuinely needs
explicitly, and prefer mounting a secret as a file over putting it in the prompt/context.

### Multi-tenancy
The box-per-data-dir isolation invariant holds (distinct ids, no shared writable mounts), and
`DiskVolume(allowed_root=…)` pins a tenant's volumes under a root. Full per-tenant
authz/quotas in the daemon are in progress; for adversarial multi-tenant today, combine
`--no-net`, enforced limits (systemd delegation), and `allowed_root` containment.

## The leak-test

`tests/leak/` is the **acceptance gate** for the properties above — it runs them against the
real `Box` boundary (which the MCP tools and `temenos exec` both funnel through):

- write `/etc/passwd` / `/usr/bin/x` → denied (read-only system)
- read a host secret not mounted → invisible (directly and via `/proc/1/root`)
- connect out with `network=False` → no route
- `/proc/1/root` resolves to the box, not the host
- allocate past the memory cap → OOM-killed (when systemd enforcement is available)

Run it on your host, and **re-run it whenever your agent harness upgrades** — new tools are
new holes. A harness config isn't "supported" until it's green.

```bash
PYTHONPATH=. pytest tests/leak/ -v
```

See [Agents & MCP](agents.md#wiring-another-harness-the-sole-execution-path-checklist) for the
per-harness checklist that complements these box-level checks.
