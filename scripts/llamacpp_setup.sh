#!/usr/bin/env bash
# Stopgap model server inside the (unprivileged) dev container: llama.cpp built from a pinned tag,
# serving the same pinned Qwen2.5-7B-Instruct revision the vLLM plan uses. No system changes:
# everything lands in .tools/ and models/ (both gitignored). See docs/host-setup.md for the
# intended vLLM path; the gateway is identical for both (only GATEWAY_UPSTREAM_URL changes).
set -euo pipefail
cd "$(dirname "$0")/.."
TAG=b11439
REVISION=a09a35458c702b33eeacc393d103063234e8bc28
SNAP="${HF_HOME:-$HOME/.cache/huggingface}/hub/models--Qwen--Qwen2.5-7B-Instruct/snapshots/$REVISION"
F16=models/qwen2.5-7b-instruct-a09a354-f16.gguf
Q4=models/qwen2.5-7b-instruct-a09a354-Q4_K_M.gguf

mkdir -p .tools models
if [ ! -d .tools/llama.cpp ]; then
  git clone --depth 1 --branch "$TAG" https://github.com/ggml-org/llama.cpp .tools/llama.cpp
fi
# GB10 is compute capability 12.1; the host's CUDA 12.8 compiler targets up to 12.0. Code built for
# 12.0 runs on 12.1 (same major version), and the 12.0 PTX is embedded as a JIT fallback.
if [ ! -x .tools/llama.cpp/build/bin/llama-server ]; then
  cmake -S .tools/llama.cpp -B .tools/llama.cpp/build -G Ninja -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=120 \
        -DLLAMA_CURL=OFF -DCMAKE_BUILD_TYPE=Release
  ninja -C .tools/llama.cpp/build -j 12 llama-server llama-quantize
fi
if [ ! -f "$Q4" ]; then
  # The converter imports sentencepiece before discovering Qwen2 has no SentencePiece model and
  # falling back to BPE; without the package the fallback never runs. Kept in its own small venv
  # (reusing the host's torch/numpy) so the system Python is untouched.
  if [ ! -x .tools/convert-venv/bin/python ]; then
    uv venv --system-site-packages --python /usr/bin/python3 .tools/convert-venv
    uv pip install --python .tools/convert-venv/bin/python sentencepiece
  fi
  [ -f "$F16" ] || PYTHONPATH=.tools/llama.cpp/gguf-py .tools/convert-venv/bin/python .tools/llama.cpp/convert_hf_to_gguf.py "$SNAP" --outtype f16 --outfile "$F16"
  .tools/llama.cpp/build/bin/llama-quantize "$F16" "$Q4" Q4_K_M
fi
sha256sum "$Q4" | tee models/SHA256SUMS
