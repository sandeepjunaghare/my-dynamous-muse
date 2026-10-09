"""The repository's contract, against a real Postgres.

Load-bearing beyond the method they name: **provenance survives persist → load** (the ticket's
round-trip test), the upsert is idempotent and never erases a citation, and the invariants the
service relies on are refused **by the database** — a check in the service can be forgotten by a
slice nobody has written yet.
"""

from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import ScalarResult, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cost import BillableKind, RunCost
from app.shared.provenance import RetrievalMethod, is_promotable
from app.sourcing.models import Candidate, SourcingRun
from app.sourcing.repository import SourcingRepository
from app.sourcing.schemas import (
    CandidateFields,
    RunCostSummary,
    RunOutcome,
    RunStatus,
    SourcingRunResponse,
)
from tests.conftest import requires_db
from tests.sourcing.builders import a_brief, a_candidate, an_active_manifest, sourced

pytestmark = requires_db

_CITED_ID: dict[str, object] = sourced("1234567").model_dump(mode="json")
"""A well-formed ``fields.registry_id``, as the typed API writes it."""

_CITATION: dict[str, object] = {k: v for k, v in _CITED_ID.items() if k != "value"}


def _without(payload: dict[str, object], key: str) -> dict[str, object]:
    return {k: v for k, v in payload.items() if k != key}


async def _a_run(session: AsyncSession) -> SourcingRun:
    manifest_id, vertical = await an_active_manifest(session)
    return await SourcingRepository(session).create_run(a_brief(vertical), manifest_id)


async def _reload(session: AsyncSession, candidate_id: UUID) -> CandidateFields:
    """Drop everything the session holds, then read the row back from Postgres."""
    session.expunge_all()
    loaded = await SourcingRepository(session).get_candidate(candidate_id)
    assert loaded is not None
    return CandidateFields.model_validate(loaded.fields)


class TestRuns:
    async def test_a_new_run_is_running_with_server_defaults(
        self, db_session: AsyncSession
    ) -> None:
        run = await _a_run(db_session)

        assert run.status == RunStatus.running.value
        assert run.started_at is not None
        assert run.finished_at is None
        assert run.counts == {}
        assert run.cost == {}
        assert run.icp_band["headcount_min"] == 20

    async def test_the_brief_and_manifest_are_recorded(self, db_session: AsyncSession) -> None:
        manifest_id, vertical = await an_active_manifest(db_session)
        run = await SourcingRepository(db_session).create_run(a_brief(vertical), manifest_id)

        response = SourcingRunResponse.model_validate(run)
        assert (response.vertical, response.geography) == (vertical, "dfw")
        assert response.manifest_id == manifest_id
        assert response.icp_band.headcount_max == 200

    async def test_mark_finished_persists_counts_and_cost(self, db_session: AsyncSession) -> None:
        repository = SourcingRepository(db_session)
        run = await _a_run(db_session)
        cost = RunCost()
        cost.record(BillableKind.places_lookup, count=2, usd=Decimal("0.034"))

        await repository.mark_finished(
            run,
            RunOutcome.degraded,
            {"sourced": 600, "verified": 80},
            RunCostSummary.from_run_cost(cost),
            "places circuit breaker tripped",
        )
        db_session.expunge_all()
        loaded = await repository.get_run(run.id)

        assert loaded is not None
        response = SourcingRunResponse.model_validate(loaded)
        assert response.status is RunStatus.degraded
        assert response.finished_at is not None
        assert response.counts == {"sourced": 600, "verified": 80}
        assert response.cost.calls[BillableKind.places_lookup] == 2
        assert response.cost.total_usd() == Decimal("0.034")
        assert response.status_detail == "places circuit breaker tripped"

    async def test_get_run_returns_none_for_an_unknown_id(self, db_session: AsyncSession) -> None:
        assert await SourcingRepository(db_session).get_run(uuid4()) is None


class TestProvenanceRoundTrip:
    async def test_provenance_survives_persist_then_load(self, db_session: AsyncSession) -> None:
        """The ticket's named test: every citation comes back from Postgres as it went in."""
        run = await _a_run(db_session)
        original = a_candidate("7654321")

        stored = await SourcingRepository(db_session).upsert_candidate(run.id, original)
        loaded = await _reload(db_session, stored.id)

        assert loaded == original
        assert loaded.registry_id.source_url == original.registry_id.source_url
        assert loaded.registry_id.retrieved_at == original.registry_id.retrieved_at
        assert loaded.registry_id.retrieved_at.utcoffset() is not None
        assert loaded.phone is not None
        assert loaded.phone.retrieval_method is RetrievalMethod.registry_api
        assert loaded.address is not None
        assert loaded.address.value.postal_code == "76011"

    async def test_an_unprovenanced_field_is_storable_but_not_promotable(
        self, db_session: AsyncSession
    ) -> None:
        """The workbench may hold it; HubSpot may not receive it."""
        run = await _a_run(db_session)

        stored = await SourcingRepository(db_session).upsert_candidate(
            run.id, a_candidate(with_phone=False)
        )
        loaded = await _reload(db_session, stored.id)

        assert loaded.phone is None
        assert is_promotable(loaded.phone) is False
        assert is_promotable(loaded.legal_name) is True
        assert loaded.unprovenanced_fields() == ("phone", "website")

    async def test_absent_fields_are_absent_keys_not_nulls(self, db_session: AsyncSession) -> None:
        """What lets the upsert merge without erasing — see ``upsert_candidate``."""
        run = await _a_run(db_session)
        stored = await SourcingRepository(db_session).upsert_candidate(
            run.id, a_candidate(with_phone=False)
        )

        keys: ScalarResult[str] = (
            await db_session.execute(
                text("select jsonb_object_keys(fields) from candidate where id = :id"),
                {"id": stored.id},
            )
        ).scalars()
        assert set(keys) == {"registry_id", "legal_name", "dba_name", "address"}


