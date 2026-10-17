"""The weekly run's shapes: the status set matches the table, requests refuse bad input, a
playbook number cannot fake a zero, M7 is measured, and every counter the report shows is one a
stage or promotion actually reports."""

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import CheckConstraint

from app.promotion.schemas import PROMOTE_COUNTS
from app.runs.models import WeeklyRun
from app.runs.schemas import (
    FUNNEL_COUNTERS,
    PROMOTION_COUNTERS,
    PlaybookNumber,
    WeeklyRunRequest,
    WeeklyRunResponse,
    WeeklyRunStatus,
)
from app.tools import resolve_owner, verify_business

ROOT = Path(__file__).resolve().parents[2]

REQUESTED = datetime(2026, 10, 19, 11, 0, tzinfo=UTC)


def a_response(**overrides: object) -> WeeklyRunResponse:
    values: dict[str, object] = {
        "id": uuid4(),
        "vertical": "freight",
        "status": WeeklyRunStatus.running,
        "status_detail": None,
        "dry_run": False,
        "batch_size": None,
        "allow_degraded": False,
        "requested_at": REQUESTED,
        "sourcing_run_id": None,
        "sourced_at": None,
        "promoted_at": None,
        "finished_at": None,
        "promotion_counts": {},
        "promotion_stopped": None,
    }
    values.update(overrides)
    return WeeklyRunResponse.model_validate(values)


class TestStatus:
    def test_the_enum_matches_the_check_constraint(self) -> None:
        [check] = [
            arg
            for arg in WeeklyRun.__table_args__
            if isinstance(arg, CheckConstraint) and arg.name == "ck_weekly_run_status"
        ]
        allowed = set(re.findall(r"'([a-z_]+)'", str(check.sqltext)))
        assert allowed == {status.value for status in WeeklyRunStatus}


class TestRequest:
    def test_defaults(self) -> None:
        request = WeeklyRunRequest(vertical="freight")
        assert (request.batch_size, request.allow_degraded, request.dry_run) == (None, False, False)

    @pytest.mark.parametrize(
        "body",
        [
            {"vertical": "Freight!"},
            {"vertical": "freight", "batch_size": 0},
            {"vertical": "freight", "geography": "dfw"},
        ],
        ids=["bad-slug", "zero-batch", "extra-key"],
    )
    def test_bad_input_is_refused(self, body: dict[str, object]) -> None:
        with pytest.raises(ValidationError):
            WeeklyRunRequest.model_validate(body)


class TestPlaybookNumber:
    def test_a_counted_number(self) -> None:
        assert PlaybookNumber(name="doors knocked", value=0, configured=True).value == 0

    def test_not_configured(self) -> None:
        number = PlaybookNumber(name="paid assessments sold", value=None, configured=False)
        assert number.model_dump(mode="json")["value"] is None

    @pytest.mark.parametrize(("value", "configured"), [(0, False), (None, True)])
    def test_a_zero_cannot_stand_in_for_not_configured(
        self, value: int | None, configured: bool
    ) -> None:
        with pytest.raises(ValidationError, match="never a 0"):
            PlaybookNumber(name="paid assessments sold", value=value, configured=configured)


class TestM7:
    def test_none_until_promoted(self) -> None:
        assert a_response().brief_to_hubspot_minutes is None

    def test_minutes_from_request_to_promotion(self) -> None:
        response = a_response(
            sourced_at=REQUESTED + timedelta(minutes=30),
            promoted_at=REQUESTED + timedelta(minutes=42, seconds=30),
        )
        assert response.brief_to_hubspot_minutes == pytest.approx(42.5)

    def test_it_is_derived_never_taken_from_input(self) -> None:
        assert a_response(brief_to_hubspot_minutes=1.0).brief_to_hubspot_minutes is None

    def test_it_reaches_the_json(self) -> None:
        response = a_response(sourced_at=REQUESTED, promoted_at=REQUESTED + timedelta(minutes=6))
        assert response.model_dump(mode="json")["brief_to_hubspot_minutes"] == 6.0


class TestCounterNames:
    """A renamed counter must break a test, not quietly show ``None`` on a Friday."""

    def test_every_funnel_counter_is_a_key_some_stage_reports(self) -> None:
        declared = set(verify_business.COUNT_KEYS) | set(resolve_owner.COUNT_KEYS)
        # The other stages build their counts inline; their keys are literals in these files.
        sources = "".join(
            (ROOT / path).read_text(encoding="utf-8")
            for path in (
                "app/tools/search_registry.py",
                "app/tools/classify_rollup.py",
                "app/routing/service.py",
            )
        )
        missing = [
            name for name in FUNNEL_COUNTERS if name not in declared and f'"{name}"' not in sources
        ]
        assert missing == []

    def test_every_promotion_counter_is_in_promote_counts(self) -> None:
        assert set(PROMOTION_COUNTERS) <= set(PROMOTE_COUNTS)
