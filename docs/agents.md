# Agents & MCP

temenos's reason for existing: let a **trusted agent** keep running on the host (its auth,
updates, and model API intact) while its **only execution path** is a box. That's enforced by
two halves — the box's MCP tools (the path you *allow*) and the harness's tool controls (the
host tools you *deny*).

## The per-box MCP data plane

The daemon mounts one MCP server that routes per box by URL path: `…/mcp/<box-id>`. Each
connection is bound to exactly one box and gated by the daemon's Bearer token. The tools
operate only on that box:

| Tool (Claude sees `mcp__temenos__<name>`) | Signature | Maps to |
|---|---|---|
| `exec` | `(command: string[], cwd?, timeout_s?)` → `{stdout, stderr, exit_code, truncated}` | `Box.exec` (argv, not a shell string) |
| `read` | `(path)` → `{content}` | read a file in the box |
| `write` | `(path, content)` → `{bytes}` | write a file (overlay, never the host) |
| `list` | `(path)` → `{entries}` | list a directory |

There is deliberately **no** create/delete/commit/fetch tool: the agent can run/read/write
inside the box but can't change the box's lifecycle, touch the host, or open network on its
own. Transport is stateless Streamable-HTTP with JSON responses, verified against the
reference MCP client (so any compliant client — Claude Code included — can drive it).

## `temenos claude`

```bash
cd ~/code/my-repo
temenos claude --box default        # network on by default; --no-net to isolate
```

What it does:

1. **Resolve/ensure the box** in this project (`--box`, default `default`; `--global` for a
   global box), applying any [box-flags](cli.md#box-creation-flags).
2. **Write a scoped MCP config** to `<box-dir>/mcp.json`:
   ```json
   {"mcpServers": {"temenos": {"type": "http",
     "url": "http://127.0.0.1:8839/mcp/<box-id>",
     "headers": {"Authorization": "Bearer <daemon-token>"}}}}
   ```
3. **Launch claude with natives banned, only temenos allowed:**
   ```
   claude --strict-mcp-config --mcp-config <box>/mcp.json \
     --disallowedTools Bash,Read,Write,Edit,MultiEdit,NotebookEdit,Glob,Grep,WebFetch,WebSearch,Task \
     --allowedTools mcp__temenos__exec,mcp__temenos__read,mcp__temenos__write,mcp__temenos__list \
     <your args>
   ```
   - `--strict-mcp-config` is **load-bearing**: it stops a stray `.mcp.json` from
     re-introducing a host-capable MCP server.
   - `Task` is denied so subagents can't spawn with a different toolset.

`temenos claude --dry-run` prints the exact box id, config path, and command without
launching — useful for auditing the wiring.

The repo mounts **live-writable**, so the agent's edits land in your real files (the box
contains *execution*, not the trusted agent's edits). `--ephemeral` flips the repo read-only.

## Wiring another harness (the sole-execution-path checklist)

temenos's guarantee holds only if the box is the agent's **sole** way to execute. For any
MCP-capable harness, point it at the box endpoint (the JSON above) and then walk this:

| Vector | What to do |
|---|---|
| Native shell / exec | deny it; the agent uses `mcp__temenos__exec` |
| File read / write / edit | deny; use `mcp__temenos__read`/`write` |
| File search (glob/grep) | deny; the agent greps via `exec` |
| Web fetch / browser | deny |
| Other MCP servers / plugins | remove; allow only `mcp__temenos__*` (e.g. `--strict-mcp-config`) |
| Subagents / spawned tasks | confirm they inherit the deny rules |
| Auto-approve / "yolo" modes | fine for native tools **once they're removed** — there's nothing dangerous left to approve |

If a harness can't have its native tools cleanly removed (e.g. a built-in shell that can't be
disabled), don't try to enumerate-and-disable it — run the **whole harness inside a box**
instead.

A config isn't "supported" until the [leak-test](security.md#the-leak-test) passes against it,
and you re-run that when the harness upgrades (new tools are new holes). See
[`examples/`](../examples/) for a config sample and the checklist in runnable form.
