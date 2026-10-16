"""Stage 2 over a real run: the Places check stores the place ID and nothing else (D13), the website
comes from a free fetch, the breaker degrades, and a missing source refuses (A3).

Every request goes to :class:`MockPlaces` or :class:`MockWeb`, each of which raises on anything it
does not know: no Places key and no network are needed.
"""

import json
from collections.abc import Sequence
from decimal import Decimal
from typing import cast
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cost import BillableKind, RunCost
from app.manifests.schemas import ManifestBody, RuleOperator, SourceKind
from app.qualification.service import QualificationService
from app.shared.provenance import RetrievalMethod
from app.sourcing.exceptions import SourceUnavailableError
from app.sourcing.pool import PoolRepository
from app.sourcing.schemas import CandidateFields, CandidateResponse, MissKind, PostalAddress
from app.sourcing.service import SourcingService
from app.sourcing.sources.fmcsa import census_pool_record
from app.sourcing.sources.google_places import place_url
from app.sourcing.stages import PipelineStage
from app.tools.registry import StageContext, StageResult, registered_stages
from app.tools.verify_business import COUNT_KEYS, NO_PLACES_SOURCE, VerifyBusinessStage
from tests.conftest import requires_db
from tests.qualification.builders import a_body_with, an_active_manifest_with, census_row, predicate
from tests.sourcing.builders import (
    ARLINGTON,
    PLACES_SOURCE_URL,
    REGISTRY,
    a_brief,
    a_source,
    a_verdict,
    sourced,
)
from tests.sourcing.conftest import (
    RUN_CLOCK,
    TEST_PLACES_KEY,
    MockPlaces,
    MockWeb,
    a_clock,
    load_fixture,
    routed,
)

FREE_RULE = predicate("no_broker_entity_type", "carship", RuleOperator.not_contains, "B")
STREET_ADDRESS = "2100 Olympic Dr, Arlington, TX 76011, USA"
"""Places' formatted address for every fixture business — what must never reach a stored row."""


def _body(*, places: bool = True) -> ManifestBody:
    body = a_body_with(FREE_RULE)
    if not places:
        return body
    return body.model_copy(
        update={
            "sources": (
                *body.sources,
                a_source("places", SourceKind.web_lookup, PLACES_SOURCE_URL),
            )
        }
    )


def _candidate(name: str, *, email: str | None = None) -> CandidateFields:
    """A census-shaped candidate at the shared Arlington address, named ``{name} Freight``."""
    return CandidateFields(
        registry_id=sourced(f"usdot:{name.lower()}"),
        legal_name=sourced(f"{name} Holdings LLC"),
        dba_name=sourced(f"{name} Freight"),
        address=sourced(ARLINGTON),
        email=sourced(email) if email else None,
    )


