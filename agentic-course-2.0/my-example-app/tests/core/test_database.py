"""Database wiring, and the test isolation that finding #5 asked for.

Nothing here opens a connection — `create_async_engine` builds the engine lazily, so constructing
one against the dummy DATABASE_URL never dials out.
"""

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.core.database import (
    Base,
    dispose_engine,
    get_engine,
    get_sessionmaker,
    is_initialised,
)


class TestEngine:
    def test_engine_is_a_singleton(self) -> None:
        assert get_engine() is get_engine()

    def test_engine_uses_the_async_driver(self) -> None:
        """A bare `postgresql://` URL would select the sync driver and block the event loop."""
        assert get_engine().dialect.is_async is True

    def test_sessionmaker_is_a_singleton(self) -> None:
        assert get_sessionmaker() is get_sessionmaker()

    async def test_dispose_clears_both_singletons(self) -> None:
        get_sessionmaker()
        assert is_initialised() is True

        await dispose_engine()

        assert is_initialised() is False

    async def test_dispose_is_idempotent(self) -> None:
        await dispose_engine()
        await dispose_engine()


class TestIsolationBetweenTests:
    """Proves the autouse `_reset_database_state` fixture actually resets.

    These two run in file order, which pytest guarantees, and that ordering is the point: the first
    deliberately leaves an engine behind, and the second asserts it did not survive. Without the
    fixture the second test fails — which is exactly the T4 failure mode finding #5 described, where
    a test monkeypatches DATABASE_URL and silently gets an engine built from the previous URL.
    """

    def test_a_leaves_an_engine_behind(self) -> None:
        engine = get_engine()
        assert isinstance(engine, AsyncEngine)
        assert is_initialised() is True

    def test_b_does_not_inherit_it(self) -> None:
        assert is_initialised() is False, "the autouse reset fixture did not dispose the engine"


class TestBase:
    def test_base_carries_metadata_for_alembic(self) -> None:
        """Alembic's env.py autogenerates against this, so a slice's tables must reach it.

        T1 asserted ``tables == {}`` — true then, and a snapshot that any slice adding a table
        would break. What it was really guarding is that ``Base.metadata`` is what ``env.py``
        compares against, so it now asserts that a registered slice table is actually there. A
        table missing here is autogenerate proposing to drop it.
        """
        assert Base.metadata is not None
        assert "vertical_manifest" in Base.metadata.tables

    def test_sessionmaker_does_not_expire_on_commit(self) -> None:
        """Expiring would trigger a lazy refresh on attribute access, which async code cannot do."""
        maker = get_sessionmaker()
        assert isinstance(maker, async_sessionmaker)
        assert maker.kw["expire_on_commit"] is False
