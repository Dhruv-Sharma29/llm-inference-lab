"""Benchmark client: replays a prompt set against an OpenAI-compatible endpoint and records every
request (successes AND failures) to a JSONL file. It never filters or aggregates; summarize.py does.

    python -m benchmarks.client.bench --base-url http://127.0.0.1:9000 --model <served-name> \
        --prompts benchmarks/prompts/dev.jsonl --concurrency 1,4,8 --repetitions 3 --label baseline

Definitions used in the output (keep these stable across experiments):
  ttft_ms   client send -> first non-empty content delta (end-to-end, as a user sees it)
  e2e_ms    client send -> stream end
  output_tokens / input_tokens   from the server's usage report (stream_options.include_usage)
  mem_used_gib   system-wide MemTotal - MemAvailable from /proc/meminfo (Linux) or active + wired +
                 compressed pages from vm_stat (macOS), sampled every 0.5 s.
                 On GB10 unified memory this includes the model server AND every other process,
                 so record a baseline before the server starts and keep background load fixed.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import platform
import random
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx

from benchmarks.validators.validators import validate

ROOT = Path(__file__).resolve().parents[2]


def mem_used_gib() -> float | None:
    try:
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                key, value = line.split(":", 1)
                info[key] = int(value.split()[0])
        return round((info["MemTotal"] - info["MemAvailable"]) / 1024**2, 2)
    except (OSError, KeyError, ValueError):
        return _mem_used_gib_macos() if sys.platform == "darwin" else None


def _mem_used_gib_macos() -> float | None:
    """macOS (no /proc): active + wired + compressed pages from vm_stat, like Activity Monitor's 'used'."""
    import re
    import subprocess
    try:
        out = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=2).stdout
        page = int(re.search(r"page size of (\d+) bytes", out).group(1))
        pages = {k: int(v) for k, v in re.findall(r"Pages (active|wired down|occupied by compressor):\s+(\d+)", out)}
        return round(sum(pages.values()) * page / 1024**3, 2)
    except (OSError, AttributeError, ValueError, subprocess.SubprocessError):
        return None


class MemSampler:
    def __init__(self, interval_s: float = 0.5):
        self.interval_s = interval_s
        self.samples: list[float] = []
        self._task: asyncio.Task | None = None

    async def _run(self) -> None:
        while True:
            value = mem_used_gib()
            if value is not None:
                self.samples.append(value)
            await asyncio.sleep(self.interval_s)

    def __enter__(self) -> MemSampler:
        self._task = asyncio.get_running_loop().create_task(self._run())
        return self

    def __exit__(self, *exc) -> None:
        self._task.cancel()

    def summary(self) -> dict:
        if not self.samples:
            return {"mem_peak_gib": None, "mem_min_gib": None}
        return {"mem_peak_gib": max(self.samples), "mem_min_gib": min(self.samples)}


async def one_request(client: httpx.AsyncClient, args, prompt: dict) -> dict:
    payload = {"model": args.model, "messages": prompt["messages"], "max_tokens": args.max_tokens,
               "temperature": args.temperature, "stream": True, "stream_options": {"include_usage": True}}
    if args.seed is not None:
        payload["seed"] = args.seed
    rec = {"status": None, "error": None, "ttft_ms": None, "e2e_ms": None, "input_tokens": None,
           "output_tokens": None, "finish_reason": None, "request_id": None}
    text_parts: list[str] = []
    t0 = time.perf_counter()
    try:
        async with client.stream("POST", "/v1/chat/completions", json=payload,
                                 headers={"X-Request-ID": uuid.uuid4().hex}) as r:
            rec["status"] = r.status_code
            rec["request_id"] = r.headers.get("x-request-id")
            if r.status_code != 200:
                raw = await r.aread()
                try:
                    rec["error"] = json.loads(raw).get("error", {}).get("type") or f"http_{r.status_code}"
                except (ValueError, AttributeError):
                    rec["error"] = f"http_{r.status_code}"
            else:
                async for line in r.aiter_lines():
                    if not line.startswith("data: ") or line == "data: [DONE]":
                        continue
                    event = json.loads(line[6:])
                    if "error" in event:  # gateway-injected mid-stream error
                        rec["error"] = event["error"].get("type", "stream_error")
                        continue
                    if event.get("usage"):
                        rec["input_tokens"] = event["usage"].get("prompt_tokens")
                        rec["output_tokens"] = event["usage"].get("completion_tokens")
                    for choice in event.get("choices") or []:
                        content = (choice.get("delta") or {}).get("content")
                        if content:
                            if rec["ttft_ms"] is None:
                                rec["ttft_ms"] = round((time.perf_counter() - t0) * 1000, 2)
                            text_parts.append(content)
                        if choice.get("finish_reason"):
                            rec["finish_reason"] = choice["finish_reason"]
    except httpx.HTTPError as e:
        rec["error"] = type(e).__name__
    rec["e2e_ms"] = round((time.perf_counter() - t0) * 1000, 2)
    rec["ok"] = rec["status"] == 200 and rec["error"] is None
    text = "".join(text_parts)
    if rec["ok"]:
        rec["validator_pass"], rec["validator_reason"] = validate(prompt["validator"], text)
    else:
        rec["validator_pass"], rec["validator_reason"] = None, None
    if args.store_output:
        rec["output"] = text
    return rec


