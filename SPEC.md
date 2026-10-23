# 01 — LLM inference lab and gateway

**Build first.** This supplies the local model endpoint and performance baseline for the rest of the AI portfolio.

## Objective

Build a private service that serves a local language model, measures its behavior under load, and handles overload predictably. Add model routing only after one model works reliably.

Main question: how can you improve useful throughput while preserving task quality and response latency on one GX10?

## Why this fits Amazon and Microsoft

Amazon's [Trainium collaboration](https://www.aboutamazon.com/news/company-news/amazon-invests-additional-5-billion-anthropic-ai) covers future accelerator generations and a ten-year AWS commitment. Microsoft's [Maia 200 announcement](https://blogs.microsoft.com/blog/2026/01/26/maia-200-the-ai-accelerator-built-for-inference/) emphasizes inference economics. This project studies related serving problems on NVIDIA hardware; it does not implement or benchmark Trainium or Maia.

2028+ assessment: inference latency, memory efficiency, scheduling, and cost should remain useful skills even as models and accelerators change.

## Minimum viable scope

- One compatible open-weight model, initially around 3–8B parameters.
- A model server with a documented HTTP interface.
- A gateway with request IDs, a bounded queue, timeouts, and clear errors.
- A benchmark client and machine-readable result files.
- Request latency, output length, errors, and memory measurements.

## Proposed stack and architecture

Use a compatible vLLM Spark recipe, Python/FastAPI for the first gateway, Docker, and Prometheus/Grafana when basic measurements work. Keep the benchmark client separate. Go is an optional later gateway implementation, not a prerequisite.

```text
Client / load generator -> Gateway -> Local model server -> GB10
                              |
                         Metrics + logs
```

Validate Arm, driver, CUDA, model, and container compatibility using [NVIDIA's current guide](https://build.nvidia.com/spark/vllm/over). Pin a working image and configuration. An API compatible with an existing provider's format can be served entirely locally.

## Implementation stages

1. **Hardware smoke test:** serve one model and record the exact environment. Exit when another run using your instructions succeeds.
2. **Baseline:** create a fixed prompt set with short/long inputs and a fixed output budget. Exit when results include all successful and failed requests.
3. **Gateway correctness:** implement bounded queues, timeout propagation, cancellation, and quotas. Exit when overload produces explicit rejection rather than uncontrolled growth.
4. **Serving experiments:** vary one factor at a time: concurrency, context length, compatible quantization, batching, or caching. Exit when you can explain each tradeoff.
5. **Optional router:** add a second model and compare static routing with a simple task-based policy. Use sequential loading if both models cannot fit comfortably.

## Data and measurement

Start with approximately 100 prompts, covering structured extraction, tool-call formatting, and short reasoning tasks. Keep a held-out quality set. Use one model revision and repeat identical workloads across configurations.

| Measure | Reporting requirement |
| --- | --- |
| Time to first token | Separate server/gateway and end-to-end timing where possible |
| Generation speed | Define whether measured per request or aggregate |
| p50/p95 latency | Record input/output lengths and concurrency |
| Throughput | Count successful completed requests; report errors separately |
| Peak memory | Include measurement method and warm/cold state |
| Task quality | Use deterministic validators where possible |

Warm-up, repetition count, output limits, and cache conditions must be documented. If calculating financial cost, state energy/cloud price assumptions separately from raw performance.

## Important tests

- A client cancels a streaming request; resources are eventually released.
- Queue saturation produces a defined response.
- A model server crash does not leave requests hanging indefinitely.
- Oversized inputs are rejected with a clear reason.
- Logs do not inadvertently store credentials or private prompt content.

## Definition of done

The endpoint is reproducible, the gateway survives the failure scenarios, and you publish at least one fair comparison against the original baseline. A measured result showing an optimization did not help is valid if the experiment is sound.

Deliver `README.md`, environment/configuration files, `benchmarks/`, raw and summarized results, an architecture diagram, and a demo of overload handling. Résumé template: “Built a local inference gateway; measured [actual result] under [defined workload], with bounded queueing and cancellation.”

## Keep out of the first version

Kubernetes, custom CUDA kernels, distributed inference, numerous models, and complex routing. Add these only to answer a specific measured limitation.
