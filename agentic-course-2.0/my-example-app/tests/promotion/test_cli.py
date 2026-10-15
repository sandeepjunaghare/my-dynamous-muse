"""``lpe promotion``: what a person reads, and the exit codes. Whole lines, never bare ids (PR #14).

The service is stubbed: what it does is ``test_promote`` and ``test_review_sync``'s business. These
tests pin the surface — the lines, the warnings on stderr, and 0 / 1 / 2.
"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from app.cli import main
from app.promotion.exceptions import RunNotPromotableError
from app.promotion.schemas import (
    PROMOTE_COUNTS,
    REVIEW_COUNTS,
    DuplicateProspect,
    PromotedProspect,
    PromotionFailure,
    PromotionReport,
    ReviewDecision,
    ReviewOutcome,
    ReviewSyncReport,
)
from app.promotion.service import PromotionService

RUN = UUID("732c039e-1ad5-432b-8329-d1dce5922f03")
COMPANY_URL = "https://app-na2.hubspot.com/contacts/244766495/record/0-2/7001"
CONTACT_URL = "https://app-na2.hubspot.com/contacts/244766495/record/0-1/4001"


def _counts(names: tuple[str, ...], **given: int) -> dict[str, int]:
    return {name: given.get(name, 0) for name in names}


def _prospect(**overrides: str | bool | None) -> PromotedProspect:
    values: dict[str, str | bool | None] = {
        "registry_id": "4184493",
        "legal_name": "AEROWAY TRANSPORTATION",
        "company_id": "7001",
        "contact_id": "4001",
        "company_url": COMPANY_URL,
        "contact_url": CONTACT_URL,
        "route_label": "A · 75094",
        "first_name": "DAVE",
        "last_name": "MUSGRAVES",
        "name_ambiguous": False,
    }
    values.update(overrides)
    return PromotedProspect.model_validate(values)


def _report(**overrides: object) -> PromotionReport:
    values: dict[str, object] = {
        "run_id": RUN,
        "vertical": "freight",
        "dry_run": False,
        "counts": _counts(PROMOTE_COUNTS, considered=1, created=1),
        "created": (_prospect(),),
        "duplicates": (),
        "failures": (),
        "missing_field_tally": {},
    }
    values.update(overrides)
    return PromotionReport.model_validate(values)


def _stub_promote(monkeypatch: pytest.MonkeyPatch, outcome: PromotionReport | Exception) -> None:
    async def promote_run(
        self: PromotionService, run_id: UUID, *, dry_run: bool = False
    ) -> PromotionReport:
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(PromotionService, "promote_run", promote_run)


def _stub_sync(monkeypatch: pytest.MonkeyPatch, report: ReviewSyncReport) -> None:
    async def sync_reviews(self: PromotionService, *, dry_run: bool = False) -> ReviewSyncReport:
        return report

    monkeypatch.setattr(PromotionService, "sync_reviews", sync_reviews)


class TestPromote:
    def test_a_created_prospect_is_one_line_with_its_links_and_route(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _stub_promote(monkeypatch, _report())

        assert main(["promotion", "promote", "--run", str(RUN)]) == 0

        lines = capsys.readouterr().out.splitlines()
        assert lines[0] == f"run {RUN}  freight  promote"
        assert lines[1] == (
            f"created  AEROWAY TRANSPORTATION  company {COMPANY_URL}  contact {CONTACT_URL}"
            "  route A · 75094"
        )
        assert "  created                            1" in lines

    def test_an_ambiguous_name_gets_a_warning_line(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        prospect = _prospect(first_name="JUAN", last_name="JOSE DE LEON", name_ambiguous=True)
        _stub_promote(monkeypatch, _report(created=(prospect,)))

        main(["promotion", "promote", "--run", str(RUN)])

        assert (
            "  ! name split uncertain: first=JUAN last=JOSE DE LEON — check it at review"
            in capsys.readouterr().out.splitlines()
        )

    def test_duplicates_say_what_they_matched(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        duplicates = (
            DuplicateProspect(
                registry_id="1", legal_name="ACME", matched_company_id="7009",
                matched_company_url=COMPANY_URL,
            ),
            DuplicateProspect(registry_id="2", legal_name="ACME CARRIER", duplicate_of="1"),
        )  # fmt: skip
        _stub_promote(monkeypatch, _report(created=(), duplicates=duplicates))

        main(["promotion", "promote", "--run", str(RUN)])

        lines = capsys.readouterr().out.splitlines()
        assert f"duplicate  ACME — already in HubSpot: {COMPANY_URL}" in lines
        assert "duplicate  ACME CARRIER — the same business as 1 in this vertical" in lines

    def test_a_dry_run_says_would_and_prints_no_ids(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        prospect = _prospect(company_id=None, contact_id=None, company_url=None, contact_url=None)
        _stub_promote(monkeypatch, _report(dry_run=True, created=(prospect,)))

        assert main(["promotion", "promote", "--run", str(RUN), "--dry-run"]) == 0

        lines = capsys.readouterr().out.splitlines()
        assert lines[0] == f"run {RUN}  freight  promote (dry run)"
        assert lines[1] == "would create  AEROWAY TRANSPORTATION  route A · 75094"

    def test_the_missing_field_tally_is_printed(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        counts = _counts(PROMOTE_COUNTS, considered=3, refused_missing_fields=3)
        tally = {"business_check": 3, "owner": 1}
        report = _report(created=(), counts=counts, missing_field_tally=tally)
        _stub_promote(monkeypatch, report)

        main(["promotion", "promote", "--run", str(RUN)])

        out = capsys.readouterr().out
        assert "missing required fields:\n  business_check                     3\n" in out

    def test_a_failure_is_printed_and_exits_1(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        failure = PromotionFailure(registry_id="1000001", message="HubSpot answered 500")
        report = _report(
            created=(), failures=(failure,), counts=_counts(PROMOTE_COUNTS, considered=1, failed=1)
        )
        _stub_promote(monkeypatch, report)

        assert main(["promotion", "promote", "--run", str(RUN)]) == 1
        assert "failed  1000001: HubSpot answered 500" in capsys.readouterr().out.splitlines()

    def test_a_stopped_pass_says_why_on_stderr_and_exits_1(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _stub_promote(monkeypatch, _report(stopped="HubSpot refused the token (403)"))

        assert main(["promotion", "promote", "--run", str(RUN)]) == 1
        assert capsys.readouterr().err.splitlines()[-1] == (
            "error: the pass stopped early — HubSpot refused the token (403)"
        )

    def test_a_run_that_cannot_be_promoted_is_one_error_line(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _stub_promote(monkeypatch, RunNotPromotableError(f"run {RUN} is failed"))

        assert main(["promotion", "promote", "--run", str(RUN)]) == 1
        captured = capsys.readouterr()
        assert captured.err.splitlines()[-1] == f"error: run {RUN} is failed"
        assert "Traceback" not in captured.err

    def test_a_malformed_run_id_is_a_usage_error(self) -> None:
        with pytest.raises(SystemExit) as raised:
            main(["promotion", "promote", "--run", "not-a-uuid"])
        assert raised.value.code == 2


class TestSync:
    def _decision(self, outcome: ReviewOutcome, **overrides: object) -> ReviewDecision:
        values: dict[str, object] = {
            "registry_id": "4184493",
            "outcome": outcome,
            "company_id": "7001",
            "contact_id": "4001",
            "company_url": COMPANY_URL,
        }
        values.update(overrides)
        return ReviewDecision.model_validate(values)

    def _report(self, *decisions: ReviewDecision, **overrides: object) -> ReviewSyncReport:
        values: dict[str, object] = {
            "dry_run": False,
            "counts": _counts(REVIEW_COUNTS, read=len(decisions)),
            "decisions": decisions,
            "failures": (),
            "unrecognised": (),
        }
        values.update(overrides)
        return ReviewSyncReport.model_validate(values)

    def test_each_decision_is_one_line(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        due = datetime(2026, 10, 14, 4, 59, tzinfo=UTC)  # 23:59 in Dallas on the 13th
        report = self._report(
            self._decision(ReviewOutcome.enrolled, first_due_at=due),
            self._decision(ReviewOutcome.rejected, registry_id="2"),
            self._decision(ReviewOutcome.withdrawn, registry_id="3", company_id="7003"),
        )
        _stub_sync(monkeypatch, report)

        assert main(["promotion", "sync"]) == 0

        lines = capsys.readouterr().out.splitlines()
        assert lines[:4] == [
            "promotion sync",
            "enrolled  4184493  contact 4001 — first call due 2026-10-13",
            f"rejected  2  company {COMPANY_URL}",
            "withdrawn  3 — company 7003 is gone from HubSpot",
        ]

    def test_an_orphaned_task_is_named(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        decision = self._decision(ReviewOutcome.enrolled, orphan_task_id="88001")
        _stub_sync(monkeypatch, self._report(decision))

        main(["promotion", "sync"])

        assert (
            "enrolled  4184493  contact 4001 (task 88001 was left open — close it by hand)"
            in capsys.readouterr().out.splitlines()
        )

    def test_a_dry_run_says_would(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        report = self._report(
            self._decision(ReviewOutcome.enrolled),
            self._decision(ReviewOutcome.rejected, registry_id="2"),
            dry_run=True,
        )
        _stub_sync(monkeypatch, report)

        main(["promotion", "sync", "--dry-run"])

        assert capsys.readouterr().out.splitlines()[:3] == [
            "promotion sync (dry run)",
            "would enrol  4184493  contact 4001",
            f"would mark rejected  2  company {COMPANY_URL}",
        ]

    def test_unrecognised_values_are_warned_about(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _stub_sync(monkeypatch, self._report(unrecognised=("maybe_later",)))

        assert main(["promotion", "sync"]) == 0
        assert capsys.readouterr().err.splitlines()[-1] == (
            "warning: lpe_review_status value(s) nobody taught the engine: maybe_later — those "
            "companies are left waiting"
        )

    def test_a_failure_exits_1(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        failure = PromotionFailure(registry_id=str(uuid4()), message="HubSpot answered 500")
        report = self._report(failures=(failure,), counts=_counts(REVIEW_COUNTS, read=1, failed=1))
        _stub_sync(monkeypatch, report)

        assert main(["promotion", "sync"]) == 1
