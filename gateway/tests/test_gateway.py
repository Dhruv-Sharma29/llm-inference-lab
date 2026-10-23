"""Gateway correctness tests (PLAN.md M3). Each maps to a failure scenario in SPEC.md."""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest

from .conftest import wait_until

PATH = "/v1/chat/completions"


def body(content: str = "hello", **kw) -> dict:
    return {"model": "mock", "messages": [{"role": "user", "content": content}], "max_tokens": 8, **kw}


def health(gw) -> dict:
    return httpx.get(gw.url + "/healthz").json()


def mock_stats(mock) -> dict:
    return httpx.get(mock.url + "/stats").json()


# --- basic behaviour -------------------------------------------------------------------------

def test_non_stream_ok_and_request_id_echoed(start_gateway):
    gw = start_gateway()
    r = httpx.post(gw.url + PATH, json=body(), headers={"X-Request-ID": "abc-123"})
    assert r.status_code == 200
    assert r.headers["x-request-id"] == "abc-123"
    assert r.json()["usage"]["completion_tokens"] == 8


def test_unsafe_request_id_is_replaced(start_gateway):
    gw = start_gateway()
    r = httpx.post(gw.url + PATH, json=body(), headers={"X-Request-ID": "bad id; " + "x" * 80})
    assert r.status_code == 200
    assert len(r.headers["x-request-id"]) == 32


def test_stream_ok(start_gateway):
    gw = start_gateway()
    with httpx.stream("POST", gw.url + PATH, json=body(stream=True)) as r:
        assert r.status_code == 200
        lines = [line for line in r.iter_lines() if line]
    assert lines[-1] == "data: [DONE]"


# --- cancellation ----------------------------------------------------------------------------

def test_client_cancel_mid_stream_releases_upstream_and_slot(start_gateway, mock_upstream):
    gw = start_gateway(MAX_INFLIGHT=2)
    slow = body(stream=True, max_tokens=500, mock={"token_delay_s": 0.05})
    with httpx.stream("POST", gw.url + PATH, json=slow) as r:
        it = r.iter_lines()
        for _ in range(6):
            next(it)
        assert mock_stats(mock_upstream)["active"] == 1
    # Leaving the context manager closes the client connection mid-stream.
    assert wait_until(lambda: mock_stats(mock_upstream)["active"] == 0, timeout_s=3)
    assert mock_stats(mock_upstream)["aborted"] == 1
    assert wait_until(lambda: health(gw)["inflight"] == 0, timeout_s=3)


def test_client_cancel_while_upstream_is_silent(start_gateway, mock_upstream):
    """Disconnect must be noticed even when no tokens are flowing (nothing to fail a send on)."""
    gw = start_gateway()
    silent = body(stream=True, mock={"ttft_s": 30})
    with pytest.raises(httpx.ReadTimeout):
        with httpx.stream("POST", gw.url + PATH, json=silent, timeout=httpx.Timeout(5, read=0.5)) as r:
            for _ in r.iter_lines():
                pass
    assert wait_until(lambda: mock_stats(mock_upstream)["active"] == 0, timeout_s=3)
    assert wait_until(lambda: health(gw)["inflight"] == 0, timeout_s=3)


def test_client_leaving_the_queue_never_reaches_upstream(start_gateway, mock_upstream):
    gw = start_gateway(MAX_INFLIGHT=1, MAX_QUEUE=4, QUEUE_TIMEOUT_S=30)

    async def scenario():
        async with httpx.AsyncClient(timeout=30) as client:
            holder = asyncio.create_task(client.post(gw.url + PATH, json=body(mock={"ttft_s": 2})))
            await asyncio.sleep(0.3)
            with pytest.raises(httpx.ReadTimeout):
                await client.post(gw.url + PATH, json=body(), timeout=httpx.Timeout(5, read=0.5))
            return await holder

    assert asyncio.run(scenario()).status_code == 200
    time.sleep(0.3)
    assert mock_stats(mock_upstream)["started"] == 1  # the abandoned request was never forwarded
    assert health(gw) == {"status": "ok", "inflight": 0, "waiting": 0}


