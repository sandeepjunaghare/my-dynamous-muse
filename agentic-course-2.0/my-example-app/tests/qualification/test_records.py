"""``records_from``: T5's stored evidence as the evaluator's ``RecordSet``. No database."""

from app.qualification.records import records_from
from app.shared.provenance import RetrievalMethod
from app.sourcing.schemas import CandidateFields, SourceRecord, SourceRow
from tests.sourcing.builders import sourced

QCMOBILE = "https://mobile.fmcsa.dot.gov/qc/services/carriers/7989"
CENSUS = "https://data.transportation.gov/resource/az4n-8mr2.json?dot_number=7989"
MOTUS = "https://data.transportation.gov/resource/wb4f-neki.json"


def _fields(*records: tuple[str, str, tuple[SourceRow, ...]]) -> CandidateFields:
    return CandidateFields(
        registry_id=sourced("usdot:7989"),
        source_records=tuple(
            sourced(SourceRecord(source=name, rows=rows), url, RetrievalMethod.registry_api)
            for name, url, rows in records
        ),
    )


class TestRecordsFrom:
    def test_a_one_row_source_is_its_record_as_text_with_its_url(self) -> None:
        row = SourceRow(values=(("power_units", 0), ("carship", "B")))
        result = records_from(_fields(("fmcsa_company_census", CENSUS, (row,))))
        assert result.records == {"fmcsa_company_census": {"carship": "B", "power_units": "0"}}
        assert result.source_urls == {"fmcsa_company_census": CENSUS}

    def test_a_source_that_found_nothing_is_left_out(self) -> None:
        """Absent, so its rules are ``source_missing`` — never a drop on an absent field."""
        result = records_from(_fields(("fmcsa_qcmobile", QCMOBILE, ())))
        assert result.records == {}
        assert result.source_urls == {}

    def test_a_many_row_source_is_left_to_the_judgment_node(self) -> None:
        rows = (
            SourceRow(values=(("order1_type_desc", "Voluntary"),)),
            SourceRow(values=(("order1_type_desc", "Involuntary"),)),
        )
        qcmobile = SourceRow(values=(("allowedToOperate", "Y"),))
        result = records_from(
            _fields(("fmcsa_qcmobile", QCMOBILE, (qcmobile,)), ("fmcsa_revocations", MOTUS, rows))
        )
        assert set(result.records) == {"fmcsa_qcmobile"}

    def test_no_source_records_is_an_empty_record_set(self) -> None:
        assert records_from(CandidateFields(registry_id=sourced("usdot:1"))).records == {}
