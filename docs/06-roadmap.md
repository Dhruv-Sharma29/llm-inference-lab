# 06 — Roadmap

[PLAN.md](../PLAN.md) holds the checklist for each milestone. This doc gives the status of each
milestone, the evidence behind it, and the order of what is left. Status as of 2026-10-23. The
latest recorded work is from 2026-10-07.

## Status by milestone

| Milestone | Exit criterion (PLAN) | Status | Evidence |
|---|---|---|---|
| **M0** Environment record | A stranger can tell from ENVIRONMENT.md whether their machine matches | **Done**, with two blockers recorded: no container runtime, shared GPU | `env/ENVIRONMENT.md` |
| **M1** Hardware smoke test | A second clean run from written instructions succeeds | **Blocked** on vLLM (no container runtime in the dev environment). **Stopgap running**: llama.cpp serves the pinned model | [RUNBOOK.md](../RUNBOOK.md) § 1–2; `results/summary/real_upstream_checks.json` |
| **M2** Baseline benchmark | Raw results include every success and failure; the summary regenerates from raw with one command | **Done 2026-10-23** on the llama.cpp stopgap (vLLM still blocked) | `results/summary/baseline-q4.md`, [EXP-01](../results/summary/EXP-01-concurrency.md) |
| **M3** Gateway correctness | Overload produces explicit, measured rejections; all tests pass | **Done** against the mock and llama.cpp. vLLM abort and a recorded overload demo still open | 24 gateway tests; `real_upstream_checks.json` |
| **M4** Serving experiments | One fair comparison against M2, with every trade-off explained | **Exit met**: Exp 1 (concurrency) and Exp 5 (quantization) done; Exps 2–4 not run | [EXP-01](../results/summary/EXP-01-concurrency.md), [EXP-05](../results/summary/EXP-05-quantization.md) |
| **M5** Optional router | Static vs task-category routing on quality, latency, memory | **Not started.** Only after M1–M4 | – |

### Open items inside "done" milestones

- **M0:** agree benchmark windows with the owners of the background jobs (PLAN M0, unchecked).
- **M3:** confirm that vLLM aborts the sequence on disconnect, using its running-requests metric.
  Record `scripts/overload_demo.py` against a real model, with quotas on (PLAN M3).

## Definition of done (SPEC), today

| Item | State |
|---|---|
| Reproducible endpoint (M1 exit) | ✗ |
| Gateway failure tests pass | ✓ (32 collected: 24 gateway + 8 harness) |
| ≥ 1 fair comparison vs baseline | ✓ (EXP-05, interleaved Q4_K_M vs Q8_0) |
| Architecture diagram | ✓ ([01](01-architecture.md), README) |
| Overload demo | partly: script exists, mock run reported in README **[verify]**, real-model burst in `real_upstream_checks.json` |
| README with real numbers | ✓ (baseline and EXP-05 on the llama.cpp stopgap) |

## What comes next, in order

1. **M1 on the host** ([RUNBOOK.md](../RUNBOOK.md) § 1). Someone with host access runs
   `scripts/host_run_vllm.sh`, records the image digest and vLLM version in `env/ENVIRONMENT.md`,
   and then follows the README from scratch a second time. Everything after this depends on it.
2. **Rerun the real-server checks against vLLM** ([RUNBOOK.md](../RUNBOOK.md) § 4), adding vLLM's running-requests metric.
   This closes the last open M3 item.
3. ~~**Fix the protocol, then run the M2 baseline.**~~ Done 2026-10-23 on the llama.cpp stopgap,
   with the GPU otherwise idle ([EXP-01](../results/summary/EXP-01-concurrency.md)). Re-run on vLLM once step 1 is done.
4. ~~**M4 Exp 1 (concurrency)**~~ done (gateway limits for this server: 32 in flight, queue 32).
   Exp 5 (quantization) done. Next: find the throughput step between concurrency 16 and 32, then
   Exps 2–4.
5. **Monitoring.** A Prometheus scrape config and a Grafana dashboard over the existing `/metrics`
   (PLAN layout: `monitoring/`). Do this only once there is a baseline worth watching.

## The cut line

If time runs short, cut from the bottom. M5 is optional by definition (SPEC). Monitoring can stay
at "`/metrics` exists". Exps 3–5 can go. **Steps 1–3 and Exp 1 cannot be cut.** Without them the
project has no baseline, so the SPEC's résumé line, *"measured [actual result] under [defined
workload]"*, has nothing to fill it with.

## Not planned

Kubernetes, custom CUDA kernels, distributed inference, many models, complex routing, and a Go
gateway. The SPEC calls the Go gateway an "optional later implementation". None of these is added
unless a measured limitation calls for it ([00](00-charter.md) § Scope).
