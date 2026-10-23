# 03 — Streaming and cancellation

A streamed generation is a long-lived connection on the client side and a long-lived GPU sequence
on the server side. This doc covers how the gateway keeps the two coupled. When the client goes,
the generation stops. When the server goes, the client learns quickly. No stream outlives its
deadline.

## The problem with the stock response class

`gateway/streaming.py` explains the motivation in its docstring. In Starlette 1.x, under ASGI spec
≥ 2.4, `StreamingResponse` notices a disconnect only when a send fails, never closes the body
iterator, and skips background tasks on disconnect. For an LLM stream, a cancelled client can then
keep both its gateway slot and the server's sequence. Two cases make this worse:

- **The silent stream.** Before the first token (long prefill, a queue inside the server), nothing
  is being sent, so no send can fail and nothing notices the disconnect.
  `test_client_cancel_while_upstream_is_silent` covers exactly this: the mock waits 30 s before its
  first token, and the client gives up after 0.5 s.
- **The cleanup that never runs.** If closing the upstream depends on code that runs after the
  body is sent, a disconnect skips it.

## The mechanism

`ManagedStreamingResponse.__call__` runs two tasks in one `anyio` task group:

| Task | Ends when | Then |
|---|---|---|
| `stream()` | the body iterator is exhausted, or a send raises `OSError` (client gone) | cancels the group |
| `watch_disconnect()` | `listen_for_disconnect` sees `http.disconnect` | cancels the group |

Whichever finishes first cancels the other. Then, in a `finally` under a **shielded** cancel
scope, so it runs even while the task is being cancelled:

1. `body_iterator.aclose()` closes the generator in `StreamSession.iterate`.
2. `on_close()` is `StreamSession.close()`, which is idempotent:
   - `resp.aclose()` drops the upstream HTTP connection. Closing the connection is the signal the
     server acts on: it aborts the sequence.
   - `ticket.release()` frees the gateway slot.
   - If no outcome was recorded, the outcome becomes `client_cancelled`, and `on_finish` writes the
     single log line and metric.

The mock upstream uses the same class for its own streams (`mock_upstream/server.py`). Without it,
the mock would not reliably notice that the gateway had closed the connection, so the tests would
be checking the mock, not the gateway.

## One deadline, end to end

`REQUEST_TIMEOUT_S` (default 120 s) starts when the request is received, and it covers everything
that follows:

| Phase | Enforced by | On expiry |
|---|---|---|
| Queueing | `QUEUE_TIMEOUT_S` (a separate, shorter bound) | `503 queue_timeout` |
| Connect + response headers | `wait_for(client.send(...), remaining)` | `504 timeout` |
| Each streamed line | `wait_for(anext(lines), deadline - now)` | in-band `timeout` event |
| Non-streaming body | `wait_for(client.post(...), remaining)` | `504 timeout` |

The httpx client has `read=None` on purpose. A fixed per-read timeout would either kill a slow
but healthy generation or allow a stalled one far beyond the request's budget. The remaining
deadline is the only read timeout that means anything. `test_hung_upstream_hits_deadline_stream`
and `..._non_stream` (1 s deadline, upstream that never answers) both finish in under 2.5 s, with
the slot released and the mock's active count back at 0.

## Errors after the status line

Once a stream has sent `200`, the status cannot change. A failure mid-stream is therefore reported
in-band, as one SSE event with no `[DONE]` after it:

```text
data: {"error": {"type": "upstream_error", "message": "Model server connection failed mid-stream.", "request_id": "..."}}
```

`benchmarks/client/bench.py` treats any event that has an `error` key as a failed request, so the
summary counts these as errors and not as short successes. There is one gap, recorded in
[09](09-implementation-notes.md) § 10. A stream that ends cleanly **without** `[DONE]` and without
an error event (the upstream closes normally partway through) is recorded as `ok` by both the
gateway and the client. The client never checks that it saw `[DONE]`.

## Upstream crash

The crash case turns a connection failure into a fast, explicit error instead of a hang:

- **Mid-stream:** `httpx.TransportError` in `iterate` → outcome `upstream_error` → in-band error
  event → `close()` releases the slot.
- **Next request:** the connect fails within `CONNECT_TIMEOUT_S` → `502 upstream_unavailable`.

`test_upstream_crash_mid_stream_terminates_requests` SIGKILLs the mock after 4 lines. The stream
ends with `upstream_error` in under 5 s (against a 60 s deadline), the slot is released, and the
next request gets `502` in under 3 s.

## Evidence against a real server

All of these come from `results/summary/real_upstream_checks.json` (2026-10-07, llama.cpp stopgap,
Qwen2.5-7B Q4_K_M, GPU at 95 % utilisation before the run). They are behaviour checks, not
benchmarks (`scripts/real_upstream_checks.py` docstring).

| Check | Recorded result |
|---|---|
| Client disconnects after 10 content chunks of a 400-token request | 1 slot busy while streaming. Slot idle **0.4 s** after disconnect. **11** tokens generated in total, **0** in the 3 s after idle |
| Model server SIGKILLed after 8 lines | Stream ended **0.01 s** after the kill with an in-band `upstream_error`. Next request `502 upstream_unavailable` in **0.04 s**. Server restarted |
| Basics | Non-stream `200` with text `"OK"`. Stream `200` ending in `data: [DONE]` |

The cancellation check measures what matters. It reads llama.cpp's own
`llamacpp:tokens_predicted_total` counter and `/slots` state, so it shows that the *server*
stopped generating, not just that the client stopped reading.

**Not yet shown:** the same thing on vLLM. PLAN M3 says to confirm it with vLLM's running-requests
metric, once the host server exists ([RUNBOOK.md](../RUNBOOK.md) § 1).

## What the tests prove, and how we know they are not vacuous

Both cancellation tests assert on the mock's `/stats` (`active` back to 0, `aborted == 1`), not on
anything the client can see. [01](01-architecture.md) § Decisions records a mutation test from
2026-10-06: with Starlette's stock `StreamingResponse` swapped back in, both cancellation tests
fail. No artefact of that run is committed. The conditions under which it holds are discussed in
[09](09-implementation-notes.md) § 1, marked **[verify]**.

## Known gap: non-streaming disconnects

A non-streaming request whose client disconnects **after** it has been forwarded runs to
completion upstream ([01](01-architecture.md) § Known limitations). Disconnects are watched only
while a request is queued and while it is streaming. The benchmark client always streams
(`"stream": true` in `bench.py`), so the gap does not affect measurements. It does affect any
companion project that calls the gateway without streaming. **[verify]** which of them do.
