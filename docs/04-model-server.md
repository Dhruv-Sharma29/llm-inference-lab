# 04 — Model server

The gateway is only as reproducible as the server behind it. This doc records what is planned,
what actually runs, and how the two differ. The step-by-step commands are in
[RUNBOOK.md](../RUNBOOK.md).

## Why the server cannot run where the code runs

The development environment is itself a Docker container on the GX10, and it is unprivileged
(`env/ENVIRONMENT.md`): `/.dockerenv` is present, effective capabilities are `a80425fb` with no
`CAP_SYS_ADMIN`, cgroup2 is read-only, and there is no host Docker socket. A Docker daemon cannot
run inside it, so the planned vLLM container has to run **on the host**, and the gateway reaches
it over the Docker bridge (`172.17.0.1`). No one with host access has run that step yet, so the
vLLM path is blocked (PLAN M0/M1).

A llama.cpp server built inside the container is the stopgap. It needs no privileges, it serves
the same pinned model revision, and the gateway in front of it is identical.

## Planned vs. running

| | Planned: vLLM on the host | Running: llama.cpp stopgap |
|---|---|---|
| Where | GX10 host, in Docker | inside the dev container, no Docker |
| Launch | `scripts/host_run_vllm.sh` | `scripts/llamacpp_setup.sh`, then `scripts/llamacpp_serve.sh start` |
| Server version | image `vllm/vllm-openai:qwen38`. Digest **TBD**, vLLM version **TBD** (`env/ENVIRONMENT.md`) | llama.cpp tag `b11439`, commit `6753a033f058fbf778d282556ed9b16c78de7c71` |
| Weights | HF checkpoint, revision `a09a35458c702b33eeacc393d103063234e8bc28`, no quantisation flag | GGUF **Q4_K_M** converted from the same revision, sha256 `c7bc8ace…40f9e` |
| Bound to | `172.17.0.1:8000` (bridge only) | `127.0.0.1:21000` (loopback only) |
| Concurrency | `--max-num-seqs 16` | `--parallel 4` slots |
| Context | `--max-model-len 8192` | `-c 16384` shared by 4 slots = 4,096 per slot |
| Memory share | `--gpu-memory-utilization 0.25` (~30 GB of 120 GB) | all layers on GPU (`-ngl 99`) |
| Prefix caching | off for the baseline (`--no-enable-prefix-caching`), on in Exp 4 | not configured in the script. **[verify]** the llama.cpp default |
| Tool calls | `--enable-auto-tool-choice --tool-call-parser hermes` | `--jinja` (the model's own chat template) |
| Server-side metrics | vLLM `/metrics` | `--metrics` (Prometheus) and `--slots` (`/slots`) |
| Status | scripted, **never run** | running, and used by the companion projects through the gateway (README) |

The stopgap was chosen to keep the M3 checks unblocked. It is a different server running
quantised weights, so its numbers are **not** a preview of the vLLM baseline. M2 and M4 are
defined against vLLM ([05](05-evaluation.md)).

## What is pinned, and what is not yet

| Item | Pinned in | State |
|---|---|---|
| Model repo + revision | `env/serve.env`, `scripts/host_run_vllm.sh`, `scripts/llamacpp_setup.sh` | pinned (`a09a354…`). Apache-2.0 per the model card (`env/ENVIRONMENT.md`) |
| vLLM image | `env/serve.env` | **by tag only**. The digest is printed by `host_run_vllm.sh` and has not been recorded |
| vLLM launch args | `env/serve.env` *and* `scripts/host_run_vllm.sh` | duplicated: the script does not read `serve.env` ([07](07-risks.md) § R8) |
| llama.cpp | `scripts/llamacpp_setup.sh` (`TAG=b11439`), `env/ENVIRONMENT.md` (commit) | pinned. The build flags disagree between the two files ([09](09-implementation-notes.md) § 5) |
| GGUF file | `models/SHA256SUMS` (gitignored), `env/ENVIRONMENT.md` | the hash is recorded in ENVIRONMENT.md |
| Python deps | `uv.lock` | pinned. Python 3.12.3 |

Fallback model (`env/ENVIRONMENT.md`): Llama-3.2-3B-Instruct. The cached copy is the `unsloth/`
re-upload. For provenance, switch to the official `meta-llama/` repo if it is ever used.

## Building llama.cpp for GB10

This is the hardware-specific part, from `env/ENVIRONMENT.md`. The GB10 is compute capability
12.1. The host CUDA toolkit is 12.8 and targets at most 12.0. llama.cpp rewrites the
architecture `120` to the arch-specific `120a`, which fails on 12.1 with "no kernel image is
available". The recorded working build therefore uses `-DCMAKE_CUDA_ARCHITECTURES=OFF` with
`-gencode=arch=compute_120,code=compute_120`. That embeds plain compute_120 PTX, which the driver
JIT-compiles for 12.1 on first use. Two practical consequences:

- **The first request is slow.** In the smoke run, the first warm-up request had a TTFT of
  16.7 s, against about 0.2 s for the requests after it
  (`results/raw/smoke/20261007T035456Z-smoke-llamacpp-sharedgpu.jsonl`). That fits the JIT
  explanation, but the run does not isolate it.
- **The JIT cache needs room.** `CUDA_CACHE_MAXSIZE` must be raised. `real_upstream_checks.py`
  sets it to `4294967296` when it restarts the server.

## Two traps on a shared host

- **Port 8000 is taken by another service.** A `/health` check there was answered by it
  (`env/ENVIRONMENT.md`). The stopgap moved to port 21000. Readiness in `llamacpp_serve.sh` and
  `check_upstream.sh` now requires the served model id `qwen2.5-7b-instruct` in `/v1/models`, not
  only a `200` from `/health`.
- **Killed servers become zombies.** In the container, PID 1 is `tail`, which never reaps orphans.
  A killed server lingers as a zombie, and `kill -0` still reports it alive. `running()` in
  `llamacpp_serve.sh` checks the process state for `Z`. The crash check found this.

## Memory on unified memory

Unified memory makes "peak GPU memory" ambiguous (PLAN § Risks). The harness uses one definition
and keeps it: system-wide `MemTotal − MemAvailable` from `/proc/meminfo`, sampled every 0.5 s
(`bench.py` docstring). On GB10 that number includes the model server **and every other
process**. In the smoke run it was 98.76 GiB before the run and peaked at 99.36 GiB. Most of that
is other users' jobs, so it says almost nothing about the model server alone. A meaningful number
needs a reading taken before the server starts, with background load held fixed.
