"""Inference gateway: an OpenAI-compatible /v1/chat/completions front for a local model server.

Order of checks for each request (cheapest first, nothing reaches the model unless all pass):
auth -> quota -> body size -> JSON shape -> input tokens / max_tokens -> bounded queue -> upstream.

Run:  uvicorn --factory gateway.app:create_app_from_env --host 127.0.0.1 --port 9000
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from .admission import AdmissionController, Ticket
from .config import Settings
from .errors import GatewayError
from .metrics import Metrics
from .quota import QuotaLimiter
from .streaming import ManagedStreamingResponse, StreamSession
from .tokens import TokenCounter

log = logging.getLogger("gateway")

REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
# The only fields that may appear in a request log line. Prompt text, outputs, and credentials
# are excluded by construction rather than by remembering to redact them.
LOG_FIELDS = ("request_id", "key_id", "stream", "status", "outcome", "input_tokens",
              "output_tokens", "queue_ms", "ttft_ms", "total_ms")


class ClientGone(Exception):
    """The client disconnected before the request reached the model server."""


@dataclass
class RequestContext:
    request_id: str
    started: float
    key_id: str = "-"
    stream: bool = False
    input_tokens: int | None = None
    queue_ms: float | None = None


def _request_id(request: Request) -> str:
    supplied = request.headers.get("x-request-id", "")
    return supplied if REQUEST_ID_RE.match(supplied) else uuid.uuid4().hex


def create_app(settings: Settings) -> FastAPI:
    admission = AdmissionController(settings.max_inflight, settings.max_queue)
    quota = QuotaLimiter(settings.quota_rps, settings.quota_burst)
    counter = TokenCounter(settings.tokenizer_file)
    metrics = Metrics()
    metrics.inflight.set_function(lambda: admission.inflight)
    metrics.waiting.set_function(lambda: admission.waiting)
    upstream_headers = {"Authorization": f"Bearer {settings.upstream_api_key}"} if settings.upstream_api_key else {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.client = httpx.AsyncClient(
            base_url=settings.upstream_url,
            headers=upstream_headers,
            # Read timeout is governed by the per-request deadline instead.
            timeout=httpx.Timeout(connect=settings.connect_timeout_s, read=None, write=10.0, pool=10.0),
            limits=httpx.Limits(max_connections=settings.max_inflight + 4,
                                max_keepalive_connections=settings.max_inflight),
        )
        yield
        await app.state.client.aclose()

    app = FastAPI(title="inference-gateway", lifespan=lifespan)

    def finish(ctx: RequestContext, status: int, outcome: str, ttft_s: float | None = None,
               output_tokens: int | None = None) -> None:
        total_s = time.monotonic() - ctx.started
        metrics.requests.labels(outcome=outcome).inc()
        metrics.latency.labels(stream=str(ctx.stream).lower()).observe(total_s)
        if ttft_s is not None:
            metrics.ttft.observe(ttft_s)
        fields = {
            "request_id": ctx.request_id, "key_id": ctx.key_id, "stream": ctx.stream, "status": status,
            "outcome": outcome, "input_tokens": ctx.input_tokens, "output_tokens": output_tokens,
            "queue_ms": ctx.queue_ms, "ttft_ms": None if ttft_s is None else round(ttft_s * 1000, 1),
            "total_ms": round(total_s * 1000, 1),
        }
        log.info(json.dumps({k: fields[k] for k in LOG_FIELDS}, separators=(",", ":")))

    def authenticate(request: Request) -> str:
        if not settings.api_keys:
            return "anonymous"
        scheme, _, key = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or key not in settings.api_keys:
            raise GatewayError(401, "unauthorized", "Missing or invalid API key.")
        return "k_" + hashlib.sha256(key.encode()).hexdigest()[:8]

    async def read_body(request: Request) -> dict[str, Any]:
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > settings.max_body_bytes:
            raise GatewayError(413, "body_too_large", f"Request body exceeds {settings.max_body_bytes} bytes.")
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > settings.max_body_bytes:
                raise GatewayError(413, "body_too_large", f"Request body exceeds {settings.max_body_bytes} bytes.")
            chunks.append(chunk)
        try:
            body = json.loads(b"".join(chunks))
        except ValueError:
            raise GatewayError(400, "invalid_json", "Request body is not valid JSON.") from None
        if not isinstance(body, dict):
            raise GatewayError(400, "invalid_request", "Request body must be a JSON object.")
        return body

    def check_limits(body: dict[str, Any], ctx: RequestContext) -> None:
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages or not all(isinstance(m, dict) for m in messages):
            raise GatewayError(400, "invalid_request", "`messages` must be a non-empty list of objects.")
        ctx.stream = body.get("stream") is True
        ctx.input_tokens = counter.count_messages(messages)
        if ctx.input_tokens > settings.max_input_tokens:
            raise GatewayError(413, "input_too_large",
                               f"Input is ~{ctx.input_tokens} tokens; the limit is {settings.max_input_tokens}.")
        requested = body.get("max_completion_tokens", body.get("max_tokens"))
        if requested is None:
            body["max_tokens"] = settings.max_output_tokens
        elif not isinstance(requested, int) or requested < 1 or requested > settings.max_output_tokens:
            raise GatewayError(400, "invalid_max_tokens",
                               f"max_tokens must be an integer from 1 to {settings.max_output_tokens}.")

    async def wait_for_disconnect(request: Request) -> None:
        while (await request.receive())["type"] != "http.disconnect":
            pass

    async def acquire_slot(request: Request) -> Ticket:
        """Wait for capacity, but give the place up if the client disconnects while queued."""
        acquire = asyncio.ensure_future(admission.acquire(settings.queue_timeout_s))
        watcher = asyncio.ensure_future(wait_for_disconnect(request))
        try:
            await asyncio.wait({acquire, watcher}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            watcher.cancel()
        if acquire.done():
            return acquire.result()  # raises GatewayError on queue_full / queue_timeout
        acquire.cancel()
        await asyncio.wait({acquire})
        if not acquire.cancelled() and acquire.exception() is None:
            acquire.result().release()
        raise ClientGone

    def upstream_error_response(ctx: RequestContext, status: int, content: bytes) -> tuple[Response, str]:
        if 400 <= status < 500:  # the model server rejected the request itself (e.g. context length)
            try:
                payload = json.loads(content)
            except ValueError:
                payload = {"error": {"type": "upstream_rejected", "message": "Model server rejected the request."}}
            return JSONResponse(payload, status_code=status, headers={"X-Request-ID": ctx.request_id}), "upstream_rejected"
        err = GatewayError(502, "upstream_error", f"Model server returned HTTP {status}.")
        return err.response(ctx.request_id), "upstream_error"

    def remaining(ctx: RequestContext) -> float:
        return max(0.0, ctx.started + settings.request_timeout_s - time.monotonic())

    async def proxy_once(request: Request, ctx: RequestContext, body: dict[str, Any], ticket: Ticket) -> Response:
        client: httpx.AsyncClient = request.app.state.client
        try:
            resp = await asyncio.wait_for(
                client.post("/v1/chat/completions", json=body, headers={"X-Request-ID": ctx.request_id}),
                remaining(ctx))
        except TimeoutError:
            raise GatewayError(504, "timeout", "Request deadline exceeded.") from None
        except httpx.TransportError:
            raise GatewayError(502, "upstream_unavailable", "Model server is unreachable.") from None
        finally:
            ticket.release()
        if resp.status_code != 200:
            response, outcome = upstream_error_response(ctx, resp.status_code, resp.content)
            finish(ctx, response.status_code, outcome)
            return response
        usage = (resp.json().get("usage") or {}) if resp.content else {}
        finish(ctx, 200, "ok", output_tokens=usage.get("completion_tokens"))
        return Response(resp.content, media_type="application/json", headers={"X-Request-ID": ctx.request_id})

    async def start_stream(request: Request, ctx: RequestContext, body: dict[str, Any], ticket: Ticket) -> Response:
        client: httpx.AsyncClient = request.app.state.client
        upstream_req = client.build_request("POST", "/v1/chat/completions", json=body,
                                            headers={"X-Request-ID": ctx.request_id})
        try:
            resp = await asyncio.wait_for(client.send(upstream_req, stream=True), remaining(ctx))
        except TimeoutError:
            ticket.release()
            raise GatewayError(504, "timeout", "Request deadline exceeded before generation started.") from None
        except httpx.TransportError:
            ticket.release()
            raise GatewayError(502, "upstream_unavailable", "Model server is unreachable.") from None
        if resp.status_code != 200:
            content = await resp.aread()
            await resp.aclose()
            ticket.release()
            response, outcome = upstream_error_response(ctx, resp.status_code, content)
            finish(ctx, response.status_code, outcome)
            return response

        def on_finish(s: StreamSession) -> None:
            finish(ctx, 200, s.outcome or "client_cancelled", ttft_s=s.ttft_s, output_tokens=s.output_tokens)

        session = StreamSession(resp, ticket, ctx.request_id, ctx.started, on_finish)
        return ManagedStreamingResponse(
            session.iterate(ctx.started + settings.request_timeout_s), on_close=session.close,
            media_type="text/event-stream",
            headers={"X-Request-ID": ctx.request_id, "Cache-Control": "no-cache"},
        )

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> Response:
        ctx = RequestContext(request_id=_request_id(request), started=time.monotonic())
        try:
            ctx.key_id = authenticate(request)
            quota.check(ctx.key_id)
            body = await read_body(request)
            check_limits(body, ctx)
            ticket = await acquire_slot(request)
            ctx.queue_ms = round((time.monotonic() - ctx.started) * 1000, 1)
            if ctx.stream:
                return await start_stream(request, ctx, body, ticket)
            return await proxy_once(request, ctx, body, ticket)
        except GatewayError as e:
            finish(ctx, e.status, e.code)
            return e.response(ctx.request_id)
        except ClientGone:
            finish(ctx, 499, "client_cancelled")
            return Response(status_code=499)

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"status": "ok", "inflight": admission.inflight, "waiting": admission.waiting}

    @app.get("/readyz")
    async def readyz(request: Request) -> Response:
        try:
            r = await request.app.state.client.get("/health", timeout=2.0)
            ready = r.status_code == 200
        except httpx.HTTPError:
            ready = False
        return JSONResponse({"ready": ready}, status_code=200 if ready else 503)

    @app.get("/metrics")
    async def prometheus_metrics() -> Response:
        return Response(generate_latest(metrics.registry), media_type=CONTENT_TYPE_LATEST)

    return app


def create_app_from_env() -> FastAPI:
    if not log.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        log.addHandler(handler)
        log.setLevel(logging.INFO)
        log.propagate = False
    return create_app(Settings.from_env())
