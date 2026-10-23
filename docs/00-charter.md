# 00 — Charter

## The problem

One GX10 (NVIDIA GB10, 119.7 GiB of unified CPU+GPU memory, `env/ENVIRONMENT.md`) has to serve a
local language model to the rest of a portfolio of projects. The machine is shared. When the
environment was recorded, other users' jobs held about 77 GiB and kept the GPU at 95–96 %
utilisation.

A model server alone does not make that safe to depend on. Three failure modes matter:

- **Unbounded queueing.** Under overload a server accepts everything. Latency rises for every
  request, and the client cannot tell "slow" from "never".
- **Ghost work.** A client that disconnects, often to retry, can leave its generation running.
  The GPU then spends capacity on output nobody will read, at exactly the moment it is overloaded.
- **Hanging on failure.** If the model server crashes or hangs, requests wait on dead sockets with
  no deadline.

The brief's main question ([SPEC.md](../SPEC.md)) is *how to improve useful throughput while
preserving task quality and response latency on one GX10*. You cannot answer that until the
serving path behaves predictably under load, and until "throughput" counts only successful,
correct answers.

## What it does

**Input:** OpenAI-style `POST /v1/chat/completions` requests, streaming or not, from clients or
from the load generator.

**Output:**
1. The model's response, relayed with an `X-Request-ID`. Or an **explicit, typed rejection**: a
   status code, an `error.type`, and `Retry-After` where retrying makes sense
   ([01](01-architecture.md) § Error contract). Never an indefinite wait.
2. Per-request benchmark records (`results/raw/*.jsonl`) and summaries with written-down metric
   definitions ([05](05-evaluation.md)).
3. Prometheus metrics at `/metrics`, and one metadata-only JSON log line per request.

## Scope for v1

### In
- One model: `Qwen/Qwen2.5-7B-Instruct` at a pinned revision (`env/serve.env`), served by vLLM
  (planned) or llama.cpp (stopgap) ([04](04-model-server.md)).
- One gateway replica, one endpoint (chat completions), plus health, readiness and metrics.
- Admission control, a deadline per request, cancellation, input and output limits, API keys and
  per-key quotas, metadata-only logs.
- A benchmark harness: deterministic prompt sets with known answers, validators, an async
  streaming load client, a summariser.
- One-factor-at-a-time serving experiments (M4, planned, [06](06-roadmap.md)).

### Out (deliberately, not from running out of time)
- **Kubernetes, custom CUDA kernels, distributed inference, many models, complex routing.** The
  SPEC rules these out of the first version, to be added only to answer a measured limitation.
- **Multiple gateway replicas.** Queue and quota state live in the process. A shared store would
  add a second system to measure without changing the question ([01](01-architecture.md) § Known
  limitations).
- **An LLM judge.** Every prompt has a known answer and a deterministic validator. A judge model
  would compete for the same GPU and add a second source of variance.
- **Cost modelling.** The SPEC requires that energy and price assumptions be stated separately
  from raw performance. No cost figures exist yet, so none are claimed.
- **Trainium or Maia.** The SPEC links them as context only. This project runs on NVIDIA hardware.

### Why this scoping holds up in an interview
The measurement is already confounded by a shared GPU ([07](07-risks.md) § R1). Every extra moving
part (a second model, a router, a replica) adds variables to an experiment that MUST change one
factor at a time. With one model, one server and one gateway, a change in a number can be traced
to the single setting that changed.

## Non-goals

- **Out-scheduling the model server.** The gateway does not batch. Batching is the server's job.
  The gateway decides how many requests the server gets at once and what happens to the rest.
- **Being a production API gateway.** It has no TLS, no multi-tenant billing, and no shared state.
- **Fast numbers on a shared GPU.** Numbers taken under other users' load are labelled as such and
  are never compared with numbers taken under different load.

## Success criteria

These come from SPEC "Important tests" and "Definition of done", and from the PLAN milestone exits.

| # | Criterion | How it's checked | Status (2026-10-23) |
|---|---|---|---|
| S1 | Queue saturation produces a defined response, and waiting never exceeds the limit | `test_queue_saturation_is_rejected_explicitly`, `test_queue_wait_is_bounded_by_timeout`; real: `real_upstream_checks.json` → `overload` | Met (mock and llama.cpp) |
| S2 | A client that cancels a stream releases the server's resources | `test_client_cancel_mid_stream_releases_upstream_and_slot`, `test_client_cancel_while_upstream_is_silent`; real: `cancellation` | Met on llama.cpp. **Not checked on vLLM** |
| S3 | A model-server crash does not leave requests hanging | `test_upstream_crash_mid_stream_terminates_requests`, `test_hung_upstream_hits_deadline_*`; real: `crash` | Met (mock and llama.cpp) |
| S4 | Oversized inputs are rejected with a clear reason before they reach the server | `test_oversized_input_rejected_before_upstream`, `test_oversized_body_rejected`; real: `oversized` | Met |
| S5 | Logs do not store credentials or prompt content | `test_logs_contain_no_prompts_outputs_or_credentials` | Met (mock) |
| S6 | Reproducible endpoint: a second clean run from the written instructions succeeds | PLAN M1 exit | **Not met.** vLLM path never run. The llama.cpp script differs from the recorded build ([09](09-implementation-notes.md) § 5) |
| S7 | Raw results include every success and failure, and the summary regenerates with one command | `test_summary_counts_failures_and_excludes_warmup`; `summarize.py` reproduces the committed smoke summary | Met for the harness. **No baseline run** |
| S8 | At least one fair comparison against the baseline, with every trade-off explained | `results/summary/EXP-NN.md` (PLAN M4) | **Not met** |
| S9 | Overload demo recorded against a real model | PLAN M3 | Partly: the burst check is in `real_upstream_checks.json`, but there is no `overload_demo.py` recording |

## The honest risk to the thesis

The gateway's correctness claims (S1–S5) do not depend on the GPU being quiet. The throughput
claim does. PLAN says the gateway's real `max_inflight` should come from the measured concurrency
knee (Exp 1). That measurement needs a benchmark window without other users' jobs, and no such
window has happened. Until it does, the limits are guesses ([02](02-admission-and-limits.md)
§ Sizing). The checks against a real server also ran on the **stopgap** (llama.cpp, Q4_K_M
weights), not on the planned vLLM server. Cancellation working on llama.cpp is evidence about the
gateway, but it does not prove that vLLM aborts the sequence. Both gaps are tracked as risks in
[07](07-risks.md) § R1–R2.
