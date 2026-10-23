# Architecture and design decisions

```text
                      ┌──────────────────────────── gateway (FastAPI, :9000) ──────────────────────────┐
benchmark client ───► │ auth → quota → body size → JSON shape → input tokens / max_tokens → bounded queue │ ───► model server
overload demo         │      401      429         413          400            413 / 400          429 / 503    │      (vLLM, :8000)
                      │                                                                                 │          │
                      │  streaming relay: deadline per read, disconnect watcher, always closes upstream │       GB10
                      └───────────┬───────────────────────────────────┬─────────────────────────────────┘
                                  │ JSON log line per request         │ /metrics (Prometheus)
                                  ▼ (metadata only)                   ▼
```

All checks run before a request can occupy model capacity, cheapest first.

## Error contract

| Situation | Status | `error.type` | Retry-After |
| --- | --- | --- | --- |
| Missing/invalid API key | 401 | `unauthorized` | – |
| Per-key quota exceeded | 429 | `rate_limited` | yes |
| Body over byte limit | 413 | `body_too_large` | – |
| Malformed JSON / messages | 400 | `invalid_json` / `invalid_request` | – |
| Input over token limit | 413 | `input_too_large` (states size and limit) | – |
| `max_tokens` out of range | 400 | `invalid_max_tokens` | – |
| Wait queue full | 429 | `queue_full` | yes |
| Waited too long for a slot | 503 | `queue_timeout` | yes |
| Model server unreachable | 502 | `upstream_unavailable` | – |
| Model server 5xx | 502 | `upstream_error` | – |
| Model server 4xx | passed through | upstream's body | – |
| Deadline before first byte | 504 | `timeout` | – |
| Deadline / upstream failure mid-stream | 200 then SSE `data: {"error": ...}`, no `[DONE]` | `timeout` / `upstream_error` | – |

Once a stream has started, the HTTP status is already sent, so mid-stream failures are signalled
in-band. A stream without `[DONE]` is never a success; the benchmark client counts it as an error.

## Decisions

**Bounded queue with immediate rejection.** Under overload an unbounded queue turns every request
into a slow one and eventually exhausts memory. Rejecting at the door keeps latency for admitted
requests predictable and tells clients to back off. `max_inflight` should come from the measured
concurrency knee (Exp 1), not a guess.

**Disconnect handling is explicit.** Starlette 1.x `StreamingResponse` (ASGI spec ≥ 2.4) only
notices a disconnect when a send fails, never closes the body iterator, and skips background tasks
on disconnect. Measured consequence (mutation test, 2026-10-06): with the stock class, both
cancellation tests fail — the upstream generation keeps running. `ManagedStreamingResponse`
watches for `http.disconnect` concurrently and always closes the upstream response, which makes
vLLM abort the sequence.

**Leaving the queue.** A client that disconnects while queued gives up its place and is never
forwarded. Without this, clients that time out and retry create ghost work exactly when the
system is most overloaded.

**Deadline covers everything.** One per-request deadline spans queueing, connect, and every
streamed read, so a hung model server cannot pin a slot indefinitely.

**Logs are allow-listed.** Request logs contain only fixed metadata fields
(`LOG_FIELDS` in `gateway/app.py`). Keys are logged as a short hash prefix. This is enforced by
construction and checked by a test that searches the log for prompt, output, and key sentinels.

**Token counting.** With `GATEWAY_TOKENIZER_FILE` set, input limits use the model's tokenizer
(plus a small per-message allowance for the chat template). Without it, a chars/4 heuristic is
used. The model server still enforces its own context length, and its 4xx is passed through.

## Known limitations

- A non-streaming request whose client disconnects *after* being forwarded still runs to
  completion upstream (disconnects are only watched while queued and while streaming).
- Quota and queue state are in-process: one gateway replica only. Multiple replicas would need a
  shared store; that is out of scope for v1.
- Disconnect detection while queued relies on the server delivering `http.disconnect` (uvicorn
  does). Behind a proxy that buffers requests, detection may be delayed.
