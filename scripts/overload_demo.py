"""Overload demo: fire a burst far above gateway capacity and show that excess load is rejected
explicitly (429/503 with Retry-After) instead of queueing without bound.

    python scripts/overload_demo.py --gateway http://127.0.0.1:9000 --model <name> --burst 60
    python scripts/overload_demo.py ... --mock   # against mock_upstream: adds slow fake generation
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from collections import Counter

import httpx


async def main(args) -> None:
    body = {"model": args.model, "max_tokens": args.max_tokens,
            "messages": [{"role": "user", "content": "Write one sentence about queues."}]}
    if args.mock:
        body["mock"] = {"ttft_s": 0.2, "token_delay_s": 0.02}
    peak = {"inflight": 0, "waiting": 0}
    done = asyncio.Event()

    async def watch(client):
        while not done.is_set():
            h = (await client.get("/healthz")).json()
            peak["inflight"], peak["waiting"] = max(peak["inflight"], h["inflight"]), max(peak["waiting"], h["waiting"])
            await asyncio.sleep(0.02)

    async def fire(client):
        t0 = time.perf_counter()
        r = await client.post("/v1/chat/completions", json=body)
        kind = r.json().get("error", {}).get("type", "ok") if r.status_code != 200 else "ok"
        return r.status_code, kind, (time.perf_counter() - t0) * 1000

    limits = httpx.Limits(max_connections=args.burst + 8)
    async with httpx.AsyncClient(base_url=args.gateway, timeout=120, limits=limits) as client:
        watcher = asyncio.create_task(watch(client))
        results = await asyncio.gather(*(fire(client) for _ in range(args.burst)))
        done.set()
        await watcher

    by_kind = Counter((s, k) for s, k, _ in results)
    print(f"burst={args.burst} peak_inflight={peak['inflight']} peak_waiting={peak['waiting']}")
    print(f"{'status':>6}  {'outcome':<14} {'count':>5}  {'max_ms':>8}")
    for (status, kind), n in sorted(by_kind.items()):
        worst = max(ms for s, k, ms in results if (s, k) == (status, kind))
        print(f"{status:>6}  {kind:<14} {n:>5}  {worst:>8.0f}")
    if args.out:
        with open(args.out, "w") as f:
            json.dump({"burst": args.burst, "peak": peak,
                       "results": [{"status": s, "outcome": k, "ms": round(ms, 1)} for s, k, ms in results]}, f)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--gateway", default="http://127.0.0.1:9000")
    p.add_argument("--model", default="mock")
    p.add_argument("--burst", type=int, default=60)
    p.add_argument("--max-tokens", type=int, default=64)
    p.add_argument("--mock", action="store_true")
    p.add_argument("--out")
    asyncio.run(main(p.parse_args()))
