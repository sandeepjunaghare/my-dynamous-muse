"""Promotion's own failures: what each carries, and the status each renders as."""

from uuid import uuid4

from app.core.exceptions import LocalProspectEngineError
from app.promotion.exceptions import (
    PromotionRefusedError,
    PromotionStateError,
    RefusalReason,
    RunNotPromotableError,
)


class TestPromotionRefusedError:
    def test_it_names_every_missing_field_and_the_reason(self) -> None:
        error = PromotionRefusedError(
            "refused",
            candidate_id="c-1",
            fields=("business_check", "owner"),
            reason=RefusalReason.missing_fields,
        )

        assert error.candidate_id == "c-1"
        assert error.fields == ("business_check", "owner")
        assert error.reason is RefusalReason.missing_fields
        assert error.code == "promotion_refused"
        assert error.status_code == 422

    def test_the_reasons_are_the_four_the_plan_counts(self) -> None:
        assert [reason.value for reason in RefusalReason] == [
            "disqualified",
            "dead_score",
            "no_principal",
            "missing_fields",
        ]


class TestRunAndStateErrors:
    def test_a_run_that_is_not_finished_cleanly_is_a_conflict(self) -> None:
        error = RunNotPromotableError("run is failed")

        assert error.code == "run_not_promotable"
        assert error.status_code == 409

    def test_a_lost_transition_carries_the_row_and_the_status_it_found(self) -> None:
        row_id = uuid4()
        error = PromotionStateError("lost", promotion_id=row_id, status="enrolled")

        assert error.promotion_id == row_id
        assert error.status == "enrolled"
        assert error.code == "promotion_state_conflict"
        assert error.status_code == 409

    def test_every_one_is_rendered_by_the_central_handlers(self) -> None:
        for error_type in (PromotionRefusedError, RunNotPromotableError, PromotionStateError):
            assert issubclass(error_type, LocalProspectEngineError)