def _found(place_id: str, display_name: str, status: str = "OPERATIONAL") -> tuple[int, object]:
    return (
        200,
        {
            "places": [
                {
                    "id": place_id,
                    "displayName": {"text": display_name, "languageCode": "en"},
                    "formattedAddress": STREET_ADDRESS,
                    "businessStatus": status,
                    "types": ["moving_company", "establishment"],
                }
            ]
        },
    )


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Places key unless a test removes it; never whatever ``.env`` holds."""
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", TEST_PLACES_KEY)
    monkeypatch.setenv("MAX_PLACES_CALLS_PER_RUN", "500")
    monkeypatch.setenv("PLACES_TEXT_SEARCH_USD", "0.032")


def _stage(
    places: MockPlaces, web: MockWeb, *, allow_degraded: bool = False
) -> VerifyBusinessStage:
    return VerifyBusinessStage(
        transport=routed(places, web),
        clock=a_clock(),
        backoff_seconds=0.0,
        throttle=False,
        allow_degraded=allow_degraded,
    )


async def _setup(
    session: AsyncSession, *candidates: CandidateFields, places: bool = True
) -> tuple[StageContext, list[CandidateResponse]]:
    manifest = await an_active_manifest_with(session, _body(places=places))
    service = SourcingService(session)
    run = await service.start_run(a_brief(manifest.vertical))
    recorded = await service.record_candidates(run.id, list(candidates), stage=REGISTRY)
    context = StageContext(run_id=run.id, manifest=manifest, session=session, cost=RunCost())
    return context, recorded


async def _stored(context: StageContext) -> dict[str, CandidateFields]:
    candidates = await SourcingService(context.session).list_candidates(context.run_id)
    return {candidate.fields.registry_id.value: candidate.fields for candidate in candidates}


async def _run(stage: VerifyBusinessStage, context: StageContext) -> StageResult:
    return await stage.run(context)


def test_the_stage_is_registered_second() -> None:
    stages = [stage.stage for stage in registered_stages()]
    assert stages.index(PipelineStage.verify_business) == 1


@requires_db
class TestTheCheck:
    async def test_a_match_stores_the_place_id_and_the_census_address_and_nothing_else(
        self, db_session: AsyncSession
    ) -> None:
        places = MockPlaces(answers={"Alpha Freight": _found("ChIJ-alpha", "Alpha Freight Co")})
        context, _ = await _setup(db_session, _candidate("Alpha"))

        result = await _run(_stage(places, MockWeb()), context)

        stored = (await _stored(context))["usdot:alpha"]
        check = stored.business_check
        assert check is not None
        assert check.value.place_id == "ChIJ-alpha"
        assert check.value.checked_address == ARLINGTON
        assert check.source_url == "https://places.googleapis.com/v1/places/ChIJ-alpha"
        assert check.retrieval_method is RetrievalMethod.web_lookup
        assert check.retrieved_at == RUN_CLOCK
        assert stored.has_verified_address() is True
        dumped = json.dumps(stored.to_stored())
        assert "Alpha Freight Co" not in dumped, "the Places display name is never stored"
        assert STREET_ADDRESS not in dumped, "the Places formatted address is never stored"
        assert "moving_company" not in dumped, "the Places types are never stored"
        assert result.counts["places_verified"] == 1
        assert result.degraded_reason is None

    async def test_the_channel_holds_evidence_for_the_matched_candidate_only(
        self, db_session: AsyncSession
    ) -> None:
        places = MockPlaces(
            answers={
                "Alpha Freight": _found("ChIJ-alpha", "Alpha Freight Co"),
                "Bravo Freight": (200, load_fixture("places_no_match")),
            }
        )
        context, _ = await _setup(db_session, _candidate("Alpha"), _candidate("Bravo"))

        result = await _run(_stage(places, MockWeb()), context)

        assert set(context.places) == {"usdot:alpha"}
        evidence = context.places["usdot:alpha"]
        assert evidence.display_name == "Alpha Freight Co"
        assert evidence.business_status == "OPERATIONAL"
        assert result.counts["places_not_found"] == 1
        assert (await _stored(context))["usdot:bravo"].business_check is None

    async def test_a_disqualified_candidate_is_never_sent(self, db_session: AsyncSession) -> None:
        """Free before paid (D8): nothing paid touches a candidate a free rule removed."""
        places = MockPlaces(answers={"Bravo Freight": _found("ChIJ-bravo", "Bravo Freight")})
        context, recorded = await _setup(db_session, _candidate("Alpha"), _candidate("Bravo"))
        run = await SourcingService(db_session).get_run(context.run_id)
        await QualificationService(db_session).disqualify_by_predicates(
            recorded[0], run, context.manifest, {"fmcsa": census_row(carship="C")}
        )

        await _run(_stage(places, MockWeb()), context)

        assert places.queries() == ["Bravo Freight, 2100 Olympic Dr, Arlington, TX 76011"]

    async def test_a_retry_makes_no_places_call(self, db_session: AsyncSession) -> None:
        places = MockPlaces(answers={"Alpha Freight": _found("ChIJ-alpha", "Alpha Freight")})
        context, _ = await _setup(db_session, _candidate("Alpha"))
        await _run(_stage(places, MockWeb()), context)
        assert len(places.requests) == 1

        retry = await _run(_stage(places, MockWeb()), context)

        assert len(places.requests) == 1
        assert retry.counts["places_skipped"] == 1
        assert context.cost.total_calls(BillableKind.places_lookup) == 1

    async def test_a_permanently_closed_match_stores_no_check_and_passes_no_evidence(
        self, db_session: AsyncSession
    ) -> None:
        places = MockPlaces(
            answers={"Alpha Freight": _found("ChIJ-x", "Alpha Freight", "CLOSED_PERMANENTLY")}
        )
        context, _ = await _setup(db_session, _candidate("Alpha"))

        result = await _run(_stage(places, MockWeb()), context)

        assert result.counts["places_closed"] == 1
        assert (await _stored(context))["usdot:alpha"].business_check is None
        assert context.places == {}

    async def test_a_temporarily_closed_match_is_still_a_match(
        self, db_session: AsyncSession
    ) -> None:
        places = MockPlaces(
            answers={"Alpha Freight": _found("ChIJ-x", "Alpha Freight", "CLOSED_TEMPORARILY")}
        )
        context, _ = await _setup(db_session, _candidate("Alpha"))
        result = await _run(_stage(places, MockWeb()), context)
        assert result.counts["places_verified"] == 1

    async def test_a_candidate_with_no_address_is_not_checkable(
        self, db_session: AsyncSession
    ) -> None:
        places = MockPlaces()
        bare = _candidate("Alpha").model_copy(update={"address": None})
        context, _ = await _setup(db_session, bare)
        result = await _run(_stage(places, MockWeb()), context)
        assert result.counts["places_not_checkable"] == 1
        assert places.requests == []

    async def test_run_cost_counts_every_request_made(self, db_session: AsyncSession) -> None:
        places = MockPlaces(
            answers={
                "Alpha Freight": _found("ChIJ-alpha", "Alpha Freight"),
                "Bravo Freight": (200, load_fixture("places_empty")),
                "Charlie Freight": (500, {}),
            }
        )
        context, _ = await _setup(
            db_session, _candidate("Alpha"), _candidate("Bravo"), _candidate("Charlie")
        )

        result = await _run(_stage(places, MockWeb()), context)

        # Charlie's 500 is retried inside one charged call; the breaker counts attempts once.
        assert context.cost.total_calls(BillableKind.places_lookup) == 3
        assert context.cost.total_usd(BillableKind.places_lookup) == Decimal("0.096")
        assert result.counts["places_failed"] == 1
        assert result.degraded_reason is not None
        assert "1 Places check(s) failed" in result.degraded_reason


@requires_db
class TestTheBreaker:
    async def test_the_cap_stops_places_degrades_and_the_website_step_goes_on(
        self, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MAX_PLACES_CALLS_PER_RUN", "2")
        places = MockPlaces(
            answers={
                name: _found(f"ChIJ-{name}", f"{name} Freight")
                for name in ("Alpha", "Bravo", "Charlie")
            }
        )
        web = MockWeb()
        web.html("https://charlie.test/")
        context, _ = await _setup(
            db_session,
            _candidate("Alpha"),
            _candidate("Bravo"),
            _candidate("Charlie", email="ops@charlie.test"),
        )

        result = await _run(_stage(places, web), context)

        assert len(places.requests) == 2
        assert (
            result.degraded_reason == "Places breaker reached at 2 calls; 1 candidate(s) unchecked"
        )
        stored = await _stored(context)
        assert stored["usdot:charlie"].business_check is None
        assert stored["usdot:charlie"].website is not None, "the free step still runs"
        assert context.cost.total_calls(BillableKind.places_lookup) == 2


@requires_db
class TestTheRefusal:
    """A3 and PR #21 M1: a batch is never spent without its evidence unless someone said so."""

    async def test_no_places_source_declared_refuses_with_no_request(
        self, db_session: AsyncSession
    ) -> None:
        places = MockPlaces()
        context, _ = await _setup(db_session, _candidate("Alpha"), places=False)

        with pytest.raises(SourceUnavailableError, match="--allow-degraded"):
            await _run(_stage(places, MockWeb()), context)
        assert places.requests == []

    async def test_with_allow_degraded_it_finishes_degraded_and_fills_websites(
        self, db_session: AsyncSession
    ) -> None:
        places = MockPlaces()
        web = MockWeb()
        web.html("https://alpha.test/")
        context, _ = await _setup(
            db_session, _candidate("Alpha", email="ops@alpha.test"), places=False
        )

        result = await _run(_stage(places, web, allow_degraded=True), context)

        assert result.degraded_reason == NO_PLACES_SOURCE
        assert places.requests == []
        assert (await _stored(context))["usdot:alpha"].website is not None

    @pytest.mark.parametrize("key", ["", "   "])
    async def test_no_key_refuses_the_same_way(
        self, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, key: str
    ) -> None:
        monkeypatch.setenv("GOOGLE_PLACES_API_KEY", key)
        places = MockPlaces()
        context, _ = await _setup(db_session, _candidate("Alpha"))

        with pytest.raises(SourceUnavailableError, match="GOOGLE_PLACES_API_KEY"):
            await _run(_stage(places, MockWeb()), context)
        assert places.requests == []

    async def test_a_bad_key_mid_batch_refuses(self, db_session: AsyncSession) -> None:
        places = MockPlaces(default=(400, load_fixture("places_bad_key")))
        context, _ = await _setup(db_session, _candidate("Alpha"), _candidate("Bravo"))

        with pytest.raises(SourceUnavailableError, match="refused the API key"):
            await _run(_stage(places, MockWeb()), context)
        assert len(places.requests) == 1

    async def test_a_bad_key_with_the_flag_stops_asking_and_degrades(
        self, db_session: AsyncSession
    ) -> None:
        places = MockPlaces(default=(400, load_fixture("places_bad_key")))
        context, _ = await _setup(db_session, _candidate("Alpha"), _candidate("Bravo"))

        result = await _run(_stage(places, MockWeb(), allow_degraded=True), context)

        assert len(places.requests) == 1, "the key will not get better on the next candidate"
        assert result.degraded_reason is not None
        assert "refused the API key" in result.degraded_reason


