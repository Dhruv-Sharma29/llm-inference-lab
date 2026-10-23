"""Deterministic answer checkers. Each returns (passed, reason); reasons feed failure analysis."""

from __future__ import annotations

import json
import re
from typing import Any

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)
_ANSWER = re.compile(r"ANSWER:\s*\$?\s*(-?[\d,]+)", re.IGNORECASE)


def extract_json_object(text: str) -> Any:
    """First JSON object in the text, tolerating code fences and surrounding prose."""
    fenced = _FENCE.search(text)
    if fenced:
        text = fenced.group(1)
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object found")
    obj, _ = json.JSONDecoder().raw_decode(text[start:])
    return obj


def _normalise(value: Any) -> Any:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return {k: _normalise(v) for k, v in value.items()}
    return value


def json_equals(expected: dict, text: str) -> tuple[bool, str]:
    try:
        got = extract_json_object(text)
    except ValueError as e:
        return False, f"unparseable: {e}"
    if not isinstance(got, dict):
        return False, "not an object"
    got, expected = _normalise(got), _normalise(expected)
    if set(got) != set(expected):
        return False, f"keys differ: missing={sorted(set(expected) - set(got))} extra={sorted(set(got) - set(expected))}"
    wrong = sorted(k for k in expected if got[k] != expected[k])
    return (not wrong), ("ok" if not wrong else f"wrong values: {wrong}")


def final_integer(expected: int, text: str) -> tuple[bool, str]:
    matches = _ANSWER.findall(text)
    if not matches:
        return False, "no ANSWER line"
    got = int(matches[-1].replace(",", ""))
    return (got == expected), ("ok" if got == expected else f"got {got}")


VALIDATORS = {"json_equals": json_equals, "final_integer": final_integer}


def validate(spec: dict, text: str) -> tuple[bool, str]:
    return VALIDATORS[spec["type"]](spec["expected"], text)
