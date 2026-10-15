"""Stage 4 over a real run: census first, the node only for verified officer-less survivors."""

from collections.abc import AsyncIterator, Mapping

import pytest
from claude_agent_sdk import ClaudeAgentOptions, Message
from pydantic import JsonValue
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cost import RunCost
from app.manifests.schemas import ManifestResponse, RuleOperator
from app.qualification.schemas import RecordSet
from app.qualification.service import QualificationService
from app.shared.provenance import RetrievalMethod
from app.sourcing.owner import judgment
from app.sourcing.schemas import CandidateResponse, Principal, SourcingRunResponse
from app.sourcing.service import SourcingService
from app.sourcing.stages import PipelineStage
from app.tools.registry import StageContext, registered_stages
from app.tools.resolve_owner import STAGE
from tests.conftest import requires_db
from tests.manifests.replay import Replay, StreamLine, result_line, web_fetch_call, web_fetch_result
from tests.qualification.builders import a_body_with, a_run_with, census_row, predicate
from tests.sourcing.builders import (
    CENSUS_URL,
    REGISTRY,
    VERIFY,
    a_candidate,
    place_checked,
    sourced,
)

ABOUT = "https://www.acme.test/about"
BODY = a_body_with(predicate("no_broker_entity_type", "carship", RuleOperator.not_contains, "B"))
OWNER = PipelineStage.resolve_owner


def _stream(answer: JsonValue) -> list[StreamLine]:
    return [web_fetch_call("t1", ABOUT), web_fetch_result("t1", ABOUT), result_line(answer)]


def _named(name: str, quote: str | None = None) -> list[StreamLine]:
    citation: JsonValue = {"url": ABOUT, "quote": quote or f"{name} founded the firm."}
    return _stream({"name": name, "title": "Founder", "reason": "", "citation": citation})


GRIGGS = _named("Bryan Griggs")
A_COMPANY = _named("ACME LOGISTICS LLC", "ACME LOGISTICS LLC is the owner.")
FAILED: list[StreamLine] = [result_line(None, subtype="error_max_turns", is_error=True)]


class _ByRegistryId:
    """A runner that replays a different recording per candidate, found by its id in the prompt."""

    def __init__(self, streams: Mapping[str, list[StreamLine]]) -> None:
        self._streams = streams
        self.prompts: list[str] = []

    def __call__(self, prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Message]:
        self.prompts.append(prompt)
        registry_id = next(rid for rid in self._streams if f"registry id: {rid} " in prompt)
        return Replay(self._streams[registry_id])(prompt, options)

    def called_for(self, registry_id: str) -> bool:
        return any(f"registry id: {registry_id} " in prompt for prompt in self.prompts)


async def _verify(session: AsyncSession, run: SourcingRunResponse, *registry_ids: str) -> None:
    await SourcingService(session).record_candidates(
        run.id,
        [
            a_candidate(rid).model_copy(update={"business_check": place_checked()})
            for rid in registry_ids
        ],
        stage=VERIFY,
    )


async def _setup(
    session: AsyncSession, *officerless_verified: str
) -> tuple[ManifestResponse, SourcingRunResponse]:
    """701 census-owned and verified; 702 disqualified; 703 officer-less and unverified; and each
    of ``officerless_verified`` officer-less and verified."""
    ids = ("701", "702", "703", *officerless_verified)
    manifest, run, candidates = await a_run_with(session, BODY, *ids)
    sourcing = SourcingService(session)
    await sourcing.record_candidates(
        run.id,
        [a_candidate("701").model_copy(update={"owner": sourced(Principal(name="Dana Reyes"))})],
        stage=REGISTRY,
    )
    await _verify(session, run, "701", "702", *officerless_verified)
    records: RecordSet = {"fmcsa": census_row(carship="C")}
    await QualificationService(session).disqualify_by_predicates(
        candidates[1], run, manifest, records
    )
    return manifest, run


def _context(
    manifest: ManifestResponse, run: SourcingRunResponse, session: AsyncSession
) -> StageContext:
    return StageContext(run_id=run.id, manifest=manifest, session=session, cost=RunCost())


async def _by_id(session: AsyncSession, run: SourcingRunResponse) -> dict[str, CandidateResponse]:
    return {c.registry_id: c for c in await SourcingService(session).list_candidates(run.id)}


def test_resolve_owner_runs_right_after_classify_rollup() -> None:
    stages = [stage.stage for stage in registered_stages()]
    assert (
        stages.index(PipelineStage.resolve_owner) == stages.index(PipelineStage.classify_rollup) + 1
    )


