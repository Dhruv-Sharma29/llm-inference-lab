#!/usr/bin/env bash
# RUN ON THE GX10 HOST (not inside the development container): the development environment is an
# unprivileged container and cannot run a Docker daemon. See RUNBOOK.md (section 1).
#
# Adapted from NVIDIA's DGX Spark vLLM playbook (dgx-spark-playbooks, playbook-vllm,
# sync-vllm-single-spark.sh): same image and container flags, changed for this project:
#   - model: the project's pinned 7B model instead of the playbook's 27B one
#   - GPU memory share: 0.25 instead of 0.7 (unified memory shared with other users' jobs)
#   - published only on the Docker bridge address, so it is reachable from containers on this
#     host but not from the network
#   - prefix caching OFF for the baseline (experiment 4 turns it on); tool-call parsing ON
set -euo pipefail

NAME="${NAME:-inference-lab-vllm}"
IMAGE="${IMAGE:-vllm/vllm-openai:qwen38}"          # NVIDIA's tested Spark image; record its digest below
MODEL="${MODEL:-Qwen/Qwen2.5-7B-Instruct}"
REVISION="${REVISION:-a09a35458c702b33eeacc393d103063234e8bc28}"
SERVED_NAME="${SERVED_NAME:-qwen2.5-7b-instruct}"
BIND="${BIND:-172.17.0.1}"                         # docker0 gateway; containers reach it, the network doesn't
PORT="${PORT:-8000}"
GPU_MEM_UTIL="${GPU_MEM_UTIL:-0.25}"               # ~30 GB of 120 GB unified memory; check free -g first
HF_CACHE="${HF_CACHE:-$HOME/.cache/huggingface/hub}"

if [ "${1:-}" = "stop" ]; then docker rm -f "${NAME}"; exit 0; fi

echo "Free memory now (other jobs share it):"; free -g | sed -n 2p
docker rm -f "${NAME}" >/dev/null 2>&1 || true
docker pull "${IMAGE}"
docker run -d \
  --name "${NAME}" \
  --gpus all \
  --ipc host \
  --ulimit memlock=-1 \
  --ulimit stack=67108864 \
  --entrypoint "" \
  -p "${BIND}:${PORT}:8000" \
  -v "${HF_CACHE}:/root/.cache/huggingface/hub:ro" \
  -e HF_HUB_OFFLINE=1 \
  "${IMAGE}" \
  vllm serve "${MODEL}" \
    --revision "${REVISION}" \
    --served-model-name "${SERVED_NAME}" \
    --host 0.0.0.0 \
    --port 8000 \
    --tensor-parallel-size 1 \
    --gpu-memory-utilization "${GPU_MEM_UTIL}" \
    --max-model-len 8192 \
    --max-num-seqs 16 \
    --no-enable-prefix-caching \
    --enable-auto-tool-choice \
    --tool-call-parser hermes

echo "Image digest (paste into env/ENVIRONMENT.md):"
docker inspect --format '{{index .RepoDigests 0}}' "${IMAGE}"
echo "Waiting for health on ${BIND}:${PORT} ..."
for _ in $(seq 1 120); do
  if curl -fsS "http://${BIND}:${PORT}/health" >/dev/null 2>&1; then echo "healthy"; exit 0; fi
  sleep 5
done
echo "not healthy after 10 minutes; see: docker logs ${NAME}" >&2
exit 1
