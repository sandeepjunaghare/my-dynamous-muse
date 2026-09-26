"""Structured logging: JSON out, correlation id in, exceptions formatted rather than lost.

Event names follow ``domain.component.action_state`` — four segments of meaning in three dots, per
CLAUDE.md::

    logger.info("core.database.connect_started", url_host=host)
    logger.info("core.database.connect_succeeded")
    logger.error("core.database.connect_failed", error=str(exc), exc_info=True)

Every entry carries the current ``request_id``, so one request can be traced across slices.
"""

import logging
import uuid
from contextvars import ContextVar

import structlog
from structlog.types import EventDict, FilteringBoundLogger, Processor, WrappedLogger

from app.core.config import get_settings

request_id_var: ContextVar[str] = ContextVar("request_id", default="")


def get_request_id() -> str:
    """Return the current request's correlation id, or an empty string outside a request."""
    return request_id_var.get()


def set_request_id(request_id: str | None = None) -> str:
    """Set the correlation id for this context, generating one when the caller has none."""
    resolved = request_id or str(uuid.uuid4())
    request_id_var.set(resolved)
    return resolved


def add_request_id(
    logger: WrappedLogger,
    method_name: str,
    event_dict: EventDict,
) -> EventDict:
    """structlog processor: stamp every entry with the current correlation id."""
    request_id = get_request_id()
    if request_id:
        event_dict["request_id"] = request_id
    return event_dict


def _resolve_level(log_level: str) -> int:
    """Map a configured level name onto its numeric value, defaulting to INFO.

    ``logging.getLevelName`` round-trips names to numbers but is typed as returning ``Any`` and
    returns a string for an unknown name — both unhelpful here.
    """
    return logging.getLevelNamesMapping().get(log_level.upper(), logging.INFO)


def setup_logging() -> None:
    """Configure structlog for the process. Called once, from the application lifespan."""
    settings = get_settings()

    processors: list[Processor] = [
        add_request_id,
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        # Renders `exc_info=True` into a traceback string inside the JSON, rather than dropping it.
        structlog.processors.format_exc_info,
        structlog.processors.JSONRenderer(),
    ]

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(_resolve_level(settings.log_level)),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> FilteringBoundLogger:
    """Return a logger for a module.

    The reference example types this ``-> Any``, which this project forbids outright.
    """
    logger: FilteringBoundLogger = structlog.get_logger(name)
    return logger
