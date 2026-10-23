| label | concurrency | requests | ok | errors | e2e_p50_ms | e2e_p95_ms | ttft_p50_ms | ttft_p95_ms | decode_tok_s_p50 | agg_out_tok_s | ok_req_s | quality_of_ok | mem_peak_gib |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| smoke-llamacpp-sharedgpu | 1 | 20 | 20 | 0 | 10327.8 | 10715.1 | 221.3 | 245.2 | 5.7 | 5.6 | 0.1 | 0.95 | 99.36 |
| smoke-llamacpp-sharedgpu | 4 | 20 | 20 | 0 | 11026.2 | 14401.9 | 670.8 | 4286.7 | 5.6 | 19.2 | 0.32 | 0.95 | 99.36 |