async def run_phase(client, args, prompts, concurrency, rep, phase, write) -> dict:
    sem = asyncio.Semaphore(concurrency)

    async def guarded(prompt):
        async with sem:
            rec = await one_request(client, args, prompt)
        write({"type": "request", "phase": phase, "rep": rep, "concurrency": concurrency,
               "prompt_id": prompt["id"], "category": prompt["category"], "length": prompt["length"], **rec})

    with MemSampler() as mem:
        t0 = time.perf_counter()
        await asyncio.gather(*(guarded(p) for p in prompts))
        wall_s = time.perf_counter() - t0
    return {"wall_s": round(wall_s, 3), **mem.summary()}


async def main_async(args) -> Path:
    prompts_bytes = Path(args.prompts).read_bytes()
    prompts = [json.loads(line) for line in prompts_bytes.decode().splitlines() if line.strip()]
    if args.limit:
        prompts = prompts[: args.limit]
    concurrencies = [int(c) for c in args.concurrency.split(",")]

    config = {k: v for k, v in vars(args).items() if k not in {"out_dir", "api_key_env"}}
    config_hash = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()[:12]
    run_id = f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{args.label}"
    out_path = Path(args.out_dir) / f"{run_id}.jsonl"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with open(out_path, "w") as out:
        def write(obj: dict) -> None:
            out.write(json.dumps({"run_id": run_id, **obj}) + "\n")
            out.flush()

        write({"type": "run", "label": args.label, "started_at": datetime.now(UTC).isoformat(),
               "config": config, "config_hash": config_hash,
               "prompts_sha256": hashlib.sha256(prompts_bytes).hexdigest()[:16], "n_prompts": len(prompts),
               "env": {"python": sys.version.split()[0], "machine": platform.machine()},
               "mem_used_gib_before": mem_used_gib()})

        headers = {}
        if args.api_key_env and os.environ.get(args.api_key_env):
            headers["Authorization"] = f"Bearer {os.environ[args.api_key_env]}"
        limits = httpx.Limits(max_connections=max(concurrencies) + 4)
        async with httpx.AsyncClient(base_url=args.base_url, headers=headers, limits=limits,
                                     timeout=httpx.Timeout(args.timeout)) as client:
            if args.warmup:
                warm = prompts[: args.warmup]
                stats = await run_phase(client, args, warm, 1, 0, "warmup", write)
                write({"type": "phase", "phase": "warmup", "concurrency": 1, "rep": 0, **stats})
            for concurrency in concurrencies:
                for rep in range(1, args.repetitions + 1):
                    order = prompts[:]
                    random.Random(rep).shuffle(order)  # same order for a given rep across runs
                    stats = await run_phase(client, args, order, concurrency, rep, "measure", write)
                    write({"type": "phase", "phase": "measure", "concurrency": concurrency, "rep": rep, **stats})
                    print(f"concurrency={concurrency} rep={rep} wall={stats['wall_s']}s", file=sys.stderr)

        write({"type": "run_end", "finished_at": datetime.now(UTC).isoformat(),
               "mem_used_gib_after": mem_used_gib()})
    return out_path


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base-url", default="http://127.0.0.1:9000")
    p.add_argument("--model", required=True, help="served model name as the server reports it")
    p.add_argument("--prompts", default=str(ROOT / "benchmarks/prompts/dev.jsonl"))
    p.add_argument("--concurrency", default="1", help="comma-separated, e.g. 1,4,8")
    p.add_argument("--repetitions", type=int, default=3)
    p.add_argument("--warmup", type=int, default=5, help="requests sent first and labelled phase=warmup")
    p.add_argument("--max-tokens", type=int, default=256)
    p.add_argument("--temperature", type=float, default=0.0)
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--limit", type=int, default=0, help="use only the first N prompts (smoke tests)")
    p.add_argument("--timeout", type=float, default=300.0)
    p.add_argument("--label", default="run")
    p.add_argument("--store-output", action="store_true", help="keep model text for failure analysis")
    p.add_argument("--api-key-env", default="GATEWAY_API_KEY", help="env var holding the gateway key")
    p.add_argument("--out-dir", default=str(ROOT / "results/raw"))
    return p.parse_args(argv)


def main(argv=None) -> None:
    path = asyncio.run(main_async(parse_args(argv)))
    print(path)


if __name__ == "__main__":
    main()
