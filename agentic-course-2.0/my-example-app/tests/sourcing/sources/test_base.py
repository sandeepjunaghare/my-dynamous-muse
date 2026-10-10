"""Binding a manifest's declared sources to adapters, by ``base_url`` — no database."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.manifests.schemas import ManifestBody, ManifestResponse, ManifestStatus
from app.sourcing.exceptions import NoRegistrySourceError
from app.sourcing.sources.base import (
    DiscoveryFactory,
    DiscoveryPull,
    SourceEnv,
    bind_sources,
    dataset_matcher,
    normalise_dataset_url,
)
from app.sourcing.sources.fmcsa import FMCSA_ADAPTERS
from tests.sourcing.builders import (
    LEGACY_REVOCATIONS_URL,
    a_fire_body,
    a_freight_body,
)
from tests.sourcing.conftest import a_settings


def _manifest(body: ManifestBody) -> ManifestResponse:
    return ManifestResponse(
        id=uuid4(),
        vertical="test_vertical",
        version=1,
        status=ManifestStatus.active,
        body=body,
        created_at=datetime(2026, 10, 10, tzinfo=UTC),
        activated_at=datetime(2026, 10, 10, tzinfo=UTC),
        activated_by="test",
    )


class StubRegistry:
    """A discovery source for a registry FMCSA knows nothing about."""

    def __init__(self, name: str, env: SourceEnv) -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    async def pull(self, rules: object) -> DiscoveryPull:
        return DiscoveryPull(records=())


class TestNormalise:
    @pytest.mark.parametrize(
        "url",
        [
            "https://data.transportation.gov/d/az4n-8mr2",
            "https://data.transportation.gov/resource/az4n-8mr2.json",
            "https://data.transportation.gov/api/views/az4n-8mr2",
            "https://DATA.Transportation.gov/d/az4n-8mr2/",
        ],
    )
    def test_every_socrata_form_is_one_identity(self, url: str) -> None:
        assert normalise_dataset_url(url) == "data.transportation.gov/az4n-8mr2"

    def test_other_urls_keep_their_path_without_a_trailing_slash(self) -> None:
        assert (
            normalise_dataset_url("https://mobile.fmcsa.dot.gov/qc/services/")
            == "mobile.fmcsa.dot.gov/qc/services"
        )


class TestBindFreight:
    async def test_census_qcmobile_and_motus_bind_and_authority_history_does_not(
        self, source_env: SourceEnv
    ) -> None:
        bound = bind_sources(_manifest(a_freight_body()), FMCSA_ADAPTERS, source_env)
        assert bound.discovery.name == "fmcsa_company_census"
        assert [lookup.name for lookup in bound.lookups] == ["fmcsa_qcmobile"]
        assert [join.name for join in bound.joins] == ["fmcsa_revocations"]
        assert bound.unbound == ("fmcsa_authority_history",)
        assert bound.degraded == ()

    async def test_freight_v3s_frozen_legacy_revocations_file_is_not_bound(
        self, source_env: SourceEnv
    ) -> None:
        """A frozen list reads as "no revocations" — the wrong answer that looks right."""
        body = a_freight_body(revocations_url=LEGACY_REVOCATIONS_URL)
        bound = bind_sources(_manifest(body), FMCSA_ADAPTERS, source_env)
        assert bound.joins == ()
        assert "fmcsa_revocations" in bound.unbound

    @pytest.mark.parametrize("webkey", [None, "", "   "])
    async def test_no_webkey_degrades_qcmobile_rather_than_failing(
        self, source_env: SourceEnv, webkey: str | None
    ) -> None:
        env = SourceEnv(
            client=source_env.client,
            settings=a_settings(fmcsa_webkey=webkey),
            clock=source_env.clock,
        )
        bound = bind_sources(_manifest(a_freight_body()), FMCSA_ADAPTERS, env)
        assert bound.lookups == ()
        assert bound.degraded == ("fmcsa_qcmobile: FMCSA_WEBKEY is not set",)


class TestBindRefusals:
    async def test_fire_with_only_the_fmcsa_adapters_has_no_registry(
        self, source_env: SourceEnv
    ) -> None:
        """And Places, declared by fire, never binds as a discovery source (D13 rule 3)."""
        with pytest.raises(NoRegistrySourceError, match="binds 0"):
            bind_sources(_manifest(a_fire_body()), FMCSA_ADAPTERS, source_env)

    async def test_fire_with_a_stub_for_its_registry_binds_and_places_stays_unbound(
        self, source_env: SourceEnv
    ) -> None:
        stub = DiscoveryFactory(
            matches=dataset_matcher("www.tdi.texas.gov/fire/fmlicense.html"), build=StubRegistry
        )
        bound = bind_sources(_manifest(a_fire_body()), (*FMCSA_ADAPTERS, stub), source_env)
        assert bound.discovery.name == "tx_fire_marshal"
        assert bound.unbound == ("places",)

    async def test_two_discovery_sources_are_refused(self, source_env: SourceEnv) -> None:
        anything = DiscoveryFactory(matches=lambda source: True, build=StubRegistry)
        with pytest.raises(NoRegistrySourceError, match="binds 4") as raised:
            bind_sources(_manifest(a_freight_body()), (anything,), source_env)
        assert len(raised.value.bound) == 4
