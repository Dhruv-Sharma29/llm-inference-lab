"""A stand-in for the model server, for gateway tests and harness development without a GPU.

Speaks the subset of the OpenAI chat API the gateway uses. Tokens are fake; timing is controlled
per request through an extra `mock` field in the body (ignored by real servers' clients):
  {"mock": {"ttft_s": 0.05, "token_delay_s": 0.01, "hang": false, "status": 200}}
GET /stats exposes how many generations are active, which the cancellation tests rely on.

Run:  python -m mock_upstream.server --port 8000
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

# Real model servers abort a generation when the client connection drops. Starlette's plain
# StreamingResponse doesn't reliably close the generator on disconnect, so reuse the gateway's.
from gateway.streaming import ManagedStreamingResponse

app = FastAPI(title="mock-upstream")
stats = {"active": 0, "started": 0, "completed": 0, "aborted": 0}


def _chunk(cid: str, delta: dict, finish: str | None = None) -> str:
    body = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()), "model": "mock",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    return f"data: {json.dumps(body)}\n\n"


@app.get("/health")
async def health() -> dict:
    return {"ok": True}


@app.get("/stats")
async def get_stats() -> dict:
    return stats


@app.post("/v1/chat/completions")
async def chat(request: Request):
    body = await request.json()
    opts = body.get("mock") or {}
    if opts.get("status", 200) != 200:
        return JSONResponse({"error": {"type": "mock_error", "message": "forced"}}, status_code=opts["status"])
    n_tokens = int(body.get("max_tokens") or 16)
    ttft_s = float(opts.get("ttft_s", 0.02))
    token_delay_s = float(opts.get("token_delay_s", 0.005))
    prompt_tokens = sum(len(str(m.get("content", ""))) // 4 + 1 for m in body["messages"])
    usage = {"prompt_tokens": prompt_tokens, "completion_tokens": n_tokens, "total_tokens": prompt_tokens + n_tokens}
    cid = "chatcmpl-" + uuid.uuid4().hex[:12]
    text = opts.get("text")  # optional fixed reply, used to exercise validators
    stats["started"] += 1

    if not body.get("stream"):
        stats["active"] += 1
        try:
            await asyncio.sleep(ttft_s + n_tokens * token_delay_s)
            if opts.get("hang"):
                await asyncio.sleep(3600)
        finally:
            stats["active"] -= 1
        stats["completed"] += 1
        content = text if text is not None else " ".join(f"tok{i}" for i in range(n_tokens))
        return {"id": cid, "object": "chat.completion", "model": "mock", "usage": usage,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                             "finish_reason": "length"}]}

    include_usage = bool((body.get("stream_options") or {}).get("include_usage"))

    async def events():
        stats["active"] += 1
        finished = False
        try:
            await asyncio.sleep(ttft_s)
            if opts.get("hang"):
                await asyncio.sleep(3600)
            yield _chunk(cid, {"role": "assistant", "content": ""})
            pieces = [text] if text is not None else [f"tok{i} " for i in range(n_tokens)]
            for piece in pieces:
                yield _chunk(cid, {"content": piece})
                await asyncio.sleep(token_delay_s)
            yield _chunk(cid, {}, finish="length")
            if include_usage:
                yield f"data: {json.dumps({'id': cid, 'object': 'chat.completion.chunk', 'choices': [], 'usage': usage})}\n\n"
            yield "data: [DONE]\n\n"
            finished = True
        finally:
            stats["active"] -= 1
            stats["completed" if finished else "aborted"] += 1

    async def noop() -> None:
        pass

    return ManagedStreamingResponse(events(), on_close=noop, media_type="text/event-stream")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
