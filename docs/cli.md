# CLI reference

The CLI is a thin client over the [daemon](configuration.md#the-daemon); it auto-spawns one
if none is running. `temenos image …` works with no extras installed; the box/agent commands
need `temenos[all]`.

## Scope: project-local by default, `--global` for global

Every box-targeting command operates in **one** scope and never crosses it:

- **Default = project.** The box lives in the nearest `.temenos/` (walking up from CWD).
- **`--global`** targets `$XDG_DATA_HOME/temenos/boxes/<name>` instead.

If a name isn't found in the chosen scope you get a `no such box …` error (hinting `--global`
when a same-named global box exists) — local commands don't silently act on global boxes.

> **`exec` flag placement.** `exec` slurps the command after the box name, so its own flags
> (`--global`, `--cwd`, `--timeout`) must come **before** the box name:
> `temenos exec --global mybox -- ls`. Other commands accept `--global` anywhere.

## Commands

### `temenos doctor`
Capability check. Prints whether gVisor (`runsc`) is present and which platform it'll use
(`kvm`/`systrap`/`ptrace`), whether `mmdebstrap` is installed, and whether `systemd-run` is
available (without it, resource limits are **unenforced** — see [Security](security.md)).

### `temenos create [NAME] [box-flags]`
Create (or ensure) a box. `NAME` defaults to `default`. Without `--global` it creates the box
in this repo's `.temenos/` (initializing `.temenos/` + a `.gitignore` if needed). Idempotent —
re-running returns the existing box (restoring from its checkpoint if present). See
[box-flags](#box-creation-flags).

### `temenos ls [--global] [--all]`
List boxes **on disk** (so stopped ones show too), annotated running/stopped from the daemon.

| | Lists |
|---|---|
| `temenos ls` | this project's boxes (default) |
| `temenos ls --global` | global boxes |
| `temenos ls --all` | both, labeled by scope |

Project boxes are marked with `*`.

### `temenos exec [--global] [--cwd DIR] [--timeout S] NAME -- CMD…`
Run a command (argv, not a shell string) in a box and stream back stdout/stderr/exit code.
The `--` is optional but recommended to separate the box command from temenos flags. Example:
```bash
temenos exec api -- pytest -q
temenos exec --global tools -- python3 --version
```

### `temenos shell [--global] NAME`
A minimal REPL: each line runs as a fresh `sh -c` in the box. Filesystem changes persist; the
working directory is tracked client-side. (No PTY — a true interactive terminal is post-v1;
for an interactive *agent*, use `temenos claude`.)

### `temenos rm [--global] [--keep-data] NAME`
Stop the box and delete its data dir. `--keep-data` stops it but keeps the dir (checkpoint +
overlay) so it can be resumed later.

### `temenos audit [--global] NAME`
Print the box's audit log (opens/execs/writes/checkpoints with timestamps). The log lives in
the live box, so the daemon must be running.

### `temenos diff [--global] NAME`
List files under the box's write paths — the write-set manifest for human review. (A true
diff-vs-original is post-v1.)

### `temenos claude [--box NAME] [box-flags] [-- claude-args]`
Attach a Claude Code session to a box: ensure the box, write a scoped MCP config, then launch
`claude` with native host tools **banned** and only `mcp__temenos__*` allowed. See
[Agents & MCP](agents.md). `--dry-run` prints the exact invocation without launching. Args
after `--` pass through to claude.
```bash
temenos claude                        # box 'default' in this repo
temenos claude --box review --no-net  # an isolated box
temenos claude -- --model opus        # forward flags to claude
```

### `temenos serve [--port N]`
Run the per-user daemon in the foreground. Usually unnecessary — clients auto-spawn it.

### `temenos image build|ls|rm`
Manage box base [images](images.md).
```bash
temenos image build base                       # mmdebstrap (default): clean apt base
temenos image build thin --from minimal        # tiny ldd-resolved rootfs
temenos image build snap --from host-copy --force-copy   # full host /usr copy (guarded)
temenos image build deb  --from download --url <rootfs.tar.gz>
temenos image ls
temenos image rm base
```

### `temenos version`
Print the version.

## Box-creation flags

Accepted by `create` and `claude`:

| Flag | Effect |
|---|---|
| `--image NAME` | boot from a built image (writable `/usr` → `apt`/`pip`/`npm`) |
| `--net` / `--no-net` | host network passthrough — **on by default**; `--no-net` isolates |
| `--scratch disk\|memory` | root-overlay medium (disk = checkpointable default; memory = ephemeral) |
| `--force-memory` | confirm `--scratch memory` (ephemeral, not checkpointable) |
| `--ephemeral-fs` | never checkpoint (throwaway filesystem) |
| `--no-autosave` | checkpoint only on close (disable the background loop) |
| `--ephemeral` | mount the repo **read-only** (default is live-writable) |
| `--volume HOST:TARGET[:ro\|rw]` | mount an extra host dir (repeatable) |
| `--memory MB` | memory cap |
| `--cpu SECONDS` | CPU-time cap |
| `--global` | create in the global scope instead of `.temenos/` |

See [Concepts → Durability](concepts.md#durability-checkpoint--restore) for the checkpoint
modes and [Security](security.md) for what the network/limits flags actually buy you.
