"""FastAPI application: lifespan, middleware, centralized error handling, health.

Feature routers mount here as slices land. T2 mounts the first — the read-only manifests router.
The weekly-run trigger endpoint is T10.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.core.config import get_settings
from app.core.database import dispose_engine
from app.core.dependencies import SettingsDep
from app.core.exceptions import LocalProspectEngineError
from app.core.logging import get_logger, get_request_id, setup_logging
from app.core.middleware import RequestContextMiddleware
from app.manifests.routes import router as manifests_router

logger = get_logger(__name__)


class HealthResponse(BaseModel):
    """The liveness payload."""

    status: str
    version: str
    environment: str


class ErrorResponse(BaseModel):
    """The one shape every error takes, whatever raised it."""

    error: str
    detail: str
    code: str


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Configure logging on startup; close database connections on shutdown."""
    setup_logging()
    settings = get_settings()
    logger.info(
        "core.app.startup_succeeded",
        app_name=settings.app_name,
        version=settings.version,
        environment=settings.environment,
    )
    yield
    await dispose_engine()
    logger.info("core.app.shutdown_succeeded")


app = FastAPI(
    title="Local Prospect Engine",
    version=get_settings().version,
    lifespan=lifespan,
)
app.add_middleware(RequestContextMiddleware)
app.include_router(manifests_router)


def _error_response(status_code: int, payload: ErrorResponse) -> JSONResponse:
    """Render an error, carrying the correlation id back to the client.

    The id has to be attached here rather than in the middleware: when a route raises, the response
    is produced by Starlette's ServerErrorMiddleware, which sits *outside* RequestContextMiddleware,
    so the middleware's own header assignment never runs. A failed request is precisely when
    someone needs the id to find the matching log line.
    """
    return JSONResponse(
        status_code=status_code,
        content=payload.model_dump(),
        headers={"X-Request-ID": get_request_id()},
    )


@app.exception_handler(LocalProspectEngineError)
async def handle_known_error(request: Request, exc: LocalProspectEngineError) -> JSONResponse:
    """Render a deliberate error as structured JSON — never a stack trace to the client."""
    logger.warning(
        "core.app.request_rejected",
        path=request.url.path,
        code=exc.code,
        error=exc.message,
    )
    return _error_response(
        exc.status_code,
        ErrorResponse(error=type(exc).__name__, detail=exc.message, code=exc.code),
    )


@app.exception_handler(RequestValidationError)
async def handle_validation_error(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    """Return field-level detail for invalid input, at 422."""
    logger.info("core.app.request_invalid", path=request.url.path, errors=exc.errors())
    return _error_response(
        status.HTTP_422_UNPROCESSABLE_ENTITY,
        ErrorResponse(
            error="RequestValidationError",
            detail="request payload failed validation",
            code="validation_error",
        ),
    )


@app.exception_handler(Exception)
async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    """Log the full exception server-side; return a safe message to the client."""
    logger.error(
        "core.app.request_errored",
        path=request.url.path,
        error=str(exc),
        exc_info=True,
    )
    return _error_response(
        status.HTTP_500_INTERNAL_SERVER_ERROR,
        ErrorResponse(
            error="InternalServerError",
            detail="an unexpected error occurred",
            code="internal_error",
        ),
    )


@app.get("/health", response_model=HealthResponse)
async def health(settings: SettingsDep) -> HealthResponse:
    """Liveness only.

    Deliberately does **not** touch the database: a health check that fails when Supabase blips is
    a readiness check wearing a liveness check's name. A database probe belongs at
    ``/health/ready`` if one is ever wanted.
    """
    return HealthResponse(
        status="ok",
        version=settings.version,
        environment=settings.environment,
    )
