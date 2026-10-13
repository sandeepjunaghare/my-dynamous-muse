"""Stage 2 of 5: ``verify_business`` — a Google Places check of each survivor, and its website (T6).

For every candidate of the run that no free rule removed, in ``registry_id`` order:

1. **Website, free, first.** The census email's domain — not free-mail, not a domain three or more
   pool businesses share — is fetched once (``app/sourcing/website.py``). A 2xx HTML homepage on
   that domain becomes ``website``, cited ``web_lookup`` to the page read.
2. **Places, paid.** One Text Search (Pro fields only) by the registry's own name and address,
   matched deterministically (street number, ZIP, a shared name word; A4). A match that is not
   permanently closed stores ``business_check`` — the place ID and the census address it checked,
   and nothing else from Places (D13). What Places saw goes on ``context.places`` for
   ``classify_rollup`` within the run, and dies with it.

**Bound only where declared** (A3). Terms are accepted per declared source at ``activate``, so a
manifest declaring no Places source — or a missing key — refuses the run before a call is made,
unless ``--allow-degraded`` says to go on without it (PR #21 M1: a batch is never spent without its
evidence unless someone said so). A refused key mid-batch is the same choice.

**The breaker.** Every call is charged first (``RunCost.charge``) against
``max_places_calls_per_run``. Reaching it stops Places for the rest of the batch and finishes the
stage **degraded**; the free website step still runs for everyone. Within a run, a candidate
already verified is not paid for again. Across runs it is: candidates are run-scoped, so a failed
run's batch goes back to the pool and the next run's rows start with no check (PR #32 L3). One
failed call or one refused write is counted and the batch goes on.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import httpx
from pydantic import ValidationError

from app.core.config import Settings, get_settings
from app.core.cost import BillableKind, RunCost
from app.core.exceptions import CostLimitExceededError
from app.core.logging import get_logger
from app.manifests.schemas import ManifestBody
from app.qualification.service import QualificationService
from app.shared.provenance import ProvenancedValue, RetrievalMethod
from app.sourcing.exceptions import SourceAuthError, SourceRequestError, SourceUnavailableError
from app.sourcing.schemas import CandidateFields, PlaceCheck, PlacesEvidence
from app.sourcing.service import SourcingService
from app.sourcing.sources import http
from app.sourcing.sources.google_places import (
    CLOSED_PERMANENTLY,
    PlacesClient,
    declared_places_source,
    match_place,
    place_url,
    places_unavailable,
    search_query,
)
from app.sourcing.stages import PipelineStage
from app.sourcing.website import HomepageChecker, email_domain
from app.tools.registry import StageContext, StageResult

logger = get_logger(__name__)

NO_PLACES_SOURCE = "the manifest declares no Places source"
_WHEN_MID_BATCH = "this run's batch goes back to the backlog"
_WHEN_BEFORE_BATCH = "no batch was taken"


def _places_unavailable(body: ManifestBody, settings: Settings) -> str | None:
    """Why Places cannot run for this manifest here, or ``None``: one test for both refusals."""
    if declared_places_source(body) is None:
        return NO_PLACES_SOURCE
    return places_unavailable(settings)


_HTTP_TIMEOUT = httpx.Timeout(10.0)

COUNT_KEYS: tuple[str, ...] = (
    "places_checked",
    "places_verified",
    "places_not_found",
    "places_closed",
    "places_failed",
    "places_not_checkable",
    "places_skipped",
    "website_found",
    "website_unreachable",
    "website_no_domain",
    "verify_write_refused",
)
"""Every count this stage reports, all prefixed: the runner refuses a name two stages share."""

type Checked = tuple[ProvenancedValue[PlaceCheck], PlacesEvidence]


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class _PlacesRun:
    """One run's Places state: the client (``None`` once abandoned) and the breaker."""

    client: PlacesClient | None
    source: str
    cap: int
    usd: Decimal
    tripped: bool = False
    unchecked: int = 0


