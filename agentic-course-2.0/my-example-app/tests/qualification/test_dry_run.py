"""The manifest dry-run over a 30-row census-shaped extract whose funnel is known by construction.

``fixtures/census_extract.csv``: 6 rows outside the DFW counties, 4 in DFW with no broker code, 4 in
DFW with 25 power units, 1 with a blank power-units field, and 15 small DFW brokers. Every number
asserted below follows from that.
"""

from pathlib import Path

import pytest

from app.manifests.schemas import DryRunFlag, DryRunReport, DryRunStep, RuleOperator, SourceKind
from app.qualification.dry_run import dry_run
from app.qualification.exceptions import DryRunSourceError
from tests.qualification.builders import (
    FREIGHT_V3_PREDICATES,
    a_body_with,
    a_manifest,
    predicate,
)

EXTRACT = Path(__file__).parent / "fixtures" / "census_extract.csv"

V1_RULE = predicate("v1_asset_based", "carrier_operation", RuleOperator.equals, "asset_based")
SHADOWED_RULE = predicate("out_of_state_county", "phy_cnty", RuleOperator.equals, "201")
UNKNOWN_FIELD_RULE = predicate("stale_mcs150", "mcs150_date", RuleOperator.less_than, 2024)
QCMOBILE_RULE = predicate(
    "not_allowed_to_operate", "allowToOperate", RuleOperator.equals, "N", source="qcmobile"
)
MALFORMED_RULE = predicate("bad_set", "phy_cnty", RuleOperator.in_set, "113")

SOURCES = (("fmcsa", SourceKind.bulk_file), ("qcmobile", SourceKind.registry_api))


def _report() -> DryRunReport:
    body = a_body_with(
        *FREIGHT_V3_PREDICATES,
        V1_RULE,
        SHADOWED_RULE,
        UNKNOWN_FIELD_RULE,
        QCMOBILE_RULE,
        MALFORMED_RULE,
        sources=SOURCES,
    )
    return dry_run(a_manifest(body), {"fmcsa": EXTRACT})


def _step(report: DryRunReport, rule_id: str) -> DryRunStep:
    return next(step for step in report.steps if step.rule_id == rule_id)


class TestFunnel:
    def test_the_freight_v3_funnel(self) -> None:
        report = _report()
        steps = [
            (s.rule_id, s.pool_before, s.removed, s.not_evaluable, s.flags)
            for s in report.steps[:3]
        ]
        assert steps == [
            ("outside_dfw_metro", 30, 6, 0, ()),
            ("no_broker_entity_type", 24, 4, 0, ()),
            ("asset_based_carrier", 20, 4, 1, ()),
        ]
        (summary,) = report.files
        assert (summary.rows, summary.pool_end) == (30, 16)

    def test_an_unjudgeable_row_stays_in_the_pool(self) -> None:
        """The blank power-units row is counted as not evaluable and is never removed."""
        assert _step(_report(), "asset_based_carrier").not_evaluable == 1
        assert _report().files[0].pool_end == 16

    def test_steps_follow_declaration_order(self) -> None:
        assert [s.rule_id for s in _report().steps] == [
            "outside_dfw_metro",
            "no_broker_entity_type",
            "asset_based_carrier",
            "v1_asset_based",
            "out_of_state_county",
            "stale_mcs150",
            "not_allowed_to_operate",
            "bad_set",
        ]


class TestFlags:
    def test_a_rule_whose_literal_never_appears_matches_nothing(self) -> None:
        """Freight v1: ``carrier_operation equals 'asset_based'`` against the real A/B/C."""
        assert _step(_report(), "v1_asset_based").flags == (
            DryRunFlag.value_not_seen,
            DryRunFlag.matches_nothing,
        )

    def test_a_rule_shadowed_by_an_earlier_one_excludes_nothing(self) -> None:
        step = _step(_report(), "out_of_state_county")
        assert (step.matched_standalone, step.removed) == (2, 0)
        assert step.flags == (DryRunFlag.excludes_nothing,)

    def test_a_rule_on_a_missing_column(self) -> None:
        step = _step(_report(), "stale_mcs150")
        assert step.flags == (DryRunFlag.unknown_field,)
        assert step.not_evaluable == 16

    def test_a_rule_on_a_per_record_api_is_not_evaluable_offline(self) -> None:
        step = _step(_report(), "not_allowed_to_operate")
        assert step.flags == (DryRunFlag.not_evaluable_offline,)
        assert step.source == "qcmobile"

    def test_a_malformed_rule_is_flagged_not_raised(self) -> None:
        assert _step(_report(), "bad_set").flags == (DryRunFlag.malformed,)

    def test_a_rule_that_empties_the_pool(self) -> None:
        everything = predicate("everyone", "legal_name", RuleOperator.contains, "Carrier")
        report = dry_run(a_manifest(a_body_with(everything)), {"fmcsa": EXTRACT})
        step = report.steps[0]
        assert step.removed == 30
        assert DryRunFlag.excludes_everything in step.flags

    def test_a_clean_manifest_flags_nothing(self) -> None:
        report = dry_run(a_manifest(a_body_with(*FREIGHT_V3_PREDICATES)), {"fmcsa": EXTRACT})
        assert report.flagged() == ()

    def test_the_summary_names_each_flag(self) -> None:
        assert "v1_asset_based: matches_nothing" in _report().flagged()


