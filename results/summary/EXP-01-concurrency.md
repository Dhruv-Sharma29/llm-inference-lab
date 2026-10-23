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
