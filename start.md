# Start here

How to run the gateway and harness, and what they are doing while they run. The
[README](README.md) makes the case for the design. This file gets you to a result.

Everything below runs on **CPU, with no GPU and no Docker**. The gateway runs against
`mock_upstream/`, a fake OpenAI-compatible server whose timing you control per request. That is
deliberate: admission, deadlines, cancellation and crash handling are all gateway behaviour, and
testing them should not depend on a busy GPU. The steps that need the host or the GPU are in
[RUNBOOK.md](RUNBOOK.md).

---

## 1. Install

Python 3.12 is required (`pyproject.toml`: `>=3.12,<3.13`), and [uv](https://docs.astral.sh/uv/)
manages the environment. Dependency versions are pinned in `uv.lock`.

```bash
uv sync --extra tokenizer      # creates .venv; the extra adds `tokenizers` for exact token counts
```

## 2. Run the tests

```bash
uv run pytest                  # 32 tests: 24 gateway (gateway/tests/) + 8 harness (benchmarks/)
```

The gateway tests start **real processes**, a mock upstream and a gateway, each a uvicorn on a free
loopback port, and talk to them over TCP. Disconnects and crashes only behave realistically over
real sockets. Expect the run to take tens of seconds, not milliseconds. What each test asserts is
listed in [docs/01-architecture.md](docs/01-architecture.md) § Components.

## 3. Start the stack and send a request

```bash
scripts/dev_stack.sh           # mock on 127.0.0.1:8000, gateway on 127.0.0.1:9000; Ctrl-C stops both
```

If port 8000 is taken (it is on the shared GX10, see `env/ENVIRONMENT.md`), move both ports:
`MOCK_PORT=18000 GATEWAY_PORT=19000 scripts/dev_stack.sh`, and change `9000` to `19000` below.

In another shell:

```bash
curl -sS http://127.0.0.1:9000/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"mock","messages":[{"role":"user","content":"hello"}],"max_tokens":8}'
curl -sS http://127.0.0.1:9000/healthz      # {"status":"ok","inflight":0,"waiting":0}
curl -sS http://127.0.0.1:9000/metrics | grep ^gateway_
```

The mock answers with placeholder tokens (`"tok0 tok1 … tok7"`). Every response carries an
`X-Request-ID`: yours if you send a safe one, a generated one otherwise. Now trigger a rejection:

```bash
curl -sS http://127.0.0.1:9000/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"mock","messages":[{"role":"user","content":"hello"}],"max_tokens":5000}'
# → 400 {"error": {"type": "invalid_max_tokens", ...}}
```

The full list of rejections is in [docs/01-architecture.md](docs/01-architecture.md) § Error
contract.

## 4. See overload handled

The default limits (8 running + 32 waiting) absorb a 40-request burst against the mock. To see
rejections, restart the stack with a small capacity:

```bash
GATEWAY_MAX_INFLIGHT=4 GATEWAY_MAX_QUEUE=8 scripts/dev_stack.sh
uv run python scripts/overload_demo.py --burst 40 --mock     # add --gateway http://127.0.0.1:19000 if you moved ports
```

The output has this shape:

```text
burst=40 peak_inflight=4 peak_waiting=8
status  outcome        count    max_ms
   200  ok               ...       ...
   429  queue_full       ...       ...
```

How to read it. `peak_inflight` and `peak_waiting` never exceed the limits you set. Everything
beyond 4 + 8 is a `429 queue_full` that returns in milliseconds instead of waiting. `--mock` makes
the mock generate slowly (0.2 s TTFT, 20 ms per token), so the admitted requests take seconds,
and you can see the queue fill. The README reports 12 served and 28 rejected for this
configuration. Add `--out <file>` to save the run.

## 5. Run the benchmark harness against the mock

```bash
uv run python -m benchmarks.client.bench --model mock --limit 20 --concurrency 1,4 --label smoke
uv run python -m benchmarks.client.summarize results/raw/*smoke*.jsonl
```

`bench` prints the path of the raw file it wrote under `results/raw/`. Don't commit mock runs.
**Quality is 0 by design:** the mock returns placeholder text, and the validators are strict. The
latency columns show the mock's configured timing, not a model's.

To see real numbers in the same format, regenerate the committed smoke summary from its raw file:

```bash
uv run python -m benchmarks.client.summarize results/raw/smoke/20261007T035456Z-smoke-llamacpp-sharedgpu.jsonl
```

That reproduces `results/summary/smoke-llamacpp-sharedgpu.md`. Read
[docs/05-evaluation.md](docs/05-evaluation.md) § Results so far before quoting any of it. It is 20
short extraction prompts on a GPU about 95 % busy with other jobs, **not a baseline**.

## 6. Read the output

| Column | Meaning |
|---|---|
| `ok`, `errors` | ok = HTTP 200 and no in-band error event. Errors are counted by type, never dropped |
| `e2e_p50_ms`, `e2e_p95_ms` | client send → stream end, nearest-rank, over ok requests |
| `ttft_p50_ms`, `ttft_p95_ms` | client send → first content token |
| `decode_tok_s_p50` | **per request**: output tokens / (e2e − ttft) |
| `agg_out_tok_s`, `ok_req_s` | **aggregate**: what the whole box delivered per second of wall time |
| `quality_of_ok` | validator passes / ok requests |
| `mem_peak_gib` | system-wide used memory, which includes **every** process on the machine |

The full definitions are in [docs/05-evaluation.md](docs/05-evaluation.md) § Metrics.

## 7. Where to read next

- [docs/README.md](docs/README.md): the reading order and current status.
- [docs/03-streaming-and-cancellation.md](docs/03-streaming-and-cancellation.md): the part a
  plain proxy gets wrong.
- [RUNBOOK.md](RUNBOOK.md): running against a real model (llama.cpp stopgap, or vLLM on the host).

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `scripts/dev_stack.sh: .venv/bin/python: No such file` | Run `uv sync --extra tokenizer` first. The script calls the venv's Python directly |
| `address already in use` | Set `MOCK_PORT` / `GATEWAY_PORT` (step 3) |
| Overload demo shows no `429` | The default capacity (8 + 32) absorbs the burst. Use the small limits in step 4 |
| `401 unauthorized` from the gateway | `GATEWAY_API_KEYS` is set in your shell. Unset it, or send `Authorization: Bearer <key>`. `bench` reads the key from `$GATEWAY_API_KEY` |
| `413 input_too_large` on modest inputs | Without `GATEWAY_TOKENIZER_FILE`, the count is chars/4. Raise `GATEWAY_MAX_INPUT_TOKENS` or point it at the model's `tokenizer.json` |
| Tests hang or fail on a loaded machine | The gateway tests have timing bounds (e.g. a rejection in < 0.5 s). Run them on an otherwise idle CPU |
| On macOS | Supported for development, but not yet run on a real Mac (README § macOS). `uv sync --extra tokenizer && uv run pytest` is the minimum |