@requires_db
class TestTheWebsite:
    async def test_the_homepage_of_the_email_domain_is_the_website(
        self, db_session: AsyncSession
    ) -> None:
        web = MockWeb()
        web.redirect("https://alpha.test/", "https://www.alpha.test/")
        web.html("https://www.alpha.test/")
        context, _ = await _setup(db_session, _candidate("Alpha", email="ops@alpha.test"))

        result = await _run(_stage(MockPlaces(), web), context)

        website = (await _stored(context))["usdot:alpha"].website
        assert website is not None
        assert website.value == "https://www.alpha.test"
        assert website.source_url == "https://www.alpha.test/"
        assert website.retrieval_method is RetrievalMethod.web_lookup
        assert result.counts["website_found"] == 1

    async def test_free_mail_and_a_shared_domain_are_never_fetched(
        self, db_session: AsyncSession
    ) -> None:
        context, _ = await _setup(
            db_session,
            _candidate("Alpha", email="owner@gmail.com"),
            _candidate("Bravo", email="agent@agents.test"),
        )
        # Three pool businesses on one domain make it a service's, not anyone's own site.
        rows = [
            {**census_row(dot_number=str(n)), "email_address": "agent@agents.test"}
            for n in (1, 2, 3)
        ]
        await _seed_pool(db_session, context, rows)
        web = MockWeb()

        result = await _run(_stage(MockPlaces(), web), context)

        assert web.requests == []
        assert result.counts["website_no_domain"] == 2

    async def test_an_unreachable_homepage_is_counted_and_the_batch_goes_on(
        self, db_session: AsyncSession
    ) -> None:
        web = MockWeb()
        web.status("https://alpha.test/", 404)
        context, _ = await _setup(db_session, _candidate("Alpha", email="ops@alpha.test"))
        places = MockPlaces(answers={"Alpha Freight": _found("ChIJ-alpha", "Alpha Freight")})
        result = await _run(_stage(places, web), context)
        assert result.counts["website_unreachable"] == 1
        assert result.counts["places_verified"] == 1

    async def test_a_homepage_on_the_email_domain_verifies_without_places(
        self, db_session: AsyncSession
    ) -> None:
        """Website-verification B1/B2: the second way to be verified."""
        web = MockWeb()
        web.html("https://alpha.test/")
        places = MockPlaces(answers={"Alpha Freight": (200, load_fixture("places_no_match"))})
        context, _ = await _setup(db_session, _candidate("Alpha", email="ops@alpha.test"))

        result = await _run(_stage(places, web), context)

        stored = (await _stored(context))["usdot:alpha"]
        assert stored.business_check is None
        assert stored.is_verified()
        assert result.counts["verify_website_only"] == 1

    async def test_a_places_verified_candidate_is_not_counted_website_only(
        self, db_session: AsyncSession
    ) -> None:
        web = MockWeb()
        web.html("https://alpha.test/")
        places = MockPlaces(answers={"Alpha Freight": _found("ChIJ-alpha", "Alpha Freight")})
        context, _ = await _setup(db_session, _candidate("Alpha", email="ops@alpha.test"))
        result = await _run(_stage(places, web), context)
        assert result.counts["verify_website_only"] == 0

    async def test_a_retry_that_changes_nothing_still_counts_website_only(
        self, db_session: AsyncSession
    ) -> None:
        """PR #36 L3: the website is stored and the verdict is current, so nothing is written."""
        web = MockWeb()
        web.html("https://alpha.test/")
        places = MockPlaces(answers={"Alpha Freight": (200, load_fixture("places_mail_drop"))})
        context, _ = await _setup(db_session, _candidate("Alpha", email="ops@alpha.test"))
        await _run(_stage(places, web), context)

        retry = await _run(_stage(places, web), context)

        assert retry.counts["places_skipped"] == 1
        assert retry.counts["verify_website_only"] == 1


