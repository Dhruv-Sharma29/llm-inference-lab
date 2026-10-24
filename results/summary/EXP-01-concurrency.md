# EXP-01: concurrency (also the M2 baseline)

**Hypothesis.** Aggregate throughput rises with concurrency until the server saturates; past a knee,
latency grows faster than throughput. The knee sets the gateway's in-flight limit.

**Configuration.** llama.cpp b11439 (build `6753a03`) stopgap, Qwen2.5-7B-Instruct a09a354 Q4_K_M,
32 slots × 2,048 tokens (`PARALLEL=32 CTX=65536`), prompt cache off (`--no-cache-prompt`), served
directly (no gateway). Client: `benchmarks.client.bench`, all 100 dev prompts, 5 warm-ups,
3 shuffled repetitions per concurrency, `max_tokens` 256, temperature 0, stop at EOS only.
Server settings: `results/raw/20261023T153343Z-baseline-props.json`. Background load, recorded
with the run (`results/raw/20261023T153343Z-bench-env.txt`): no other process on the GPU at the
start or the end; the GPU at 0 % before and 92 % (this server) at the end.

**Results** (`results/raw/20261023T153400Z-baseline-q4.jsonl` → `results/summary/baseline-q4.md`;
1,800 measured requests, 0 errors):

| Concurrency | e2e p50 / p95 (s) | TTFT p50 / p95 (ms) | Decode tok/s per request (p50) | Aggregate output tok/s | OK req/s | Quality |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 1.79 / 4.82 | 63 / 480 | 42.2 | 39.1 | 0.46 | 0.95 |
| 2 | 3.18 / 10.00 | 105 / 525 | 21.0 | 40.0 | 0.47 | 0.95 |
| 4 | 5.75 / 18.83 | 194 / 623 | 11.3 | 44.1 | 0.52 | 0.95 |
| 8 | 8.95 / 27.25 | 329 / 769 | 7.5 | 57.2 | 0.67 | 0.947 |
| 16 | 14.04 / 46.23 | 537 / 1,315 | 4.6 | 68.9 | 0.81 | 0.95 |
| 32 | 9.04 / 31.30 | 669 / 3,808 | 7.3 | 156.3 | 1.84 | 0.957 |

**Explanation.** From 1 to 4 concurrent requests the box gains almost nothing (0.46 → 0.52 req/s)
while p95 latency nearly quadruples: decode is memory-bound, so extra requests split the same token
rate. Throughput only climbs at 8–32. Concurrency 32 is faster *per request* than 16 in all three
repetitions (100 prompts in 50–58 s vs 120–127 s). That is not explained by the client or the load
record; a likely cause is llama.cpp choosing different CUDA matmul kernels by batch size, which
would make throughput non-monotonic in batch size **[verify]** (sweep 12–32 to find the step).
Quality is flat at 0.95 on dev (one prompt type fails throughout).

**Decision (keep).** For this server at 32 slots: `GATEWAY_MAX_INFLIGHT=32` (32 dominates 8 and 16:
higher throughput, similar p50, similar or lower p95) and, by `max_inflight × queue_timeout_s /
service_time` = 32 × 10 / 9 ≈ 35, `GATEWAY_MAX_QUEUE=32` with the 10 s queue timeout. For
interactive latency (p50 under 2 s), cap at 1–2 in flight and accept ~0.46 req/s. The code
defaults stay as they are: they target the planned vLLM server (16 sequences).

## On vLLM (2026-10-24)

Same harness and protocol against natively installed vLLM 0.32.0 (`results/summary/m1-vllm-native.md`):
bf16 weights (not quantised), 16 sequences, 8,192 context, prefix caching off, 0.25 of GPU memory;
the GPU otherwise idle (`results/raw/20261024T024142Z-vllm-bench-env.txt`). 1,800 requests, 0 errors
(`results/raw/20261024T024152Z-baseline-vllm.jsonl` → `results/summary/baseline-vllm.md`); held-out
quality 60/60 (`heldout-vllm.md`).

| Concurrency | e2e p50 / p95 (s) | TTFT p50 / p95 (ms) | Decode tok/s per request | Aggregate output tok/s | OK req/s | Quality |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 4.87 / 15.32 | 154 / 369 | 13.4 | 13.0 | 0.15 | 0.95 |
| 2 | 4.15 / 12.87 | 214 / 431 | 16.3 | 30.5 | 0.36 | 0.95 |
| 4 | 4.23 / 13.14 | 203 / 432 | 16.1 | 58.7 | 0.70 | 0.95 |
| 8 | 4.43 / 13.89 | 207 / 465 | 15.2 | 105.5 | 1.26 | 0.95 |
| 16 | 5.00 / 15.55 | 345 / 723 | 13.8 | 176.2 | 2.10 | 0.95 |
| 32 | 11.15 / 21.45 | 6,445 / 9,038 | 13.7 | 176.1 | 2.10 | 0.95 |

**Explanation.** vLLM's continuous batching keeps per-request latency almost flat up to its 16
sequences while throughput rises 14x; at 32 the extra 16 wait inside vLLM (TTFT 6.4 s) for no gain.
One request at a time it is slower than llama.cpp because it serves bf16 weights, about 4x the bytes
per token of Q4_K_M; the two servers differ in engine *and* precision, so this is not a one-factor
comparison between them.

**Decision for vLLM (keep).** `GATEWAY_MAX_INFLIGHT=16` = `--max-num-seqs` (now the code default and
in `env/serve.env`); queue 16 × 10 / 5 ≈ 32, which is the existing `GATEWAY_MAX_QUEUE=32`.
