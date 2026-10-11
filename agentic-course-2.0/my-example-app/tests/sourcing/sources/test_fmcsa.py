"""The FMCSA adapters against the mock FMCSA — no database, no network."""

from datetime import date

import pytest
from structlog.testing import capture_logs

from app.shared.provenance import RetrievalMethod
from app.sourcing.exceptions import SourceAuthError
from app.sourcing.sources.base import PoolRecord, SourceEnv
from app.sourcing.sources.fmcsa import (
    CensusSource,
    QcMobileSource,
    RevocationsSource,
    census_pool_record,
)
from tests.sourcing.builders import freight_v3_rules
from tests.sourcing.conftest import RUN_CLOCK, TEST_WEBKEY, MockFmcsa, census_rows, load_fixture


def _record(dot: str = "1000001") -> PoolRecord:
    row = next(row for row in census_rows() if row["dot_number"] == dot)
    record = census_pool_record(row, source="fmcsa_company_census", retrieved_at=RUN_CLOCK)
    assert record is not None
    return record


class TestCensusRowMapping:
    def test_identity_fields_are_mapped_and_cited_to_the_row(self) -> None:
        record = _record("1000002")
        fields = record.fields
        assert record.registry_id == "usdot:1000002"
        assert record.native_id == "1000002"
        assert fields.legal_name is not None and fields.legal_name.value == "BRAVO FREIGHT INC"
        assert fields.dba_name is not None and fields.dba_name.value == "BRAVO BROKERAGE"
        assert fields.phone is not None and fields.phone.value == "(817) 555-0102"
        assert fields.address is not None and fields.address.value.city == "ARLINGTON"
        url = "https://data.transportation.gov/resource/az4n-8mr2.json?dot_number=1000002"
        for cited in (fields.registry_id, fields.legal_name, fields.address, fields.phone):
            assert cited.source_url == url
            assert cited.retrieval_method is RetrievalMethod.bulk_file
            assert cited.retrieved_at == RUN_CLOCK

    def test_an_address_missing_any_part_is_absent_not_partial(self) -> None:
        assert _record("1000007").fields.address is None

    def test_the_whole_row_is_kept_with_integer_columns_cast(self) -> None:
        (census,) = _record("1000001").fields.source_records
        assert census.value.source == "fmcsa_company_census"
        (row,) = census.value.rows
        assert row.get("power_units") == 0
        assert row.get("total_drivers") == 0
        assert row.get("phy_cnty") == "439"
        assert row.get("mcs150_date") == "20260115"
        assert row.get("company_officer_1") == "JANE ALPHA"

    def test_a_count_that_is_not_a_number_is_left_out_not_zeroed(self) -> None:
        with capture_logs() as logs:
            (census,) = _record("1000006").fields.source_records
        assert census.value.rows[0].get("power_units") is None
        assert any(log["event"] == "sourcing.census.value_uncastable" for log in logs)

    @pytest.mark.parametrize(
        ("dot", "currency", "principal"),
        [
            ("1000001", date(2026, 1, 15), True),
            ("1000002", date(2025, 3, 1), False),
            ("1000004", None, False),
        ],
    )
    def test_ordering_keys(self, dot: str, currency: date | None, principal: bool) -> None:
        record = _record(dot)
        assert record.currency_date == currency
        assert record.has_principal is principal

    def test_an_unreadable_date_is_none(self) -> None:
        row = {**census_rows()[0], "mcs150_date": "2026-99-99"}
        record = census_pool_record(row, source="c", retrieved_at=RUN_CLOCK)
        assert record is not None and record.currency_date is None

    def test_a_row_without_a_usdot_is_skipped(self) -> None:
        row = {key: value for key, value in census_rows()[0].items() if key != "dot_number"}
        assert census_pool_record(row, source="c", retrieved_at=RUN_CLOCK) is None


class TestCensusPull:
    async def test_the_pull_sends_the_pushdown_and_returns_sorted_records(
        self, source_env: SourceEnv, mock_fmcsa: MockFmcsa
    ) -> None:
        mock_fmcsa.census = list(reversed(census_rows()))
        pull = await CensusSource("fmcsa_company_census", source_env).pull(freight_v3_rules())

        assert [record.registry_id for record in pull.records] == [
            f"usdot:100000{n}" for n in range(1, 9)
        ]
        assert "asset_based_carrier" in pull.pushed_down
        (request,) = mock_fmcsa.calls_to("az4n-8mr2")
        assert "contains(carship, 'B')" in request.url.params["$where"]
        assert request.url.params["$order"] == "dot_number"

    async def test_duplicates_are_collapsed_and_rows_without_a_usdot_dropped(
        self, source_env: SourceEnv, mock_fmcsa: MockFmcsa
    ) -> None:
        first = census_rows()[0]
        mock_fmcsa.census = [first, dict(first), {"legal_name": "NO DOT LLC"}]
        pull = await CensusSource("census", source_env).pull(())
        assert [record.registry_id for record in pull.records] == ["usdot:1000001"]


