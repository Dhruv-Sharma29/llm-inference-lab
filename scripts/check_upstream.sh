#!/usr/bin/env bash
# Run INSIDE the development container: is the host's vLLM reachable and serving the expected model?
set -uo pipefail
URL="${UPSTREAM:-http://172.17.0.1:8000}"
echo "health:";  curl -sS -m 5 -o /dev/null -w "%{http_code}\n" "$URL/health" || { echo "unreachable: is scripts/host_run_vllm.sh running on the host?"; exit 1; }
echo "models:";  MODELS=$(curl -sS -m 5 "$URL/v1/models"); echo "$MODELS"
# Health alone isn't proof: another service on the same port can answer it.
echo "$MODELS" | grep -q '"qwen2.5-7b-instruct"' || { echo "expected model qwen2.5-7b-instruct not served at $URL"; exit 1; }
echo "chat:";    curl -sS -m 120 "$URL/v1/chat/completions" -H "Content-Type: application/json" \
  -d '{"model":"qwen2.5-7b-instruct","messages":[{"role":"user","content":"Reply with the word ready."}],"max_tokens":5,"temperature":0}'; echo
