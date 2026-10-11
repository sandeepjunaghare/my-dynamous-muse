"""Fixtures for the sourcing suites: a mock FMCSA, settings, and a factory of fresh sessions.

**No test here touches the network.** :class:`MockFmcsa` is an ``httpx.MockTransport`` that serves
the census, Motus revocations and QCMobile from recorded fixtures and **raises on any request it
has no route for** — the same stance as ``tests/promotion/conftest.py``'s ``MockPortal``, so
"fixtures only" is a proven property rather than an intention. The fixture shapes were taken from
one live read of each public dataset on 2026-10-10; QCMobile's follows its documentation, since
there is no webKey yet to record a real answer.
"""

import json
import re
from collections.abc import AsyncGenerator, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.config import Settings
from app.sourcing.sources.base import SourceEnv

FIXTURES = Path(__file__).parent / "fixtures"

TEST_WEBKEY = "qcmobile-webkey-fixture-not-a-secret"
"""Asserted never to reach a citation or a log line."""

RUN_CLOCK = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
"""The pinned clock: citations stamp it, and the batch's two-year cutoff is taken from it."""

_IN_LIST = re.compile(r"'([^']*)'")


def load_fixture(name: str) -> object:
    """Read a recorded body. Synthetic, but schema-accurate."""
    loaded: object = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return loaded


def census_rows() -> list[dict[str, str]]:
    """The eight-row census fixture: every ordering tier and every edge case once."""
    return cast(list[dict[str, str]], load_fixture("census_page"))


def a_settings(**overrides: object) -> Settings:
    """Settings for a test. The webKey is set unless the test says otherwise."""
    values: dict[str, object] = {"fmcsa_webkey": TEST_WEBKEY, "socrata_app_token": None}
    values.update(overrides)
    return Settings.model_validate(values)


def a_clock(at: datetime = RUN_CLOCK) -> Callable[[], datetime]:
    return lambda: at


@dataclass
class MockFmcsa:
    """A mock of the three FMCSA endpoints, and a log of what was asked of each.

    ``census`` is served a page at a time by ``$limit``/``$offset``; ``revocations`` is filtered by
    the USDOT numbers in the ``$where``; a QCMobile carrier is answered from ``carriers`` by USDOT,
    falling back to the found-it fixture with that USDOT. ``status_by_dot`` forces a status.
    """

    census: list[dict[str, str]] = field(default_factory=census_rows)
    revocations: list[dict[str, str]] = field(
        default_factory=lambda: cast(list[dict[str, str]], load_fixture("revocations"))
    )
    carriers: dict[str, tuple[int, object]] = field(default_factory=dict[str, tuple[int, object]])
    census_status: list[int] = field(default_factory=list[int])
    """Statuses to answer the census with before serving it — how a retry is tested honestly."""
    requests: list[httpx.Request] = field(default_factory=list[httpx.Request])

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host, path = request.url.host, request.url.path
        params = request.url.params
        if host == "data.transportation.gov" and path == "/resource/az4n-8mr2.json":
            if self.census_status:
                return httpx.Response(self.census_status.pop(0), json={"message": "busy"})
            offset, limit = int(params["$offset"]), int(params["$limit"])
            return httpx.Response(200, json=self.census[offset : offset + limit])
        if host == "data.transportation.gov" and path == "/resource/wb4f-neki.json":
            wanted = set(_IN_LIST.findall(params.get("$where", "")))
            return httpx.Response(
                200, json=[row for row in self.revocations if row.get("usdot_number") in wanted]
            )
        if host == "mobile.fmcsa.dot.gov" and path.startswith("/qc/services/carriers/"):
            dot = path.rsplit("/", 1)[1]
            status, body = self.carriers.get(dot, (200, self._found(dot)))
            return httpx.Response(status, json=body)
        raise AssertionError(
            f"unexpected request to the mock FMCSA: {request.method} {request.url}"
        )

    @staticmethod
    def _found(dot: str) -> object:
        body = cast(dict[str, dict[str, dict[str, object]]], load_fixture("qcmobile_carrier"))
        body["content"]["carrier"]["dotNumber"] = int(dot)
        return body

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def calls_to(self, path_fragment: str) -> list[httpx.Request]:
        return [request for request in self.requests if path_fragment in request.url.path]


@pytest.fixture
def mock_fmcsa() -> MockFmcsa:
    return MockFmcsa()


@pytest.fixture
async def source_env(mock_fmcsa: MockFmcsa) -> AsyncGenerator[SourceEnv, None]:
    """A ``SourceEnv`` on the mock: pinned clock, no backoff, no pacing."""
    async with httpx.AsyncClient(transport=mock_fmcsa.transport()) as client:
        yield SourceEnv(
            client=client,
            settings=a_settings(),
            clock=a_clock(),
            backoff_seconds=0.0,
            throttle=False,
        )


type SessionFactory = Callable[[], AsyncSession]


@pytest.fixture
async def session_factory(migrated_database: str) -> AsyncGenerator[SessionFactory, None]:
    """Fresh sessions on **one** connection whose outer transaction is always rolled back.

    The ``db_session`` recipe (``tests/conftest.py``), returning a factory instead of a session —
    which is what lets the pipeline's crash path, "finish a failed run on a *fresh* session", be
    tested for real without leaking anything into the next test.
    """
    engine = create_async_engine(migrated_database)
    sessions: list[AsyncSession] = []
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()

            def make() -> AsyncSession:
                session = AsyncSession(
                    bind=connection,
                    expire_on_commit=False,
                    join_transaction_mode="create_savepoint",
                )
                sessions.append(session)
                return session

            try:
                yield make
            finally:
                for session in sessions:
                    await session.close()
                await transaction.rollback()
    finally:
        await engine.dispose()


def params_of(request: httpx.Request) -> Mapping[str, str]:
    return dict(request.url.params)
