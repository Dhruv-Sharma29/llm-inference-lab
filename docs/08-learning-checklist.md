# 08 — Learning checklist

The skills are listed in the order the milestones force you to learn them. "Done when" names
something in this repository that shows the skill was used, not just read about.

## M0 — Environment record

| Skill | Why | Done when |
|---|---|---|
| Reading a container's privilege boundary (`/proc/self/status` CapEff, cgroup mounts, `/.dockerenv`) | Settles whether Docker can run here before you spend a day trying | `env/ENVIRONMENT.md` states why the container runtime is unavailable, with the evidence |
| Unified-memory accounting (`/proc/meminfo`, `nvidia-smi`, `free`) | "GPU memory" is ambiguous on GB10, so you have to pick a definition and keep it | One definition in `bench.py`, restated in [04](04-model-server.md) § Memory |
| Recording an environment so it can be compared | Results are comparable only between runs with the same record | Every row of ENVIRONMENT.md has a "How measured" column |

## M1 — Serving a model

| Skill | Why | Done when |
|---|---|---|
| Pinning by revision hash and image digest, not by tag | A tag can move under you, a digest cannot | Model revision pinned. **Image digest still TBD** |
| CUDA compute capability vs toolkit target, and PTX JIT | GB10 (12.1) is newer than what CUDA 12.8 compiles natively | The llama.cpp build note in ENVIRONMENT.md, plus the `CUDA_CACHE_MAXSIZE` setting |
| HF → GGUF conversion and quantisation | The stopgap needs llama.cpp's format | `llamacpp_setup.sh` produces a Q4_K_M file whose sha256 is recorded |
| Health checks that prove identity, not just liveness | Port 8000 was answered by someone else's service | Readiness requires the served model id (`check_upstream.sh`, `llamacpp_serve.sh`) |
| Process supervision without an init system | PID 1 that never reaps leaves zombies that look alive | `running()` checks process state, not just `kill -0` |

## M2 — Measurement

| Skill | Why | Done when |
|---|---|---|
| Deterministic, verifiable prompt generation | Quality must be scorable without a judge model | 160 prompts with known answers. Every prompt passes its own validator |
| Dev / held-out separation beyond a seed | Tuning against dev must not leak into held-out | Disjoint entity pools, asserted by a test |
| Percentile definitions (nearest rank) and small-n honesty | p95 of 20 samples is the 19th value | `test_percentile_nearest_rank`; [05](05-evaluation.md) explains the TTFT p95 |
| Per-request vs aggregate throughput | They answer different questions, and the SPEC requires you to say which | Both columns in every summary |
| Counting failures instead of filtering them | Faster by failing must not look like faster | `test_summary_counts_failures_and_excludes_warmup` |

## M3 — Gateway correctness

| Skill | Why | Done when |
|---|---|---|
| ASGI lifecycle: `http.disconnect`, `receive`/`send`, spec versions | Disconnects are where streaming proxies leak | `ManagedStreamingResponse`; both cancellation tests |
| `anyio` task groups and shielded cancel scopes | Cleanup has to run *while* being cancelled | The `finally` with `CancelScope(shield=True)` in `streaming.py` |
| `asyncio` races: `wait(FIRST_COMPLETED)`, cancelling a pending acquire safely | Leaving a queue without leaking a slot granted at the same moment | `acquire_slot()`; `test_client_leaving_the_queue_never_reaches_upstream` |
| Deadline propagation instead of per-call timeouts | One budget across queue, connect and every read | `remaining()` and the per-line `wait_for` |
| Admission control and backpressure semantics (`429` vs `503`, `Retry-After`) | The client should learn from the status whether it was ever close to being served | [02](02-admission-and-limits.md) § The bounded queue |
| Token-bucket rate limiting | Per-key fairness | `quota.py`; `test_per_key_quota` |
| Logging by allow-list | Redaction you have to remember will eventually be forgotten | `LOG_FIELDS`; the sentinel test |
| Process-level integration tests and mutation testing | A test that passes without the fix proves nothing | Real servers over TCP. The stock-class mutation fails the cancellation tests |

## M4 — Serving experiments (ahead)

| Skill | Why | Done when |
|---|---|---|
| One-factor experiment design and reporting | The SPEC's "fair comparison" | `results/summary/EXP-01.md` with hypothesis → diff → table → explanation → decision |
| vLLM scheduler knobs (`max-num-seqs`, batched tokens, prefix caching) | Exps 3–4 | Each knob's effect explained, not only measured |
| Reading a latency/throughput knee | Sets the gateway limits from data | `GATEWAY_MAX_INFLIGHT` changed from "initial guess" to a measured value |
| Quantisation vs quality on held-out | Exp 5 | Held-out quality reported next to the speed-up |

## Interview-ready talking points, mapped to what was built

| Question | Where the answer lives |
|---|---|
| "How did you scope this?" | [00](00-charter.md) § Scope: one model, one replica, one factor at a time, because the measurement is already confounded |
| "What happens under overload?" | [02](02-admission-and-limits.md): two bounds, `429` vs `503`, and the real-server burst numbers |
| "How do you know cancellation actually stops the GPU?" | [03](03-streaming-and-cancellation.md) § Evidence: the server's own token counter, 0 tokens after idle. Plus the mutation test |
| "What if the model server crashes?" | [03](03-streaming-and-cancellation.md) § Upstream crash: in-band error 0.01 s after SIGKILL, next request `502` in 0.04 s |
| "Why not just vLLM's own queue?" | [07](07-risks.md) § The question to have a real answer for |
| "What are your throughput numbers?" | [05](05-evaluation.md) § Results so far, and why they are not a baseline. Say that first |
| "What went wrong?" | [09](09-implementation-notes.md): no Docker, GB10 CUDA arch, port 8000, zombies, the Starlette disconnect behaviour |
| "What would you do differently?" | Have a real answer. Candidates: agree the benchmark window before building the harness, make `host_run_vllm.sh` read `serve.env`, check `[DONE]` in the client |
