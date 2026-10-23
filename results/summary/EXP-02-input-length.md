# EXP-02: input length (analysis of the EXP-01 sweep)

**Hypothesis.** Longer prompts cost prefill time, so time to first token rises with input length;
end-to-end latency depends mostly on output length.

**Data.** No new run: the 1,800 measured requests of EXP-01 (`results/raw/20261023T153400Z-baseline-q4.jsonl`),
split by each prompt's length bucket (`benchmarks/prompts/dev.jsonl`: 75 short, 25 long).

**Confound, stated first.** The buckets differ in more than length. All 35 reasoning prompts are
short, and reasoning replies are long; the long bucket is extraction and tool-call prompts only. So
short prompts average 104 input and 98 output tokens, long prompts 1,373 input and 45 output tokens.
TTFT isolates prefill; end-to-end latency here mostly measures the output side.

| Concurrency | Bucket | Requests | Input / output tokens | TTFT p50 / p95 (ms) | e2e p50 / p95 (ms) | Quality |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | short | 225 | 104 / 98 | 53 / 86 | 1,454 / 4,998 | 0.933 |
| 1 | long | 75 | 1,373 / 45 | 433 / 530 | 1,840 / 2,056 | 1.000 |
| 8 | short | 225 | 104 / 98 | 294 / 755 | 9,788 / 27,833 | 0.929 |
| 8 | long | 75 | 1,373 / 45 | 672 / 801 | 7,422 / 11,673 | 1.000 |
| 32 | short | 225 | 104 / 98 | 411 / 4,088 | 10,760 / 32,211 | 0.942 |
| 32 | long | 75 | 1,373 / 45 | 714 / 3,321 | 7,257 / 11,050 | 1.000 |

**Explanation.** At concurrency 1, 13x more input costs 8x more TTFT (53 → 433 ms; about 3,200
prompt tokens/s end to end on the long prompts), but it is still under half a second. End-to-end latency is
set by output length: long prompts finish *sooner* at concurrency 8 and 32 because their replies are
half as long, and their p95 is far tighter. Quality failures are all on short prompts (reasoning).

**Decision.** No serving change. For latency targets, budget TTFT by input length (~0.3 ms per
input token at low concurrency here) and end-to-end by `max_tokens`. A clean input-length
experiment would hold category and output length fixed; the prompt set doesn't allow it.
