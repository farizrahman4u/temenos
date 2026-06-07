# temenos documentation

temenos runs a **trusted agent on the host** and confines the **untrusted code it executes**
to a rootless [gVisor](https://gvisor.dev) box. These pages go deeper than the
[project README](../README.md); start with **Concepts**, then jump to whatever surface you're
using.

| Page | What's in it |
|---|---|
| [Concepts](concepts.md) | The mental model — boxes, policies, the daemon, project vs. global scope, checkpoints, images. Read this first. |
| [CLI reference](cli.md) | Every command and flag, with the scope rules (`--global`, `ls --all`) and examples. |
| [Python API](python-api.md) | The core library — `Policy`, `Box`, `BoxManager`, `ExecResult`, storage providers. The CLI/MCP are thin layers over this. |
| [Agents & MCP](agents.md) | The per-box MCP data plane, `temenos claude`, banning native tools, and wiring other harnesses (the sole-execution-path checklist). |
| [Box images](images.md) | Building a writable base rootfs so boxes can `apt`/`pip`/`npm` install. Builders, recipes, persistence. |
| [Security model](security.md) | The threat model in depth, what gVisor enforces, the honest limits (network-on default, systemd delegation, side channels), and the leak-test gate. |
| [Configuration](configuration.md) | Environment variables, on-disk layout, the daemon, and ports. |

## The one-paragraph orientation

You point an agent harness (Claude Code today) at a **box** and remove its host-touching
tools, so its only execution path is the box's MCP tools. A box is a `Policy` (what the code
may do) + a gVisor runtime + a data dir; a single **per-user daemon** auto-spawns and
supervises every box, serving REST (the CLI) and per-box MCP (the agents). Boxes are durable
(checkpointed + restored on next use) and addressed by name — **project-local** (`.temenos/`
in your repo) by default, or **global** with `--global`.

> Design rationale, decisions, and the verification log live in
> [`plan.md`](../plan.md); this directory is the user-facing reference.
