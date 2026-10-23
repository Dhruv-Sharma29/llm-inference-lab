# 05 — Evaluation

## What question the measurement answers

Not "how fast is the model". Specifically:

> **How many correct answers per second does this setup deliver, at what latency, and what does
> each serving change cost in quality?**

That framing sets three rules for the harness. Throughput counts only successful requests. Every
successful request is also scored for correctness, so a configuration that gets faster by getting
wrong shows up. Errors are counted and never dropped, so a configuration that gets faster by
rejecting more shows up too.

## Prompt sets

`benchmarks/prompts/generate.py` builds both sets deterministically, with known answers. Counts
below come from the committed `.jsonl` files:

| Category | Validator | dev short | dev long | held-out short | held-out long |
|---|---|---|---|---|---|
| Structured extraction (an order record → JSON) | `json_equals` | 20 | 15 | 12 | 8 |
| Tool-call formatting (a request → `{"tool", "arguments"}`) | `json_equals` | 20 | 10 | 12 | 8 |
| Short reasoning (arithmetic → `ANSWER: <int>`) | `final_integer` | 35 | – | 20 | – |
| **Total** | | **100** | | **60** | |

- **Length buckets** (generator docstring): short is under about 200 tokens. Long wraps the task
  in about 1.5–2.5k tokens of filler context. Reasoning is short-only, because long context adds
  nothing to it.
- **Held-out is separated three ways.** It uses a different seed (`77001` vs `20261006`),
  disjoint customer and product pools, and different phrasings.
  `test_heldout_does_not_share_entities_or_prompts_with_dev` asserts that no prompt and no
  extraction customer is shared. The held-out set MUST NOT be used for tuning (PLAN M2).
- **The generator cannot disagree with its own validators.**
  `test_every_prompt_is_answerable_by_its_own_expected_value` feeds each prompt's expected answer
  back through its validator and requires a pass on all 160 prompts. Without this test, a scoring
  bug would look like a model weakness.

## Validators

`benchmarks/validators/validators.py`. Each returns `(passed, reason)`, and the reason is stored
with the record for failure analysis.

| Validator | Passes when | Tolerates | Rejects |
|---|---|---|---|
| `json_equals` | The first JSON object in the text has exactly the expected keys and values (strings trimmed) | code fences, prose around the object | missing or extra keys, wrong values, no object |
| `final_integer` | The **last** `ANSWER: <int>` line equals the expected value | a leading `$`, thousands separators, earlier self-corrected answers | no `ANSWER` line, a number only in prose |

Both validators are strict on purpose. "Close enough" JSON is a failure, because a downstream
tool-calling client would fail on it too.

## What gets recorded

`benchmarks/client/bench.py` streams every request with `stream_options.include_usage` and writes
one JSONL line per request, failures included. Each line has `request_id, prompt_id, category,
length, concurrency, rep, phase, status, error, ttft_ms, e2e_ms, input_tokens, output_tokens,
finish_reason, ok, validator_pass, validator_reason`, plus `output` with `--store-output`. Each
file also carries a `run` header (full config, `config_hash`, `prompts_sha256`, Python version,
machine, memory before the run), a `phase` line for every warm-up or measured phase (wall time,
memory peak and minimum), and `run_end` (memory after).

| Field | Definition (`bench.py` docstring) |
|---|---|
| `ttft_ms` | client send → first non-empty content delta, end to end, as a user sees it |
| `e2e_ms` | client send → stream end |
| `input_tokens` / `output_tokens` | from the server's usage report |
| `ok` | HTTP 200 and no in-band error event |
| `mem_used_gib` | system-wide `MemTotal − MemAvailable`, every 0.5 s ([04](04-model-server.md) § Memory) |

## Metrics

`benchmarks/client/summarize.py` writes one row per (run, concurrency), from the measure phase
only:

