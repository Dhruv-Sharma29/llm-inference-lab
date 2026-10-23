"""Process-level fixtures: real mock-upstream and gateway servers on loopback ports.

Running real processes (not in-process ASGI transports) matters here: cancellation, disconnects,
and upstream crashes only behave realistically over actual TCP connections.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_until(predicate, timeout_s: float = 5.0, interval_s: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if predicate():
                return True
        except httpx.HTTPError:
            pass
        time.sleep(interval_s)
    return False


@dataclass
class Server:
    url: str
    proc: subprocess.Popen
    log_path: Path

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGKILL)
            self.proc.wait(5)

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.kill()

    def log_text(self) -> str:
        return self.log_path.read_text(errors="replace")


def _spawn(args: list[str], env: dict[str, str], log_path: Path, health_url: str) -> Server:
    log_file = open(log_path, "w")
    proc = subprocess.Popen(args, cwd=ROOT, env={**os.environ, **env}, stdout=log_file, stderr=subprocess.STDOUT)
    url = health_url.rsplit("/", 1)[0]
    if not wait_until(lambda: httpx.get(health_url, timeout=1).status_code == 200, timeout_s=15):
        proc.kill()
        raise RuntimeError(f"server failed to start: {args}\n{log_path.read_text()}")
    return Server(url, proc, log_path)


@pytest.fixture
def mock_upstream(tmp_path):
    port = free_port()
    server = _spawn([sys.executable, "-m", "mock_upstream.server", "--port", str(port)], {},
                    tmp_path / "mock.log", f"http://127.0.0.1:{port}/health")
    yield server
    server.stop()


@pytest.fixture
def start_gateway(tmp_path, mock_upstream):
    started: list[Server] = []

    def _start(**settings) -> Server:
        port = free_port()
        env = {"GATEWAY_UPSTREAM_URL": mock_upstream.url}
        env.update({f"GATEWAY_{k}": str(v) for k, v in settings.items()})
        server = _spawn(
            [sys.executable, "-m", "uvicorn", "--factory", "gateway.app:create_app_from_env",
             "--host", "127.0.0.1", "--port", str(port)],
            env, tmp_path / f"gateway-{port}.log", f"http://127.0.0.1:{port}/healthz")
        started.append(server)
        return server

    yield _start
    for server in started:
        server.stop()
