"""
MCP server that gives Claude Code access to a Python debugger (debugpy).

Tools exposed:
  - debug_launch          Launch a script with debugpy attached
  - debug_set_breakpoints Set breakpoints in a file
  - debug_continue        Resume execution
  - debug_step_over       Step to next line
  - debug_step_into       Step into function call
  - debug_step_out        Step out of current function
  - debug_stack_trace     Get current call stack
  - debug_variables       Inspect variables in a frame
  - debug_evaluate        Evaluate an expression at a breakpoint
  - debug_status          Check debugger state
  - debug_stop            Terminate the debug session
"""

import asyncio
import json
import os
import socket
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

from mcp_debugger.dap_client import DebugSession

# ---------------------------------------------------------------------------
# Session manager (singleton — one debug session at a time)
# ---------------------------------------------------------------------------

_session: Optional[DebugSession] = None


def _find_free_port() -> int:
    """Find a free TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _launch_debugpy(program: str, args: list[str], cwd: str | None, port: int) -> asyncio.subprocess.Process:
    """Start debugpy as a subprocess listening on *port*."""
    cmd = [
        "python", "-m", "debugpy",
        "--listen", f"127.0.0.1:{port}",
        "--wait-for-client",
        program,
        *args,
    ]
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    # Give debugpy a moment to bind
    await asyncio.sleep(0.5)
    return process


async def _connect_dap(port: int, retries: int = 10) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Connect to debugpy's DAP server, retrying until it's ready."""
    for i in range(retries):
        try:
            return await asyncio.open_connection("127.0.0.1", port)
        except ConnectionRefusedError:
            await asyncio.sleep(0.3)
    raise ConnectionError(f"Could not connect to debugpy on port {port}")


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _fmt_frame(f: dict) -> str:
    """Format a single stack frame for display."""
    source = f.get("source", {}).get("path", "?")
    name = f.get("name", "?")
    line = f.get("line", "?")
    return f"  {name} at {source}:{line}"


def _fmt_variable(v: dict) -> str:
    vtype = v.get("type", "")
    return f"  {v['name']}: {v.get('value', '?')}  ({vtype})"


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

mcp = FastMCP("python_debugger_mcp")


# ---- Launch / Stop ----

class LaunchInput(BaseModel):
    """Input for launching a Python script under the debugger."""
    program: str = Field(..., description="Absolute path to the Python script to debug")
    args: list[str] = Field(default_factory=list, description="Command-line arguments for the script")
    cwd: Optional[str] = Field(default=None, description="Working directory (defaults to script's directory)")
    stop_on_entry: bool = Field(default=True, description="If true, pause immediately on first line")


