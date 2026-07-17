from __future__ import annotations

import json
from typing import Any, Awaitable, Callable


AsgiScope = dict[str, Any]
AsgiReceive = Callable[[], Awaitable[dict[str, Any]]]
AsgiSend = Callable[[dict[str, Any]], Awaitable[None]]
AsgiApp = Callable[[AsgiScope, AsgiReceive, AsgiSend], Awaitable[None]]


class RequestBodyTooLarge(RuntimeError):
    """Raised internally when a streamed JSON request crosses its byte budget."""


class JsonRequestBodyLimitMiddleware:
    """Bound JSON request bytes before Starlette decodes the request body.

    Multipart document uploads are intentionally outside this middleware and
    remain governed by FileStore's streaming upload limits. Both declared
    Content-Length and chunked/streamed bodies are enforced.
    """

    def __init__(self, app: AsgiApp, *, max_body_bytes: int) -> None:
        self.app = app
        self.max_body_bytes = int(max_body_bytes)
        if self.max_body_bytes <= 0:
            raise ValueError("max_body_bytes must be a positive integer.")

    async def __call__(self, scope: AsgiScope, receive: AsgiReceive, send: AsgiSend) -> None:
        if scope.get("type") != "http" or not _is_json_content_type(scope):
            await self.app(scope, receive, send)
            return

        declared_length = _content_length(scope)
        if declared_length is not None and declared_length > self.max_body_bytes:
            await _send_too_large(scope, receive, send, self.max_body_bytes)
            return

        consumed = 0
        response_started = False

        async def limited_receive() -> dict[str, Any]:
            nonlocal consumed
            message = await receive()
            if message.get("type") == "http.request":
                consumed += len(message.get("body") or b"")
                if consumed > self.max_body_bytes:
                    raise RequestBodyTooLarge
            return message

        async def tracked_send(message: dict[str, Any]) -> None:
            nonlocal response_started
            if message.get("type") == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracked_send)
        except RequestBodyTooLarge:
            if response_started:
                raise
            await _send_too_large(scope, receive, send, self.max_body_bytes)


def _is_json_content_type(scope: dict[str, Any]) -> bool:
    content_type = _header_value(scope, b"content-type").split(";", 1)[0].strip().lower()
    return content_type == "application/json" or content_type.endswith("+json")


def _content_length(scope: dict[str, Any]) -> int | None:
    raw = _header_value(scope, b"content-length").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value >= 0 else None


def _header_value(scope: dict[str, Any], name: bytes) -> str:
    values = [
        value.decode("latin-1")
        for key, value in scope.get("headers") or []
        if bytes(key).lower() == name
    ]
    return values[-1] if values else ""


async def _send_too_large(scope: dict[str, Any], receive, send, max_body_bytes: int) -> None:
    del receive
    body = json.dumps(
        {"detail": f"JSON request body exceeds the {max_body_bytes}-byte limit."},
        separators=(",", ":"),
    ).encode("utf-8")
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode("ascii")),
    ]
    await send({"type": "http.response.start", "status": 413, "headers": headers})
    await send({"type": "http.response.body", "body": body})
