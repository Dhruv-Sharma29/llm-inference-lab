#!/usr/bin/env bash
# start | stop for natively installed vLLM (scripts/vllm_native_setup.sh) on 127.0.0.1:$PORT.
# Model and limits follow env/serve.env (pinned revision, 8192 context, 16 sequences, 0.25 of GPU
# memory, prefix caching off). Port 8000 from serve.env is taken by another service on this host.
set -euo pipefail
cd "$(dirname "$0")/.."
source env/serve.env
PORT="${VLLM_PORT:-21020}"
SNAP="${HF_HOME:-$HOME/.cache/huggingface}/hub/models--Qwen--Qwen2.5-7B-Instruct/snapshots/$MODEL_REVISION"
PIDFILE=.run/vllm-native.pid
mkdir -p .run
case "${1:-start}" in
  start)
    [ -d "$SNAP" ] || { echo "missing $SNAP (hf download $MODEL --revision $MODEL_REVISION)" >&2; exit 1; }
    prefix=--no-enable-prefix-caching; [ "$ENABLE_PREFIX_CACHING" = true ] && prefix=--enable-prefix-caching
    t0=$(date +%s)
    HF_HUB_OFFLINE=1 setsid sh -c 'echo $$ > "$0"; exec "$@"' "$PIDFILE" .tools/vllm-venv/bin/vllm serve "$SNAP" \
      --served-model-name "$SERVED_MODEL_NAME" --host 127.0.0.1 --port "$PORT" --max-model-len "$MAX_MODEL_LEN" \
      --gpu-memory-utilization "$GPU_MEMORY_UTILIZATION" --max-num-seqs "$MAX_NUM_SEQS" $prefix \
      > .run/vllm-native.log 2>&1 < /dev/null &
    for _ in $(seq 50); do [ -s "$PIDFILE" ] && break; sleep 0.1; done   # written by the child above
    for _ in $(seq 900); do
      curl -fsS "http://127.0.0.1:$PORT/v1/models" 2>/dev/null | grep -q "\"$SERVED_MODEL_NAME\"" && {
        echo "ready: $SERVED_MODEL_NAME on 127.0.0.1:$PORT after $(( $(date +%s) - t0 )) s"; exit 0; }
      kill -0 "$(cat $PIDFILE 2>/dev/null)" 2>/dev/null || { echo "vllm exited; see .run/vllm-native.log" >&2; exit 1; }
      sleep 1
    done
    echo "not ready after 900 s" >&2; exit 1 ;;
  stop)   # vLLM shuts down gracefully and slowly: SIGTERM the process group, then SIGKILL after 30 s
    if [ -f "$PIDFILE" ]; then
      pg=$(cat "$PIDFILE"); kill -- -"$pg" 2>/dev/null || true
      for _ in $(seq 30); do kill -0 "$pg" 2>/dev/null || break; sleep 1; done
      kill -9 -- -"$pg" 2>/dev/null || true
    fi
    rm -f "$PIDFILE"; echo stopped ;;
esac