# --- overload --------------------------------------------------------------------------------

def test_queue_saturation_is_rejected_explicitly(start_gateway):
    gw = start_gateway(MAX_INFLIGHT=2, MAX_QUEUE=2, QUEUE_TIMEOUT_S=30)

    async def scenario():
        async with httpx.AsyncClient(timeout=30) as client:
            held = [asyncio.create_task(client.post(gw.url + PATH, json=body(mock={"ttft_s": 2})))
                    for _ in range(4)]
            for _ in range(50):
                if (await client.get(gw.url + "/healthz")).json() == {"status": "ok", "inflight": 2, "waiting": 2}:
                    break
                await asyncio.sleep(0.05)
            t0 = time.monotonic()
            rejected = await client.post(gw.url + PATH, json=body())
            elapsed = time.monotonic() - t0
            return rejected, elapsed, await asyncio.gather(*held)

    rejected, elapsed, held = asyncio.run(scenario())
    assert rejected.status_code == 429
    assert rejected.json()["error"]["type"] == "queue_full"
    assert "retry-after" in rejected.headers
    assert elapsed < 0.5  # rejected immediately, not after waiting
    assert [r.status_code for r in held] == [200, 200, 200, 200]


def test_queue_wait_is_bounded_by_timeout(start_gateway):
    gw = start_gateway(MAX_INFLIGHT=1, MAX_QUEUE=4, QUEUE_TIMEOUT_S=0.5)

    async def scenario():
        async with httpx.AsyncClient(timeout=30) as client:
            holder = asyncio.create_task(client.post(gw.url + PATH, json=body(mock={"ttft_s": 2})))
            await asyncio.sleep(0.2)
            t0 = time.monotonic()
            r = await client.post(gw.url + PATH, json=body())
            elapsed = time.monotonic() - t0
            await holder
            return r, elapsed

    r, elapsed = asyncio.run(scenario())
    assert r.status_code == 503
    assert r.json()["error"]["type"] == "queue_timeout"
    assert 0.4 < elapsed < 1.5


# --- upstream failure ------------------------------------------------------------------------

def test_upstream_crash_mid_stream_terminates_requests(start_gateway, mock_upstream):
    gw = start_gateway(REQUEST_TIMEOUT_S=60)
    slow = body(stream=True, max_tokens=500, mock={"token_delay_s": 0.05})
    t0 = time.monotonic()
    with httpx.stream("POST", gw.url + PATH, json=slow, timeout=10) as r:
        it = r.iter_lines()
        for _ in range(4):
            next(it)
        mock_upstream.kill()
        rest = [line for line in it if line]
    assert time.monotonic() - t0 < 5  # ended promptly, far below the 60 s deadline
    assert "upstream_error" in rest[-1]
    assert wait_until(lambda: health(gw)["inflight"] == 0, timeout_s=3)

    t0 = time.monotonic()
    r = httpx.post(gw.url + PATH, json=body(), timeout=10)
    assert r.status_code == 502
    assert r.json()["error"]["type"] == "upstream_unavailable"
    assert time.monotonic() - t0 < 3


def test_hung_upstream_hits_deadline_non_stream(start_gateway):
    gw = start_gateway(REQUEST_TIMEOUT_S=1.0)
    t0 = time.monotonic()
    r = httpx.post(gw.url + PATH, json=body(mock={"hang": True}), timeout=10)
    assert r.status_code == 504
    assert time.monotonic() - t0 < 2.5
    assert health(gw)["inflight"] == 0


def test_hung_upstream_hits_deadline_stream(start_gateway, mock_upstream):
    gw = start_gateway(REQUEST_TIMEOUT_S=1.0)
    t0 = time.monotonic()
    with httpx.stream("POST", gw.url + PATH, json=body(stream=True, mock={"hang": True}), timeout=10) as r:
        lines = [line for line in r.iter_lines() if line]
    assert time.monotonic() - t0 < 2.5
    assert '"timeout"' in lines[-1]
    assert wait_until(lambda: mock_stats(mock_upstream)["active"] == 0, timeout_s=3)


