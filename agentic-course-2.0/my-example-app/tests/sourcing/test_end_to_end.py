"""All five stages over one run: the census, Places, the judgment, the owner and the routes (T6).

The real runner and the real registered stages over a real database. Every outside party is
mocked: FMCSA (census, QCMobile, revocations), Places and the Census Geocoder by
``httpx.MockTransport``, and both judgment nodes by Replay. Four candidates, one per path:

* **ALPHA** — the census names an officer, and Places verifies it: no owner call;
* **BRAVO** — the census officer is refused (a company and a person in one string), Places
  verifies it, and the owner node finds the person on a page it read;
* **CHARLIE** — no officer, and Places finds nothing: unverified, so neither node pays (OQ-5);
* **DELTA** — QCMobile says not allowed to operate: removed by a free predicate, before anything
  paid.
"""

from collections.abc import AsyncIterator, Mapping
from decimal import Decimal
from typing import cast

import pytest
from claude_agent_sdk import ClaudeAgentOptions, Message
from pydantic import JsonValue

import app.routing.service as routing_service
from app.core.cost import BillableKind
from app.manifests.schemas import ManifestBody, SourceKind
from app.qualification import judgment
from app.routing.geocoder import CensusGeocoder
from app.shared.provenance import RetrievalMethod
from app.sourcing.owner import judgment as owner_judgment
from app.sourcing.pipeline import SourcingPipeline
from app.sourcing.schemas import CandidateFields, RunStatus
from app.sourcing.service import SourcingService
from app.tools import resolve_owner, verify_business
from app.tools.classify_rollup import STAGE as CLASSIFY_ROLLUP
from app.tools.cluster_routes import STAGE as CLUSTER_ROUTES
from app.tools.registry import Stage
from app.tools.resolve_owner import STAGE as RESOLVE_OWNER
from app.tools.verify_business import VerifyBusinessStage
from tests.conftest import requires_db
from tests.manifests.replay import Replay, StreamLine, result_line, web_fetch_call, web_fetch_result
from tests.routing.conftest import MockCensus, a_match, json_answer
from tests.sourcing.builders import (
    PLACES_SOURCE_URL,
    a_brief,
    a_freight_body,
    a_source,
    an_active_manifest_with,
)
from tests.sourcing.conftest import (
    TEST_PLACES_KEY,
    TEST_WEBKEY,
    MockFmcsa,
    MockPlaces,
    MockWeb,
    SessionFactory,
    a_clock,
    census_rows,
    load_fixture,
    routed,
)
from tests.sourcing.test_pipeline import registry_stage

pytestmark = requires_db

ALPHA, BRAVO, CHARLIE, DELTA = (f"usdot:300000{n}" for n in range(1, 5))
STREET = "2100 Olympic Dr"
ABOUT = "https://www.bravofreight.test/about"
LISTED = "Hub of the Metroplex"
"""In every Places display name: what must reach the judgment prompt, and never a stored row."""


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keys and caps for the run, never whatever ``.env`` holds."""
    monkeypatch.setenv("FMCSA_WEBKEY", TEST_WEBKEY)
    monkeypatch.setenv("SOCRATA_APP_TOKEN", "")
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", TEST_PLACES_KEY)
    monkeypatch.setenv("PLACES_TEXT_SEARCH_USD", "0.032")


def _row(dot: str, name: str, **changes: str) -> dict[str, str]:
    """A census row shaped like the fixture's first (a DFW broker), as a different business."""
    row = {
        k: v for k, v in census_rows()[0].items() if k not in ("email_address", "company_officer_1")
    }
    row.update(dot_number=dot, legal_name=f"{name} LOGISTICS LLC", docket1=f"9{dot}", **changes)
    return row


def _carrier(dot: str, **changes: object) -> tuple[int, object]:
    body = cast(dict[str, dict[str, dict[str, object]]], load_fixture("qcmobile_carrier"))
    body["content"]["carrier"]["dotNumber"] = int(dot)
    body["content"]["carrier"].update(changes)
    return 200, body


def _found(name: str) -> tuple[int, object]:
    return (
        200,
        {
            "places": [
                {
                    "id": f"ChIJ-{name.lower()}",
                    "displayName": {"text": f"{name} Logistics {LISTED}", "languageCode": "en"},
                    "formattedAddress": f"{STREET}, Arlington, TX 76011, USA",
                    "businessStatus": "OPERATIONAL",
                    "types": ["moving_company", "establishment"],
                }
            ]
        },
    )


def _body() -> ManifestBody:
    base = a_freight_body()
    places = a_source("places", SourceKind.web_lookup, PLACES_SOURCE_URL)
    return base.model_copy(update={"sources": (*base.sources, places)})


class _ByRegistryId:
    """Replays a different recording per candidate, found by its id in the prompt."""

    def __init__(self, streams: Mapping[str, list[StreamLine]]) -> None:
        self._streams = streams
        self.prompts: list[str] = []

    def __call__(self, prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Message]:
        self.prompts.append(prompt)
        registry_id = next(rid for rid in self._streams if f"registry id: {rid} " in prompt)
        return Replay(self._streams[registry_id])(prompt, options)


