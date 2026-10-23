# 01 — LLM inference lab: execution plan

Brief: [SPEC.md](SPEC.md). Main question: how to raise *useful* throughput on one GX10 without losing task quality or latency.

## Progress (2026-10-06)

- **M0:** done → `env/ENVIRONMENT.md`. Two blockers found: no container runtime; GPU shared with other long-running jobs (~95 % util, ~77 GiB used).
- **M1 (stopgap):** a real model (llama.cpp, same pinned Qwen2.5-7B) is served inside the container; the gateway checks against it pass (cancellation, overload, crash, limits). The vLLM host path is scripted ([RUNBOOK.md](RUNBOOK.md) § 1) but not run.
- **M1 (original):** blocked on container runtime. Candidate model cached: Qwen2.5-7B-Instruct (Apache-2.0), revision recorded.
- **M2:** harness built and tested against the mock (prompt generator 100 dev + 60 held-out, validators, load client, summariser). No real-model baseline yet; one smoke run of the harness on the llama.cpp stopgap (`results/summary/smoke-llamacpp-sharedgpu.md`).
- **M3:** gateway built; 24 failure-scenario tests pass against a mock upstream over real TCP, 3/3 runs. Cancellation tests verified to fail without the fix. Still to do against real vLLM: confirm vLLM aborts the sequence on disconnect (check its running-requests metric), quotas in the overload demo recording.
- **Deviation from layout:** tests live in `gateway/tests/` and `benchmarks/test_benchmarks.py`; mock server added in `mock_upstream/`.

**Produces for later projects:** a private OpenAI-compatible endpoint (via the gateway), a benchmark harness + result schema, and Prometheus/Grafana monitoring.

## Target layout

```text
01-llm-inference-lab/
  env/
    ENVIRONMENT.md        OS, arch, driver, CUDA, image digest, model revision
    serve.env             exact vLLM launch args
    compose.yaml          model server + gateway + prometheus + grafana
  gateway/
    app/                  FastAPI: routing, queue, limits, errors, metrics
    tests/                failure-scenario tests (see below)
  benchmarks/
    prompts/dev.jsonl     ~100 prompts (extraction, tool-call format, short reasoning)
    prompts/heldout.jsonl quality set — never used for tuning
    validators/           deterministic checkers (JSON schema, exact match, etc.)
    client/               async load generator, writes results/raw/*.jsonl
    configs/              one YAML per experiment
  monitoring/             prometheus.yml, grafana dashboards
  results/raw/  results/summary/
  docs/01-architecture.md diagram + data/control flow (full doc set: docs/README.md)
```

## Milestones

### M0 — Environment record (½ day)
- [x] Record `uname -m`, OS release, `nvidia-smi`, CUDA version, container runtime version into `env/ENVIRONMENT.md`.
- [ ] Resolve: `docker` is not on PATH in the current shell. Install Docker + NVIDIA Container Toolkit per NVIDIA's Spark guide, or confirm containers must be run from the host.
- [x] Identify what is using ~77 GiB of memory now (Ivy training + long-running `app.py` services). [ ] Decide with their owners when they can be stopped for benchmark windows.
- **Exit:** a stranger could read ENVIRONMENT.md and know whether their machine matches.

### M1 — Hardware smoke test (1–2 days)
- [ ] Open NVIDIA's current Spark vLLM page; pick a model it lists as supported, 3–8B. Check licence.
- [ ] Pin: image **digest** (not tag), model repo + **revision hash**, launch args → `env/serve.env`.
- [ ] Serve bound to `127.0.0.1` only. Send one request with `curl`; save request + response.
- [ ] Record cold-start time and memory after load (state the method, e.g. `nvidia-smi` vs `free`; on unified memory, say which you trust and why).
- [ ] Tear everything down, follow your own README from scratch, confirm it works again.
- **Exit:** second clean run from written instructions succeeds.

