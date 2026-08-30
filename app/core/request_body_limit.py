from __future__ import annotations

import json
import inspect
from typing import Any, Awaitable, Callable


AsgiScope = dict[str, Any]
AsgiReceive = Callable[[], Awaitable[dict[str, Any]]]
AsgiSend = Callable[[dict[str, Any]], Awaitable[None]]
AsgiApp = Callable[[AsgiScope, AsgiReceive, AsgiSend], Awaitable[None]]
RejectionObserver = Callable[[AsgiScope, int], Awaitable[None] | None]


class RequestBodyTooLarge(RuntimeError):
    """Raised internally when a streamed JSON request crosses its byte budget."""


class JsonRequestBodyLimitMiddleware:
    """Bound request bytes before Starlette decodes the request body.

    Every request is capped except the one streamed document-upload endpoint,
    which is intentionally governed by FileStore's streaming upload limits.
    Gating on the JSON Content-Type would let a forged non-JSON header (e.g.
    ``text/plain``) skip the cap while the framework still buffers the whole
    body for a body-model endpoint. Both declared Content-Length and
    chunked/streamed bodies are enforced.
    """

    def __init__(
        self,
        app: AsgiApp,
        *,
        max_body_bytes: int,
        rejection_observer: RejectionObserver | None = None,
    ) -> None:
        self.app = app
        self.max_body_bytes = int(max_body_bytes)
        self.rejection_observer = rejection_observer
        if self.max_body_bytes <= 0:
            raise ValueError("max_body_bytes must be a positive integer.")

    async def __call__(self, scope: AsgiScope, receive: AsgiReceive, send: AsgiSend) -> None:
        if scope.get("type") != "http" or _is_streamed_document_upload(scope):
            await self.app(scope, receive, send)
            return

        declared_length = _content_length(scope)
        if declared_length is not None and declared_length > self.max_body_bytes:
            await _send_too_large(
                scope,
                receive,
                send,
                self.max_body_bytes,
                rejection_observer=self.rejection_observer,
            )
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
            await _send_too_large(
                scope,
                receive,
                send,
                self.max_body_bytes,
                rejection_observer=self.rejection_observer,
            )


def _is_streamed_document_upload(scope: dict[str, Any]) -> bool:
    content_type = _header_value(scope, b"content-type").split(";", 1)[0].strip().lower()
    return (
        content_type == "multipart/form-data"
        and str(scope.get("method") or "").upper() == "POST"
        and str(scope.get("path") or "") == "/api/documents"
    )


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


async def _send_too_large(
    scope: dict[str, Any],
    receive,
    send,
    max_body_bytes: int,
    *,
    rejection_observer: RejectionObserver | None = None,
) -> None:
    del receive
    if rejection_observer is not None:
        try:
            observed = rejection_observer(scope, 413)
            if inspect.isawaitable(observed):
                await observed
        except Exception:  # noqa: S110 - optional audit must not weaken 413 denial
            # The byte limit remains fail-closed if optional audit storage fails.
            pass
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
