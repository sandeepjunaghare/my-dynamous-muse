"""``lpe sourcing run``, end to end through ``app.cli.main``.

``lpe`` opens its own engine from ``DATABASE_URL`` in its own event loop, so it cannot see the
rolled-back test transaction. Like the manifests CLI suite, these tests **commit** their setup under
a throwaway vertical and delete everything under it afterwards. The registry stage is swapped for
one on the mock FMCSA, so nothing reaches the network.
"""

import asyncio
from collections.abc import Iterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.cli import main
from app.core.config import get_settings
from app.tools.registry import Stage
from app.tools.search_registry import SearchRegistryStage
from app.tools.verify_business import VerifyBusinessStage
from tests.conftest import requires_db
from tests.manifests.builders import a_vertical
from tests.sourcing.builders import a_freight_body, an_active_manifest_with
from tests.sourcing.conftest import (
    TEST_WEBKEY,
    MockFmcsa,
    MockPlaces,
    MockWeb,
    a_clock,
    census_rows,
    routed,
)

pytestmark = requires_db


@pytest.fixture
def cli_database(migrated_database: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    monkeypatch.setenv("DATABASE_URL", migrated_database)
    monkeypatch.setenv("FMCSA_WEBKEY", TEST_WEBKEY)
    get_settings.cache_clear()
    yield migrated_database
    get_settings.cache_clear()


@pytest.fixture
def mock_registry(monkeypatch: pytest.MonkeyPatch) -> MockFmcsa:
    mock = MockFmcsa()
    stage = SearchRegistryStage(
        transport=mock.transport(), clock=a_clock(), backoff_seconds=0.0, throttle=False
    )

    def stages() -> tuple[Stage, ...]:
        return (stage,)

    monkeypatch.setattr("app.sourcing.cli.registered_stages", stages)
    return mock


@pytest.fixture
def committed_freight(cli_database: str) -> Iterator[str]:
    """An ACTIVE freight-shaped manifest under a throwaway vertical; everything removed after."""
    vertical = a_vertical()

    async def create() -> None:
        engine = create_async_engine(cli_database)
        try:
            async with AsyncSession(engine, expire_on_commit=False) as session:
                await an_active_manifest_with(session, a_freight_body(), vertical=vertical)
                await session.commit()
        finally:
            await engine.dispose()

    async def remove() -> None:
        engine = create_async_engine(cli_database)
        try:
            async with AsyncSession(engine) as session:
                runs = "select id from sourcing_run where vertical = :vertical"
                await session.execute(
                    text(f"delete from candidate where run_id in ({runs})"), {"vertical": vertical}
                )
                await session.execute(
                    text("delete from sourcing_pool where vertical = :vertical"),
                    {"vertical": vertical},
                )
                for table in ("sourcing_run", "vertical_manifest"):
                    await session.execute(
                        text(f"delete from {table} where vertical = :vertical"),
                        {"vertical": vertical},
                    )
                await session.commit()
        finally:
            await engine.dispose()

    asyncio.run(create())
    try:
        yield vertical
    finally:
        asyncio.run(remove())


@pytest.fixture
def mock_registry_and_places(monkeypatch: pytest.MonkeyPatch) -> tuple[MockFmcsa, MockPlaces]:
    """The registry on the mock FMCSA, then ``verify_business`` on mock Places and web."""
    fmcsa, places = MockFmcsa(), MockPlaces()
    registry = SearchRegistryStage(
        transport=fmcsa.transport(), clock=a_clock(), backoff_seconds=0.0, throttle=False
    )
    web = MockWeb()
    for row in census_rows():
        # Every fixture business's homepage is down: the website step runs, and finds nothing.
        web.status(f"https://{row['email_address'].split('@')[1]}/", 404)
    verify = VerifyBusinessStage(
        transport=routed(places, web), clock=a_clock(), backoff_seconds=0.0, throttle=False
    )

    def stages() -> tuple[Stage, ...]:
        return (registry, verify)

    monkeypatch.setattr("app.sourcing.cli.registered_stages", stages)
    return fmcsa, places


class TestRun:
    def test_a_run_prints_its_id_status_and_counts(
        self,
        committed_freight: str,
        mock_registry: MockFmcsa,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        assert main(["sourcing", "run", "--vertical", committed_freight, "--batch-size", "2"]) == 0
        out = capsys.readouterr().out
        assert f"{committed_freight}  completed" in out
        assert "batched" in out and "pool_seen" in out
        assert "$0.00" in out
        # --batch-size reached the stage, and the stage kept its mock transport.
        assert len(mock_registry.calls_to("/carriers/")) == 2

    def test_a_degraded_run_exits_zero_and_warns(
        self,
        committed_freight: str,
        mock_registry: MockFmcsa,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("FMCSA_WEBKEY", "")
        get_settings.cache_clear()
        assert main(["sourcing", "run", "--vertical", committed_freight, "--allow-degraded"]) == 0
        captured = capsys.readouterr()
        assert "degraded" in captured.out
        assert "warning:" in captured.err and "FMCSA_WEBKEY" in captured.err

    def test_an_unusable_source_is_refused_without_allow_degraded(
        self,
        committed_freight: str,
        mock_registry: MockFmcsa,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """M1: the batch is not spent without its evidence unless the person says so."""
        monkeypatch.setenv("FMCSA_WEBKEY", "")
        get_settings.cache_clear()
        assert main(["sourcing", "run", "--vertical", committed_freight]) == 1
        err = capsys.readouterr().err
        (message,) = [line for line in err.splitlines() if line.startswith("error: ")]
        assert "FMCSA_WEBKEY is not set" in message
        assert "--allow-degraded" in message
        assert mock_registry.requests == []

    def test_a_vertical_with_no_active_manifest_is_one_line_not_a_traceback(
        self, cli_database: str, mock_registry: MockFmcsa, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["sourcing", "run", "--vertical", a_vertical()]) == 1
        err = capsys.readouterr().err
        # Log lines share stderr; the person-facing part is the one `error:` line.
        (message,) = [line for line in err.splitlines() if line.startswith("error: ")]
        assert "no active manifest" in message
        assert "Traceback" not in err
        assert mock_registry.requests == []


class TestPlaces:
    """A3: freight with no Places source declared refuses unless --allow-degraded (T6)."""

    def test_no_places_source_fails_the_run_with_one_error_line(
        self,
        committed_freight: str,
        mock_registry_and_places: tuple[MockFmcsa, MockPlaces],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _, places = mock_registry_and_places
        assert main(["sourcing", "run", "--vertical", committed_freight, "--batch-size", "2"]) == 1
        err = capsys.readouterr().err
        (message,) = [line for line in err.splitlines() if line.startswith("error: ")]
        assert "declares no Places source" in message
        assert "--allow-degraded" in message
        assert places.requests == []

    def test_with_the_flag_it_exits_zero_and_warns(
        self,
        committed_freight: str,
        mock_registry_and_places: tuple[MockFmcsa, MockPlaces],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _, places = mock_registry_and_places
        argv = ["sourcing", "run", "--vertical", committed_freight, "--batch-size", "2"]
        assert main([*argv, "--allow-degraded"]) == 0
        captured = capsys.readouterr()
        assert "degraded" in captured.out
        assert "warning:" in captured.err and "declares no Places source" in captured.err
        assert places.requests == []


@pytest.mark.parametrize("bad", ["0", "-3", "many"])
def test_a_batch_size_below_one_is_refused_by_argparse(bad: str) -> None:
    with pytest.raises(SystemExit) as exited:
        main(["sourcing", "run", "--vertical", "freight", "--batch-size", bad])
    assert exited.value.code == 2