### M2 — Baseline benchmark (3–5 days)
- [x] Build `prompts/dev.jsonl` (~100) with category, input-length bucket (short/long), and a validator per prompt. Build a separate `heldout.jsonl`.
- [ ] Fix the output budget (`max_tokens`), temperature (0 for validators), and stop conditions.
- [x] Benchmark client: async, streaming, records per request → `request_id, prompt_id, concurrency, input_tokens, output_tokens, ttft_ms, e2e_ms, status, error, validator_pass`.
- [ ] Protocol: N warm-up requests discarded; ≥3 repetitions; cache state documented (prefix cache on/off).
- [x] Summary script → p50/p95 latency, TTFT, tokens/s (state per-request vs aggregate), successful req/s, error count, validator pass rate.
- **Exit:** raw results include every success **and** failure; summary regenerates from raw with one command.

### M3 — Gateway correctness (1–2 weeks)
Design decisions to write down in [`docs/01-architecture.md`](docs/01-architecture.md) first:
- Admission control: max in-flight to model server + bounded wait queue. Full queue → `429` (or `503`) with `Retry-After`. Never unbounded growth.
- Timeouts: total deadline per request, propagated upstream; deadline exceeded → `504` and upstream request aborted.
- Cancellation: client disconnect on a stream → gateway closes upstream stream so the server frees the sequence.
- Input limits: token-count (not char-count) check before enqueue → `413` with reason.
- Upstream failure: connect error / crash → fast `502`, health endpoint flips, no request waits forever.
- Request IDs: generated or accepted from header, returned in response, present in every log line.
- Quotas: simple per-API-key token bucket.
- Logging: metadata only (IDs, lengths, timings, status). No prompt text, no auth headers.

Tests (`gateway/tests/`):
- [x] Cancel mid-stream → upstream running-sequences metric returns to baseline within T seconds.
- [x] Saturate queue → excess requests get the defined status code; queue length stays ≤ limit.
- [x] Kill model server mid-request → all in-flight requests terminate with `502`/`504` within deadline.
- [x] Oversized input → `413` with a readable reason, never reaches model server.
- [x] Grep logs after a test run for prompt sentinels and fake keys → zero hits.
- [x] Overload demo script (`scripts/overload_demo.py`) — [ ] recording against real model.
- **Exit:** overload produces explicit rejections, measured; all tests pass.

### M4 — Serving experiments (1–2 weeks)
One factor per experiment, same prompts, same model revision. Template per experiment in `results/summary/EXP-NN.md`: hypothesis → config diff → results table → explanation → keep/discard.

| Exp | Factor | Values (initial) |
| --- | --- | --- |
| 1 | Concurrency | 1, 2, 4, 8, 16, 32 → find the knee in p95 vs throughput |
| 2 | Input length | short vs long buckets at fixed concurrency |
| 3 | Batching limit | vLLM max sequences / batched tokens |
| 4 | Prefix caching | on vs off with shared system prompt |
| 5 | Quantization | only formats the recipe supports on GB10; **check quality on held-out** |

- Use Exp 1 to set the gateway's real queue/in-flight limits (M3 starts with a guess).
- **Exit:** one fair comparison vs M2 baseline, with an explanation of every trade-off.

### M5 — Optional router
Only if M1–M4 are done. Second model loaded sequentially if memory is tight. Compare static routing vs a simple task-category rule on quality, latency, and memory.

## Risks / open questions
- Unified memory makes "peak GPU memory" ambiguous — define the measurement method once and keep it.
- Background processes on this machine will contaminate results. Record `free -g` before each run.
- Quantized formats on GB10 may not be supported by the pinned image — verify before planning Exp 5.

## Definition of done
- [ ] Reproducible endpoint (M1 exit) · [ ] gateway failure tests pass · [ ] ≥1 fair comparison vs baseline · [ ] architecture diagram · [ ] overload demo · [ ] README with real numbers