VERDICT_FIXTURES = {
    "places_mail_drop": (MissKind.mail_drop, "ChIJ-usps-arlington", "United States Postal Service"),
    "places_no_listing": (MissKind.no_listing, "ChIJ-suite-300", None),
    "places_other_business": (MissKind.other_business, "ChIJ-napco", "Napco Bag & Film"),
}


@requires_db
class TestTheVerdict:
    """Website-verification OQ-1/OQ-2: a miss at the census address records our verdict."""

    @pytest.mark.parametrize("fixture", sorted(VERDICT_FIXTURES))
    async def test_a_miss_at_the_address_stores_our_verdict_and_no_places_content(
        self, db_session: AsyncSession, fixture: str
    ) -> None:
        kind, place_id, display_name = VERDICT_FIXTURES[fixture]
        body = load_fixture(fixture)
        places = MockPlaces(answers={"Alpha Freight": (200, body)})
        context, _ = await _setup(db_session, _candidate("Alpha"))

        result = await _run(_stage(places, MockWeb()), context)

        stored = (await _stored(context))["usdot:alpha"]
        verdict = stored.places_verdict
        assert verdict is not None
        assert verdict.value.kind is kind
        assert verdict.value.place_id == place_id
        assert verdict.value.checked_address == ARLINGTON
        assert verdict.source_url == place_url(place_id)
        assert verdict.retrieval_method is RetrievalMethod.web_lookup
        assert stored.business_check is None
        assert context.places == {}, "a miss passes no evidence on"
        dumped = stored.model_dump_json()
        (listing,) = cast(list[dict[str, object]], cast(dict[str, object], body)["places"])
        assert str(listing["formattedAddress"]) not in dumped
        for kind_name in cast(list[str], listing["types"]):
            assert f'"{kind_name}"' not in dumped, "a Places type is never stored"
        if display_name is not None:
            assert display_name not in dumped
        assert result.counts[f"places_{kind.value}"] == 1
        assert result.counts["places_not_found"] == 1

    async def test_a_miss_at_another_number_stores_no_verdict(
        self, db_session: AsyncSession
    ) -> None:
        places = MockPlaces(
            answers={
                "Alpha Freight": (
                    200,
                    {
                        "places": [
                            {
                                "id": "ChIJ-far",
                                "formattedAddress": "12100 Olympic Dr, Arlington, TX 76011, USA",
                                "types": ["post_office"],
                            }
                        ]
                    },
                )
            }
        )
        context, _ = await _setup(db_session, _candidate("Alpha"))
        result = await _run(_stage(places, MockWeb()), context)
        assert (await _stored(context))["usdot:alpha"].places_verdict is None
        assert result.counts["places_not_found"] == 1
        assert result.counts["places_mail_drop"] == 0

    async def test_a_match_records_no_verdict(self, db_session: AsyncSession) -> None:
        places = MockPlaces(answers={"Alpha Freight": _found("ChIJ-alpha", "Alpha Freight")})
        context, _ = await _setup(db_session, _candidate("Alpha"))
        await _run(_stage(places, MockWeb()), context)
        assert (await _stored(context))["usdot:alpha"].places_verdict is None

    async def test_a_retry_with_a_current_verdict_makes_no_places_call(
        self, db_session: AsyncSession
    ) -> None:
        """B4: a verdict for this address is not paid for twice."""
        places = MockPlaces(answers={"Alpha Freight": (200, load_fixture("places_mail_drop"))})
        context, _ = await _setup(db_session, _candidate("Alpha"))
        await _run(_stage(places, MockWeb()), context)
        assert len(places.requests) == 1

        retry = await _run(_stage(places, MockWeb()), context)

        assert len(places.requests) == 1
        assert retry.counts["places_skipped"] == 1
        assert context.cost.total_calls(BillableKind.places_lookup) == 1

    async def test_a_verdict_for_an_old_address_does_not_stop_a_new_check(
        self, db_session: AsyncSession
    ) -> None:
        moved = PostalAddress(
            street="900 Main St", city="Fort Worth", state="TX", postal_code="76102"
        )
        stale = _candidate("Alpha").model_copy(update={"places_verdict": a_verdict(address=moved)})
        places = MockPlaces(answers={"Alpha Freight": _found("ChIJ-alpha", "Alpha Freight")})
        context, _ = await _setup(db_session, stale)

        result = await _run(_stage(places, MockWeb()), context)

        assert len(places.requests) == 1
        assert result.counts["places_verified"] == 1


