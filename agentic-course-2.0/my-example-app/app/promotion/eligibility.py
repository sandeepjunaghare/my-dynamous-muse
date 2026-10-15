"""Whether a candidate may be promoted: qualified (OQ1-D), and carrying every required field. Pure.

**The required set** (decided 2026-10-11): ``registry_id``, ``legal_name``, ``address``, ``phone``,
``business_check`` and ``owner``. The cadence is keyed on a contact, so a company is never promoted
without its owner; and a business Places never found is owned, not verified, so it is not promoted
either. Everything else (``website``, ``dba_name``, ``priority``…) is optional: an absent optional
field never refuses.

**Qualified, under OQ1-D:** not suppressed (no ``disqualification`` for its ``(vertical,
registry_id)``) and not a *cited-dead* score. An absent score is allowed — absent is not bad, and
the founder judges the unknowns at review (confirmed again 2026-10-13, with both live runs scoring
almost nothing). A candidate whose judgment call failed looks like one judged clean; T7 has no
"judged" marker, so it can reach review, and the founder catches it there.

The reasons are checked in a fixed order — ``disqualified``, then the missing fields, then
``dead_score`` — so a candidate refused for two reasons is always counted under the same one.
"""

from typing import Final

from app.core.logging import get_logger
from app.promotion.exceptions import PromotionRefusedError, RefusalReason
from app.sourcing.schemas import CandidateResponse, ScoreBand

logger = get_logger(__name__)

REQUIRED_FIELDS: Final = (
    "registry_id",
    "legal_name",
    "address",
    "phone",
    "business_check",
    "owner",
)
"""The fields a promoted candidate must cite, in ``CandidateFields`` declaration order."""


def check_promotable(candidate: CandidateResponse, *, suppressed: bool) -> None:
    """Raise :class:`PromotionRefusedError` if ``candidate`` may not be promoted; else return.

    ``suppressed`` is whether its ``(vertical, registry_id)`` has a disqualification, which the
    caller reads for the whole run at once. ``fields`` on the error names every missing required
    field, whatever the reason; ``no_principal`` is ``missing_fields`` with the owner as the only
    gap.
    """
    fields = candidate.fields
    unprovenanced = set(fields.unprovenanced_fields())
    missing = tuple(name for name in REQUIRED_FIELDS if name in unprovenanced)

    reason: RefusalReason | None = None
    if suppressed:
        reason = RefusalReason.disqualified
    elif missing == ("owner",):
        reason = RefusalReason.no_principal
    elif missing:
        reason = RefusalReason.missing_fields
    elif fields.priority is not None and fields.priority.value.band is ScoreBand.dead:
        reason = RefusalReason.dead_score
    if reason is None:
        return

    logger.warning(
        "promotion.gate.write_refused",
        candidate_id=str(candidate.id),
        registry_id=candidate.registry_id,
        reason=reason.value,
        fields=list(missing),
    )
    detail = f"; missing {', '.join(missing)}" if missing else ""
    raise PromotionRefusedError(
        f"candidate {candidate.registry_id} is not promotable: {reason.value}{detail}",
        candidate_id=str(candidate.id),
        fields=missing,
        reason=reason,
    )
