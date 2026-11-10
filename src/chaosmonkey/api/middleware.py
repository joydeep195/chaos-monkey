"""HTTP hardening: security headers, request IDs, body size limit and rate limiting.

Written as plain ASGI middleware so they wrap every response, including errors.
"""

import json
import time
import uuid
from collections.abc import Callable, Mapping

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# The API returns JSON only, so the policy forbids everything a browser could load.
API_CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
# The interactive docs (development only) load Swagger UI from jsDelivr.
DOCS_CSP = (
    "default-src 'none'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'self' https://cdn.jsdelivr.net; img-src 'self' data: https://fastapi.tiangolo.com; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
)
EXEMPT_FROM_RATE_LIMIT = frozenset({"/healthz", "/readyz"})


async def send_json(
    send: Send, status: int, body: Mapping[str, object], extra: dict[str, str]
) -> None:
    payload = json.dumps(body).encode()
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(payload)).encode()),
    ]
    headers += [(k.encode(), v.encode()) for k, v in extra.items()]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": payload})


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp, *, hsts: bool, docs_paths: frozenset[str]) -> None:
        self.app = app
        self.hsts = hsts
        self.docs_paths = docs_paths

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        csp = DOCS_CSP if scope["path"] in self.docs_paths else API_CSP

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["X-Request-ID"] = request_id
                headers["Content-Security-Policy"] = csp
                headers["X-Content-Type-Options"] = "nosniff"
                headers["X-Frame-Options"] = "DENY"
                headers["Referrer-Policy"] = "no-referrer"
                headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
                headers["Cross-Origin-Opener-Policy"] = "same-origin"
                headers["Cross-Origin-Resource-Policy"] = "same-origin"
                headers["Cache-Control"] = "no-store"
                if self.hsts:
                    headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
            await send(message)

        await self.app(scope, receive, send_with_headers)


class _BodyTooLargeError(Exception):
    pass


class BodySizeLimitMiddleware:
    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = Headers(scope=scope).get("content-length")
        if declared is not None and (not declared.isdigit() or int(declared) > self.max_bytes):
            await self._refuse(send)
            return

        received = 0
        started = False

        async def counting_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise _BodyTooLargeError
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, counting_receive, tracking_send)
        except _BodyTooLargeError:
            if not started:
                await self._refuse(send)

    async def _refuse(self, send: Send) -> None:
        body = {"error": "too_large", "message": f"Request body over {self.max_bytes} bytes"}
        await send_json(send, 413, body, {})


class RateLimitMiddleware:
    """Fixed one-minute window per client address. Per replica; add one at the gateway too."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        per_minute: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.app = app
        self.per_minute = per_minute
        self.clock = clock
        self.windows: dict[str, tuple[int, int]] = {}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in EXEMPT_FROM_RATE_LIMIT:
            await self.app(scope, receive, send)
            return
        client = scope.get("client")
        key = client[0] if client else "unknown"
        now = self.clock()
        window = int(now // 60)
        current_window, count = self.windows.get(key, (window, 0))
        count = count + 1 if current_window == window else 1
        self.windows[key] = (window, count)
        if len(self.windows) > 10_000:
            self.windows = {k: v for k, v in self.windows.items() if v[0] == window}
        if count > self.per_minute:
            retry = str(max(1, 60 - int(now % 60)))
            body = {"error": "rate_limited", "message": "Too many requests; slow down"}
            await send_json(send, 429, body, {"retry-after": retry})
            return
        await self.app(scope, receive, send)
