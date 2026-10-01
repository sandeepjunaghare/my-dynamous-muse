"""Shared fixtures.

``DATABASE_URL`` is required config, so it is set before anything imports a module that reads
settings. The value is never connected to — nothing in T1 touches a database, by design:
``/health`` must not, and the provenance type has no storage.

The assignment is unconditional, **not** ``setdefault``. A developer with a real hosted URL
exported in their shell — which is now every worktree, since the worktree setup copies ``.env`` —
would otherwise have that URL flow into the test process, and the first test that opens a
connection would run against real Supabase. Nothing here opens one today; T4 is where it starts,
and the cost of learning this then is a polluted sourcing table. A test that needs a live database
gets a fixture that names the database it wants, rather than inheriting one by accident.

Tests that care about the value ``Settings`` sees monkeypatch it per-test, which still works —
this only fixes the floor.
"""

import os

os.environ["DATABASE_URL"] = "postgresql+asyncpg://test:test@localhost:5432/test"

from collections.abc import AsyncGenerator, Iterator
from pathlib import Path

import pytest
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from alembic import command
from app.core.config import get_settings
from app.core.database import dispose_engine, get_db
from app.main import app

ROOT = Path(__file__).resolve().parents[1]

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")
"""A **throwaway** database for the tests that need one — never ``DATABASE_URL``.

The two are deliberately different variables. ``DATABASE_URL`` is forced to a fake value above so
that a real hosted URL — which every worktree now has, since the setup copies ``.env`` — cannot
reach a test. A test that wants a real database has to name the database it wants.
"""

requires_db = pytest.mark.skipif(
    TEST_DATABASE_URL is None,
    reason=(
        "needs a throwaway Postgres. Start one and point TEST_DATABASE_URL at it:\n"
        "  docker run --rm -d --name lpe-test-pg -p 5433:5432"
        " -e POSTGRES_PASSWORD=test postgres:16\n"
        "  export TEST_DATABASE_URL=postgresql+asyncpg://postgres:test@localhost:5433/postgres"
    ),
)
"""Mark for database-backed tests. The reason names the exact command that un-skips them."""


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


@pytest.fixture(scope="session")
def migrated_database() -> Iterator[str]:
    """Apply the migration chain to ``TEST_DATABASE_URL`` once, and hand back the URL.

    Deliberately a **sync** fixture. ``alembic/env.py`` calls ``asyncio.run`` itself, which raises
    if it is invoked from inside a running loop — so the upgrade cannot happen in an async fixture.

    ``DATABASE_URL`` is swapped for the duration because ``env.py`` reads the URL from application
    settings, and put back afterwards so the fake value this module installs keeps protecting every
    other test.
    """
    if TEST_DATABASE_URL is None:  # pragma: no cover — every consumer carries `requires_db`
        pytest.skip("TEST_DATABASE_URL is not set")

    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))

    previous = os.environ["DATABASE_URL"]
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL
    get_settings.cache_clear()
    try:
        command.upgrade(config, "head")
    finally:
        os.environ["DATABASE_URL"] = previous
        get_settings.cache_clear()

    yield TEST_DATABASE_URL


@pytest.fixture
async def db_session(migrated_database: str) -> AsyncGenerator[AsyncSession, None]:
    """A session inside a transaction that is always rolled back.

    The SQLAlchemy "join an external transaction" recipe: the connection owns a transaction, the
    session joins it with ``create_savepoint``, and the rollback at the end discards everything the
    test wrote. ``create_savepoint`` is what lets the code under test call ``commit()`` for real —
    the service commits, and a test that could not tolerate that would be testing something else.
    """
    engine = create_async_engine(migrated_database)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            session = AsyncSession(
                bind=connection,
                expire_on_commit=False,
                join_transaction_mode="create_savepoint",
            )
            try:
                yield session
            finally:
                await session.close()
                await transaction.rollback()
    finally:
        await engine.dispose()


@pytest.fixture
async def client_with_db(db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    """An httpx client whose routes run against the rolled-back test session."""

    async def _override_get_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as async_client:
            yield async_client
    finally:
        app.dependency_overrides.clear()