class TestUpsert:
    async def test_the_same_input_twice_is_one_row(self, db_session: AsyncSession) -> None:
        repository = SourcingRepository(db_session)
        run = await _a_run(db_session)

        first = await repository.upsert_candidate(run.id, a_candidate())
        first_id, first_fields = first.id, dict(first.fields)
        second = await repository.upsert_candidate(run.id, a_candidate())

        assert second.id == first_id
        assert second.fields == first_fields
        assert len(await repository.list_candidates(run.id)) == 1

    async def test_a_later_write_that_knows_less_keeps_the_citation(
        self, db_session: AsyncSession
    ) -> None:
        repository = SourcingRepository(db_session)
        run = await _a_run(db_session)
        await repository.upsert_candidate(run.id, a_candidate())

        merged = await repository.upsert_candidate(run.id, a_candidate(with_phone=False))

        fields = CandidateFields.model_validate(merged.fields)
        assert fields.phone is not None
        assert fields.phone.value == "+1-817-555-0100"

    async def test_a_later_cited_value_replaces_the_earlier_one(
        self, db_session: AsyncSession
    ) -> None:
        repository = SourcingRepository(db_session)
        run = await _a_run(db_session)
        await repository.upsert_candidate(run.id, a_candidate())

        renamed = CandidateFields(
            registry_id=sourced("1234567"), legal_name=sourced("Acme Renamed LLC")
        )
        merged = await repository.upsert_candidate(run.id, renamed)

        fields = CandidateFields.model_validate(merged.fields)
        assert fields.legal_name is not None
        assert fields.legal_name.value == "Acme Renamed LLC"
        assert fields.dba_name is not None  # untouched by a write that did not carry it

    async def test_an_object_already_in_the_session_is_refreshed(
        self, db_session: AsyncSession
    ) -> None:
        """Guards ``populate_existing``: holding the first object keeps it in the identity map.

        Without the option, the second upsert hands back that same in-memory object with its
        *old* fields — the row in Postgres is right and the object the caller reads is not.
        """
        repository = SourcingRepository(db_session)
        run = await _a_run(db_session)
        held = await repository.upsert_candidate(run.id, a_candidate())

        renamed = CandidateFields(
            registry_id=sourced("1234567"), legal_name=sourced("Acme Renamed LLC")
        )
        merged = await repository.upsert_candidate(run.id, renamed)

        assert merged is held
        fields = CandidateFields.model_validate(held.fields)
        assert fields.legal_name is not None
        assert fields.legal_name.value == "Acme Renamed LLC"

    async def test_the_same_registry_id_in_two_runs_is_two_rows(
        self, db_session: AsyncSession
    ) -> None:
        """The key is (run, registry id) — each week's run keeps its own record of what it saw."""
        repository = SourcingRepository(db_session)
        first_run = await _a_run(db_session)
        second_run = await _a_run(db_session)

        first = await repository.upsert_candidate(first_run.id, a_candidate())
        second = await repository.upsert_candidate(second_run.id, a_candidate())

        assert first.id != second.id

    async def test_list_candidates_is_ordered_by_registry_id(
        self, db_session: AsyncSession
    ) -> None:
        repository = SourcingRepository(db_session)
        run = await _a_run(db_session)
        for registry_id in ("300", "100", "200"):
            await repository.upsert_candidate(run.id, a_candidate(registry_id))

        listed = await repository.list_candidates(run.id)

        assert [candidate.registry_id for candidate in listed] == ["100", "200", "300"]

    async def test_padding_does_not_make_a_second_row(self, db_session: AsyncSession) -> None:
        """The same carrier read twice — once from a padded CSV column — is one candidate."""
        repository = SourcingRepository(db_session)
        run = await _a_run(db_session)

        first = await repository.upsert_candidate(run.id, a_candidate("555"))
        second = await repository.upsert_candidate(run.id, a_candidate(" 555 "))

        assert second.id == first.id
        assert second.registry_id == "555"
        assert len(await repository.list_candidates(run.id)) == 1

    async def test_a_registry_id_at_the_column_limit_persists(
        self, db_session: AsyncSession
    ) -> None:
        """The schema's limit and the column's agree: the longest id the schema allows fits."""
        run = await _a_run(db_session)

        stored = await SourcingRepository(db_session).upsert_candidate(
            run.id, a_candidate("9" * 128)
        )

        assert len(stored.registry_id) == 128