def test_upstream_4xx_is_passed_through(start_gateway):
    gw = start_gateway()
    r = httpx.post(gw.url + PATH, json=body(mock={"status": 400}))
    assert r.status_code == 400


def test_upstream_5xx_becomes_502(start_gateway):
    gw = start_gateway()
    r = httpx.post(gw.url + PATH, json=body(mock={"status": 500}))
    assert r.status_code == 502
    assert r.json()["error"]["type"] == "upstream_error"


# --- input validation ------------------------------------------------------------------------

def test_oversized_input_rejected_before_upstream(start_gateway, mock_upstream):
    gw = start_gateway(MAX_INPUT_TOKENS=100)
    r = httpx.post(gw.url + PATH, json=body("x" * 2000))
    assert r.status_code == 413
    assert r.json()["error"]["type"] == "input_too_large"
    assert "limit is 100" in r.json()["error"]["message"]
    assert mock_stats(mock_upstream)["started"] == 0


def test_oversized_body_rejected(start_gateway):
    gw = start_gateway(MAX_BODY_BYTES=500)
    r = httpx.post(gw.url + PATH, json=body("y" * 1000))
    assert r.status_code == 413
    assert r.json()["error"]["type"] == "body_too_large"


@pytest.mark.parametrize("max_tokens", [0, 5000, "10"])
def test_invalid_max_tokens(start_gateway, max_tokens):
    gw = start_gateway(MAX_OUTPUT_TOKENS=1024)
    r = httpx.post(gw.url + PATH, json=body(max_tokens=max_tokens))
    assert r.status_code == 400
    assert r.json()["error"]["type"] == "invalid_max_tokens"


@pytest.mark.parametrize("payload", [b"not json", b"[1,2]", b'{"messages": []}'])
def test_malformed_requests(start_gateway, payload):
    gw = start_gateway()
    r = httpx.post(gw.url + PATH, content=payload, headers={"content-type": "application/json"})
    assert r.status_code == 400


# --- auth, quota, logging --------------------------------------------------------------------

def test_auth_required_when_keys_configured(start_gateway):
    gw = start_gateway(API_KEYS="sk-a,sk-b")
    assert httpx.post(gw.url + PATH, json=body()).status_code == 401
    assert httpx.post(gw.url + PATH, json=body(), headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert httpx.post(gw.url + PATH, json=body(), headers={"Authorization": "Bearer sk-b"}).status_code == 200


def test_per_key_quota(start_gateway):
    gw = start_gateway(API_KEYS="sk-a,sk-b", QUOTA_RPS=0.1, QUOTA_BURST=2)
    a, b = {"Authorization": "Bearer sk-a"}, {"Authorization": "Bearer sk-b"}
    codes = [httpx.post(gw.url + PATH, json=body(), headers=a).status_code for _ in range(3)]
    assert codes == [200, 200, 429]
    assert httpx.post(gw.url + PATH, json=body(), headers=b).status_code == 200  # other key unaffected


def test_logs_contain_no_prompts_outputs_or_credentials(start_gateway):
    secret, sentinel = "sk-test-SECRET-9f2", "PROMPT-SENTINEL-77"
    gw = start_gateway(API_KEYS=secret, MAX_INPUT_TOKENS=50)
    auth = {"Authorization": f"Bearer {secret}"}
    httpx.post(gw.url + PATH, json=body(sentinel), headers=auth)
    with httpx.stream("POST", gw.url + PATH, json=body(sentinel, stream=True), headers=auth) as r:
        r.read()
    httpx.post(gw.url + PATH, json=body(sentinel * 50), headers=auth)  # rejected: too large
    httpx.post(gw.url + PATH, json=body(sentinel), headers={"Authorization": "Bearer " + sentinel})  # 401
    gw.stop()

    text = gw.log_text()
    assert secret not in text and sentinel not in text
    assert "tok0" not in text  # model output never logged
    records = [json.loads(line) for line in text.splitlines() if line.startswith("{")]
    assert [r["status"] for r in records] == [200, 200, 413, 401]
    assert all(r["request_id"] for r in records)
