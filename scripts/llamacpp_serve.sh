#!/usr/bin/env bash
# start | stop | status for the llama.cpp stopgap server on 127.0.0.1:21000 (loopback only).
# Port 8000 is taken by another service on the shared GX10; an earlier version used it, and a plain
# /health check was answered by that other service. Readiness now requires our model id.
set -euo pipefail
cd "$(dirname "$0")/.."
# Linux (GX10): the CUDA build from scripts/llamacpp_setup.sh. macOS: Homebrew's Metal build
# (`brew install llama.cpp`), e.g. LLAMA_SERVER=llama-server MODEL_ARGS="-hf Qwen/Qwen2.5-7B-Instruct-GGUF:Q4_K_M".
BIN="${LLAMA_SERVER:-.tools/llama.cpp/build/bin/llama-server}"
MODEL_ARGS="${MODEL_ARGS:--m models/qwen2.5-7b-instruct-a09a354-Q4_K_M.gguf}"
CTX="${CTX:-16384}"            # total context, shared by the parallel slots
PARALLEL="${PARALLEL:-4}"
PIDFILE=.run/llama-server.pid
PORT="${PORT:-21000}"
ALIAS=qwen2.5-7b-instruct
mkdir -p .run

# Alive and not a zombie. In this container PID 1 is `tail`, which never reaps orphans, so a killed
# server lingers as a zombie that `kill -0` still reports as alive (found by the crash check).
running() {
  [ -f "$PIDFILE" ] || return 1
  local state; state="$(ps -o stat= -p "$(cat "$PIDFILE")" 2>/dev/null | tr -d ' ')"
  [ -n "$state" ] && [ "${state#Z}" = "$state" ]
}

case "${1:-status}" in
  start)
    if running; then echo "already running (pid $(cat $PIDFILE))"; exit 0; fi
    if (exec 3<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null; then
      echo "port $PORT is already in use by another process; set PORT=..." >&2; exit 1
    fi
    # 4 parallel slots x 4096 tokens of context; --jinja applies the model's chat template,
    # which tool calling needs; --metrics exposes Prometheus counters; /slots shows per-slot state.
    DETACH=setsid; command -v setsid >/dev/null || DETACH=nohup   # macOS has no setsid
    # shellcheck disable=SC2086  # MODEL_ARGS is intentionally word-split
    $DETACH "$BIN" $MODEL_ARGS --alias "$ALIAS" --host 127.0.0.1 --port "$PORT" \
      -ngl 99 -c "$CTX" --parallel "$PARALLEL" --jinja --metrics --slots > .run/llama-server.log 2>&1 &
    echo $! > "$PIDFILE"
    for _ in $(seq 1 120); do
      if curl -fsS "http://127.0.0.1:$PORT/v1/models" 2>/dev/null | grep -q "\"$ALIAS\""; then
        echo "ready: $ALIAS on 127.0.0.1:$PORT (pid $(cat $PIDFILE))"; exit 0
      fi
      running || { echo "server exited; see .run/llama-server.log" >&2; exit 1; }
      sleep 1
    done
    echo "not healthy after 120 s; see .run/llama-server.log" >&2; exit 1 ;;
  stop)
    if running; then kill "$(cat $PIDFILE)"; sleep 2; running && kill -9 "$(cat $PIDFILE)" || true; fi
    rm -f "$PIDFILE"; echo stopped ;;
  status)
    if running; then echo "running (pid $(cat $PIDFILE))"; curl -sS "http://127.0.0.1:$PORT/health"; echo; else echo "not running"; fi ;;
esac
