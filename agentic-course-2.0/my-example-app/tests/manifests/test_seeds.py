"""What the seed migration actually put in the database.

These read the rows ``0003`` wrote, not a fixture imitating them — the seeds are the M9 claim in
concrete form ("same code, different rows"), and a fixture would prove nothing about the migration.

The load-bearing one is the last: **fire cannot be activated while Google Places' terms are
unrecorded.** That open question is exactly why fire ships DRAFT.
"""

from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.manifests.exceptions import TermsOfUseNotRecordedError
from app.manifests.repository import ManifestRepository
from app.manifests.schemas import ManifestStatus, RuleKind, SourceKind
from app.manifests.service import ManifestService
from app.shared.provenance import RetrievalMethod
from tests.conftest import requires_db

pytestmark = requires_db


async def _seeded(session: AsyncSession, vertical: str) -> ManifestStatus:
    manifest = await ManifestRepository(session).get(vertical, 1)
    assert manifest is not None, f"{vertical} v1 was not seeded"
    return ManifestStatus(manifest.status)


async def _seeded_id(session: AsyncSession, vertical: str) -> UUID:
    manifest = await ManifestRepository(session).get(vertical, 1)
    assert manifest is not None, f"{vertical} v1 was not seeded"
    return manifest.id


class TestBothVerticalsAreSeededAsDrafts:
    async def test_freight_and_fire_exist_at_version_one(self, db_session: AsyncSession) -> None:
        for vertical in ("freight", "fire"):
            await _seeded(db_session, vertical)

    async def test_neither_is_active(self, db_session: AsyncSession) -> None:
        """A migration that could write an ACTIVE row would be a second door around the gate."""
        for vertical in ("freight", "fire"):
            assert await _seeded(db_session, vertical) is ManifestStatus.draft
            assert await ManifestRepository(db_session).get_active(vertical) is None


class TestFreight:
    async def test_it_names_fmcsa_and_excludes_asset_based_carriers(
        self, db_session: AsyncSession
    ) -> None:
        freight_id = await _seeded_id(db_session, "freight")
        body = (await ManifestService(db_session).get(freight_id)).body

        assert body.source_names() == ("fmcsa",)
        assert body.sources[0].value.kind is SourceKind.bulk_file
        assert "asset_based_carrier" in {rule.value.id for rule in body.disqualifier_rules}
        assert body.icp_band.value.headcount_min == 20
        assert body.icp_band.value.headcount_max == 200
        assert body.icp_band.value.requires_office_function is True
        assert "loads" in body.vocabulary.value.terms

    async def test_every_seeded_field_cites_manual_research(self, db_session: AsyncSession) -> None:
        """A hand-written row says so. ``web_lookup`` would be the small lie the gate prevents."""
        body = (await ManifestService(db_session).get(await _seeded_id(db_session, "freight"))).body
        # Collected as (method, url) pairs rather than as the citations themselves: the six wrap
        # six different value types, so a list of them would widen to `object`.
        citations: list[tuple[RetrievalMethod, str]] = [
            *((item.retrieval_method, item.source_url) for item in body.sources),
            *((item.retrieval_method, item.source_url) for item in body.disqualifier_rules),
            *((item.retrieval_method, item.source_url) for item in body.qualifying_signals),
            (body.icp_band.retrieval_method, body.icp_band.source_url),
            (body.vocabulary.retrieval_method, body.vocabulary.source_url),
        ]
        assert {method for method, _ in citations} == {RetrievalMethod.manual_research}
        assert all(url.startswith("https://") for _, url in citations)


class TestFire:
    async def test_it_names_the_fire_marshal_and_places(self, db_session: AsyncSession) -> None:
        body = (await ManifestService(db_session).get(await _seeded_id(db_session, "fire"))).body

        assert set(body.source_names()) == {"tx_fire_marshal", "places"}
        assert body.undecided_sources() == ("tx_fire_marshal", "places")

    async def test_the_rollup_rule_is_a_judgment_not_a_predicate(
        self, db_session: AsyncSession
    ) -> None:
        """E7's rule needs judgment about ownership, which is why classify_rollup exists."""
        body = (await ManifestService(db_session).get(await _seeded_id(db_session, "fire"))).body
        rule = next(
            item.value
            for item in body.disqualifier_rules
            if item.value.id == "national_rollup_no_local_owner"
        )
        assert rule.kind is RuleKind.judgment
        assert rule.field is None and rule.operator is None

    async def test_it_cannot_be_activated_while_places_is_undecided(
        self, db_session: AsyncSession
    ) -> None:
        """AC12, and the reason fire ships DRAFT: Places' terms are an open question."""
        service = ManifestService(db_session)
        fire_id = await _seeded_id(db_session, "fire")

        with pytest.raises(TermsOfUseNotRecordedError) as exc_info:
            await service.activate(fire_id, frozenset({"tx_fire_marshal"}), "sandeep")

        assert exc_info.value.missing_sources == ("places",)
