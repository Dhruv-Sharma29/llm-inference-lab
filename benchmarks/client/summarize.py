"""Summarise raw benchmark files into one row per (run, concurrency), measure phase only.

    python -m benchmarks.client.summarize results/raw/*.jsonl --out results/summary/baseline

Metric definitions:
  ok / errors        ok = HTTP 200 with no stream error. Errors are counted, never dropped.
  e2e/ttft p50/p95   nearest-rank percentiles over ok requests.
  decode_tok_s_p50   per-request: output_tokens / (e2e - ttft), median over ok requests.
  agg_out_tok_s      aggregate: sum(output_tokens of ok requests) / sum(phase wall time).
  ok_req_s           successful requests / sum(phase wall time).
  quality            validator passes / ok requests (and / all requests, which counts errors as fails).
  mem_peak_gib       max system-wide used memory across the run's measure phases (see bench.py).
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from collections import Counter, defaultdict
from pathlib import Path


def pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q / 100 * len(ordered)) - 1)]


def load(paths: list[Path]) -> tuple[dict, dict, dict]:
    runs, requests, phases = {}, defaultdict(list), defaultdict(list)
    for path in paths:
        for line in path.read_text().splitlines():
            rec = json.loads(line)
            key = (rec["run_id"], rec.get("concurrency"))
            if rec["type"] == "run":
                runs[rec["run_id"]] = rec
            elif rec["type"] == "request" and rec["phase"] == "measure":
                requests[key].append(rec)
            elif rec["type"] == "phase" and rec["phase"] == "measure":
                phases[key].append(rec)
    return runs, requests, phases


def summarise(runs, requests, phases) -> list[dict]:
    rows = []
    for (run_id, concurrency), reqs in sorted(requests.items()):
        ok = [r for r in reqs if r["ok"]]
        wall = sum(p["wall_s"] for p in phases[(run_id, concurrency)])
        decode = [r["output_tokens"] / ((r["e2e_ms"] - r["ttft_ms"]) / 1000) for r in ok
                  if r["output_tokens"] and r["output_tokens"] > 1 and r["ttft_ms"] and r["e2e_ms"] > r["ttft_ms"]]
        passes = sum(1 for r in ok if r["validator_pass"])
        mems = [p["mem_peak_gib"] for p in phases[(run_id, concurrency)] if p.get("mem_peak_gib") is not None]
        errors = Counter(r["error"] or f"http_{r['status']}" for r in reqs if not r["ok"])
        rnd = lambda v, n=1: None if v is None else round(v, n)  # noqa: E731
        rows.append({
            "run_id": run_id, "label": runs.get(run_id, {}).get("label"), "concurrency": concurrency,
            "requests": len(reqs), "ok": len(ok), "errors": sum(errors.values()),
            "error_types": ";".join(f"{k}={v}" for k, v in sorted(errors.items())),
            "e2e_p50_ms": rnd(pct([r["e2e_ms"] for r in ok], 50)),
            "e2e_p95_ms": rnd(pct([r["e2e_ms"] for r in ok], 95)),
            "ttft_p50_ms": rnd(pct([r["ttft_ms"] for r in ok if r["ttft_ms"]], 50)),
            "ttft_p95_ms": rnd(pct([r["ttft_ms"] for r in ok if r["ttft_ms"]], 95)),
            "decode_tok_s_p50": rnd(pct(decode, 50)),
            "agg_out_tok_s": rnd(sum(r["output_tokens"] or 0 for r in ok) / wall if wall else None),
            "ok_req_s": rnd(len(ok) / wall if wall else None, 2),
            "quality_of_ok": rnd(passes / len(ok) if ok else None, 3),
            "quality_of_all": rnd(passes / len(reqs) if reqs else None, 3),
            "mem_peak_gib": max(mems) if mems else None,
        })
    return rows


def to_markdown(rows: list[dict]) -> str:
    cols = ["label", "concurrency", "requests", "ok", "errors", "e2e_p50_ms", "e2e_p95_ms", "ttft_p50_ms",
            "ttft_p95_ms", "decode_tok_s_p50", "agg_out_tok_s", "ok_req_s", "quality_of_ok", "mem_peak_gib"]
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    lines += ["| " + " | ".join("" if r[c] is None else str(r[c]) for c in cols) + " |" for r in rows]
    return "\n".join(lines) + "\n"


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("files", nargs="+", type=Path)
    p.add_argument("--out", type=Path, help="path prefix; writes <out>.md and <out>.csv")
    args = p.parse_args(argv)
    rows = summarise(*load(args.files))
    md = to_markdown(rows)
    print(md)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.with_suffix(".md").write_text(md)
        with open(args.out.with_suffix(".csv"), "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)


if __name__ == "__main__":
    main()
