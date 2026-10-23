"""Tests for the benchmark harness: deterministic prompts, honest validators, correct summaries."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx

from benchmarks.client.bench import one_request
from benchmarks.client.summarize import pct, summarise
from benchmarks.prompts.generate import build
from benchmarks.validators.validators import validate


def test_prompt_sets_are_deterministic_and_sized():
    assert build("dev") == build("dev")
    assert len(build("dev")) == 100
    assert len(build("heldout")) == 60


def test_heldout_does_not_share_entities_or_prompts_with_dev():
    dev, held = build("dev"), build("heldout")
    dev_texts = {json.dumps(p["messages"]) for p in dev}
    assert not any(json.dumps(p["messages"]) in dev_texts for p in held)
    dev_customers = {p["validator"]["expected"]["customer"] for p in dev if p["category"] == "extraction"}
    held_customers = {p["validator"]["expected"]["customer"] for p in held if p["category"] == "extraction"}
    assert not dev_customers & held_customers


def test_every_prompt_is_answerable_by_its_own_expected_value():
    """A perfect model must score 100%: guards against generator/validator mismatches."""
    for p in build("dev") + build("heldout"):
        expected = p["validator"]["expected"]
        perfect = json.dumps(expected) if p["validator"]["type"] == "json_equals" else f"work...\nANSWER: {expected}"
        assert validate(p["validator"], perfect) == (True, "ok"), p["id"]


def test_json_validator_tolerates_fences_but_not_wrong_values():
    spec = {"type": "json_equals", "expected": {"tool": "none", "arguments": {}}}
    assert validate(spec, '```json\n{"tool": "none", "arguments": {}}\n```')[0]
    assert validate(spec, 'Sure! {"tool": "none", "arguments": {}} hope that helps')[0]
    assert not validate(spec, '{"tool": "get_logs", "arguments": {}}')[0]
    assert not validate(spec, '{"tool": "none"}')[0]
    assert not validate(spec, "no json here")[0]


def test_integer_validator_uses_last_answer_line():
    spec = {"type": "final_integer", "expected": 42}
    assert validate(spec, "ANSWER: 41\nwait, recheck\nANSWER: 42")[0]
    assert validate(spec, "ANSWER: 1,042")[0] is False
    assert validate(spec, "the answer is 42")[0] is False


def test_percentile_nearest_rank():
    assert pct([5, 1, 3, 2, 4], 50) == 3
    assert pct(list(range(1, 101)), 95) == 95
    assert pct([], 50) is None


def test_summary_counts_failures_and_excludes_warmup():
    run_id = "r1"
    def req(ok, phase="measure", **kw):
        base = {"run_id": run_id, "type": "request", "phase": phase, "concurrency": 2, "ok": ok,
                "status": 200 if ok else 429, "error": None if ok else "queue_full", "e2e_ms": 1000.0,
                "ttft_ms": 100.0, "output_tokens": 90, "validator_pass": ok}
        return {**base, **kw}
    runs = {run_id: {"label": "t"}}
    requests = {(run_id, 2): [req(True), req(True, validator_pass=False), req(False)]}
    phases = {(run_id, 2): [{"wall_s": 2.0, "mem_peak_gib": 10.0}]}
    [row] = summarise(runs, requests, phases)
    assert (row["requests"], row["ok"], row["errors"]) == (3, 2, 1)
    assert row["error_types"] == "queue_full=1"
    assert row["quality_of_ok"] == 0.5 and row["quality_of_all"] == 0.333
    assert row["agg_out_tok_s"] == 90.0  # 180 tokens / 2 s
    assert row["decode_tok_s_p50"] == 100.0  # 90 tokens / 0.9 s


def test_macos_memory_parser_on_real_vm_stat_format(monkeypatch):
    """vm_stat doesn't exist on Linux; feed its macOS output format to the parser."""
    import subprocess

    from benchmarks.client import bench
    sample = ("Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
              "Pages free:                               12345.\n"
              "Pages active:                            262144.\n"
              "Pages inactive:                           99999.\n"
              "Pages wired down:                        131072.\n"
              "Pages occupied by compressor:             65536.\n")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=sample))
    # (262144 + 131072 + 65536) pages x 16 KiB = 7.0 GiB
    assert bench._mem_used_gib_macos() == 7.0


def _stream(lines: list[str]) -> dict:
    """One request against a fake upstream that streams exactly `lines`."""
    body = "".join(f"{line}\n\n" for line in lines).encode()
    transport = httpx.MockTransport(lambda req: httpx.Response(200, content=body,
                                                               headers={"content-type": "text/event-stream"}))
    args = SimpleNamespace(model="m", max_tokens=8, temperature=0, seed=None, store_output=False)
    prompt = {"messages": [{"role": "user", "content": "x"}], "validator": {"type": "final_integer", "expected": 7}}

    async def go():
        async with httpx.AsyncClient(transport=transport, base_url="http://upstream") as c:
            return await one_request(c, args, prompt)
    return asyncio.run(go())


def test_stream_without_done_is_an_error_not_a_success():
    chunk = 'data: {"choices": [{"delta": {"content": "7"}}]}'
    assert _stream([chunk, "data: [DONE]"])["ok"]
    cut = _stream([chunk])                      # connection closed before [DONE]
    assert not cut["ok"] and cut["error"] == "stream_truncated"
