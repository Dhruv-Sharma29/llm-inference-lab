# 09 — Implementation notes: where the code departs from the plan

These docs are the design record. When building something proved the plan, or the platform, wrong,
the note belongs here and not in a commit message. Each entry says what changed and why. None of
them changes the thesis or the scope in [00](00-charter.md).

## Platform behaviour found while building

### 1. The stock streaming response does not clean up on disconnect

PLAN M3 assumed that "gateway closes upstream stream" would follow naturally from a client
disconnect. It does not. `gateway/streaming.py` records the reason: in Starlette 1.x under ASGI
spec ≥ 2.4, `StreamingResponse` notices a disconnect only on a failed send, never closes the
iterator, and skips background tasks. `ManagedStreamingResponse` adds a concurrent disconnect
watcher and a shielded cleanup ([03](03-streaming-and-cancellation.md)).
[01](01-architecture.md) § Decisions records the check, a mutation test from 2026-10-06: with the
stock class, both cancellation tests fail.

**[verify]** The explanation depends on the ASGI spec version the server advertises, and no output
of the mutation run is committed. Before quoting it, rerun the two cancellation tests with the
pinned `uvicorn` 0.54.0 / `starlette` 1.7.0 (`uv.lock`) in two variants: (a) a bare stock
`StreamingResponse` with no close hook, and (b) a stock `StreamingResponse` with
`background=BackgroundTask(session.close)`. Then commit the output. If (b) passes, the accurate
claim is narrower: the managed class is needed to make cleanup independent of the server's
advertised spec version, not to make it work at all.

### 2. The mock needs the same fix

`mock_upstream/server.py` uses `ManagedStreamingResponse` for its own streams. Without it, the mock
would not reliably notice that the gateway had dropped the connection, and the cancellation tests
would be measuring the mock.

### 3. "Healthy" is not "ours"

The first stopgap used port 8000. A plain `/health` check there was answered by another service on
the machine (`env/ENVIRONMENT.md`). The stopgap moved to port 21000, and readiness in
`llamacpp_serve.sh` and `check_upstream.sh` now requires the model id `qwen2.5-7b-instruct` in
`/v1/models`. The gateway's own `/readyz` still checks `/health` only. That gap is open
([01](01-architecture.md) § Known limitations).

### 4. Zombies look alive

In the dev container, PID 1 is `tail`, which never reaps orphaned children. After the crash check
SIGKILLed the server, it lingered as a zombie, and `kill -0` still reported it alive, so the
restart logic thought nothing was wrong. `running()` in `llamacpp_serve.sh` now reads the
process state and treats `Z` as dead.

### 5. GB10 needs PTX; the script now matches the record

ENVIRONMENT.md: GB10 is compute capability 12.1, host CUDA 12.8 targets at most 12.0, and
llama.cpp's rewrite of `120` to `120a` fails on 12.1 with "no kernel image is available". The
recorded working build is `-DCMAKE_CUDA_ARCHITECTURES=OFF
-DCMAKE_CUDA_FLAGS=-gencode=arch=compute_120,code=compute_120`. That is plain PTX, JIT-compiled by
the driver on first use, and it needs `CUDA_CACHE_MAXSIZE` raised.

The committed `scripts/llamacpp_setup.sh` used to pass `-DCMAKE_CUDA_ARCHITECTURES=120`, the
setting that fails. Fixed 2026-10-23: the script now passes the recorded flags, and a configure-only
run of its cmake line into a scratch directory gives the same `CMAKE_CUDA_ARCHITECTURES` (OFF),
`CMAKE_CUDA_FLAGS`, `GGML_CUDA`, `LLAMA_CURL` and `CMAKE_BUILD_TYPE` as the working build's
`CMakeCache.txt`. Then a clean rebuild (2026-10-23) from the script's cmake line into an empty scratch directory built
`llama-server` at the same commit (`6753a03`); `--list-devices` shows `CUDA0: NVIDIA GB10`, and with
every layer offloaded (`-ngl 99`) it answered a request correctly ("What is 17 + 25?" → `42`) with no
"no kernel image" error. That closes S6 for the llama.cpp stopgap; the vLLM path was
still not run.

### 6. The GGUF converter imports sentencepiece for a BPE model

`convert_hf_to_gguf.py` imports `sentencepiece` before it discovers that Qwen2 has no
SentencePiece model and falls back to BPE. Without the package, the fallback never runs.
`llamacpp_setup.sh` installs it into its own small venv under `.tools/convert-venv`, which reuses
the host's torch and numpy through `--system-site-packages`, so the system Python is untouched.

## Deviations from the plan

### 7. A llama.cpp stopgap instead of waiting for vLLM

PLAN M1 assumed Docker. The dev environment cannot run it ([04](04-model-server.md)). Instead of
blocking M3 on host access, a llama.cpp server built from a pinned tag serves the same model
revision inside the container. The gateway is unchanged; only `GATEWAY_UPSTREAM_URL` differs. The
cost is that every real-server result is a llama.cpp result on Q4_K_M weights
([07](07-risks.md) § R2).

