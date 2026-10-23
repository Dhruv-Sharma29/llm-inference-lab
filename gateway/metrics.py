"""Prometheus metrics. Labels are low-cardinality on purpose: no request IDs, keys, or prompts."""

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

LATENCY_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2, 4, 8, 16, 32, 64, 128)


class Metrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self.requests = Counter("gateway_requests", "Finished requests by outcome", ["outcome"],
                                registry=self.registry)
        self.latency = Histogram("gateway_request_seconds", "Gateway end-to-end latency", ["stream"],
                                 buckets=LATENCY_BUCKETS, registry=self.registry)
        self.ttft = Histogram("gateway_ttft_seconds", "Gateway receipt to first upstream byte",
                              buckets=LATENCY_BUCKETS, registry=self.registry)
        self.inflight = Gauge("gateway_inflight", "Requests holding an upstream slot", registry=self.registry)
        self.waiting = Gauge("gateway_queue_waiting", "Requests waiting for a slot", registry=self.registry)
