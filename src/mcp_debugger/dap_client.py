"""
DAP (Debug Adapter Protocol) client for communicating with debugpy.

Handles the low-level JSON protocol over TCP to control the debug session:
launch, breakpoints, stepping, variable inspection, expression evaluation.
"""

import asyncio
import json
from typing import Any, Optional
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# DAP message helpers
# ---------------------------------------------------------------------------

def _encode_dap_message(payload: dict) -> bytes:
    """Encode a DAP JSON payload with Content-Length header."""
    body = json.dumps(payload).encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
    return header + body


async def _read_dap_message(reader: asyncio.StreamReader) -> dict:
    """Read one DAP message (Content-Length framed) from *reader*."""
    # Read headers until blank line
    content_length = 0
    while True:
        line = await reader.readline()
        if line == b"\r\n" or line == b"\n":
            break
        if line.lower().startswith(b"content-length:"):
            content_length = int(line.split(b":")[1].strip())
    body = await reader.readexactly(content_length)
    return json.loads(body)


# ---------------------------------------------------------------------------
# Debugger state
# ---------------------------------------------------------------------------

@dataclass
class DebugSession:
    """Represents one active debug session connected to debugpy."""

    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    process: Optional[asyncio.subprocess.Process] = None

    _seq: int = 1
    _pending: dict[int, asyncio.Future] = field(default_factory=dict)
    _events: list[dict] = field(default_factory=list)
    _stopped_event: Optional[asyncio.Event] = field(default=None)
    _listener_task: Optional[asyncio.Task] = None
    _initialized: bool = False
    _terminated: bool = False

    def __post_init__(self):
        self._stopped_event = asyncio.Event()

    # ---- low-level transport ----

    async def _send_request(self, command: str, arguments: dict | None = None) -> dict:
        """Send a DAP request and wait for the matching response."""
        seq = self._seq
        self._seq += 1
        msg = {
            "seq": seq,
            "type": "request",
            "command": command,
        }
        if arguments:
            msg["arguments"] = arguments

        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[seq] = future

        self.writer.write(_encode_dap_message(msg))
        await self.writer.drain()

        return await asyncio.wait_for(future, timeout=30.0)

    async def _listen(self):
        """Background task: read messages and dispatch responses/events."""
        try:
            while True:
                msg = await _read_dap_message(self.reader)
                msg_type = msg.get("type")

                if msg_type == "response":
                    req_seq = msg.get("request_seq")
                    fut = self._pending.pop(req_seq, None)
                    if fut and not fut.done():
                        if msg.get("success"):
                            fut.set_result(msg.get("body", {}))
                        else:
                            fut.set_exception(
                                RuntimeError(msg.get("message", "DAP error"))
                            )

                elif msg_type == "event":
                    event_name = msg.get("event")
                    self._events.append(msg)
                    if event_name == "stopped":
                        self._stopped_event.set()
                    elif event_name == "terminated":
                        self._terminated = True
                        self._stopped_event.set()
                        break

        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        finally:
            self._cancel_pending_futures("listener ended")

    # ---- lifecycle ----

    async def initialize(self):
        """Send DAP initialize + launch handshake."""
        self._listener_task = asyncio.create_task(self._listen())

        await self._send_request("initialize", {
            "clientID": "mcp-debugger",
            "adapterID": "python",
            "pathFormat": "path",
            "supportsVariableType": True,
            "supportsRunInTerminalRequest": False,
            "locale": "en-US",
        })
        self._initialized = True

    async def launch(self, program: str, args: list[str] | None = None, cwd: str | None = None, stop_on_entry: bool = False):
        """Attach to the debugpy process that is already running the script.

        debugpy only responds to the ``attach`` request after
        ``configurationDone`` is received, so both must be sent
        concurrently to avoid a deadlock.

        When *stop_on_entry* is True, a temporary breakpoint is set on
        line 1 of *program* before ``configurationDone`` so the script
        pauses at the first executable line.
        """
        attach_args: dict[str, Any] = {
            "justMyCode": True,
            "stopOnEntry": stop_on_entry,
        }

        async def _do_attach():
            return await self._send_request("attach", attach_args)

        async def _do_configure():
            # Small delay so that "attach" is sent first.
            await asyncio.sleep(0.05)
            if stop_on_entry:
                await self.set_breakpoints(program, [1])
            await asyncio.sleep(0.05)
            return await self._send_request("configurationDone")

        await asyncio.gather(_do_attach(), _do_configure())

    async def disconnect(self):
        """Terminate the debug session."""
        try:
            await self._send_request("disconnect", {"terminateDebuggee": True})
        except Exception:
            pass
        self._cancel_pending_futures("disconnecting")
        self.writer.close()
        await self.writer.wait_closed()
        if self._listener_task:
            self._listener_task.cancel()
        if self.process:
            self.process.terminate()

    # ---- breakpoints ----

    async def set_breakpoints(self, file: str, lines: list[int]) -> list[dict]:
        """Set breakpoints in *file* at the given *lines*."""
        body = await self._send_request("setBreakpoints", {
            "source": {"path": file},
            "breakpoints": [{"line": ln} for ln in lines],
        })
        return body.get("breakpoints", [])

    async def set_function_breakpoint(self, name: str) -> list[dict]:
        """Set a breakpoint on a function by name."""
        body = await self._send_request("setFunctionBreakpoints", {
            "breakpoints": [{"name": name}],
        })
        return body.get("breakpoints", [])

    # ---- execution control ----

    async def continue_execution(self, thread_id: int = 1) -> dict:
        """Resume execution."""
        self._stopped_event.clear()
        return await self._send_request("continue", {"threadId": thread_id})

    async def step_over(self, thread_id: int = 1) -> dict:
        """Step over (next line)."""
        self._stopped_event.clear()
        return await self._send_request("next", {"threadId": thread_id})

    async def step_into(self, thread_id: int = 1) -> dict:
        """Step into."""
        self._stopped_event.clear()
        return await self._send_request("stepIn", {"threadId": thread_id})

    async def step_out(self, thread_id: int = 1) -> dict:
        """Step out of the current function."""
        self._stopped_event.clear()
        return await self._send_request("stepOut", {"threadId": thread_id})

    async def wait_for_stop(self, timeout: float = 60.0) -> dict | None:
        """Block until the debuggee hits a breakpoint or step completes."""
        try:
            await asyncio.wait_for(self._stopped_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return {"reason": "timeout"}
        if self._terminated:
            return None
        # Return the last stopped event body
        for ev in reversed(self._events):
            if ev.get("event") == "stopped":
                return ev.get("body", {})
        return None

    # ---- inspection ----

    async def get_threads(self) -> list[dict]:
        body = await self._send_request("threads")
        return body.get("threads", [])

    async def get_stack_trace(self, thread_id: int = 1, levels: int = 20) -> list[dict]:
        body = await self._send_request("stackTrace", {
            "threadId": thread_id,
            "startFrame": 0,
            "levels": levels,
        })
        return body.get("stackFrames", [])

    async def get_scopes(self, frame_id: int) -> list[dict]:
        body = await self._send_request("scopes", {"frameId": frame_id})
        return body.get("scopes", [])

    async def get_variables(self, variables_reference: int) -> list[dict]:
        body = await self._send_request("variables", {
            "variablesReference": variables_reference,
        })
        return body.get("variables", [])

    async def evaluate(self, expression: str, frame_id: int, context: str = "repl") -> dict:
        """Evaluate an expression in the given frame context.

        *context* can be 'watch', 'repl', or 'hover'.
        """
        return await self._send_request("evaluate", {
            "expression": expression,
            "frameId": frame_id,
            "context": context,
        })

    # ---- internal helpers ----

    def _cancel_pending_futures(self, reason: str):
        """Cancel all pending request futures with a descriptive message."""
        for seq, fut in self._pending.items():
            if not fut.done():
                fut.cancel(msg=f"DAP request {seq} cancelled: {reason}")
        self._pending.clear()