### 8. Layout

PLAN Progress records part of this: tests live in `gateway/tests/` and
`benchmarks/test_benchmarks.py`, and `mock_upstream/` was added so the gateway could be built and
tested without a GPU. The rest, read from the tree: `gateway/` is a flat package, not
`gateway/app/`, and `env/compose.yaml`, `monitoring/` and `benchmarks/configs/` do not exist.
Compose and monitoring wait on Docker and a baseline. Per-experiment configs wait on M4.

### 9. Leaving the queue, and `503` as a separate status

PLAN M3 lists "full queue → `429` (or `503`)". The build uses both, for different cases: `429
queue_full` at the door and `503 queue_timeout` after waiting. It also adds a case the plan did not
list: a client that disconnects while queued gives up its place and is never forwarded
(`test_client_leaving_the_queue_never_reaches_upstream`). Retrying clients would otherwise create
ghost work during overload ([02](02-admission-and-limits.md)).

### 10. Mid-stream failures are in-band, and the client now requires `[DONE]` (fixed 2026-10-23)

Once a stream has sent `200`, the status cannot change, so timeouts and upstream failures arrive as
an SSE `error` event with no `[DONE]` after it. [01](01-architecture.md) says *"a stream without
`[DONE]` is never a success; the benchmark client counts it as an error."* Until 2026-10-23
`bench.py` failed a request only when it saw an `error` event, so a stream truncated without one
counted as a success. It now records `stream_truncated` when a `200` stream ends without `[DONE]`;
`test_stream_without_done_is_an_error_not_a_success` checks both cases and fails on the old code.
The committed smoke run predates the fix; its 40 measured requests (and 2 warm-ups) all ended with
finish reason `stop`, so its numbers are unaffected.

### 11. The gateway fills in `max_tokens`

If a request omits it, the gateway sets `max_tokens = MAX_OUTPUT_TOKENS` instead of leaving the
choice to the server. Every forwarded request then has a known upper bound on its generation
([02](02-admission-and-limits.md) § Output limit).

### 12. The real-server checks use their own limits

`scripts/real_upstream_checks.py` runs the gateway with 4 running + 4 queued, `MAX_INPUT_TOKENS`
3500, `MAX_OUTPUT_TOKENS` 512, a 120 s queue timeout and a 300 s deadline. These are not the
`env/serve.env` values (8 / 32 / 4096 / 1024). The script does not record why. The 4 running match
llama.cpp's 4 slots, and 3500 sits under the 4,096-token context per slot. Read its results as "the
mechanism works", not "the production settings work".

### 13. macOS support, untested on a Mac

The README adds a macOS development path: Homebrew's Metal `llama-server`, `vm_stat` memory
sampling, and `nohup` when `setsid` is missing. `test_macos_memory_parser_on_real_vm_stat_format`
feeds the parser recorded `vm_stat` output on Linux. The README states that none of it has been run
on a real Mac.

## Honest downgrades

### 14. The only real performance run is a smoke run

`smoke-llamacpp-sharedgpu` used 20 short extraction prompts, 1 repetition and `max_tokens` 96, with
the GPU about 95 % busy. Its label says what it is. It is kept because it shows the harness works
end to end, and because it shows how differently per-request and aggregate throughput behave
([05](05-evaluation.md) § Results so far). It is not a baseline.

### 15. The mock overload demo was not saved

The README reports a mock run of `overload_demo.py`: a burst of 40 against 4 running + 8 queued
gave 12 served and 28 rejected, each in under 30 ms. No output file is committed. The script
supports `--out`, but it was not used. **[verify]** by rerunning the demo with `--out` and
committing the result. The real-server burst in `real_upstream_checks.json` is the committed
evidence for overload behaviour.

### 16. Non-streaming disconnects after forwarding are not cancelled

The design record lists this from the start ([01](01-architecture.md) § Known limitations).
Disconnects are watched only while a request is queued and while it is streaming. It is
acceptable for the benchmark, which always streams.

### 17. vLLM installed with pip instead of the container (2026-10-24)

PLAN M1 assumed NVIDIA's vLLM container, and the dev environment can't run Docker. vLLM 0.32.0
also ships as a pip wheel for aarch64 with torch 2.13.0+cu130, whose `sm_120` kernels run on the
GB10 (`sm_121`) under the 580 driver. `scripts/vllm_native_setup.sh` and `vllm_native_serve.sh`
pin the version and read the model and limits from `env/serve.env`. What this gives up: the
container's pinned image digest (replaced by a pinned package version and a lockable venv) and
NVIDIA's tested GB10 build. Reproducing it from scratch caught a pid-file race in the first serve
script, now fixed.

## Deferred, not dropped

| Item | Waits on | Where tracked |
|---|---|---|
| vLLM aborts the sequence on disconnect | vLLM running | PLAN M3 |
| Prometheus scrape config + Grafana | a baseline worth watching | PLAN layout `monitoring/` |
| Exps 1–5 | the baseline | PLAN M4 |
| Router | M1–M4 | PLAN M5 (optional) |
