# 07 — Risks and failure modes

There are two kinds of risk. **Thesis risks** mean the system works but does not show what it
claims. **Delivery risks** mean it does not get finished. The first kind is more dangerous: the
gateway's 32 green tests can make the project look complete while the throughput question is
still unanswered.

## Thesis risks

### R1 — The shared GPU contaminates every performance number *(certain, high impact)*

Other users' jobs held about 77 GiB and 95–96 % GPU utilisation when the environment was recorded
(`env/ENVIRONMENT.md`). Unified memory means the model server competes with them for the same RAM.
Single-request generation speed differed by about 27 % between two sessions on the same server
([05](05-evaluation.md) § How noisy).

**Detection:** record `nvidia-smi` and `free -g` before every run. `bench.py` already records
system memory before, during and after each run. Look at decode-speed spread across repetitions.
**Mitigation:** benchmark windows agreed with the job owners (PLAN M0, open). Until then, label
every result with its load, never compare across differing loads, and treat the smoke run as a
harness check only.

### R2 — The stopgap is not the planned server *(certain, medium impact)*

Every real-server check ran on llama.cpp with Q4_K_M weights, not on vLLM with the HF checkpoint.
Cancellation on llama.cpp is shown by its own token counter (`real_upstream_checks.json`). On vLLM
it has never been observed.

**Detection:** rerun `scripts/real_upstream_checks.py` against vLLM. Note that its token counter
and `/slots` probes are llama.cpp-specific (`predicted_tokens()` raises "is this llama.cpp?"), so
the vLLM run needs vLLM's running-requests metric instead (PLAN M3).
**Mitigation:** never present stopgap numbers as vLLM numbers ([Claim hygiene](#claim-hygiene)).

### R3 — The limits are guesses *(high likelihood, medium impact)*

The defaults `MAX_INFLIGHT=8`, `MAX_QUEUE=32` and `QUEUE_TIMEOUT_S=10` were not measured. Eight in
flight does not match either server (llama.cpp 4 slots, vLLM 16 sequences), and at the observed
latency most of a full 32-deep queue would time out ([02](02-admission-and-limits.md) § Sizing).

**Detection:** Exp 1. **Mitigation:** set all three limits from the Exp 1 knee, and set
`max_inflight` no higher than the server's concurrent capacity.

### R4 — The input-token check is approximate by default *(medium likelihood, low impact)*

Without `GATEWAY_TOKENIZER_FILE` (still `TBD` in `env/serve.env`), the count is chars/4. Tool
schemas are never counted.

**Detection:** a `4xx` passed through from the server for context length means the gateway's
check missed. **Mitigation:** set the tokenizer file for every real run, as
`real_upstream_checks.py` already does. The server's own check is the backstop.

### R5 — "It's just a semaphore in front of vLLM" *(certain to be asked)*

**Mitigation:** the semaphore is about 50 lines (`gateway/admission.py`). The work is in the
places a semaphore does not reach: giving up a queue place on disconnect, one deadline across
queue + connect + every read, closing the upstream in a shielded `finally`, in-band errors after
`200`, and tests that assert on the *server's* state, not the client's
([03](03-streaming-and-cancellation.md)).

## Delivery risks

### R6 — No container runtime *(happened, blocks M1)*

The dev environment cannot run Docker ([04](04-model-server.md)). **Mitigation:** the host step is
scripted end to end ([RUNBOOK.md](../RUNBOOK.md) § 1). It needs one person with host access, about
15 GB of disk for the model, and a memory check. The stopgap keeps M3 and the companion projects
unblocked meanwhile.

### R7 — The stopgap build drifts from the committed script *(low now, medium impact)*

Until 2026-10-23, `scripts/llamacpp_setup.sh` passed `-DCMAKE_CUDA_ARCHITECTURES=120`, the setting
ENVIRONMENT.md says fails on GB10, while the working build used `OFF` with compute_120 PTX. The
script now passes the recorded flags ([09](09-implementation-notes.md) § 5). **Detection:** a
configure-only run of the script's cmake line reproduces the working build's CMake cache
(`CMAKE_CUDA_ARCHITECTURES`, `CMAKE_CUDA_FLAGS`, `GGML_CUDA`, `LLAMA_CURL`, `CMAKE_BUILD_TYPE`).
**Closed for the stopgap:** a clean rebuild (2026-10-23) from the script's cmake line into an empty scratch directory built
`llama-server` at the same commit (`6753a03`); `--list-devices` shows `CUDA0: NVIDIA GB10`, and with
every layer offloaded (`-ngl 99`) it answered a request correctly ("What is 17 + 25?" → `42`) with no
"no kernel image" error.

### R8 — Launch configuration in two places *(medium likelihood)*

`env/serve.env` says every result references it by hash. But `scripts/host_run_vllm.sh` hardcodes
its own values and does not read `serve.env`. They agree today (8192 context, 16 sequences, 0.25
memory, prefix caching off). **Mitigation:** have the script source `serve.env`, or record the
script's hash with each run. Either is a code change and has not been made.

### R9 — Port and process surprises on a shared host *(happened twice)*

Port 8000 was answered by another service. A killed server lingered as a zombie
([04](04-model-server.md) § Two traps). Both are fixed in the scripts. `/readyz` still trusts
`/health` alone ([01](01-architecture.md) § Known limitations).

### R10 — Quantisation formats may not be supported on GB10 *(unknown)*

PLAN § Risks: verify support in the pinned image before planning Exp 5. It is not verified yet.

## Claim hygiene

| Don't claim | Do claim |
|---|---|
| "Serves Qwen2.5-7B on vLLM" | "vLLM serving on the host is scripted but not run. A llama.cpp server with Q4_K_M weights of the same revision is the running stopgap" |
| "3.4× throughput from batching" | "In a 20-prompt smoke run on a GPU about 95 % busy with other jobs, aggregate output rose from 5.6 to 19.2 tokens/s at concurrency 4, with per-request speed unchanged. Not a baseline" |
| "95 % accuracy" | "19 of 20 short extraction prompts passed their validator. No tool-call, reasoning or long prompts were in that run" |
| "Cancellation frees GPU resources" | "On llama.cpp, the server slot went idle 0.4 s after a client disconnect, and 0 tokens were generated after that. Not yet checked on vLLM" |
| "Handles overload" | "A burst of 20 against 4 running + 4 queued gave 8 served and 12 explicit `429`s, the slowest in 0.07 s, with the queue never above 4" |
| "Production gateway" | "A single-replica gateway with in-process queue and quota state, 24 failure-scenario tests over real TCP" |
| "Monitoring with Prometheus/Grafana" | "A Prometheus `/metrics` endpoint. No scrape config or dashboards yet" |

## The question to have a real answer for

> *"Your server already batches and queues. Why put a gateway in front of it?"*

Because the server's queue tells the client nothing until it is too late. With the gateway, a
client beyond capacity got a typed `429` with `Retry-After` within 70 ms, instead of waiting
behind work that might never finish (`real_upstream_checks.json`). A client that disconnects while
queued is never forwarded at all. And the gateway is the one place that applies auth, quotas,
input limits and metadata-only logging to every request, whichever server sits behind it. The
honest second half of the answer: whether the gateway *improves useful throughput*, rather than
just protecting latency, is not measured yet. That is Exp 1 ([06](06-roadmap.md)).
