"""Shared fixtures.

``DATABASE_URL`` is required config, so it is set before anything imports a module that reads
settings. The value is never connected to — nothing in T1 touches a database, by design:
``/health`` must not, and the provenance type has no storage.
"""

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")

from collections.abc import AsyncGenerator, Iterator

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.core.database import dispose_engine
from app.main import app


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> Iterator[None]:
    """Drop the cached Settings around every test, so env monkeypatching takes effect."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
async def _reset_database_state() -> AsyncGenerator[None, None]:
    """Dispose the engine/sessionmaker singletons around every test.

    `get_settings` is cache-cleared above, but `app.core.database`'s module globals had no
    equivalent. Nothing in T1 opens a connection, so this changes no behaviour today — it exists
    because from T4 a test that monkeypatches DATABASE_URL after an earlier test already built the
    engine would silently reuse the stale one, and that failure is very hard to read.
    """
    await dispose_engine()
    yield
    await dispose_engine()


@pytest.fixture
async def client() -> AsyncGenerator[AsyncClient, None]:
    """An httpx client bound to the ASGI app.

    Exceptions propagate into the test rather than coming back as responses. Use `error_client`
    when the response is the thing under test.
    """
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as async_client:
        yield async_client


@pytest.fixture
async def error_client() -> AsyncGenerator[AsyncClient, None]:
    """A client that returns error responses instead of re-raising.

    Starlette's ServerErrorMiddleware re-raises after responding, "to allow test clients to
    optionally raise the error", and ASGITransport takes it up by default. Without
    `raise_app_exceptions=False` a failing route explodes inside the test and the handler's
    response can never be asserted — so this is the only way to prove the handlers are wired in
    rather than merely defined.
    """
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as async_client:
        yield async_client
