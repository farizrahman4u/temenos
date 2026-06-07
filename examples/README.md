# Examples

- **`python_api.py`** — the core `Box`/`Policy` API: run code, read/write through the box,
  see containment (denied host read, no network), and `Policy.restrict()` refusing to widen.
  Needs gVisor: `PYTHONPATH=. python examples/python_api.py`.

- **`claude_mcp_config.json`** — the shape of the per-box MCP config `temenos claude`
  generates. Useful if you want to point another MCP-capable harness at a box's data plane
  by hand. The live values (daemon URL, box id, token) are written to
  `.temenos/<box>/mcp.json` when you run `temenos claude`.

## Wiring another harness (the sole-execution-path checklist)

temenos's guarantee holds only if the box's MCP tools are the agent's **only** way to
execute. For any harness:

1. Point it at the box's MCP endpoint (above) — `mcp__temenos__exec/read/write/list`.
2. **Deny every native host-touching tool** (shell, file read/write/edit, glob/grep, web
   fetch) and any other MCP server. For Claude Code that's `--disallowedTools …
   --strict-mcp-config --allowedTools mcp__temenos__*` — exactly what `temenos claude` does.
3. Confirm subagents/spawned tasks inherit the deny rules; turn off auto-approve for natives.
4. If a harness can't have its natives cleanly removed (e.g. a built-in shell), don't
   enumerate-and-disable — run the **whole harness inside a box** instead.

Run `tests/leak/` against your final config; it isn't "supported" until that's green.
