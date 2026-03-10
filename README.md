# mcp-python-debugger

MCP server that gives Claude Code access to a real Python debugger (debugpy + DAP protocol).

Instead of spamming `print()` statements, Claude can now set breakpoints, step through code, inspect variables, and evaluate expressions — just like a developer in VS Code.

## Architecture

```
Claude Code ──► MCP tools ──► FastMCP Server ──► DAP protocol ──► debugpy ──► your script
```

The server launches your Python script under `debugpy`, connects via the Debug Adapter Protocol (DAP), and exposes debugger operations as MCP tools.

## Installation

```bash
# Clone and install
cd mcp-python-debugger
pip install -e .

# debugpy is installed as a dependency
```

## Claude Code Configuration

Add to your `.claude/settings.json`:

```json
{
  "mcpServers": {
    "python-debugger": {
      "command": "mcp-python-debugger"
    }
  }
}
```

Or in your project's `.mcp.json`:

```json
{
  "mcpServers": {
    "python-debugger": {
      "command": "mcp-python-debugger",
      "type": "stdio"
    }
  }
}
```

## Available Tools

| Tool | Description |
|------|-------------|
| `debug_launch` | Launch a script with debugpy attached |
| `debug_set_breakpoints` | Set breakpoints at specific lines |
| `debug_continue` | Resume until next breakpoint |
| `debug_step_over` | Execute current line, stop at next |
| `debug_step_into` | Step into a function call |
| `debug_step_out` | Run until current function returns |
| `debug_stack_trace` | Show the call stack with frame IDs |
| `debug_variables` | Inspect locals/globals in a frame |
| `debug_evaluate` | Evaluate a Python expression at breakpoint |
| `debug_status` | Check session state |
| `debug_stop` | Terminate the debug session |

## Example Workflow

What Claude Code would do when debugging an issue:

```
1. debug_launch(program="/app/main.py")
   → Session started, paused on entry

2. debug_set_breakpoints(file="/app/main.py", lines=[42, 67])
   → Breakpoints set at lines 42, 67

3. debug_continue()
   → Stopped: breakpoint at process_data(), main.py:42  [frame 1]

4. debug_variables(frame_id=1, scope="locals")
   → data: [1, 2, None, 4]  (list)
   → index: 2  (int)

5. debug_evaluate(expression="data[index]", frame_id=1)
   → None  (NoneType)

6. # Claude now sees the bug: None in the data at index 2
   debug_stop()
```

## How It Works

1. **debug_launch** starts `python -m debugpy --listen 127.0.0.1:<port> --wait-for-client script.py`
2. The MCP server connects to debugpy's DAP socket
3. Each tool call translates to DAP requests (JSON messages over TCP)
4. Responses are formatted for Claude's consumption

## Limitations

- One debug session at a time (by design — keeps it simple)
- `justMyCode=True` by default (doesn't step into stdlib/site-packages)
- 60s timeout on `continue`, 30s on step operations
- No conditional breakpoints yet (TODO)

## TODO

- [ ] Conditional breakpoints (`set_breakpoints` with conditions)
- [ ] Watch expressions (auto-evaluate on each stop)
- [ ] Exception breakpoints (break on raise)
- [ ] Multi-threaded debugging (thread selection)
- [ ] Attach to running process (not just launch)
- [ ] stdout/stderr capture from debuggee
