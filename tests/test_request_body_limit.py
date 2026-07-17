from __future__ import annotations

import asyncio
import json
import unittest
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.request_body_limit import JsonRequestBodyLimitMiddleware


def _scope(*, content_type: str, content_length: int | None = None) -> dict[str, Any]:
    headers = [(b"content-type", content_type.encode("ascii"))]
    if content_length is not None:
        headers.append((b"content-length", str(content_length).encode("ascii")))
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/test",
        "raw_path": b"/test",
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 1234),
        "server": ("test", 80),
    }


async def _invoke(
    *,
    content_type: str,
    messages: list[dict[str, Any]],
    max_body_bytes: int,
    content_length: int | None = None,
) -> tuple[list[dict[str, Any]], int]:
    received = 0
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        nonlocal received
        received += 1
        if messages:
            return messages.pop(0)
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    async def downstream(scope, receive, send) -> None:
        total = 0
        while True:
            message = await receive()
            if message.get("type") != "http.request":
                break
            total += len(message.get("body") or b"")
            if not message.get("more_body", False):
                break
        body = json.dumps({"received": total}).encode("utf-8")
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": body})

    middleware = JsonRequestBodyLimitMiddleware(downstream, max_body_bytes=max_body_bytes)
    await middleware(
        _scope(content_type=content_type, content_length=content_length),
        receive,
        send,
    )
    return sent, received


class JsonRequestBodyLimitMiddlewareTests(unittest.TestCase):
    def test_fastapi_integration_returns_413_before_model_parsing(self) -> None:
        api = FastAPI()
        api.add_middleware(JsonRequestBodyLimitMiddleware, max_body_bytes=16)

        @api.post("/payload")
        def payload(value: dict[str, Any]) -> dict[str, Any]:
            return value

        response = TestClient(api).post("/payload", json={"value": "x" * 100})

        self.assertEqual(413, response.status_code)
        self.assertIn("16-byte limit", response.json()["detail"])

    def test_declared_oversized_json_is_rejected_without_reading_body(self) -> None:
        sent, receive_count = asyncio.run(
            _invoke(
                content_type="application/json; charset=utf-8",
                content_length=101,
                messages=[{"type": "http.request", "body": b"{}", "more_body": False}],
                max_body_bytes=100,
            )
        )

        self.assertEqual(413, sent[0]["status"])
        self.assertEqual(0, receive_count)

    def test_chunked_json_is_rejected_when_cumulative_bytes_cross_limit(self) -> None:
        sent, receive_count = asyncio.run(
            _invoke(
                content_type="application/merge-patch+json",
                messages=[
                    {"type": "http.request", "body": b"123456", "more_body": True},
                    {"type": "http.request", "body": b"78901", "more_body": False},
                ],
                max_body_bytes=10,
            )
        )

        self.assertEqual(413, sent[0]["status"])
        self.assertEqual(2, receive_count)

    def test_declared_length_cannot_hide_larger_streamed_json(self) -> None:
        sent, _ = asyncio.run(
            _invoke(
                content_type="application/json",
                content_length=2,
                messages=[{"type": "http.request", "body": b"123456", "more_body": False}],
                max_body_bytes=5,
            )
        )

        self.assertEqual(413, sent[0]["status"])

    def test_multipart_upload_bypasses_json_limit(self) -> None:
        sent, _ = asyncio.run(
            _invoke(
                content_type="multipart/form-data; boundary=test",
                content_length=1000,
                messages=[{"type": "http.request", "body": b"x" * 1000, "more_body": False}],
                max_body_bytes=10,
            )
        )

        self.assertEqual(200, sent[0]["status"])


if __name__ == "__main__":
    unittest.main()
