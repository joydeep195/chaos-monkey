"""Builds the FastAPI application with its hardening and, optionally, the executor."""

import logging
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from chaosmonkey.api.auth import TokenVerifier
from chaosmonkey.api.middleware import (
    BodySizeLimitMiddleware,
    RateLimitMiddleware,
    SecurityHeadersMiddleware,
)
from chaosmonkey.api.routes import api, health
from chaosmonkey.audit import AuditError
from chaosmonkey.runtime import Runtime
from chaosmonkey.service import (
    ConflictError,
    ForbiddenError,
    InvalidError,
    NotFoundError,
    ServiceError,
)

log = logging.getLogger(__name__)

STATUS_BY_ERROR: dict[type[ServiceError], tuple[int, str]] = {
    NotFoundError: (404, "not_found"),
    ForbiddenError: (403, "forbidden"),
    ConflictError: (409, "conflict"),
    InvalidError: (422, "invalid"),
}
DOCS_PATHS = frozenset({"/docs", "/docs/oauth2-redirect"})


def _request_id(request: Request) -> str:
    return str(getattr(request.state, "request_id", ""))


def create_api(runtime: Runtime, verifier: TokenVerifier | None = None) -> FastAPI:
    settings = runtime.settings
    development = not settings.is_production

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        stop = threading.Event()
        thread = None
        if settings.api.run_executor:
            executor = runtime.executor()
            thread = threading.Thread(
                target=executor.run_forever, args=(stop,), name="chaosmonkey-executor", daemon=True
            )
            thread.start()
        try:
            yield
        finally:
            stop.set()
            if thread is not None:
                thread.join(timeout=settings.tick_seconds + 5)

    app = FastAPI(
        title="Chaos Monkey",
        version="0.1.0",
        summary="Chaos engineering with guard rails, approvals and a tamper-evident audit log.",
        docs_url="/docs" if development else None,
        redoc_url=None,
        openapi_url="/openapi.json" if development else None,
        lifespan=lifespan,
    )
    app.state.runtime = runtime
    app.state.verifier = verifier or TokenVerifier(settings)
    app.include_router(health)
    app.include_router(api)

    @app.exception_handler(ServiceError)
    async def service_error(request: Request, error: ServiceError) -> JSONResponse:
        code, name = STATUS_BY_ERROR.get(type(error), (400, "bad_request"))
        return JSONResponse(
            {"error": name, "message": str(error), "request_id": _request_id(request)}, code
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, error: StarletteHTTPException) -> JSONResponse:
        return JSONResponse(
            {"error": "http_error", "message": error.detail, "request_id": _request_id(request)},
            error.status_code,
            headers=error.headers,
        )

    @app.exception_handler(AuditError)
    async def audit_error(request: Request, error: AuditError) -> JSONResponse:
        log.error("Audit log refused a write: %s", error)
        return JSONResponse(
            {
                "error": "audit_unavailable",
                "message": "The audit log cannot be written, so the change was not made",
                "request_id": _request_id(request),
            },
            503,
        )

    @app.exception_handler(Exception)
    async def unexpected(request: Request, error: Exception) -> JSONResponse:
        log.exception("Unhandled error (request %s)", _request_id(request))
        return JSONResponse(
            {
                "error": "internal_error",
                "message": "Something went wrong",
                "request_id": _request_id(request),
            },
            500,
        )

    # Added innermost first: the last one added wraps all the others.
    if settings.api.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.api.cors_origins),
            allow_methods=["GET", "POST", "PUT", "DELETE"],
            allow_headers=["Authorization", "Content-Type"],
            allow_credentials=False,
        )
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.api.max_body_bytes)
    app.add_middleware(RateLimitMiddleware, per_minute=settings.api.rate_limit_per_minute)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.api.allowed_hosts))
    app.add_middleware(
        SecurityHeadersMiddleware,
        hsts=settings.is_production,
        docs_paths=DOCS_PATHS if development else frozenset(),
    )
    return app