@requires_db
class TestARefusedWrite:
    async def test_a_refused_write_is_counted_and_the_batch_goes_on(
        self, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A write the candidate model refuses costs that candidate, never the paid batch, and a
        refused candidate passes no evidence on (PR #18 review, M2)."""
        original = SourcingService.record_candidates

        async def refuses_alpha(
            self: SourcingService,
            run_id: UUID,
            candidates: Sequence[CandidateFields],
            *,
            stage: PipelineStage,
        ) -> list[CandidateResponse]:
            refused = stage is PipelineStage.verify_business and any(
                c.registry_id.value == "usdot:alpha" for c in candidates
            )
            if refused:
                CandidateFields.model_validate({"registry_id": None})
            return await original(self, run_id, candidates, stage=stage)

        monkeypatch.setattr(SourcingService, "record_candidates", refuses_alpha)
        places = MockPlaces(
            answers={
                "Alpha Freight": _found("ChIJ-alpha", "Alpha Freight"),
                "Bravo Freight": _found("ChIJ-bravo", "Bravo Freight"),
            }
        )
        context, _ = await _setup(db_session, _candidate("Alpha"), _candidate("Bravo"))

        result = await _run(_stage(places, MockWeb()), context)

        assert result.counts["verify_write_refused"] == 1
        stored = await _stored(context)
        assert stored["usdot:bravo"].business_check is not None
        assert set(context.places) == {"usdot:bravo"}


async def _seed_pool(
    session: AsyncSession, context: StageContext, rows: list[dict[str, str | None]]
) -> None:
    records = [
        census_pool_record(
            {key: value for key, value in row.items() if value is not None},
            source="fmcsa",
            retrieved_at=RUN_CLOCK,
        )
        for row in rows
    ]
    await PoolRepository(session).refresh(
        context.manifest.vertical, context.run_id, [record for record in records if record]
    )


@pytest.mark.parametrize("kind", list(MissKind))
def test_every_miss_kind_has_its_count_key(kind: MissKind) -> None:
    """PR #36 L4: ``counts`` is pre-seeded, so a kind without a key is a ``KeyError`` mid-batch."""
    assert f"places_{kind.value}" in COUNT_KEYS
