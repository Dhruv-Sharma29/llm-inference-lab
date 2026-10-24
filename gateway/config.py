"""Gateway settings, read once from environment variables (prefix GATEWAY_)."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _get(name: str, default: str) -> str:
    return os.environ.get(f"GATEWAY_{name}", default)


@dataclass(frozen=True)
class Settings:
    upstream_url: str = "http://127.0.0.1:8000"
    upstream_api_key: str | None = None  # matches the model server's --api-key, if set
    # Admission control: requests running against the model server, and requests allowed to wait.
    max_inflight: int = 16  # vLLM's --max-num-seqs; Exp 1 on vLLM put the knee there (results/summary/EXP-01-concurrency.md)
    max_queue: int = 32
    queue_timeout_s: float = 10.0
    # Total deadline for one request, including queueing and streaming.
    request_timeout_s: float = 120.0
    connect_timeout_s: float = 2.0
    # Input/output limits. Token counts use `tokenizer_file` when set, otherwise a char heuristic.
    max_body_bytes: int = 1_000_000
    max_input_tokens: int = 4096
    max_output_tokens: int = 1024
    tokenizer_file: str | None = None
    # Auth and quota. Empty api_keys disables auth (only acceptable on a loopback-bound dev server).
    api_keys: frozenset[str] = frozenset()
    quota_rps: float = 0.0  # 0 disables per-key quota
    quota_burst: int = 10

    @classmethod
    def from_env(cls) -> Settings:
        keys = frozenset(k.strip() for k in _get("API_KEYS", "").split(",") if k.strip())
        return cls(
            upstream_url=_get("UPSTREAM_URL", cls.upstream_url).rstrip("/"),
            upstream_api_key=_get("UPSTREAM_API_KEY", "") or None,
            max_inflight=int(_get("MAX_INFLIGHT", str(cls.max_inflight))),
            max_queue=int(_get("MAX_QUEUE", str(cls.max_queue))),
            queue_timeout_s=float(_get("QUEUE_TIMEOUT_S", str(cls.queue_timeout_s))),
            request_timeout_s=float(_get("REQUEST_TIMEOUT_S", str(cls.request_timeout_s))),
            connect_timeout_s=float(_get("CONNECT_TIMEOUT_S", str(cls.connect_timeout_s))),
            max_body_bytes=int(_get("MAX_BODY_BYTES", str(cls.max_body_bytes))),
            max_input_tokens=int(_get("MAX_INPUT_TOKENS", str(cls.max_input_tokens))),
            max_output_tokens=int(_get("MAX_OUTPUT_TOKENS", str(cls.max_output_tokens))),
            tokenizer_file=_get("TOKENIZER_FILE", "") or None,
            api_keys=keys,
            quota_rps=float(_get("QUOTA_RPS", str(cls.quota_rps))),
            quota_burst=int(_get("QUOTA_BURST", str(cls.quota_burst))),
        )
