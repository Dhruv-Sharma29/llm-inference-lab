# 02 — Admission and limits

What the gateway decides before a request is allowed to occupy the model server, and how those
decisions are bounded. The error codes themselves are in [01](01-architecture.md) § Error
contract.

## The check order, and why it is this order

`chat_completions()` in `gateway/app.py` runs these checks in sequence. The first failure ends the
request:

| # | Check | Cost | Rejection |
|---|---|---|---|
| 1 | Request ID: accept `X-Request-ID` if it matches `^[A-Za-z0-9._-]{1,64}$`, otherwise generate a UUID | regex | none; an unsafe ID is replaced (`test_unsafe_request_id_is_replaced`) |
| 2 | Auth: `Bearer` key in `GATEWAY_API_KEYS` | set lookup | `401 unauthorized` |
| 3 | Quota: per-key token bucket | arithmetic | `429 rate_limited` + `Retry-After` |
| 4 | Body size: declared `Content-Length`, then the bytes actually read | streaming read | `413 body_too_large` |
| 5 | JSON shape: an object with a non-empty `messages` list of objects | parse | `400 invalid_json` / `invalid_request` |
| 6 | Input tokens over the limit | tokenizer pass | `413 input_too_large`, with the size and the limit in the message |
| 7 | `max_tokens` / `max_completion_tokens` an integer in `1..MAX_OUTPUT_TOKENS` | comparison | `400 invalid_max_tokens` |
| 8 | Bounded queue | wait | `429 queue_full` / `503 queue_timeout` |

The order is cheapest first, with one deliberate exception: auth and quota come **before** the
body is read. An unauthenticated client cannot make the gateway buffer up to `MAX_BODY_BYTES`.
One consequence follows from this order: a quota token is spent before the body is validated, so
malformed requests count against a key's quota. That is intended. A client sending garbage in a
loop is exactly the client that quota exists for.

Body size is checked twice. A declared `Content-Length` over the limit is rejected without reading
anything. Otherwise the stream is read chunk by chunk and rejected as soon as it crosses the
limit, so a client that lies about its length still cannot make the gateway buffer more than the
limit.

## The bounded queue

`AdmissionController` (`gateway/admission.py`) is an `asyncio.Semaphore(max_inflight)` plus a
`waiting` counter:

- If every slot is taken **and** `waiting >= max_queue`, raise `429 queue_full` at once with
  `Retry-After: 1`. The request never waits.
- Otherwise join the wait and `wait_for(sem.acquire(), queue_timeout_s)`. On timeout, raise
  `503 queue_timeout` with `Retry-After: 1`.
- On success, return a `Ticket`. `release()` is idempotent, so every exit path can call it.

Two different statuses for two different situations. `429` means "full now, don't even wait".
`503` means "admitted to the queue, but no slot freed in time". A client can tell from the status
whether its request was ever close to being served.

**Evidence.** In `test_queue_saturation_is_rejected_explicitly` (2 running + 2 waiting), the fifth
request gets `429 queue_full` with `Retry-After` in under 0.5 s, and the four held requests all
finish with `200`. In `test_queue_wait_is_bounded_by_timeout`, a 0.5 s queue timeout produces
`503` after 0.4–1.5 s. Against llama.cpp, a burst of 20 against 4 running + 4 queued gave 8 × `200`
and 12 × `429 queue_full`. The slowest rejection took 0.07 s, and peak waiting never went above 4
(`results/summary/real_upstream_checks.json` → `overload`).

## Leaving the queue

`acquire_slot()` races the semaphore wait against a task that watches for `http.disconnect`. If
the client goes first, the gateway cancels its place in the queue. If the slot was granted in the
same instant, it releases that slot. The request is logged as `499 client_cancelled` and is
**never forwarded**. `test_client_leaving_the_queue_never_reaches_upstream` asserts that the
mock's `started` counter stays at 1 (the request holding the slot) and that the gateway returns to
`inflight 0, waiting 0`.

The reason this matters: under overload, clients time out and retry. Without this step, each
abandoned request still reaches the GPU later and produces output nobody reads, at exactly the
moment capacity is scarcest.

