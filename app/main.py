from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse

from .auth import AUTH_PATH, AttemptLimiter, clear_cookies, router
from .config import Settings, get_settings
from .fleet_api import router as fleet_router
from .fleet_store import SupabaseStore
from .supabase_auth import SupabaseAuth


def create_app(
    settings: Settings | None = None, *, transport: httpx.AsyncBaseTransport | None = None
):
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with httpx.AsyncClient(
            timeout=15.0, transport=transport, follow_redirects=False
        ) as client:
            app.state.auth = SupabaseAuth(settings, client)
            app.state.fleet = SupabaseStore(settings, client)
            yield

    app = FastAPI(
        title="DailyCar API",
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/docs" if settings.app_env != "production" else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.app_env != "production" else None,
    )
    app.state.settings = settings
    app.state.limiter = AttemptLimiter()

    @app.middleware("http")
    async def security(request: Request, call_next):
        if request.url.path.startswith("/api") and request.method in {
            "POST",
            "PUT",
            "PATCH",
            "DELETE",
        }:
            if (
                request.headers.get("origin") not in settings.allowed_origins
                or request.headers.get("x-requested-with") != "XMLHttpRequest"
            ):
                response = JSONResponse(
                    {"detail": "Request origin is not allowed."}, status_code=403
                )
            elif not request.headers.get("content-type", "").startswith("application/json"):
                response = JSONResponse({"detail": "Use application/json."}, status_code=415)
            else:
                # Authentication payloads are small. Reject excess before parsing/logging input.
                body_limit = (
                    14 * 1024 * 1024
                    if request.url.path == "/api/admin/files"
                    else 1024 * 1024
                    if request.url.path.startswith("/api/admin/import/")
                    else 16384
                    if request.url.path.startswith(AUTH_PATH)
                    else 65536
                )
                payload = bytearray()
                async for chunk in request.stream():
                    payload.extend(chunk)
                    if len(payload) > body_limit:
                        break
                if len(payload) > body_limit:
                    response = JSONResponse(
                        {"detail": "Request body is too large."}, status_code=413
                    )
                else:
                    request._body = bytes(payload)
                    response = await call_next(request)
        else:
            response = await call_next(request)
        if request.url.path.startswith("/api"):
            response.headers["Cache-Control"] = "no-store"
            response.headers["Pragma"] = "no-cache"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, error: HTTPException):
        response = JSONResponse(
            {"detail": error.detail}, status_code=error.status_code, headers=error.headers
        )
        if error.status_code in {401, 403} and request.url.path.startswith(AUTH_PATH):
            # /me may have an expired access token and still needs the refresh cookie.
            if not (request.url.path.endswith("/me") and error.status_code == 401):
                clear_cookies(response, settings)
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, _error: RequestValidationError):
        # Pydantic's default input echo can include passwords and recovery tokens.
        return JSONResponse(
            {
                "detail": "Check your email address and password fields."
                if _request.url.path.startswith(AUTH_PATH)
                else "Check the request fields and try again."
            },
            status_code=422,
        )

    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "X-Requested-With"],
    )
    app.include_router(router)
    app.include_router(fleet_router)

    @app.get("/api/health", tags=["Health"])
    async def health():
        return {"status": "ok", "auth_configured": settings.auth_configured}

    return app


app = create_app()
