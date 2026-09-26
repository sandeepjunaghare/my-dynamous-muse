"""Request middleware: correlation id in, request lifecycle logged."""

import time

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.core.logging import get_logger, get_request_id, set_request_id

logger = get_logger(__name__)


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assign every request a correlation id, log its lifecycle, echo the id back.

    The id is taken from an inbound ``X-Request-ID`` when the client supplies one, so a correlation
    id survives across services, and generated otherwise.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # Set before anything else runs, or the first log lines of the request lose their id.
        set_request_id(request.headers.get("X-Request-ID"))

        started = time.perf_counter()
        logger.info(
            "core.request.handling_started",
            method=request.method,
            path=request.url.path,
            client_host=request.client.host if request.client else None,
        )

        try:
            response = await call_next(request)
        except Exception as exc:
            logger.error(
                "core.request.handling_failed",
                method=request.method,
                path=request.url.path,
                error=str(exc),
                duration_seconds=round(time.perf_counter() - started, 3),
                exc_info=True,
            )
            raise

        logger.info(
            "core.request.handling_succeeded",
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            duration_seconds=round(time.perf_counter() - started, 3),
        )
        response.headers["X-Request-ID"] = get_request_id()
        return response