class VerifyBusinessStage:
    """``verify_business``. Built with its transport and clock, so a test needs no network."""

    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], datetime] = _utcnow,
        backoff_seconds: float = http.DEFAULT_BACKOFF_SECONDS,
        throttle: bool = True,
        allow_degraded: bool = False,
    ) -> None:
        self._transport = transport
        self._clock = clock
        self._backoff_seconds = backoff_seconds
        self._throttle = throttle
        self._allow_degraded = allow_degraded

    @property
    def stage(self) -> PipelineStage:
        return PipelineStage.verify_business

    def configured(self, *, allow_degraded: bool | None = None) -> "VerifyBusinessStage":
        """This stage with a per-run option changed and everything else kept as it was built."""
        return VerifyBusinessStage(
            transport=self._transport,
            clock=self._clock,
            backoff_seconds=self._backoff_seconds,
            throttle=self._throttle,
            allow_degraded=(allow_degraded if allow_degraded is not None else self._allow_degraded),
        )

    def _refuse(self, reasons: tuple[str, ...], *, when: str) -> SourceUnavailableError:
        logger.warning("tools.verify_business.run_refused", reasons=list(reasons), when=when)
        return SourceUnavailableError(
            f"{'; '.join(reasons)} — {when}. Fix it, or pass --allow-degraded to go on without "
            "that source; the batch is then used up without its evidence",
            reasons=reasons,
        )

    def preflight(self, body: ManifestBody) -> None:
        """Refuse from the manifest and settings alone, before any batch is taken (PR #32 L2).

        Without it, a manifest with no Places source pulls the census, takes a batch and runs
        QCMobile over it, only to fail here and hand the batch back. The stage's own refusal in
        :meth:`run` stays as the backstop for a caller that skips this (T10 should call it too).
        """
        unavailable = _places_unavailable(body, get_settings())
        if unavailable is not None and not self._allow_degraded:
            raise self._refuse((unavailable,), when=_WHEN_BEFORE_BATCH)

    async def run(self, context: StageContext) -> StageResult:
        """Check each survivor's website and Places listing (see the module doc)."""
        settings = get_settings()
        sourcing = SourcingService(context.session)
        reasons: list[str] = []

        source = declared_places_source(context.manifest.body)
        unavailable = _places_unavailable(context.manifest.body, settings)
        if unavailable is not None:
            if not self._allow_degraded:
                raise self._refuse((unavailable,), when=_WHEN_MID_BATCH)
            reasons.append(unavailable)

        removed = await QualificationService(context.session).disqualified_candidate_ids(
            context.run_id
        )
        candidates = [
            candidate
            for candidate in await sourcing.list_candidates(context.run_id)
            if candidate.id not in removed
        ]
        shared = await sourcing.shared_email_domains(context.manifest.vertical)
        counts: dict[str, int] = dict.fromkeys(COUNT_KEYS, 0)

        async with httpx.AsyncClient(transport=self._transport, timeout=_HTTP_TIMEOUT) as client:
            homepages = HomepageChecker(client, clock=self._clock)
            key = settings.google_places_api_key
            places = _PlacesRun(
                client=(
                    PlacesClient(
                        client,
                        api_key=key.get_secret_value(),
                        source=source,
                        backoff_seconds=self._backoff_seconds,
                        throttle=self._throttle,
                    )
                    if source is not None and unavailable is None and key is not None
                    else None
                ),
                source=source or "places",
                cap=settings.max_places_calls_per_run,
                usd=settings.places_text_search_usd,
            )

            for candidate in candidates:
                fields = candidate.fields
                update: dict[str, object] = {}
                if fields.website is None:
                    website = await self._website(fields, shared, homepages, counts)
                    if website is not None:
                        update["website"] = website
                checked = await self._places(context, fields, places, counts, reasons)
                if checked is not None:
                    update["business_check"] = checked[0]
                if not update:
                    continue

                try:
                    await sourcing.record_candidates(
                        context.run_id,
                        [fields.model_copy(update=update)],
                        stage=PipelineStage.verify_business,
                    )
                except ValidationError as exc:
                    # One refused write (a D13 guard, say) must not stop a paid batch half-way.
                    counts["verify_write_refused"] += 1
                    logger.warning(
                        "tools.verify_business.write_refused",
                        run_id=str(context.run_id),
                        candidate_id=str(candidate.id),
                        errors=exc.error_count(),
                    )
                    continue
                if checked is not None:
                    # Only a stored check passes its evidence on: unverified means unread (OQ-5).
                    context.places[fields.registry_id.value] = checked[1]

        if places.tripped:
            reasons.append(
                f"Places breaker reached at {places.cap} calls; "
                f"{places.unchecked} candidate(s) unchecked"
            )
        if counts["places_failed"]:
            reasons.append(
                f"{counts['places_failed']} Places check(s) failed; those candidates are unverified"
            )
        logger.info(
            "tools.verify_business.stage_completed",
            run_id=str(context.run_id),
            degraded=reasons,
            **counts,
        )
        return StageResult(counts=counts, degraded_reason="; ".join(reasons) or None)

    async def _website(
        self,
        fields: CandidateFields,
        shared: frozenset[str],
        homepages: HomepageChecker,
        counts: dict[str, int],
    ) -> ProvenancedValue[str] | None:
        """The homepage behind the census email's domain, or ``None`` (counted either way)."""
        domain = email_domain(fields.email.value) if fields.email is not None else None
        if domain is None or domain in shared:
            counts["website_no_domain"] += 1
            return None
        website = await homepages.check(domain)
        counts["website_found" if website is not None else "website_unreachable"] += 1
        return website

    async def _places(
        self,
        context: StageContext,
        fields: CandidateFields,
        places: _PlacesRun,
        counts: dict[str, int],
        reasons: list[str],
    ) -> Checked | None:
        """One candidate's Places check: the check to store and the evidence to pass on, or
        ``None`` — not bound, already verified, not checkable, past the breaker, failed, a miss
        (OQ-2: counted, nothing recorded), or a permanently closed match."""
        if places.client is None:
            return None
        if fields.has_verified_address():
            counts["places_skipped"] += 1
            return None
        query = search_query(fields)
        if query is None or fields.address is None:
            counts["places_not_checkable"] += 1
            return None
        if places.tripped or not self._charge(context.cost, places, context):
            counts["places_skipped"] += 1
            places.unchecked += 1
            return None

        try:
            results = await places.client.search_text(query)
        except SourceAuthError as exc:
            reason = f"{places.source}: {exc.message}"
            if not self._allow_degraded:
                raise self._refuse((reason,), when=_WHEN_MID_BATCH) from exc
            # The key will not get better on the next candidate. Stop asking.
            reasons.append(reason)
            places.client = None
            logger.warning(
                "tools.verify_business.source_abandoned",
                source=places.source,
                run_id=str(context.run_id),
            )
            return None
        except SourceRequestError as exc:
            counts["places_failed"] += 1
            logger.warning(
                "tools.verify_business.lookup_failed",
                registry_id=fields.registry_id.value,
                status=exc.status,
            )
            return None

        counts["places_checked"] += 1
        match = match_place(fields, results)
        if match is None:
            counts["places_not_found"] += 1
            logger.info(
                "sourcing.places.match_missing",
                registry_id=fields.registry_id.value,
                results=len(results),
            )
            return None
        if match.business_status == CLOSED_PERMANENTLY:
            # A closed business is not one to route a door to: no check, no evidence (A4).
            counts["places_closed"] += 1
            logger.info(
                "sourcing.places.match_closed",
                registry_id=fields.registry_id.value,
                place_id=match.id,
            )
            return None

        counts["places_verified"] += 1
        # The place ID only, never a Places name or address (D13).
        logger.info(
            "sourcing.places.match_found", registry_id=fields.registry_id.value, place_id=match.id
        )
        check = ProvenancedValue(
            value=PlaceCheck(place_id=match.id, checked_address=fields.address.value),
            source_url=place_url(match.id),
            retrieved_at=self._clock(),
            retrieval_method=RetrievalMethod.web_lookup,
        )
        evidence = PlacesEvidence(
            display_name=match.display_name,
            business_status=match.business_status,
            types=match.types,
        )
        return check, evidence

    def _charge(self, cost: RunCost, places: _PlacesRun, context: StageContext) -> bool:
        """Charge one call against the breaker. ``False`` — and the breaker tripped — at the cap."""
        try:
            cost.charge(BillableKind.places_lookup, cap=places.cap, usd=places.usd)
        except CostLimitExceededError:
            places.tripped = True
            logger.warning(
                "tools.verify_business.cap_reached",
                run_id=str(context.run_id),
                cap=places.cap,
            )
            return False
        return True


STAGE = VerifyBusinessStage()