class TestQcMobile:
    async def test_a_found_carrier_is_one_row_of_its_scalars_cited_without_the_key(
        self, source_env: SourceEnv, mock_fmcsa: MockFmcsa
    ) -> None:
        cited = await QcMobileSource("fmcsa_qcmobile", source_env).lookup(_record())

        assert cited is not None
        assert cited.source_url == "https://mobile.fmcsa.dot.gov/qc/services/carriers/1000001"
        assert cited.retrieval_method is RetrievalMethod.registry_api
        (row,) = cited.value.rows
        assert row.get("allowToOperate") == "Y"
        assert row.get("dotNumber") == 1000001
        # A bool, a null and a nested object are skipped, never converted.
        assert row.get("isPassengerCarrier") is None
        assert row.get("oosDate") is None
        assert row.get("carrierOperation") is None
        (request,) = mock_fmcsa.calls_to("/carriers/")
        assert request.url.params["webKey"] == TEST_WEBKEY

    @pytest.mark.parametrize(
        ("status", "body"),
        [
            (200, load_fixture("qcmobile_not_found")),
            (404, {"content": None}),
            (200, {"content": []}),
        ],
    )
    async def test_an_unknown_usdot_is_looked_and_found_nothing(
        self, source_env: SourceEnv, mock_fmcsa: MockFmcsa, status: int, body: object
    ) -> None:
        mock_fmcsa.carriers["1000001"] = (status, body)
        cited = await QcMobileSource("q", source_env).lookup(_record())
        assert cited is not None and cited.value.rows == ()

    @pytest.mark.parametrize(
        ("status", "body"),
        [(404, load_fixture("qcmobile_missing_webkey")), (401, {}), (403, {"content": "no"})],
    )
    async def test_a_refused_key_raises_so_the_stage_stops_asking(
        self, source_env: SourceEnv, mock_fmcsa: MockFmcsa, status: int, body: object
    ) -> None:
        mock_fmcsa.carriers["1000001"] = (status, body)
        with pytest.raises(SourceAuthError, match="FMCSA_WEBKEY"):
            await QcMobileSource("q", source_env).lookup(_record())

    async def test_a_persistent_server_error_is_a_failed_lookup_not_a_crash(
        self, source_env: SourceEnv, mock_fmcsa: MockFmcsa
    ) -> None:
        mock_fmcsa.carriers["1000001"] = (500, {})
        assert await QcMobileSource("q", source_env).lookup(_record()) is None
        assert len(mock_fmcsa.calls_to("/carriers/")) == 3

    async def test_an_unexpected_shape_is_a_failed_lookup(
        self, source_env: SourceEnv, mock_fmcsa: MockFmcsa
    ) -> None:
        mock_fmcsa.carriers["1000001"] = (200, {"content": {"unexpected": True}})
        assert await QcMobileSource("q", source_env).lookup(_record()) is None

    async def test_the_webkey_never_reaches_a_log_line(
        self, source_env: SourceEnv, mock_fmcsa: MockFmcsa
    ) -> None:
        mock_fmcsa.carriers["1000002"] = (500, {})
        mock_fmcsa.carriers["1000003"] = (404, load_fixture("qcmobile_missing_webkey"))
        source = QcMobileSource("q", source_env)
        with capture_logs() as logs:
            await source.lookup(_record("1000001"))
            await source.lookup(_record("1000002"))
            with pytest.raises(SourceAuthError) as raised:
                await source.lookup(_record("1000003"))
        assert logs
        assert TEST_WEBKEY not in repr(logs)
        assert TEST_WEBKEY not in str(raised.value)


class TestRevocations:
    async def test_rows_are_grouped_by_usdot_and_deduplicated(
        self, source_env: SourceEnv, mock_fmcsa: MockFmcsa
    ) -> None:
        records = [_record("1000001"), _record("1000003"), _record("1000005")]
        joined = await RevocationsSource("fmcsa_revocations", source_env).join(records)

        assert set(joined) == {"usdot:1000001", "usdot:1000003", "usdot:1000005"}
        assert joined["usdot:1000001"].value.rows == ()
        # Motus repeats rows: three come back for 1000003, two of them identical.
        assert len(joined["usdot:1000003"].value.rows) == 2
        assert len(joined["usdot:1000005"].value.rows) == 1
        cited = joined["usdot:1000003"]
        assert cited.source_url == (
            "https://data.transportation.gov/resource/wb4f-neki.json?usdot_number=1000003"
        )
        assert cited.retrieval_method is RetrievalMethod.bulk_file

    async def test_the_batch_is_queried_in_chunks_of_one_hundred(
        self, source_env: SourceEnv, mock_fmcsa: MockFmcsa
    ) -> None:
        template = _record("1000001")
        records = [
            PoolRecord(
                registry_id=f"usdot:{n}",
                native_id=str(n),
                fields=template.fields,
                currency_date=None,
                has_principal=False,
            )
            for n in range(1, 251)
        ]
        joined = await RevocationsSource("r", source_env).join(records)
        assert len(joined) == 250
        assert len(mock_fmcsa.calls_to("wb4f-neki")) == 3