| Metric | Definition |
|---|---|
| `ok` / `errors` / `error_types` | Errors are counted by type, never dropped |
| `e2e_p50/p95_ms`, `ttft_p50/p95_ms` | nearest-rank percentiles over ok requests |
| `decode_tok_s_p50` | **per request:** `output_tokens / (e2e − ttft)`, median over ok requests |
| `agg_out_tok_s` | **aggregate:** sum of ok output tokens / sum of phase wall time |
| `ok_req_s` | successful requests / sum of phase wall time |
| `quality_of_ok` / `quality_of_all` | validator passes / ok requests, and / all requests (errors count as fails) |
| `mem_peak_gib` | max system-wide used memory across measure phases |

Per-request and aggregate tokens per second answer different questions. One is what a single
user experiences, the other is what the box delivers. The SPEC requires every report to say which
it means, so the summary always carries both. `test_summary_counts_failures_and_excludes_warmup`
pins the arithmetic, and `test_percentile_nearest_rank` pins the percentile definition.

## Protocol

| Setting | Default (`bench.py`) | Status |
|---|---|---|
| Warm-up | 5 requests, recorded as `phase=warmup`, excluded from summaries | implemented |
| Repetitions | 3 per concurrency, each shuffled with `Random(rep)`, so a given rep has the same order across runs | implemented |
| Output budget | `--max-tokens 256` | **not yet fixed for the baseline** (PLAN M2, unchecked) |
| Temperature | 0 | default. A `--seed` is passed only if given |
| Prefix cache state | off for the baseline (`env/serve.env`) | **documenting it per run is still open** (PLAN M2) |
| Background load | `free -g` / `nvidia-smi` before each run (PLAN § Risks, RUNBOOK) | manual, not captured by the client |

**What makes a comparison fair here.** Same `prompts_sha256`, same model revision, same
`env/serve.env`, a recorded background load, and **only one changed factor**. Results taken under
different background load MUST NOT be compared (`env/ENVIRONMENT.md`).

## Results so far

### Baseline and Exp 1 (2026-10-23)

[EXP-01](../results/summary/EXP-01-concurrency.md): all 100 dev prompts, concurrency 1–32, 3
repetitions, 5 warm-ups, `max_tokens` 256, prompt cache off, on the llama.cpp stopgap with the GPU
otherwise idle. 1,800 measured requests, 0 errors, quality 0.95.

| Concurrency | e2e p50 / p95 (s) | Decode tok/s per request | Aggregate output tok/s | OK req/s |
| --- | --- | --- | --- | --- |
| 1 | 1.79 / 4.82 | 42.2 | 39.1 | 0.46 |
| 4 | 5.75 / 18.83 | 11.3 | 44.1 | 0.52 |
| 8 | 8.95 / 27.25 | 7.5 | 57.2 | 0.67 |
| 16 | 14.04 / 46.23 | 4.6 | 68.9 | 0.81 |
| 32 | 9.04 / 31.30 | 7.3 | 156.3 | 1.84 |

Concurrency 32 is faster per request than 16 in every repetition; the cause is not established
**[verify]** (EXP-01).

### Exp 5: quantization (2026-10-23)

[EXP-05](../results/summary/EXP-05-quantization.md): Q4_K_M vs Q8_0, both served at once and
interleaved so they see the same load. Q8_0 is 30 % slower per request, 22–24 % lower in
throughput, 3 GB larger, and not better (dev 0.93–0.94 vs 0.95–0.96; held-out 60/60 vs 59/60).
Q4_K_M stays.

### The earlier smoke run

One earlier run against a real model exists: `results/summary/smoke-llamacpp-sharedgpu.md`, from
`results/raw/smoke/20261007T035456Z-smoke-llamacpp-sharedgpu.jsonl`. Running
`summarize.py` on the raw file reproduces the committed table.