@requires_db
class TestTheStage:
    async def test_census_first_and_one_call_per_verified_officerless_survivor(
        self, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner = _ByRegistryId({"704": GRIGGS})
        monkeypatch.setattr(judgment, "sdk_runner", runner)
        manifest, run = await _setup(db_session, "704")

        result = await STAGE.run(_context(manifest, run, db_session))

        assert len(runner.prompts) == 1
        assert runner.called_for("704")
        assert dict(result.counts) == {
            "owner_from_registry": 1,
            "owner_kept": 0,
            "owner_resolved": 1,
            "owner_absent": 0,
            "owner_failed": 0,
            "owner_write_refused": 0,
            "owner_skipped_disqualified": 1,
            "owner_unverified_skipped": 1,
            "owner_capped": 0,
            "owner_calls": 1,
        }
        assert result.degraded_reason is None

        candidates = await _by_id(db_session, run)
        owner = candidates["704"].fields.owner
        assert owner is not None
        assert owner.value.name == "Bryan Griggs"
        assert owner.source_url == ABOUT
        assert owner.retrieval_method is RetrievalMethod.llm_inference
        census = candidates["701"].fields.owner
        assert census is not None and census.retrieval_method is RetrievalMethod.bulk_file
        assert candidates["702"].fields.owner is None
        assert candidates["703"].fields.owner is None

    async def test_a_retry_pays_nothing(
        self, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner = _ByRegistryId({"704": GRIGGS})
        monkeypatch.setattr(judgment, "sdk_runner", runner)
        manifest, run = await _setup(db_session, "704")
        await STAGE.run(_context(manifest, run, db_session))

        again = await STAGE.run(_context(manifest, run, db_session))

        assert len(runner.prompts) == 1
        assert (again.counts["owner_calls"], again.counts["owner_kept"]) == (0, 1)

    async def test_the_cap_ends_the_stage_degraded_and_never_raises(
        self, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MAX_OWNER_CALLS_PER_RUN", "1")
        runner = _ByRegistryId({"704": GRIGGS, "705": GRIGGS})
        monkeypatch.setattr(judgment, "sdk_runner", runner)
        manifest, run = await _setup(db_session, "704", "705")

        result = await STAGE.run(_context(manifest, run, db_session))

        assert len(runner.prompts) == 1
        assert (result.counts["owner_calls"], result.counts["owner_capped"]) == (1, 1)
        assert result.degraded_reason == (
            "owner judgment cap of 1 calls reached; 1 candidate(s) unresolved"
        )

    async def test_one_failed_call_does_not_sink_the_batch(
        self, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner = _ByRegistryId({"704": FAILED, "705": GRIGGS})
        monkeypatch.setattr(judgment, "sdk_runner", runner)
        manifest, run = await _setup(db_session, "704", "705")

        result = await STAGE.run(_context(manifest, run, db_session))

        assert len(runner.prompts) == 2
        assert (result.counts["owner_failed"], result.counts["owner_resolved"]) == (1, 1)

    async def test_an_omitted_answer_stores_no_owner(
        self, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(judgment, "sdk_runner", _ByRegistryId({"704": A_COMPANY}))
        manifest, run = await _setup(db_session, "704")

        result = await STAGE.run(_context(manifest, run, db_session))

        assert result.counts["owner_absent"] == 1
        assert (await _by_id(db_session, run))["704"].fields.owner is None


@requires_db
class TestOwnershipPR31L2:
    """``owner`` belongs to ``resolve_owner``: it may replace its own; the registry only fills."""

    async def _write(
        self,
        session: AsyncSession,
        run: SourcingRunResponse,
        stage: PipelineStage,
        name: str,
        url: str,
        method: RetrievalMethod,
    ) -> None:
        owner = sourced(Principal(name=name), url, method)
        await SourcingService(session).record_candidates(
            run.id, [a_candidate("801").model_copy(update={"owner": owner})], stage=stage
        )

    async def _stored(
        self, session: AsyncSession, run: SourcingRunResponse
    ) -> tuple[str, str, RetrievalMethod]:
        owner = (await _by_id(session, run))["801"].fields.owner
        assert owner is not None
        return owner.value.name, owner.source_url, owner.retrieval_method

    async def test_resolve_owner_replaces_its_own_owner(self, db_session: AsyncSession) -> None:
        _, run, _ = await a_run_with(db_session, BODY, "801")
        await self._write(
            db_session, run, OWNER, "Bryan Griggs", ABOUT, RetrievalMethod.llm_inference
        )
        team = "https://www.acme.test/team"
        await self._write(db_session, run, OWNER, "Dana Reyes", team, RetrievalMethod.llm_inference)
        assert await self._stored(db_session, run) == (
            "Dana Reyes",
            team,
            RetrievalMethod.llm_inference,
        )

    async def test_the_registry_does_not_replace_the_nodes_owner(
        self, db_session: AsyncSession
    ) -> None:
        _, run, _ = await a_run_with(db_session, BODY, "801")
        await self._write(
            db_session, run, OWNER, "Bryan Griggs", ABOUT, RetrievalMethod.llm_inference
        )
        await self._write(
            db_session, run, REGISTRY, "Dana Reyes", CENSUS_URL, RetrievalMethod.bulk_file
        )
        assert await self._stored(db_session, run) == (
            "Bryan Griggs",
            ABOUT,
            RetrievalMethod.llm_inference,
        )

    async def test_the_registry_fills_an_empty_owner(self, db_session: AsyncSession) -> None:
        _, run, _ = await a_run_with(db_session, BODY, "801")
        await self._write(
            db_session, run, REGISTRY, "Dana Reyes", CENSUS_URL, RetrievalMethod.bulk_file
        )
        assert await self._stored(db_session, run) == (
            "Dana Reyes",
            CENSUS_URL,
            RetrievalMethod.bulk_file,
        )
