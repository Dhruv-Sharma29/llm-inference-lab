#!/usr/bin/env bash
# vLLM without Docker: a pip install into .tools/vllm-venv (gitignored). The container recipe
# (scripts/host_run_vllm.sh) needs Docker on the host; this path needs only Python 3.12, uv and a
# CUDA 13-capable driver (580+ on the GB10). torch's sm_120 build runs on the GB10's sm_121.
set -euo pipefail
cd "$(dirname "$0")/.."
VLLM_VERSION=0.32.0   # pinned; torch 2.13.0+cu130 comes with it
[ -x .tools/vllm-venv/bin/vllm ] || {
  uv venv -q --python 3.12 .tools/vllm-venv
  uv pip install --python .tools/vllm-venv/bin/python "vllm==$VLLM_VERSION" > .tools/vllm-install.log 2>&1
}
.tools/vllm-venv/bin/python -c "import torch, vllm; print('vllm', vllm.__version__, 'torch', torch.__version__, 'device capability', torch.cuda.get_device_capability())"