NOTHING_FIRED: list[StreamLine] = [result_line({"rules": [], "signals": []})]
GRIGGS_ANSWER: JsonValue = {
    "name": "Bryan Griggs",
    "title": "President",
    "reason": "",
    "citation": {"url": ABOUT, "quote": "Bryan Griggs, President"},
}
GRIGGS: list[StreamLine] = [
    web_fetch_call("t1", ABOUT),
    web_fetch_result("t1", ABOUT),
    result_line(GRIGGS_ANSWER),
]


async def test_four_candidates_through_all_five_stages(
    session_factory: SessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with session_factory() as session:
        _, vertical = await an_active_manifest_with(session, _body())
        await session.commit()

    fmcsa = MockFmcsa(
        census=[
            _row("3000001", "ALPHA", company_officer_1="JANE ALPHA"),
            _row(
                "3000002", "BRAVO", company_officer_1="GUNNER CAPITAL PARTNERS LLC - BRYAN GRIGGS"
            ),
            _row("3000003", "CHARLIE"),
            _row("3000004", "DELTA"),
        ],
        revocations=[],
    )
    fmcsa.carriers["3000004"] = _carrier("3000004", allowedToOperate="N")

    places = MockPlaces(
        answers={"ALPHA": _found("Alpha"), "BRAVO": _found("Bravo")}, default=(200, {})
    )
    verify = VerifyBusinessStage(
        transport=routed(places, MockWeb()), clock=a_clock(), backoff_seconds=0.0, throttle=False
    )

    census = MockCensus()
    census.on(STREET, json_answer(200, a_match(32.73, -97.11, "76011")))

    def mock_geocoder(*args: object, **kwargs: object) -> CensusGeocoder:
        return census.geocoder()

    monkeypatch.setattr(routing_service, "CensusGeocoder", mock_geocoder)

    rollup = _ByRegistryId({ALPHA: NOTHING_FIRED, BRAVO: NOTHING_FIRED})
    owner = _ByRegistryId({BRAVO: GRIGGS})
    monkeypatch.setattr(judgment, "sdk_runner", rollup)
    monkeypatch.setattr(owner_judgment, "sdk_runner", owner)

    stages: list[Stage] = [
        registry_stage(fmcsa, batch_size=10),
        verify,
        CLASSIFY_ROLLUP,
        RESOLVE_OWNER,
        CLUSTER_ROUTES,
    ]
    try:
        run = await SourcingPipeline(session_factory, stages=stages).run(a_brief(vertical))
    finally:
        await census.aclose()

    assert run.status is RunStatus.completed, run.status_detail
    assert set(verify_business.COUNT_KEYS) <= set(run.counts)
    assert set(resolve_owner.COUNT_KEYS) <= set(run.counts)
    assert {"qualification_disqualified", "classified", "routing_clustered"} <= set(run.counts)

    # Free before paid: DELTA reached no paid step, CHARLIE no judgment and no owner call.
    assert len(places.requests) == 3
    assert not any("DELTA" in query for query in places.queries())
    assert len(rollup.prompts) == 2
    assert len(owner.prompts) == 1
    assert run.counts["owner_from_registry"] == 1
    assert run.counts["owner_resolved"] == 1
    assert run.counts["judgment_unverified_skipped"] == 1
    assert run.counts["owner_unverified_skipped"] == 1

    # The Places channel carried the listing to the judgment, and nowhere else.
    assert all(LISTED in prompt for prompt in rollup.prompts)
    assert not any(LISTED in prompt for prompt in owner.prompts)

    assert run.cost.calls[BillableKind.places_lookup] == 3
    assert run.cost.usd[BillableKind.places_lookup] == Decimal("0.096")
    assert run.cost.usd[BillableKind.anthropic_tokens] == Decimal("0.3")  # 2 judgment + 1 owner

    async with session_factory() as session:
        candidates = await SourcingService(session).list_candidates(run.id)
    fields: dict[str, CandidateFields] = {c.registry_id: c.fields for c in candidates}
    assert set(fields) == {ALPHA, BRAVO, CHARLIE, DELTA}

    alpha_owner, bravo_owner = fields[ALPHA].owner, fields[BRAVO].owner
    assert alpha_owner is not None and alpha_owner.retrieval_method is RetrievalMethod.bulk_file
    assert bravo_owner is not None
    assert (bravo_owner.value.name, bravo_owner.source_url) == ("Bryan Griggs", ABOUT)
    assert bravo_owner.retrieval_method is RetrievalMethod.llm_inference
    assert fields[CHARLIE].owner is None
    assert fields[DELTA].owner is None

    # D13: no Places content in any stored row.
    for stored in fields.values():
        assert LISTED not in stored.model_dump_json()