class TestDatabaseInvariants:
    async def test_a_duplicate_key_is_refused_by_a_plain_insert(
        self, db_session: AsyncSession
    ) -> None:
        run = await _a_run(db_session)
        await SourcingRepository(db_session).upsert_candidate(run.id, a_candidate())

        db_session.add(
            Candidate(
                run_id=run.id,
                registry_id="1234567",
                fields=a_candidate().model_dump(mode="json", exclude_none=True),
            )
        )
        with pytest.raises(IntegrityError, match="uq_candidate_run_registry_id"):
            await db_session.flush()

    async def test_the_key_column_must_match_its_citation(self, db_session: AsyncSession) -> None:
        run = await _a_run(db_session)

        db_session.add(
            Candidate(
                run_id=run.id,
                registry_id="9999999",
                fields=a_candidate("1234567").model_dump(mode="json", exclude_none=True),
            )
        )
        with pytest.raises(IntegrityError, match="ck_candidate_registry_id_is_cited"):
            await db_session.flush()

    @pytest.mark.parametrize(
        "fields",
        [
            pytest.param({}, id="no registry_id key"),
            pytest.param({"registry_id": None}, id="registry_id is json null"),
            pytest.param({"registry_id": {"value": "1234567"}}, id="value without any citation"),
            pytest.param({"registry_id": {**_CITATION, "value": None}}, id="value is json null"),
            pytest.param(
                {"registry_id": {**_CITATION, "value": 1234567}}, id="value is not a string"
            ),
            pytest.param({"registry_id": _without(_CITED_ID, "source_url")}, id="no source_url"),
            pytest.param(
                {"registry_id": {**_CITED_ID, "source_url": None}}, id="source_url is json null"
            ),
            pytest.param(
                {"registry_id": {**_CITED_ID, "source_url": "  "}}, id="source_url is blank"
            ),
            pytest.param(
                {"registry_id": _without(_CITED_ID, "retrieved_at")}, id="no retrieved_at"
            ),
            pytest.param(
                {"registry_id": _without(_CITED_ID, "retrieval_method")}, id="no retrieval_method"
            ),
        ],
    )
    async def test_an_uncited_registry_id_is_refused(
        self, db_session: AsyncSession, fields: dict[str, object]
    ) -> None:
        """The CHECK must refuse what it is named for — a registry id nobody can cite (E10).

        A comparison against a missing key is NULL, and a CHECK passes on NULL, so each of these
        rows was accepted before the constraint was made null-safe.
        """
        run = await _a_run(db_session)

        db_session.add(Candidate(run_id=run.id, registry_id="1234567", fields=fields))
        with pytest.raises(IntegrityError, match="ck_candidate_registry_id_is_cited"):
            await db_session.flush()

    async def test_a_running_run_cannot_have_a_finish_time(self, db_session: AsyncSession) -> None:
        run = await _a_run(db_session)

        with pytest.raises(IntegrityError, match="ck_sourcing_run_finished_at_matches_status"):
            await db_session.execute(
                text("update sourcing_run set finished_at = now() where id = :id"),
                {"id": run.id},
            )

    async def test_a_finished_run_must_have_a_finish_time(self, db_session: AsyncSession) -> None:
        run = await _a_run(db_session)

        with pytest.raises(IntegrityError, match="ck_sourcing_run_finished_at_matches_status"):
            await db_session.execute(
                text("update sourcing_run set status = 'completed' where id = :id"),
                {"id": run.id},
            )

    async def test_an_unknown_status_is_refused(self, db_session: AsyncSession) -> None:
        run = await _a_run(db_session)

        # `finished_at` is set too, so the finish-time check holds and only this one can fire.
        with pytest.raises(IntegrityError, match="ck_sourcing_run_status"):
            await db_session.execute(
                text(
                    "update sourcing_run set status = 'Completed', finished_at = now() "
                    "where id = :id"
                ),
                {"id": run.id},
            )

    async def test_a_run_must_reference_a_real_manifest(self, db_session: AsyncSession) -> None:
        db_session.add(
            SourcingRun(
                manifest_id=uuid4(),
                vertical="freight",
                geography="dfw",
                icp_band={},
                status=RunStatus.running.value,
            )
        )
        with pytest.raises(IntegrityError):
            await db_session.flush()

    async def test_candidates_are_stored_in_their_own_table(self, db_session: AsyncSession) -> None:
        """A smoke check that the ORM and the migration agree on the table name."""
        run = await _a_run(db_session)
        await SourcingRepository(db_session).upsert_candidate(run.id, a_candidate())

        count = (
            await db_session.execute(select(Candidate.id).where(Candidate.run_id == run.id))
        ).all()
        assert len(count) == 1
