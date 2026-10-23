"""One error type for every rejection the gateway makes, rendered in an OpenAI-style body."""

from __future__ import annotations

from fastapi.responses import JSONResponse


class GatewayError(Exception):
    def __init__(self, status: int, code: str, message: str, retry_after_s: float | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.retry_after_s = retry_after_s

    def response(self, request_id: str) -> JSONResponse:
        headers = {"X-Request-ID": request_id}
        if self.retry_after_s is not None:
            headers["Retry-After"] = str(max(1, round(self.retry_after_s)))
        body = {"error": {"type": self.code, "message": self.message, "request_id": request_id}}
        return JSONResponse(body, status_code=self.status, headers=headers)
