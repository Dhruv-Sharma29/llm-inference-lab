# M1: vLLM serving on the GB10, without Docker (2026-10-24)

The planned M1 path runs NVIDIA's vLLM container on the host, which needs Docker; the development
container has none. vLLM also installs with pip, so M1 is closed natively instead:
`scripts/vllm_native_setup.sh` (pinned `vllm==0.32.0`, which brings `torch 2.13.0+cu130`) and
`scripts/vllm_native_serve.sh` (model and limits from `env/serve.env`). The GB10 is compute
capability 12.1; torch's `sm_120` build runs on it, and the driver (580.159.03) supports CUDA 13.

| Item | Value |
| --- | --- |
| Model | `Qwen/Qwen2.5-7B-Instruct` at revision `a09a35458c702b33eeacc393d103063234e8bc28` (bf16, local HF snapshot) |
| Launch | `--max-model-len 8192 --gpu-memory-utilization 0.25 --max-num-seqs 16 --no-enable-prefix-caching`, served as `qwen2.5-7b-instruct` on 127.0.0.1:21020 (port 8000 in `serve.env` is taken by another service on this host) |
| Cold start | 178–185 s to the first `/v1/models` answer: 85 s loading weights, 75 s profiling, KV-cache creation and warm-up (`.run/vllm-native.log`) |
| Memory after load | 28.6 GB for the engine process (nvidia-smi), KV cache 13.3 GiB |
| First request | `results/raw/20261024-vllm-native-first-request.json` → `…-first-response.json`: "What is 17 + 25? Reply with only the number." → `42` |

**Reproduced from scratch.** The server was stopped, `.tools/vllm-venv` deleted, and the two
scripts run again as written: setup (packages from uv's cache, 6 s), serve (ready after 178 s),
same request, same answer. One race in the first version of the serve script (it read the pid file
before the child wrote it) was found this way and fixed, and `stop` now waits for vLLM's graceful
shutdown before killing the process group.

**Gateway checks on vLLM** (`scripts/real_upstream_checks.py --upstream http://127.0.0.1:21020`,
`results/summary/real_upstream_checks_vllm.json`): basics pass and streams end with `[DONE]`;
oversized input gets 413 before reaching vLLM; on a client disconnect after 10 chunks,
`vllm:num_requests_running` drops to 0 within 0.16 s and generation stops at 9 of 400 tokens, with 0
tokens generated in the next 3 s, so **vLLM aborts the sequence on disconnect**; a 20-request burst
against 4 + 4 gets 12 immediate `429 queue_full` (≤ 10 ms) and 8 served.
