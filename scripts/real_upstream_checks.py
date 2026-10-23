"""Gateway behaviour against a real model server (llama.cpp or vLLM), not the mock.

    python scripts/real_upstream_checks.py --upstream http://127.0.0.1:21000 [--crash]

Starts the gateway in front of the server and checks:
  basics        non-streaming and streaming requests succeed; exact token counting (model tokenizer)
  oversized     input over the token limit is rejected before reaching the server
  cancellation  client disconnects mid-stream -> the server's slot goes idle and its generated-token
                counter stops rising (proves generation stopped, not just the client)
  overload      a burst far above capacity gets explicit 429s quickly; admitted requests finish
  crash         (--crash) SIGKILL the llama.cpp server mid-stream -> stream ends with an in-band error
                promptly; new requests get a fast 502; the server is restarted afterwards
Writes results/summary/real_upstream_checks.json. These are behaviour checks, not benchmarks.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
TOKENIZER = Path.home() / (".cache/huggingface/hub/models--Qwen--Qwen2.5-7B-Instruct/snapshots/"
                           "a09a35458c702b33eeacc393d103063234e8bc28/tokenizer.json")
MODEL = "qwen2.5-7b-instruct"
GATEWAY_PORT = 21090
INFLIGHT, QUEUE = 4, 4


def predicted_tokens(upstream: str) -> int:
    for line in httpx.get(f"{upstream}/metrics", timeout=5).text.splitlines():
        if line.startswith("llamacpp:tokens_predicted_total"):
            return int(float(line.split()[-1]))
    raise RuntimeError("no llamacpp:tokens_predicted_total metric (is this llama.cpp?)")


def slots_busy(upstream: str) -> int:
    return sum(1 for s in httpx.get(f"{upstream}/slots", timeout=5).json() if s.get("is_processing"))


def start_gateway(upstream: str) -> subprocess.Popen:
    env = {**os.environ, "GATEWAY_UPSTREAM_URL": upstream, "GATEWAY_MAX_INFLIGHT": str(INFLIGHT),
           "GATEWAY_MAX_QUEUE": str(QUEUE), "GATEWAY_QUEUE_TIMEOUT_S": "120", "GATEWAY_REQUEST_TIMEOUT_S": "300",
           "GATEWAY_TOKENIZER_FILE": str(TOKENIZER), "GATEWAY_MAX_INPUT_TOKENS": "3500", "GATEWAY_MAX_OUTPUT_TOKENS": "512"}
    log = open(ROOT / ".run/gateway-real.log", "w")
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "--factory", "gateway.app:create_app_from_env",
                             "--host", "127.0.0.1", "--port", str(GATEWAY_PORT)], cwd=ROOT, env=env,
                            stdout=log, stderr=subprocess.STDOUT)
    for _ in range(100):
        try:
            if httpx.get(f"http://127.0.0.1:{GATEWAY_PORT}/healthz", timeout=1).status_code == 200:
                return proc
        except httpx.HTTPError:
            time.sleep(0.1)
    proc.kill()
    raise RuntimeError("gateway did not start")


def chat(content: str, **kw) -> dict:
    return {"model": MODEL, "messages": [{"role": "user", "content": content}], "temperature": 0, **kw}


def check_basics(gw: str) -> dict:
    r = httpx.post(f"{gw}/v1/chat/completions", json=chat("Reply with the single word OK.", max_tokens=5), timeout=120)
    lines = []
    with httpx.stream("POST", f"{gw}/v1/chat/completions", json=chat("Say hello.", max_tokens=8, stream=True),
                      timeout=120) as s:
        lines = [line for line in s.iter_lines() if line]
    return {"non_stream_status": r.status_code, "non_stream_text": r.json()["choices"][0]["message"]["content"],
            "stream_status": s.status_code, "stream_ended_with_done": lines[-1] == "data: [DONE]"}


def check_oversized(gw: str, upstream: str) -> dict:
    before = predicted_tokens(upstream)
    r = httpx.post(f"{gw}/v1/chat/completions", json=chat("word " * 5000, max_tokens=5), timeout=30)
    return {"status": r.status_code, "error": r.json()["error"]["type"], "message": r.json()["error"]["message"],
            "server_untouched": predicted_tokens(upstream) == before}


def check_cancellation(gw: str, upstream: str) -> dict:
    prompt = "Count from 1 to 400, numbers separated by single spaces, nothing else."
    before = predicted_tokens(upstream)
    t0 = time.monotonic()
    with httpx.stream("POST", f"{gw}/v1/chat/completions", json=chat(prompt, max_tokens=400, stream=True),
                      timeout=300) as s:
        chunks = 0
        for line in s.iter_lines():
            if line.startswith("data: {") and '"content"' in line:
                chunks += 1
                if chunks >= 10:
                    break
        busy_while_streaming = slots_busy(upstream)
    t_disconnect = time.monotonic()
    idle_after = None
    while time.monotonic() - t_disconnect < 15:
        if slots_busy(upstream) == 0:
            idle_after = time.monotonic() - t_disconnect
            break
        time.sleep(0.1)
    settled = predicted_tokens(upstream)
    time.sleep(3)
    final = predicted_tokens(upstream)
    return {"chunks_read_before_disconnect": chunks, "slots_busy_while_streaming": busy_while_streaming,
            "slot_idle_s_after_disconnect": round(idle_after, 2) if idle_after is not None else None,
            "tokens_generated_total": final - before, "requested_max_tokens": 400,
            "tokens_generated_in_3s_after_idle": final - settled, "seconds_streamed": round(t_disconnect - t0, 1)}


async def check_overload(gw: str, burst: int = 20) -> dict:
    async def one(client):
        t0 = time.monotonic()
        r = await client.post("/v1/chat/completions", json=chat("Write one short sentence about queues.", max_tokens=24))
        kind = "ok" if r.status_code == 200 else r.json().get("error", {}).get("type")
        return r.status_code, kind, time.monotonic() - t0
    peak = {"inflight": 0, "waiting": 0}
    done = asyncio.Event()

    async def watch(client):
        while not done.is_set():
            h = (await client.get("/healthz")).json()
            peak["inflight"], peak["waiting"] = max(peak["inflight"], h["inflight"]), max(peak["waiting"], h["waiting"])
            await asyncio.sleep(0.05)

    async with httpx.AsyncClient(base_url=gw, timeout=600, limits=httpx.Limits(max_connections=burst + 4)) as client:
        w = asyncio.create_task(watch(client))
        results = await asyncio.gather(*(one(client) for _ in range(burst)))
        done.set()
        await w
    by_kind: dict[str, list[float]] = {}
    for status, kind, s in results:
        by_kind.setdefault(f"{status} {kind}", []).append(s)
    return {"burst": burst, "capacity": f"{INFLIGHT} in flight + {QUEUE} queued", "peak": peak,
            "outcomes": {k: {"count": len(v), "max_s": round(max(v), 2)} for k, v in sorted(by_kind.items())}}


def check_crash(gw: str, upstream: str) -> dict:
    pid = int((ROOT / ".run/llama-server.pid").read_text())
    t0 = time.monotonic()
    last = None
    with httpx.stream("POST", f"{gw}/v1/chat/completions",
                      json=chat("Count from 1 to 300 separated by spaces.", max_tokens=300, stream=True), timeout=300) as s:
        n = 0
        for line in s.iter_lines():
            if line:
                last = line
                n += 1
                if n == 8:
                    os.kill(pid, signal.SIGKILL)
                    t_kill = time.monotonic()
    stream_end_s = time.monotonic() - t_kill
    t1 = time.monotonic()
    r = httpx.post(f"{gw}/v1/chat/completions", json=chat("hi", max_tokens=3), timeout=60)
    after = {"status": r.status_code, "error": r.json().get("error", {}).get("type"), "seconds": round(time.monotonic() - t1, 2)}
    restart = subprocess.run([str(ROOT / "scripts/llamacpp_serve.sh"), "start"], cwd=ROOT, capture_output=True, text=True,
                             env={**os.environ, "CUDA_CACHE_MAXSIZE": "4294967296"})
    return {"stream_ended_s_after_kill": round(stream_end_s, 2), "last_event": last[:160] if last else None,
            "next_request": after, "server_restarted": restart.returncode == 0, "restart_output": restart.stdout.strip()[-120:],
            "seconds_total": round(time.monotonic() - t0, 1)}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--upstream", default="http://127.0.0.1:21000")
    p.add_argument("--crash", action="store_true", help="also SIGKILL the llama.cpp server mid-stream (it is restarted)")
    args = p.parse_args()
    (ROOT / ".run").mkdir(exist_ok=True)
    gw_proc = start_gateway(args.upstream)
    gw = f"http://127.0.0.1:{GATEWAY_PORT}"
    report = {"at": datetime.now(UTC).isoformat(), "upstream": args.upstream,
              "gpu_utilisation_before": subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader"],
                                                       capture_output=True, text=True).stdout.strip()}
    try:
        for name, fn in [("basics", lambda: check_basics(gw)), ("oversized", lambda: check_oversized(gw, args.upstream)),
                         ("cancellation", lambda: check_cancellation(gw, args.upstream)),
                         ("overload", lambda: asyncio.run(check_overload(gw)))]:
            report[name] = fn()
            print(name, json.dumps(report[name]), flush=True)
        if args.crash:
            report["crash"] = check_crash(gw, args.upstream)
            print("crash", json.dumps(report["crash"]), flush=True)
    finally:
        gw_proc.terminate()
    out = ROOT / "results/summary/real_upstream_checks.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
