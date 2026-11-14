"""Fixtures for the weekly run's suites: fake stages, a fake CRM, and a service wired to both.

The service runs against ``session_factory`` (fresh sessions on one rolled-back connection, from
``tests/conftest.py``) and :class:`FakeCrm`, which raises on any request it does not know. So
"no network" stays a proven property, and promotion runs for real against the ledger.

One thing these tests cannot show: every session joins one outer transaction, so the database's
``now()`` is the same instant throughout a test. M7's timestamps are ordered (``<=``) here, never
apart; the minutes themselves are pinned in ``test_models`` and ``test_report``.
"""

from collections.abc import AsyncGenerator, Callable, Mapping, Sequence
from datetime import UTC, datetime

import pytest

from app.promotion.client import HubSpotClient, aclose_hubspot_client
from app.runs.service import WeeklyRunService
from app.sourcing.schemas import CandidateFields
from app.sourcing.service import SourcingService
from app.sourcing.stages import PipelineStage
from app.tools.registry import Stage, StageContext, StageResult
from tests.cadence.conftest import FakeClock
from tests.conftest import SessionFactory
from tests.promotion.conftest import DEFAULT_OWNER, PORTAL_ID, make_client
from tests.promotion.crm import FakeCrm
from tests.qualification.builders import a_body_with, an_active_manifest_with
from tests.sourcing.builders import REGISTRY


class Records:
    """``search_registry`` stand-in: records these candidates in the run and counts them."""

    def __init__(
        self,
        fields: Sequence[CandidateFields] = (),
        *,
        counts: Mapping[str, int] | None = None,
        degraded_reason: str | None = None,
    ) -> None:
        self._fields = list(fields)
        self._counts = dict(counts) if counts is not None else {"batched": len(self._fields)}
        self._degraded_reason = degraded_reason

    @property
    def stage(self) -> PipelineStage:
        return PipelineStage.search_registry

    async def run(self, context: StageContext) -> StageResult:
        if self._fields:
            await SourcingService(context.session).record_candidates(
                context.run_id, self._fields, stage=REGISTRY
            )
        return StageResult(counts=self._counts, degraded_reason=self._degraded_reason)


class Raises:
    """A stage that raises whatever it was given, ``CancelledError`` included."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    @property
    def stage(self) -> PipelineStage:
        return PipelineStage.verify_business

    async def run(self, context: StageContext) -> StageResult:
        raise self._exc


type MakeService = Callable[..., WeeklyRunService]


@pytest.fixture(autouse=True)
def _portal_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin everything the run reads from settings, so nothing comes from a developer's ``.env``."""
    monkeypatch.setenv("HUBSPOT_PORTAL_ID", PORTAL_ID)
    monkeypatch.setenv("HUBSPOT_UI_DOMAIN", "app-na2.hubspot.com")
    monkeypatch.setenv("HUBSPOT_DEFAULT_OWNER_ID", DEFAULT_OWNER)
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "")
    monkeypatch.setenv("WEEKLY_RUN_STALE_HOURS", "2")
    monkeypatch.setenv("WEEKLY_RUN_TARGET_MINUTES", "60")


@pytest.fixture(autouse=True)
async def _reset_hubspot_client() -> AsyncGenerator[None, None]:
    await aclose_hubspot_client()
    yield
    await aclose_hubspot_client()


@pytest.fixture
def crm() -> FakeCrm:
    return FakeCrm(FakeClock(datetime.now(UTC)))


@pytest.fixture
async def crm_client(crm: FakeCrm) -> AsyncGenerator[HubSpotClient, None]:
    client = make_client(crm.portal)
    yield client
    await client.aclose()


@pytest.fixture
def make_service(session_factory: SessionFactory, crm_client: HubSpotClient) -> MakeService:
    """``make_service(stage, …)``: a service whose every run uses exactly these stages."""

    def make(*stages: Stage) -> WeeklyRunService:
        return WeeklyRunService(
            session_factory, hubspot=lambda: crm_client, stages=lambda size, degraded: stages
        )

    return make


async def an_active_vertical(factory: SessionFactory) -> str:
    """A throwaway vertical with an ACTIVE manifest, committed on the test's connection."""
    async with factory() as session:
        manifest = await an_active_manifest_with(session, a_body_with())
        await session.commit()
    return manifest.vertical
