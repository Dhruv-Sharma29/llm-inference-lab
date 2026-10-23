# EXP-05: quantization, Q4_K_M vs Q8_0

**Hypothesis.** Q8_0 is slower and larger than Q4_K_M; it is worth it only if quality improves.

**Configuration diff.** Only the GGUF: `qwen2.5-7b-instruct-a09a354-Q8_0.gguf` (sha `ac3c69cc25e4…`,
made from the pinned HF snapshot with the same converter and `llama-quantize Q8_0`) vs the M2
Q4_K_M. Both served at once with identical flags (32 slots, prompt cache off) on ports 21000/21001,
and **interleaved**: for each repetition and concurrency, Q4 then Q8, so both see the same
background load. Dev: concurrency 1 and 8, 3 repetitions each (600 requests per model). Quality:
the 60 held-out prompts at concurrency 4, once per model. Load record:
`results/raw/20261023T153343Z-bench-env.txt` (only these two servers on the GPU).

**Results** (`results/summary/exp05-q4.md`, `exp05-q8.md`, `heldout-q4.md`, `heldout-q8.md`; 0 errors):

| | Q4_K_M | Q8_0 |
| --- | --- | --- |
| GPU memory of the server (nvidia-smi, 32 slots × 2k context) | 8.1 GB | 11.1 GB |
| Concurrency 1: e2e p50 / decode tok/s | 1.77 s / 42.3 | 2.26 s / 29.4 |
| Concurrency 8: aggregate output tok/s (3 reps) | 53.2 / 47.5 / 46.2 | 41.5 / 36.2 / 36.2 |
| Dev quality (validator pass, 6 × 100) | 0.95–0.96 | 0.93–0.94 |
| Held-out quality (60 prompts) | 59/60 (0.983) | 60/60 (1.000) |

**Explanation.** Q8_0 is about 30 % slower per request and 22–24 % lower in aggregate throughput,
and needs 3 GB more memory. Quality doesn't move in its favour: dev is 1–2 points lower, held-out one
prompt higher, both within what one or two prompts change.

**Decision (discard Q8_0).** Q4_K_M stays the serving format; projects 02–04 already use it.
