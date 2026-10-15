"""T6's two paid checks against the real services. **Paid; opt-in; never in ``/piv-validate``.**

``LPE_LIVE_EVAL=1 uv run pytest tests/sourcing/test_live_enrichment.py -s`` — about $0.10 an owner
call, plus a Places Text Search per probe. The replay suites prove the wiring and the gates; only
this shows what the owner node actually yields (M6 needs at least 8% on officer-less candidates).

**Filling the fixture.** ``fixtures/officerless_sample.json`` is committed empty and filled locally,
never committed with data in it:

* ``candidates`` — ~10 officer-less, verified candidates exported from a real run, each a
  ``CandidateFields`` JSON (``candidate.fields`` as stored). Each is sent to the owner node; the
  yield, every omission reason and the total cost are printed. Nothing about the yield is asserted:
  the founder reads it, and spot-checks each cited page by hand.
* ``places_probe`` — a few businesses (``CandidateFields`` JSON, with an address) the founder knows
  Google lists. Each must match, and only the place ID would be kept. Needs
  ``GOOGLE_PLACES_API_KEY``.
"""

import json
import os
from pathlib import Path

import httpx
import pytest
from pydantic import BaseModel

from app.core.config import get_settings
from app.core.cost import RunCost, format_usd
from app.sourcing.owner.judgment import admit_owner, run_owner_judgment
from app.sourcing.schemas import CandidateFields, PlaceCheck
from app.sourcing.sources.google_places import PlacesClient, match_place, search_query

SAMPLE = Path(__file__).parent / "fixtures" / "officerless_sample.json"

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.getenv("LPE_LIVE_EVAL") != "1", reason="paid live eval; set LPE_LIVE_EVAL=1 to run"
    ),
]


class _Sample(BaseModel):
    candidates: list[CandidateFields]
    places_probe: list[CandidateFields]


def _sample() -> _Sample:
    return _Sample.model_validate(json.loads(SAMPLE.read_text(encoding="utf-8")))


async def test_the_owner_node_yield_on_officerless_candidates() -> None:
    candidates = _sample().candidates
    if not candidates:
        pytest.skip("no officer-less candidates exported into the sample")
    cost = RunCost()
    named = 0
    for fields in candidates:
        run = await run_owner_judgment(fields, cost=cost)
        admitted = admit_owner(run.answer, reads=run.reads)
        if admitted.owner is not None:
            named += 1
            print(f"  {fields.registry_id.value}: named, cited {admitted.owner.source_url}")
        else:
            print(f"  {fields.registry_id.value}: none — {admitted.omitted}")
    print(f"  yield: {named}/{len(candidates)}; cost: {format_usd(cost.total_usd())}")


async def test_places_matches_known_businesses_and_keeps_only_the_id() -> None:
    probes = _sample().places_probe
    key = get_settings().google_places_api_key
    if not probes or key is None:
        pytest.skip("no Places probes listed, or no GOOGLE_PLACES_API_KEY")
    async with httpx.AsyncClient(timeout=30.0) as client:
        places = PlacesClient(client, api_key=key.get_secret_value(), source="places")
        for fields in probes:
            query = search_query(fields)
            assert query is not None, f"{fields.registry_id.value} has no checkable address"
            match = match_place(fields, await places.search_text(query))
            assert match is not None, f"{fields.registry_id.value} did not match"
            assert fields.address is not None
            kept = PlaceCheck(place_id=match.id, checked_address=fields.address.value)
            assert set(type(kept).model_fields) == {"place_id", "checked_address"}
