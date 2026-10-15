"""Who may be promoted (OQ1-D and the required set). Pure: no database, no portal."""

import pytest

from app.promotion.eligibility import REQUIRED_FIELDS, check_promotable
from app.promotion.exceptions import PromotionRefusedError, RefusalReason
from app.sourcing.schemas import CandidateFields
from tests.promotion.builders import a_promotable, a_score, as_response
from tests.sourcing.builders import website_verified


def _refusal(fields: CandidateFields, *, suppressed: bool = False) -> PromotionRefusedError:
    with pytest.raises(PromotionRefusedError) as raised:
        check_promotable(as_response(fields), suppressed=suppressed)
    return raised.value


def _without(fields: CandidateFields, *names: str) -> CandidateFields:
    return fields.model_copy(update=dict.fromkeys(names))


def test_every_required_field_is_a_candidate_field() -> None:
    assert set(REQUIRED_FIELDS) <= set(CandidateFields.model_fields)
    declared = [name for name in CandidateFields.model_fields if name in REQUIRED_FIELDS]
    assert list(REQUIRED_FIELDS) == declared


def test_a_fully_cited_candidate_is_promotable() -> None:
    check_promotable(as_response(a_promotable()), suppressed=False)


@pytest.mark.parametrize("absent", ["website", "dba_name", "email", "priority", "headcount_band"])
def test_an_absent_optional_field_never_refuses(absent: str) -> None:
    check_promotable(as_response(_without(a_promotable(), absent)), suppressed=False)


def test_an_unscored_candidate_is_promotable() -> None:
    """OQ1-D, confirmed 2026-10-13: absent is not bad; the founder judges it at review."""
    fields = a_promotable()
    assert fields.priority is None
    check_promotable(as_response(fields), suppressed=False)


@pytest.mark.parametrize(("intensity", "automatable"), [(3, 3), (4, 4), (5, 5)])
def test_a_middle_or_live_score_is_promotable(intensity: int, automatable: int) -> None:
    fields = a_promotable().model_copy(update={"priority": a_score(intensity, automatable)})
    check_promotable(as_response(fields), suppressed=False)


def test_a_cited_dead_score_is_refused() -> None:
    fields = a_promotable().model_copy(update={"priority": a_score(2, 4)})

    refusal = _refusal(fields)

    assert refusal.reason is RefusalReason.dead_score
    assert refusal.fields == ()


def test_a_suppressed_candidate_is_refused_as_disqualified() -> None:
    refusal = _refusal(a_promotable(), suppressed=True)

    assert refusal.reason is RefusalReason.disqualified


def test_no_owner_alone_is_no_principal() -> None:
    refusal = _refusal(a_promotable(owner=None))

    assert refusal.reason is RefusalReason.no_principal
    assert refusal.fields == ("owner",)


def test_neither_places_nor_website_verified_is_unverified() -> None:
    """Owned is not verified: a business neither check found is not promoted (B6)."""
    refusal = _refusal(_without(a_promotable(), "business_check"))

    assert refusal.reason is RefusalReason.unverified
    assert refusal.fields == ()


def test_unverified_is_checked_before_the_owner() -> None:
    """An unverified candidate never reaches the owner node; ``no_principal`` would hide why."""
    refusal = _refusal(_without(a_promotable(owner=None), "business_check"))

    assert refusal.reason is RefusalReason.unverified
    assert refusal.fields == ("owner",)


def test_a_suppressed_unverified_candidate_is_disqualified() -> None:
    refusal = _refusal(_without(a_promotable(), "business_check"), suppressed=True)

    assert refusal.reason is RefusalReason.disqualified


def test_a_website_verified_owned_candidate_is_promotable() -> None:
    """Website-verification B1: a homepage on its own email domain verifies it."""
    fields = website_verified(_without(a_promotable(), "business_check"))
    check_promotable(as_response(fields), suppressed=False)


def test_a_website_verified_candidate_without_an_owner_is_no_principal() -> None:
    fields = website_verified(_without(a_promotable(owner=None), "business_check"))

    assert _refusal(fields).reason is RefusalReason.no_principal


def test_places_verification_alone_is_enough() -> None:
    fields = a_promotable()
    assert fields.website is None and fields.email is None
    check_promotable(as_response(fields), suppressed=False)


def test_every_missing_field_is_named_in_declaration_order() -> None:
    fields = _without(a_promotable(owner=None), "phone", "legal_name")

    refusal = _refusal(fields)

    assert refusal.reason is RefusalReason.missing_fields
    assert refusal.fields == ("legal_name", "phone", "owner")


def test_disqualified_is_checked_before_missing_fields() -> None:
    refusal = _refusal(a_promotable(owner=None), suppressed=True)

    assert refusal.reason is RefusalReason.disqualified
    assert refusal.fields == ("owner",)


def test_missing_fields_are_checked_before_a_dead_score() -> None:
    fields = a_promotable(owner=None).model_copy(update={"priority": a_score(1, 1)})

    assert _refusal(fields).reason is RefusalReason.no_principal
