# Environment record

Recorded 2026-10-06. No hostnames, serials, or IP addresses. Re-record whenever any line changes;
results are only comparable across runs with the same record.

## Host

| Item | Value | How measured |
| --- | --- | --- |
| OS | Ubuntu 24.04.1 LTS | `/etc/os-release` |
| Kernel | 6.17.0-1026-nvidia | `uname -r` |
| CPU arch | aarch64 | `uname -m` |
| GPU | NVIDIA GB10 | `nvidia-smi` |
| Driver / CUDA (driver API) | 580.159.03 / 13.0 | `nvidia-smi` |
| CUDA toolkit on host | 12.8 (`nvcc`) | `nvcc --version`; containers bring their own |
| Memory | 119.7 GiB visible (unified CPU+GPU) | `/proc/meminfo` MemTotal |
| Python (harness) | 3.12.3, deps pinned in `uv.lock` | `uv sync` |
| Container runtime | **not available inside the dev environment**: it is itself an unprivileged Docker container (`/.dockerenv`; CapEff `a80425fb`, no `CAP_SYS_ADMIN`; read-only cgroup2; no host docker socket) | vLLM runs on the host: docs/host-setup.md |
| Network | dev container 172.17.0.4 on the Docker bridge; host gateway 172.17.0.1 | gateway upstream: http://172.17.0.1:8000 |

## Background load at time of recording — must be resolved before any benchmark

| Process | GPU memory | Notes |
| --- | --- | --- |
| Ivy-1.5B training (`ivy_model.train`) | ~20 GiB | started 07:20 today |
| `app.py` (one process) | ~33 GiB | running ~12 days |
| `app.py --port 8600` and another `app.py` | <0.5 GiB each | running ~12 days |

At recording: GPU utilisation 95–96 %, ~77 GiB used, ~43 GiB available. Benchmarks taken under
this load are **not** valid baselines: the GPU is shared, and unified memory means the model server
competes with these processes for the same RAM. Either schedule benchmark windows when these are
stopped, or record the background load with every run and never compare across differing loads.

## Model server (fill in during M1)

| Item | Value |
| --- | --- |
| Recipe | NVIDIA DGX Spark vLLM playbook (build.nvidia.com/spark/vllm; script `sync-vllm-single-spark.sh` from NVIDIA/dgx-spark-playbooks), accessed 2026-10-06, adapted in `scripts/host_run_vllm.sh` |
| Image | `vllm/vllm-openai:qwen38` (NVIDIA DGX Spark playbook image); digest: TBD, printed by `scripts/host_run_vllm.sh` |
| vLLM version | TBD |
| Model | Candidate: `Qwen/Qwen2.5-7B-Instruct` (Apache-2.0 per model card), cached locally |
| Model revision | `a09a35458c702b33eeacc393d103063234e8bc28` (local HF cache `refs/main`) |
| Launch args | see `serve.env` |
| Bound to | `172.17.0.1:8000` on the host (Docker bridge only, not the network) |

Fallback candidate: Llama-3.2-3B-Instruct (cached copy is the `unsloth/` re-upload, rev
`006f5dcd…`, Llama 3.2 Community License). For provenance, prefer the official `meta-llama/` repo
if switching.

Confirm the chosen model appears in (or is architecture-compatible with) the current Spark vLLM
recipe before relying on it.

## Stopgap model server inside the dev container (llama.cpp), recorded 2026-10-06

Used until the host vLLM server (docs/host-setup.md) is available. Same gateway; only
`GATEWAY_UPSTREAM_URL` changes.

| Item | Value |
| --- | --- |
| llama.cpp | tag `b11439`, commit `6753a033f058fbf778d282556ed9b16c78de7c71` (`scripts/llamacpp_setup.sh`) |
| CUDA build | `-DCMAKE_CUDA_ARCHITECTURES=OFF -DCMAKE_CUDA_FLAGS=-gencode=arch=compute_120,code=compute_120` |
| Why those flags | GB10 is compute capability 12.1; host CUDA 12.8 targets at most 12.0; llama.cpp rewrites `120` to the arch-specific `120a`, which fails on 12.1 with "no kernel image is available". Plain compute_120 PTX is JIT-compiled by the driver for 12.1 on first use (first request slow; JIT cache needs `CUDA_CACHE_MAXSIZE` raised) |
| Model file | `models/qwen2.5-7b-instruct-a09a354-Q4_K_M.gguf`, converted from the pinned revision above, sha256 `c7bc8ace2d395f025ac39f334ee338dd83ee3878b3081c0de74fa99098d40f9e` |
| Serving | `scripts/llamacpp_serve.sh start` → `127.0.0.1:21000`, 4 slots × 4,096 tokens, `--jinja` (tool calling) |
| Port note | **8000 is used by another service on this machine**; a `/health` check there was answered by it. Readiness now requires our model id |
| Observed speed | ~80 tokens/s prompt, ~7.8 tokens/s generation, single request, GPU ~95% busy with other users' jobs. **Not a benchmark** |