@mcp.tool(
    name="debug_launch",
    annotations={
        "title": "Launch Python debugger",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def debug_launch(params: LaunchInput) -> str:
    """Launch a Python script attached to debugpy and connect via DAP.

    This starts the script in a paused state. Use debug_set_breakpoints
    to set breakpoints, then debug_continue to start execution.

    If stop_on_entry is True (default), the debugger pauses on the very
    first line so you can inspect the initial state.

    Args:
        params (LaunchInput): Script path, arguments, working directory.

    Returns:
        str: Confirmation with session info, or error message.
    """
    global _session

    if _session is not None:
        return "Error: A debug session is already active. Use debug_stop first."

    program = os.path.abspath(params.program)
    if not os.path.isfile(program):
        return f"Error: File not found: {program}"

    cwd = params.cwd or str(Path(program).parent)
    port = _find_free_port()

    try:
        process = await _launch_debugpy(program, params.args, cwd, port)
        reader, writer = await _connect_dap(port)

        session = DebugSession(reader=reader, writer=writer, process=process)
        await session.initialize()

        await session.launch(program, params.args, cwd, stop_on_entry=params.stop_on_entry)

        if params.stop_on_entry:
            await session.wait_for_stop(timeout=10.0)

        _session = session
        return (
            f"Debug session started.\n"
            f"  Program: {program}\n"
            f"  PID: {process.pid}\n"
            f"  DAP port: {port}\n"
            f"  Paused: {'yes (on entry)' if params.stop_on_entry else 'no — running'}\n\n"
            f"Next steps: set breakpoints with debug_set_breakpoints, "
            f"then debug_continue to run."
        )
    except Exception as e:
        return f"Error launching debugger: {e}"


@mcp.tool(
    name="debug_stop",
    annotations={
        "title": "Stop debug session",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def debug_stop() -> str:
    """Terminate the active debug session and kill the debugged process.

    Returns:
        str: Confirmation or error if no session is active.
    """
    global _session
    if _session is None:
        return "No active debug session."
    try:
        await _session.disconnect()
    except Exception:
        pass
    _session = None
    return "Debug session terminated."


# ---- Breakpoints ----

class SetBreakpointsInput(BaseModel):
    """Input for setting breakpoints."""
    file: str = Field(..., description="Absolute path to the source file")
    lines: list[int] = Field(..., description="Line numbers where breakpoints should be set", min_length=1)


@mcp.tool(
    name="debug_set_breakpoints",
    annotations={
        "title": "Set breakpoints",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def debug_set_breakpoints(params: SetBreakpointsInput) -> str:
    """Set breakpoints at specific lines in a source file.

    This replaces any previous breakpoints in the same file.

    Args:
        params (SetBreakpointsInput): File path and line numbers.

    Returns:
        str: List of verified breakpoint locations, or error.
    """
    if _session is None:
        return "Error: No active debug session. Use debug_launch first."

    file_path = os.path.abspath(params.file)
    try:
        bps = await _session.set_breakpoints(file_path, params.lines)
        lines_out = []
        for bp in bps:
            status = "verified" if bp.get("verified") else "pending"
            lines_out.append(f"  Line {bp.get('line', '?')}: {status}")
        return f"Breakpoints in {file_path}:\n" + "\n".join(lines_out)
    except Exception as e:
        return f"Error setting breakpoints: {e}"


# ---- Execution control ----

@mcp.tool(
    name="debug_continue",
    annotations={
        "title": "Continue execution",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def debug_continue() -> str:
    """Resume execution until the next breakpoint or program end.

    Returns:
        str: The stop reason and location, or 'program ended'.
    """
    if _session is None:
        return "Error: No active debug session."
    try:
        await _session.continue_execution()
        stop = await _session.wait_for_stop(timeout=60.0)
        if stop is None:
            return "Program ended (no more stops)."
        return await _describe_stop(stop)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool(
    name="debug_step_over",
    annotations={
        "title": "Step over (next line)",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def debug_step_over() -> str:
    """Execute the current line and stop at the next line (step over calls).

    Returns:
        str: Current position after stepping.
    """
    if _session is None:
        return "Error: No active debug session."
    try:
        await _session.step_over()
        stop = await _session.wait_for_stop(timeout=30.0)
        return await _describe_stop(stop)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool(
    name="debug_step_into",
    annotations={
        "title": "Step into function",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def debug_step_into() -> str:
    """Step into the function call on the current line.

    Returns:
        str: Current position after stepping.
    """
    if _session is None:
        return "Error: No active debug session."
    try:
        await _session.step_into()
        stop = await _session.wait_for_stop(timeout=30.0)
        return await _describe_stop(stop)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool(
    name="debug_step_out",
    annotations={
        "title": "Step out of function",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def debug_step_out() -> str:
    """Run until the current function returns, then pause.

    Returns:
        str: Current position after stepping out.
    """
    if _session is None:
        return "Error: No active debug session."
    try:
        await _session.step_out()
        stop = await _session.wait_for_stop(timeout=30.0)
        return await _describe_stop(stop)
    except Exception as e:
        return f"Error: {e}"


# ---- Inspection ----

class StackTraceInput(BaseModel):
    """Input for getting the stack trace."""
    thread_id: int = Field(default=1, description="Thread ID (default: 1 for main thread)")
    levels: int = Field(default=20, description="Maximum number of frames to return", ge=1, le=100)


@mcp.tool(
    name="debug_stack_trace",
    annotations={
        "title": "Get stack trace",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def debug_stack_trace(params: StackTraceInput) -> str:
    """Get the current call stack when the debugger is paused.

    Shows the chain of function calls that led to the current position,
    with file paths and line numbers. Frame IDs can be used with
    debug_variables and debug_evaluate.

    Args:
        params (StackTraceInput): Thread ID and max depth.

    Returns:
        str: Formatted stack trace with frame IDs.
    """
    if _session is None:
        return "Error: No active debug session."
    try:
        frames = await _session.get_stack_trace(params.thread_id, params.levels)
        if not frames:
            return "No stack frames (program may have ended)."

        lines = ["Call stack (most recent first):\n"]
        for f in frames:
            fid = f.get("id", "?")
            lines.append(f"  [frame {fid}] {f.get('name', '?')} — "
                         f"{f.get('source', {}).get('path', '?')}:{f.get('line', '?')}")
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


class VariablesInput(BaseModel):
    """Input for inspecting variables."""
    frame_id: int = Field(..., description="Frame ID from debug_stack_trace")
    scope: str = Field(
        default="locals",
        description="Which scope to inspect: 'locals', 'globals', or 'all'"
    )


@mcp.tool(
    name="debug_variables",
    annotations={
        "title": "Inspect variables",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def debug_variables(params: VariablesInput) -> str:
    """Inspect local and/or global variables in the given stack frame.

    Use frame IDs from debug_stack_trace. Returns variable names,
    values, and types.

    Args:
        params (VariablesInput): Frame ID and scope filter.

    Returns:
        str: Formatted list of variables with their values and types.
    """
    if _session is None:
        return "Error: No active debug session."
    try:
        scopes = await _session.get_scopes(params.frame_id)
        results = []
        for scope in scopes:
            scope_name = scope.get("name", "").lower()
            if params.scope != "all" and params.scope not in scope_name:
                continue

            ref = scope.get("variablesReference")
            if not ref:
                continue

            variables = await _session.get_variables(ref)
            results.append(f"\n--- {scope.get('name', 'Scope')} ---")
            for v in variables:
                results.append(_fmt_variable(v))

        return "\n".join(results) if results else "No variables in the requested scope."
    except Exception as e:
        return f"Error: {e}"


class EvaluateInput(BaseModel):
    """Input for evaluating an expression."""
    expression: str = Field(..., description="Python expression to evaluate (e.g. 'len(items)', 'x + y')")
    frame_id: int = Field(..., description="Frame ID from debug_stack_trace for evaluation context")


@mcp.tool(
    name="debug_evaluate",
    annotations={
        "title": "Evaluate expression",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def debug_evaluate(params: EvaluateInput) -> str:
    """Evaluate a Python expression in the context of a paused stack frame.

    This is like typing in the pdb REPL. You can inspect values,
    call methods, check conditions, etc.

    Args:
        params (EvaluateInput): Expression string and frame ID.

    Returns:
        str: The result of the expression, or an error message.
    """
    if _session is None:
        return "Error: No active debug session."
    try:
        result = await _session.evaluate(params.expression, params.frame_id)
        val = result.get("result", "None")
        typ = result.get("type", "")
        return f"{val}  (type: {typ})"
    except Exception as e:
        return f"Evaluation error: {e}"


# ---- Status ----

@mcp.tool(
    name="debug_status",
    annotations={
        "title": "Debug session status",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def debug_status() -> str:
    """Check the current state of the debug session.

    Returns:
        str: Session state (active/inactive), process status, recent events.
    """
    if _session is None:
        return "No active debug session. Use debug_launch to start one."

    lines = ["Debug session active.\n"]

    # Check process
    if _session.process:
        rc = _session.process.returncode
        if rc is None:
            lines.append("  Process: running")
        else:
            lines.append(f"  Process: exited (code {rc})")

    # Show last few events
    recent = _session.recent_events(5)
    if recent:
        lines.append("\n  Recent events:")
        for ev in recent:
            evt = ev.get("event", "?")
            body = ev.get("body", {})
            reason = body.get("reason", "")
            lines.append(f"    - {evt}" + (f" ({reason})" if reason else ""))

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _describe_stop(stop: dict | None) -> str:
    """Build a human-readable description of a stop event, including location."""
    if stop is None:
        return "Program ended."

    reason = stop.get("reason", "unknown")
    thread_id = stop.get("threadId", 1)

    lines = [f"Stopped: {reason}"]

    if _session:
        try:
            frames = await _session.get_stack_trace(thread_id, levels=3)
            if frames:
                top = frames[0]
                src = top.get("source", {}).get("path", "?")
                line = top.get("line", "?")
                name = top.get("name", "?")
                lines.append(f"  → {name} at {src}:{line}  [frame {top.get('id', '?')}]")
        except Exception:
            pass

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    mcp.run()


if __name__ == "__main__":
    main()
