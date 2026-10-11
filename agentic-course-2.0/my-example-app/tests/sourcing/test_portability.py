"""M9 portability: the same ``search_registry`` stage sources fire from fire's own registry."""

from collections.abc import Sequence
from datetime import date
from typing import ClassVar

import pytest

from app.manifests.schemas import DisqualifierRule
from app.shared.provenance import ProvenancedValue, RetrievalMethod
from app.sourcing.pipeline import SourcingPipeline
from app.sourcing.schemas import CandidateFields, RunStatus, SourceRecord, SourceRow
from app.sourcing.service import SourcingService
from app.sourcing.sources.base import (
    DiscoveryFactory,
    DiscoveryPull,
    PoolRecord,
    SourceEnv,
    dataset_matcher,
)
from app.sourcing.sources.fmcsa import FMCSA_ADAPTERS
from app.tools.search_registry import SearchRegistryStage
from tests.conftest import requires_db
from tests.sourcing.builders import a_brief, a_fire_body, an_active_manifest_with
from tests.sourcing.conftest import RUN_CLOCK, MockFmcsa, SessionFactory, a_clock

FIRE_REGISTRY = "https://www.tdi.texas.gov/fire/fmlicense.html"


class StubFireMarshal:
    """The fire vertical's registry, stubbed. It records the rules it was handed."""

    received: ClassVar[list[Sequence[DisqualifierRule]]] = []

    def __init__(self, name: str, env: SourceEnv) -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    async def pull(self, rules: Sequence[DisqualifierRule]) -> DiscoveryPull:
        StubFireMarshal.received.append(rules)
        records: list[PoolRecord] = []
        for license_no in ("FPL-0042", "FPL-0007"):
            url = f"{FIRE_REGISTRY}?license={license_no}"

            def cite[T](value: T, url: str = url) -> ProvenancedValue[T]:
                return ProvenancedValue(
                    value=value,
                    source_url=url,
                    retrieved_at=RUN_CLOCK,
                    retrieval_method=RetrievalMethod.registry_api,
                )

            row = SourceRow(values=(("license_number", license_no),))
            records.append(
                PoolRecord(
                    registry_id=f"txfm:{license_no}",
                    native_id=license_no,
                    fields=CandidateFields(
                        registry_id=cite(f"txfm:{license_no}"),
                        legal_name=cite(f"Sprinkler Co {license_no}"),
                        source_records=(cite(SourceRecord(source=self._name, rows=(row,))),),
                    ),
                    currency_date=date(2026, 1, 1),
                    has_principal=True,
                )
            )
        return DiscoveryPull(records=tuple(sorted(records, key=lambda r: r.registry_id)))


@requires_db
class TestPortability:
    async def test_the_same_stage_sources_fire_from_its_own_registry_with_no_code_change(
        self, session_factory: SessionFactory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """M9: vertical #2 is a manifest, not a slice. The stage class is FMCSA's, unchanged; only
        the adapter table gains an entry for fire's registry URL — and the mock FMCSA, which
        raises on any request, is never touched. Places, which fire declares, never discovers."""
        monkeypatch.setenv("FMCSA_WEBKEY", "")
        StubFireMarshal.received.clear()
        async with session_factory() as session:
            _, vertical = await an_active_manifest_with(session, a_fire_body())
            await session.commit()
        stub = DiscoveryFactory(
            matches=dataset_matcher("www.tdi.texas.gov/fire/fmlicense.html"),
            build=StubFireMarshal,
        )
        fmcsa = MockFmcsa()
        stage = SearchRegistryStage(
            factories=(*FMCSA_ADAPTERS, stub),
            transport=fmcsa.transport(),
            clock=a_clock(),
            batch_size=10,
            backoff_seconds=0.0,
            throttle=False,
        )

        run = await SourcingPipeline(session_factory, stages=[stage]).run(a_brief(vertical))

        assert run.status is RunStatus.completed
        async with session_factory() as session:
            candidates = await SourcingService(session).list_candidates(run.id)
        assert [candidate.registry_id for candidate in candidates] == [
            "txfm:FPL-0007",
            "txfm:FPL-0042",
        ]
        assert fmcsa.requests == []
        (rules,) = StubFireMarshal.received
        assert [rule.id for rule in rules] == ["national_rollup"]
