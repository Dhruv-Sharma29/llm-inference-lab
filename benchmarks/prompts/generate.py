"""Deterministic prompt-set generator with known answers.

Every prompt carries a validator spec, so quality is scored without any model or human judge.
The held-out set uses a different seed AND disjoint entity pools and phrasings, so tuning
against dev cannot simply memorise held-out answers.

    python -m benchmarks.prompts.generate            # writes dev.jsonl and heldout.jsonl here

Categories (SPEC.md): structured extraction, tool-call formatting, short reasoning.
Length buckets: short (< ~200 tokens) and long (~1.5-2.5k tokens of surrounding context).
"""

from __future__ import annotations

import argparse
import json
import math
import random
from pathlib import Path

HERE = Path(__file__).resolve().parent

POOLS = {
    "dev": {
        "customers": ["Priya Sharma", "Arjun Mehta", "Lena Fischer", "Tomás Ortega", "Aiko Tanaka",
                      "Grace Okafor", "Rahul Verma", "Sofia Rossi", "Daniel Kim", "Meera Iyer"],
        "products": ["steel brackets", "USB-C hubs", "label printers", "oak shelving units",
                     "thermal sensors", "packing tape rolls", "LED panels", "safety gloves"],
        "extraction_templates": [
            "Order {order_id} was placed by {customer} on {date}. It covers {quantity} {product}.",
            "On {date}, {customer} submitted order {order_id} requesting {quantity} {product}.",
            "{customer} ordered {quantity} {product} (reference {order_id}), dated {date}.",
        ],
        "tool_phrasings": {
            "restart_service": ["Please restart the {service} in {environment}.",
                                "The {service} on {environment} is stuck, bounce it."],
            "scale_service": ["Scale the {service} to {replicas} replicas.",
                              "We need {replicas} instances of the {service} running."],
            "get_logs": ["Show me the last {minutes} minutes of {service} logs.",
                         "Pull {service} logs for the past {minutes} minutes."],
            "none": ["What does the {service} do in our system?",
                     "Thanks, that's all for the {service} for now."],
        },
    },
    "heldout": {
        "customers": ["Nadia Haddad", "Kofi Mensah", "Ingrid Larsen", "Wei Zhang", "Carlos Mendes",
                      "Ananya Rao", "Oliver Brooks", "Yuki Sato"],
        "products": ["copper fittings", "barcode scanners", "pallet wraps", "desk lamps",
                     "pressure valves", "cable ties"],
        "extraction_templates": [
            "Ref {order_id}: {quantity} {product} for {customer}, logged {date}.",
            "A request from {customer} dated {date} asks for {quantity} {product}; the order number is {order_id}.",
        ],
        "tool_phrasings": {
            "restart_service": ["Can you do a restart of {service} on the {environment} cluster?"],
            "scale_service": ["Set {service} replica count to {replicas}."],
            "get_logs": ["I need {service} log output from the previous {minutes} minutes."],
            "none": ["Who owns the {service}?"],
        },
    },
}

SERVICES = ["api", "worker", "scheduler"]
ENVIRONMENTS = ["staging", "production"]

TOOLS_SYSTEM = """You are an operations assistant. Choose exactly one tool for the user's request.
Tools:
- restart_service(service: "api"|"worker"|"scheduler", environment: "staging"|"production")
- scale_service(service: "api"|"worker"|"scheduler", replicas: integer 1-10)
- get_logs(service: "api"|"worker"|"scheduler", minutes: integer 1-120)
If no tool applies, use tool "none" with empty arguments.
Reply with only JSON of the form {"tool": "<name>", "arguments": {...}}."""

EXTRACTION_INSTRUCTION = ("Extract the order as JSON with exactly these keys: order_id, customer, date "
                          "(YYYY-MM-DD), quantity (integer), product. Reply with only the JSON.")

FILLER_WORDS = ("the quarterly review noted that warehouse throughput remained stable while supplier "
                "lead times varied across regions and the operations team planned further audits of "
                "inventory accuracy staffing levels shipping costs and customer response times").split()

REASONING_SUFFIX = "Show brief working, then finish with a final line of the form: ANSWER: <integer>"


def filler(rng: random.Random, n_paragraphs: int) -> str:
    paragraphs = []
    for _ in range(n_paragraphs):
        words = [rng.choice(FILLER_WORDS) for _ in range(rng.randint(70, 110))]
        paragraphs.append(" ".join(words).capitalize() + ".")
    return "\n\n".join(paragraphs)