class TestSamplesAndRecord:
    def test_samples_are_the_first_rows_in_file_order(self) -> None:
        step = _step(_report(), "outside_dfw_metro")
        assert [s.row_id for s in step.removed_samples] == ["1000001", "1000002", "1000003"]
        assert dict(step.removed_samples[0].fields)["phy_cnty"] == "201"
        assert len(_report().files[0].passing_samples) == 3

    def test_two_runs_give_the_same_report(self) -> None:
        manifest = a_manifest(a_body_with(*FREIGHT_V3_PREDICATES, V1_RULE))
        assert dry_run(manifest, {"fmcsa": EXTRACT}) == dry_run(manifest, {"fmcsa": EXTRACT})

    def test_only_the_file_name_is_recorded(self) -> None:
        """The record outlives this machine; a local path in it would be a 12-factor leak."""
        summary = _report().files[0]
        assert summary.file_name == "census_extract.csv"
        assert "/" not in summary.model_dump_json().split('"file_name"')[1].split(",")[0]
        assert len(summary.sha256) == 64
        assert summary.row_id_column == "dot_number"


class TestRefusals:
    def test_a_bulk_source_without_its_extract(self) -> None:
        with pytest.raises(DryRunSourceError, match="pass --source-file"):
            dry_run(a_manifest(a_body_with(*FREIGHT_V3_PREDICATES)), {})

    def test_an_undeclared_source(self) -> None:
        with pytest.raises(DryRunSourceError, match="declares no source"):
            dry_run(a_manifest(a_body_with(*FREIGHT_V3_PREDICATES)), {"socrata": EXTRACT})

    def test_a_source_that_is_not_a_bulk_file(self) -> None:
        body = a_body_with(*FREIGHT_V3_PREDICATES, sources=SOURCES)
        with pytest.raises(DryRunSourceError, match="not a bulk file"):
            dry_run(a_manifest(body), {"qcmobile": EXTRACT})

    def test_an_empty_file(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty.csv"
        empty.write_text("", encoding="utf-8")
        with pytest.raises(DryRunSourceError, match="no header"):
            dry_run(a_manifest(a_body_with(*FREIGHT_V3_PREDICATES)), {"fmcsa": empty})

    def test_a_bom_and_padded_header_are_tolerated(self, tmp_path: Path) -> None:
        extract = tmp_path / "bom.csv"
        extract.write_text(
            "﻿dot_number , phy_cnty,carship,power_units\n1,113,C;B,4\n2,201,C;B,4\n",
            encoding="utf-8",
        )
        report = dry_run(a_manifest(a_body_with(*FREIGHT_V3_PREDICATES)), {"fmcsa": extract})
        assert report.files[0].pool_end == 1
        assert report.files[0].row_id_column == "dot_number"
        assert all(DryRunFlag.unknown_field not in step.flags for step in report.steps)

    def test_blank_lines_are_not_rows(self, tmp_path: Path) -> None:
        """A blank line would otherwise join the pool and skew the numbers a person reconciles."""
        extract = tmp_path / "blank.csv"
        extract.write_text(
            "dot_number,phy_cnty,carship,power_units\n1,113,C;B,4\n\n,,,\n2,201,C;B,4\n",
            encoding="utf-8",
        )
        report = dry_run(a_manifest(a_body_with(*FREIGHT_V3_PREDICATES)), {"fmcsa": extract})
        assert (report.files[0].rows, report.files[0].pool_end) == (2, 1)

    def test_a_repeated_header_column_is_refused(self, tmp_path: Path) -> None:
        """With two ``phy_cnty`` columns the last silently wins, and a rule reads the wrong one."""
        extract = tmp_path / "dup.csv"
        extract.write_text("dot_number,phy_cnty,phy_cnty\n1,113,201\n", encoding="utf-8")
        with pytest.raises(DryRunSourceError, match="repeats header column"):
            dry_run(a_manifest(a_body_with(*FREIGHT_V3_PREDICATES)), {"fmcsa": extract})


class TestAManifestWithNoBulkFile:
    """PR #18 review H1: an API-only manifest (fire) must be able to record a dry-run."""

    def test_every_predicate_is_reported_not_evaluable_offline(self) -> None:
        body = a_body_with(QCMOBILE_RULE, sources=(("qcmobile", SourceKind.registry_api),))
        report = dry_run(a_manifest(body), {})
        assert report.files == ()
        assert [(s.rule_id, s.flags) for s in report.steps] == [
            ("not_allowed_to_operate", (DryRunFlag.not_evaluable_offline,))
        ]
