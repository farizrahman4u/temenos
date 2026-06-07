# Changelog

All notable changes to **temenos** are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.0] — 2026-06-07

### Added
- **Smart working-directory landing.** `temenos exec`, `temenos shell`, and `temenos claude`
  now open in your current directory for a **project** box (the repo is mounted at its real
  host path, so the host CWD exists inside the box) and at `/` for a **global** box. Boxes
  carry a `default_cwd` so an agent's MCP `exec` calls land in the project dir too. Plumbed
  through the Python `Box`, the daemon REST `create`, and the daemon client.
- **`TERM` forwarding** into interactive sessions, so curses tools — `vim`, `less`, `top`,
  `clear` — render correctly (defaults to `xterm-256color` when the host hasn't set one).
- **A shell welcome banner** summarizing the box at a glance (name, id, network, image,
  scratch/checkpoint mode, working dir); respects `NO_COLOR`.

## [0.2.0] — 2026-06-07

First published release.

### Added
- **Interactive shells (PTY passthrough).** `temenos shell` is now a real interactive
  terminal (bash if the box has it, else sh), and `temenos exec` gained `-it`
  (`--interactive`/`--tty`) for one-off interactive runs — REPLs and full-screen TUIs like
  `python3`, `bash -i`, and `vim` work as they would locally. Previously every shell line
  ran as a fresh captured `exec` with stdin disconnected, so any interactive program hit
  immediate EOF and exited. The CLI now wires your terminal straight into the box over a
  PTY (the `docker exec -it` model), via a new `GET /v1/boxes/{id}/attach` daemon endpoint.
  Without a controlling terminal (piped/redirected stdin), `-it` falls back to direct fd
  passthrough, so `echo … | temenos exec -it box -- python3` still works.

### Changed
- The old line-marker (`__TEMENOS_CWD__`) shell REPL is gone, replaced by the PTY shell.

### Fixed
- **Test hygiene:** a session-scoped reaper plus graceful daemon teardown ensure gVisor
  sandboxes are torn down even when a run is interrupted, so `runsc` processes no longer
  orphan and accumulate across test runs.

## [0.1.0] — unreleased

Baseline (never published). The core temenos runtime: named, persistent gVisor boxes with
a Python-native `Policy`, a per-user daemon (REST control plane + per-box MCP data plane),
the project-aware CLI (`create`/`exec`/`ls`/`rm`/`audit`/`diff`/`serve`), `temenos claude`
(native tools banned, only `mcp__temenos__*` allowed), box images (mmdebstrap/download/
minimal/host-copy builders), storage volumes, filesystem checkpoint/restore, and
systemd-backed memory limits.

[0.3.0]: https://github.com/farizrahman4u/temenos/releases/tag/v0.3.0
[0.2.0]: https://github.com/farizrahman4u/temenos/releases/tag/v0.2.0