def extraction(rng: random.Random, pool: dict, long: bool) -> tuple[list[dict], dict]:
    answer = {
        "order_id": f"{rng.choice('ABCDEFGH')}{rng.randint(1000, 9999)}",
        "customer": rng.choice(pool["customers"]),
        "date": f"2026-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}",
        "quantity": rng.randint(2, 500),
        "product": rng.choice(pool["products"]),
    }
    record = rng.choice(pool["extraction_templates"]).format(**answer)
    if long:
        before, after = filler(rng, rng.randint(6, 9)), filler(rng, rng.randint(6, 9))
        document = f"{before}\n\n{record}\n\n{after}"
        user = f"Report:\n\n{document}\n\nThe report mentions exactly one order. {EXTRACTION_INSTRUCTION}"
    else:
        user = f"{record}\n\n{EXTRACTION_INSTRUCTION}"
    return [{"role": "user", "content": user}], {"type": "json_equals", "expected": answer}


def tool_call(rng: random.Random, pool: dict, long: bool) -> tuple[list[dict], dict]:
    tool = rng.choice(["restart_service", "scale_service", "get_logs", "none"])
    slots = {"service": rng.choice(SERVICES), "environment": rng.choice(ENVIRONMENTS),
             "replicas": rng.randint(1, 10), "minutes": rng.choice([5, 10, 15, 30, 45, 60, 90])}
    args = {
        "restart_service": {"service": slots["service"], "environment": slots["environment"]},
        "scale_service": {"service": slots["service"], "replicas": slots["replicas"]},
        "get_logs": {"service": slots["service"], "minutes": slots["minutes"]},
        "none": {},
    }[tool]
    user = rng.choice(pool["tool_phrasings"][tool]).format(**slots)
    system = TOOLS_SYSTEM
    if long:
        system += "\n\nBackground runbook (for context only, does not change the tool rules):\n\n" + filler(rng, 12)
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    return messages, {"type": "json_equals", "expected": {"tool": tool, "arguments": args}}


def reasoning(rng: random.Random, pool: dict, long: bool) -> tuple[list[dict], dict]:
    kind = rng.choice(["drain", "cost", "capacity"])
    if kind == "drain":
        jobs, workers, rate = rng.randint(100, 2000), rng.randint(2, 8), rng.randint(5, 30)
        q = (f"A queue holds {jobs} jobs. There are {workers} workers and each processes {rate} jobs "
             f"per minute. How many whole minutes until the queue is empty (round up)?")
        answer = math.ceil(jobs / (workers * rate))
    elif kind == "cost":
        per_day, cents, days = rng.randint(1000, 50000), rng.randint(1, 9), rng.randint(7, 31)
        q = (f"A service handles {per_day} requests per day. Each request costs {cents} cents. "
             f"What is the total cost in cents over {days} days?")
        answer = per_day * cents * days
    else:
        rps, per_server, headroom = rng.randint(200, 5000), rng.randint(50, 400), rng.choice([10, 20, 25, 50])
        q = (f"Peak traffic is {rps} requests per second. One server handles {per_server} requests per "
             f"second. Including {headroom}% extra headroom on top of peak traffic, how many servers are "
             f"needed at minimum (round up)?")
        answer = math.ceil(rps * (100 + headroom) / 100 / per_server)
    return [{"role": "user", "content": f"{q}\n{REASONING_SUFFIX}"}], {"type": "final_integer", "expected": answer}


GENERATORS = {"extraction": extraction, "tool_call": tool_call, "reasoning": reasoning}
# (category, length, count) per split. Reasoning is short-only: long context adds nothing to it.
MIX = {
    "dev": [("extraction", "short", 20), ("extraction", "long", 15), ("tool_call", "short", 20),
            ("tool_call", "long", 10), ("reasoning", "short", 35)],
    "heldout": [("extraction", "short", 12), ("extraction", "long", 8), ("tool_call", "short", 12),
                ("tool_call", "long", 8), ("reasoning", "short", 20)],
}
SEEDS = {"dev": 20261006, "heldout": 77001}


def build(split: str) -> list[dict]:
    rng = random.Random(SEEDS[split])
    pool = POOLS[split]
    records = []
    for category, length, count in MIX[split]:
        for i in range(count):
            messages, validator = GENERATORS[category](rng, pool, long=(length == "long"))
            records.append({"id": f"{split}-{category}-{length}-{i:03d}", "split": split, "category": category,
                            "length": length, "messages": messages, "validator": validator})
    return records


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out-dir", type=Path, default=HERE)
    args = parser.parse_args()
    for split in ("dev", "heldout"):
        records = build(split)
        path = args.out_dir / f"{split}.jsonl"
        path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records))
        print(f"wrote {len(records)} prompts -> {path}")


if __name__ == "__main__":
    main()
