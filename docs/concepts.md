# Concepts

The whole model in one sitting. Everything else in the docs is detail on these pieces.

## Box

A **box** is a named, persistent sandbox: a [`Policy`](python-api.md#policy) + a gVisor
runtime + a data directory that holds everything for it (config, the write overlay, and
checkpoints). You run commands in it (`exec`), read/write files in it, inspect it, snapshot
it, and throw it away — it behaves like a long-lived object, not a one-shot `run`.

Inside a box:

- The host's `/usr` and `/etc` are mounted **read-only** (so normal tooling — `python3`,
  `git`, coreutils — works) unless you boot from a writable [image](images.md).
- Writes land in an **overlay**, never on the host, except under paths you explicitly grant
  (`write=[…]` / `--volume`).
- The host filesystem is otherwise **invisible** — a path you didn't mount simply isn't there.
- Network and resource limits follow the policy.

## Policy

A [`Policy`](python-api.md#policy) is frozen, hashable, plain data — it says *what code in
the box may do*: which host paths are readable/writable, whether the network is on, the base
image, the scratch medium, checkpoint mode, and resource caps.

Defaults: the **filesystem is locked down** (no host writes, read-only system), limits are
tight — but **network is ON by default** in v1 (full host passthrough; see
[Security](security.md#network)). `Policy.restrict(...)` derives a child that can only
*narrow* a capability; widening raises `PolicyViolation` — there is no `escalate()`.

## The daemon (one per user)

A single long-lived process per user supervises **all** boxes and exposes two faces over the
same registry:

- **REST** — the control plane the CLI uses (create/list/exec/delete/audit).
- **MCP** — a per-box data plane at `/mcp/<box-id>` that agents attach to.

It **auto-spawns** on first use (connect-or-spawn under a file lock, so concurrent calls never
start two daemons) and binds loopback only, gated by a Bearer token. You rarely run
`temenos serve` by hand. See [Configuration](configuration.md#the-daemon).

## Box identity, and project vs. global scope

A box's **id is the hash of its data dir's real path** — so two repos' `default` boxes (or
fifty swarm boxes) get distinct ids without you ever typing a path. That hash is what the
daemon registry and the MCP route key on.

Where the data dir lives defines the box's **scope**:

- **Project (local)** — `<repo>/.temenos/<name>/`. Discovered git-style by walking up from
  your CWD (stopping at `$HOME`). This is the **default** for every command.
- **Global** — `$XDG_DATA_HOME/temenos/boxes/<name>/`. Targeted explicitly with `--global`.

Commands operate in exactly one scope: local unless you pass `--global`. There is no silent
fallthrough — a local command never touches a global box. See the [CLI reference](cli.md).

## Durability (checkpoint & restore)

Boxes are durable by default. A background loop checkpoints **dirty** boxes (gVisor
`fscheckpoint`, ~30 ms) on idle-debounce or a staleness cap, and again on close. The
checkpoint lives in the box's own dir, so the **box dir is its own registry**: re-running
`temenos claude` (or `create`) in a repo **restores** the box where you left off — the daemon
doesn't track desired-running state across restarts.

Modes (set by `Policy.checkpoint`, exposed as flags):

| Mode | Flag | Behavior |
|---|---|---|
| `auto` (default) | — | background loop + commit on close |
| `on-close` | `--no-autosave` | commit only when the box closes (loop off) |
| `off` | `--ephemeral-fs` | never persist — throwaway filesystem |

(`scratch="memory"` can't be checkpointed, so it's treated as `off`.)

## Images

By default a box binds the host's read-only `/usr`, so the system isn't writable —
`apt`/system-`pip` can't install. An [**image**](images.md) is a runner-owned base rootfs
the box boots from instead, making `/usr`,`/etc` writable-but-ephemeral so installs work and
stay off the host. Build one with `temenos image build` (mmdebstrap by default) and select it
with `--image NAME`.

## Storage & mounts

Beyond the `read`/`write` path sugar, a `Policy` can carry explicit
[volumes](python-api.md#storage-providers): `MemoryVolume` (tmpfs scratch), `DiskVolume`
(a durable host dir, optionally pinned under an `allowed_root` for multi-tenant containment),
mounted at a chosen target via `Mount`. Providers are plain classes — the extension point for
custom backings (e.g. an fsspec/S3 volume, post-v1).

## Layering

```
Layer 3  surfaces      server/ (REST + per-box MCP) · cli.py
Layer 2½ registry      manager.py  (BoxManager: ids, lifecycle, checkpoint loop)
Layer 2  box           box.py      (exec/read/write/list, audit, checkpoint)
Layer 1  backend       backends/   (gVisor: OCI bundle, held-run+exec, overlay, systemd scope)
Layer 0  data          policy · result · storage · exceptions  (pure, no OS calls)
```

Lower layers never import higher ones, and REST/MCP/CLI are all the same
`Policy → Box → ExecResult` path. Delete `server/` and the core still works — which is why
the [Python API](python-api.md) is a first-class surface, not an afterthought.
