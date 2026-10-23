"""Streaming relay with guaranteed cleanup.

Starlette's StreamingResponse (1.x, ASGI spec >= 2.4) neither closes the body iterator nor runs
background tasks when the client disconnects, and only notices the disconnect on the next send.
For an LLM stream that means a cancelled client can keep its gateway slot and keep the model
server generating. ManagedStreamingResponse watches for the disconnect concurrently and always
closes the iterator and the upstream response.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable

import anyio
import httpx
from starlette.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from .admission import Ticket


class ManagedStreamingResponse(StreamingResponse):
    def __init__(self, content: AsyncIterator[bytes], on_close: Callable[[], Awaitable[None]], **kwargs):
        super().__init__(content, **kwargs)
        self._on_close = on_close

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            async with anyio.create_task_group() as tg:

                async def stream() -> None:
                    try:
                        await self.stream_response(send)
                    except OSError:  # client went away mid-send
                        pass
                    tg.cancel_scope.cancel()

                async def watch_disconnect() -> None:
                    await self.listen_for_disconnect(receive)
                    tg.cancel_scope.cancel()

                tg.start_soon(stream)
                tg.start_soon(watch_disconnect)
        finally:
            with anyio.CancelScope(shield=True):
                await self.body_iterator.aclose()
                await self._on_close()


def sse_error(code: str, message: str, request_id: str) -> bytes:
    payload = {"error": {"type": code, "message": message, "request_id": request_id}}
    return f"data: {json.dumps(payload)}\n\n".encode()


class StreamSession:
    """Relays one upstream SSE stream and records what happened to it."""

    def __init__(self, resp: httpx.Response, ticket: Ticket, request_id: str, started: float,
                 on_finish: Callable[[StreamSession], None]):
        self.resp = resp
        self.ticket = ticket
        self.request_id = request_id
        self.started = started
        self.on_finish = on_finish
        self.outcome: str | None = None
        self.ttft_s: float | None = None
        self.output_tokens: int | None = None
        self._closed = False

    def _observe(self, line: str) -> None:
        # Usage only appears if the client asked for stream_options.include_usage.
        if line.startswith("data: {") and '"usage"' in line:
            try:
                usage = json.loads(line[6:]).get("usage") or {}
                self.output_tokens = usage.get("completion_tokens", self.output_tokens)
            except ValueError:
                pass

    async def iterate(self, deadline: float) -> AsyncIterator[bytes]:
        lines = self.resp.aiter_lines()
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                try:
                    line = await asyncio.wait_for(anext(lines), remaining)
                except StopAsyncIteration:
                    break
                if self.ttft_s is None:
                    self.ttft_s = time.monotonic() - self.started
                self._observe(line)
                yield (line + "\n").encode()
            self.outcome = "ok"
        except TimeoutError:
            self.outcome = "timeout"
            yield sse_error("timeout", "Request deadline exceeded during generation.", self.request_id)
        except httpx.TransportError:
            self.outcome = "upstream_error"
            yield sse_error("upstream_error", "Model server connection failed mid-stream.", self.request_id)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self.resp.aclose()  # drops the connection, so the model server aborts generation
        finally:
            self.ticket.release()
            if self.outcome is None:
                self.outcome = "client_cancelled"
            self.on_finish(self)
