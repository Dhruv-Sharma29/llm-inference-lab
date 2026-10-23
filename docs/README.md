# inference-lab — Documentation

A private, OpenAI-compatible gateway in front of one locally served language model, plus a
benchmark harness that records every request, failures included. The gateway keeps latency
predictable under load by refusing work it cannot serve, enforcing one deadline per request, and
making sure a client that hangs up actually stops the model generating.

These docs are the plan and the design record. Read them in order the first time; after that,
use them as reference. [SPEC.md](../SPEC.md) is the brief and [PLAN.md](../PLAN.md) is the
milestone checklist. These docs explain the reasoning behind both.

## Reading order

| # | Doc | What it settles |
|---|---|---|
| 00 | [Charter](00-charter.md) | Problem, scope, non-goals, what "done" means |
| 01 | [Architecture](01-architecture.md) | Request path, the three design rules, error contract, components, repo layout |
| 02 | [Admission and limits](02-admission-and-limits.md) | Check order, bounded queue, quotas, token counting, sizing the limits |
| 03 | [Streaming and cancellation](03-streaming-and-cancellation.md) | Deadlines, disconnects, in-band errors, upstream crashes, and the evidence for each |
| 04 | [Model server](04-model-server.md) | The planned vLLM path, the llama.cpp stopgap, what is pinned, why the host |
| 05 | [Evaluation](05-evaluation.md) | Prompt sets, validators, metric definitions, protocol, results so far |
| 06 | [Roadmap](06-roadmap.md) | Milestones, status with evidence, what comes next |
| 07 | [Risks](07-risks.md) | Risk register, claim hygiene, the question to have an answer for |
| 08 | [Learning checklist](08-learning-checklist.md) | Skills by milestone, interview talking points |
| 09 | [Implementation notes](09-implementation-notes.md) | Where the code departs from the plan, and why |

Outside `docs/`: [start.md](../start.md) gets you to a running gateway with no GPU.
[RUNBOOK.md](../RUNBOOK.md) covers the steps that need the GX10 host, Docker or the GPU.
[env/ENVIRONMENT.md](../env/ENVIRONMENT.md) is the machine record.

## The one-paragraph version

Putting a FastAPI proxy in front of a model server is easy. Making it behave under overload is
harder, and that is what this project is about. Without a bound, every extra request makes every
admitted request slower. A client that times out and retries leaves ghost generations running on
the GPU. A crashed model server leaves sockets hanging. What makes the project non-trivial is
three guarantees that a plain proxy does not give, each backed by a test that runs real
processes over TCP. Nothing reaches the model until every cheap check has passed. Capacity is
bounded twice (running and waiting), and the excess gets a typed `429` now instead of a slow
`200` later. Every exit path gives the slot back and closes the upstream connection, which is
what makes the server stop generating. The second half of the project is measurement: the harness
scores quality with deterministic validators, and the summary counts every error, so
"throughput" always means *useful* throughput.

## Status

**As of 2026-10-23.**

- The gateway is complete (M3). `uv run pytest` collects 32 tests: 24 gateway failure-scenario
  tests against a mock upstream and 8 harness tests. The same behaviours were checked against a
  real model (Qwen2.5-7B, llama.cpp stopgap) in `results/summary/real_upstream_checks.json`.
- **M2 baseline done (2026-10-23)** on the llama.cpp stopgap (Qwen2.5-7B Q4_K_M, 32 slots, prompt cache off, GPU otherwise idle):
1,800 requests, 0 errors; concurrency 1 gives 1.8 s median end-to-end at 42 decode tokens/s and
0.46 req/s, concurrency 32 gives 1.84 req/s and 156 aggregate output tokens/s at 9.0 s median
(`results/summary/EXP-01-concurrency.md`).
- **The planned vLLM server (M1) is blocked.** The development environment is an unprivileged
  container that cannot run Docker. The host path is scripted in [RUNBOOK.md](../RUNBOOK.md) § 1
  but has never been run. A llama.cpp server inside the container is the stopgap, and the other
  portfolio projects use it through this gateway.
- **M4:** Exp 1 (concurrency) set the gateway limits for this server; Exp 5: Q8_0 vs Q4_K_M, interleaved on the same load: 30 % slower per request, 22–24 % less throughput,
3 GB more memory, no quality gain (dev 0.93–0.94 vs 0.95–0.96; held-out 60/60 vs 59/60), so Q4_K_M
stays (`results/summary/EXP-05-quantization.md`). Exps 2–4
  and the optional M5 router are not run.

See [06-roadmap.md](06-roadmap.md) for what comes next and
[09-implementation-notes.md](09-implementation-notes.md) for where building it changed the plan.

## Conventions used in these docs

- **MUST / SHOULD / MAY** carry weight. MUST items are acceptance criteria.
- Anything marked **[verify]** has not been confirmed against a primary source or a committed
  result file. Confirm it before relying on it.
- Every number cites the file it comes from. If a number has no path next to it, treat it as
  arithmetic on cited numbers, and the text says so.
- "Mock" means `mock_upstream/server.py`. "Real" means the llama.cpp stopgap unless vLLM is named.