## Per-key quotas

`QuotaLimiter` (`gateway/quota.py`) is a token bucket per `key_id`. The bucket holds up to
`QUOTA_BURST` tokens and refills at `QUOTA_RPS` tokens per second. A rejected request gets
`Retry-After` set to the time until the next token, rounded and at least 1 s. `QUOTA_RPS=0`
(the default) disables quotas.

- `key_id` is `k_` plus the first 8 hex characters of the key's SHA-256. The raw key never
  appears in logs or metrics.
- With auth disabled, every client is `anonymous` and **shares one bucket**.
- The buckets live in the process. Two gateway replicas would each grant the full quota.

`test_per_key_quota` (0.1 rps, burst 2) gets `[200, 200, 429]` for key A, and key B is unaffected.
PLAN M3 still lists "quotas in the overload demo recording" as open.

## Token counting

`TokenCounter` (`gateway/tokens.py`) counts message contents only:

- With `GATEWAY_TOKENIZER_FILE` set, the count is exact for each message's text (the model's
  `tokenizer.json`, no special tokens), plus `PER_MESSAGE_OVERHEAD = 8` per message as an allowance
  for the chat template.
- Without it, the count is `ceil(chars / 4)`. It is approximate, and it is the default:
  `env/serve.env` still has `GATEWAY_TOKENIZER_FILE=TBD`.

What it does **not** count: a `tools` array, non-text content parts, and the template's exact
framing. For the tool-calling clients in the companion projects, tool schemas can be a large part
of the prompt, and the gateway does not see them. The model server's own context check is the
backstop, and its `4xx` is passed through (`test_upstream_4xx_is_passed_through`). The rejection
message always prints the count as `~N`, even when the count is exact.

Against the real server with the exact tokenizer, a 5,000-word input was rejected as
`"Input is ~5009 tokens; the limit is 3500."` (`real_upstream_checks.json` → `oversized`).

## Output limit

If a request omits `max_tokens`, the gateway **fills in `MAX_OUTPUT_TOKENS`** (default 1024)
instead of leaving it to the server. Every forwarded request then has a known upper bound on its
generation. The cost is that a client that forgets the parameter reserves the maximum. A value
outside `1..MAX_OUTPUT_TOKENS`, or a non-integer such as `"10"`, is a `400`
(`test_invalid_max_tokens`, three cases).

## Sizing the limits

The defaults (`MAX_INFLIGHT=8`, `MAX_QUEUE=32`, `QUEUE_TIMEOUT_S=10`) are **placeholders**.
`env/serve.env` marks `GATEWAY_MAX_INFLIGHT=8` as an "initial guess; set from Exp 1 (concurrency
knee)", and Exp 1 has not been run ([06](06-roadmap.md)). Two constraints are already clear from
the code and the recorded configuration.

**`max_inflight` SHOULD NOT exceed what the server can run at once.** Above that, requests queue
*inside* the server, where the gateway can neither reject them early nor see how long they have
waited. The two servers differ here. llama.cpp runs with 4 slots (`scripts/llamacpp_serve.sh`,
`PARALLEL=4`). The vLLM launch uses `--max-num-seqs 16` (`scripts/host_run_vllm.sh`). The
real-server checks used 4 running + 4 queued (`INFLIGHT, QUEUE = 4, 4` in
`scripts/real_upstream_checks.py`), which matches the 4 llama.cpp slots. The default of 8 does not
match either server.

**A queue deeper than the timeout can drain turns into delayed `503`s.** This is arithmetic on
smoke numbers that are not a baseline. The smoke run's median end-to-end latency was about 10.3 s
at concurrency 1 with `max_tokens` 96 (`results/summary/smoke-llamacpp-sharedgpu.md`). With 8
slots, a request at queue position *p* waits roughly ⌈*p*/8⌉ service times. Positions 9–32 would
wait 20 s or more against a 10 s queue timeout, so most of a full default queue would end in
`503 queue_timeout`, not `200`. A rough rule is that the useful queue depth is about
`max_inflight × queue_timeout_s / service_time`. Exp 1 should set all three numbers together, not
`max_inflight` alone.
