"""Async database access over hosted Supabase.

Two things here diverge from the vertical-slice reference's example on purpose:

* **Async, not sync.** The reference shows ``create_engine`` / ``Session``, which would block the
  event loop inside every async handler. This uses ``create_async_engine`` / ``AsyncSession``.
* **``DeclarativeBase``, not ``declarative_base()``.** The legacy factory returns an untyped base
  and Pyright strict fails on every model that inherits it — with no suppression available.

The engine is built lazily rather than at import time, so importing this module does not require a
reachable ``DATABASE_URL``. Tests and the ``/health`` endpoint must not need a database.
"""

from collections.abc import AsyncGenerator
from urllib.parse import urlsplit

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


class Base(DeclarativeBase):
    """Declarative base for every ORM model in the service."""


def _safe_host(database_url: str) -> str:
    """Return just the host of a database URL, so credentials never reach a log line."""
    return urlsplit(database_url).hostname or "unknown"


def get_engine() -> AsyncEngine:
    """Return the process-wide async engine, creating it on first use."""
    global _engine
    if _engine is None:
        settings = get_settings()
        logger.info("core.database.engine_created", url_host=_safe_host(settings.database_url))
        _engine = create_async_engine(
            settings.database_url,
            pool_pre_ping=True,
            echo=settings.debug,
        )
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """Return the process-wide session factory, creating it on first use.

    ``expire_on_commit=False`` so objects stay usable after the session commits — otherwise every
    attribute access after a commit triggers a lazy refresh, which in async code is an error.
    """
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _sessionmaker


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding a session that is always closed."""
    async with get_sessionmaker()() as session:
        yield session


def is_initialised() -> bool:
    """Whether the lazily-built engine or session factory currently exists.

    Exposed so tests can assert isolation without reaching into module privates — the autouse
    reset fixture in `tests/conftest.py` is only trustworthy if something can observe it working.
    """
    return _engine is not None or _sessionmaker is not None


async def dispose_engine() -> None:
    """Close every pooled connection. Called from the application lifespan on shutdown."""
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
        logger.info("core.database.engine_disposed")
        _engine = None
    _sessionmaker = None