| Concurrency | ok / requests | e2e p50 / p95 (ms) | TTFT p50 / p95 (ms) | decode tok/s p50 (per request) | aggregate out tok/s | ok req/s | quality |
|---|---|---|---|---|---|---|---|
| 1 | 20 / 20 | 10,327.8 / 10,715.1 | 221.3 / 245.2 | 5.7 | 5.6 | 0.1 | 0.95 |
| 4 | 20 / 20 | 11,026.2 / 14,401.9 | 670.8 / 4,286.7 | 5.6 | 19.2 | 0.32 | 0.95 |

**What it is:** a check that the harness works end to end against llama.cpp. **What it is not:** a
baseline. The run header in the raw file shows how narrow it was:

- `--limit 20`, which means the first 20 dev prompts, and those are **all short extraction**. No
  tool-call, reasoning or long prompts were run.
- `repetitions 1`, `warmup 2`, `max_tokens 96`, against the stopgap server (Q4_K_M), with the GPU
  about 95 % busy with other users' jobs.
- Quality 0.95 is **19 of 20**. The same prompt (`dev-extraction-short-000`) failed at both
  concurrencies, with `wrong values: ['order_id', 'product']`. At n = 20, one prompt is 5 points.

What the run does show, read directly from the raw file:

- **Batching helped the box, not the user.** At concurrency 4, aggregate output tokens/s rose from
  5.6 to 19.2 (3.4×), while per-request decode speed stayed at about 5.6–5.7 tokens/s and the
  median end-to-end latency rose only from 10.3 to 11.0 s.
- **The concurrency-4 TTFT p95 comes from the first wave.** The first four requests dispatched
  together all had TTFTs of 4,285–4,288 ms. The other 16 had 170–709 ms. With 20 samples, a
  nearest-rank p95 is the 19th value, so that one wave sets it. The run does not record why the
  first wave was slow.

## How noisy is the shared GPU?

The repository supports only a narrow statement. Every real-model number was taken with the GPU
at about 95 % utilisation from other jobs (`env/ENVIRONMENT.md`; `real_upstream_checks.json` →
`gpu_utilisation_before: "95 %"`). The same server and weights gave different single-request
generation speeds on different days:

| Source | Date | Single-request generation speed |
|---|---|---|
| `env/ENVIRONMENT.md` ("Observed speed", method not recorded, marked "Not a benchmark") | 2026-10-06 | ~7.8 tokens/s |
| smoke run, `decode_tok_s_p50` at concurrency 1 | 2026-10-07 | 5.7 tokens/s |

The second figure is about 27 % lower. Within the smoke run, per-request decode speed was
steady: 5.70–5.79 tokens/s at concurrency 1 and 5.38–5.88 at concurrency 4. So the instability is
between sessions, not between requests in one session. With one run and one repetition,
run-to-run variance **has not been measured**, and no number here should be quoted without the
load it was taken under. Measuring that variance is the first job of the baseline: 3 repetitions
and a recorded background load ([RUNBOOK.md](../RUNBOOK.md) § 5).

## Planned experiments (M4)

From PLAN M4. Each gets `results/summary/EXP-NN.md` with hypothesis → config diff → results table →
explanation → keep or discard. Each varies one factor on the same prompts and the same model
revision. None has been run.

| Exp | Factor | Values | Notes |
|---|---|---|---|
| 1 | Concurrency | 1, 2, 4, 8, 16, 32 | finds the knee in p95 vs throughput. Sets `max_inflight`, `max_queue` and the queue timeout together ([02](02-admission-and-limits.md) § Sizing) |
| 2 | Input length | short vs long buckets at fixed concurrency | |
| 3 | Batching limit | vLLM max sequences / batched tokens | |
| 4 | Prefix caching | on vs off with a shared system prompt | the baseline is off |
| 5 | Quantisation | only formats the recipe supports on GB10 | **quality checked on held-out**. Support on GB10 is unverified (PLAN § Risks) |

A measured result showing that an optimisation did not help counts as a result (SPEC § Definition
of done).
