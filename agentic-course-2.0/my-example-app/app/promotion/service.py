"""Promotion: a finished run's qualified candidates → HubSpot for review; accepted → the cadence.

Two operations, each a CLI command (OQ7):

* :meth:`PromotionService.promote_run` — ``lpe promotion promote --run <id>``.
* :meth:`PromotionService.sync_reviews` — ``lpe promotion sync``: read the founder's decisions back,
  and hand each **accepted** contact to the cadence (``CadenceService.enrol``). Promotion's job
  ends there; it never creates a task itself.

How a promote never creates a business twice
--------------------------------------------
* **Across runs**, the ledger's ``(vertical, registry_id)`` key: a business with a row, in any
  status, is ``already_promoted``. Only ``completed`` and ``degraded`` runs are promoted — a failed
  run's batch is re-batched as new candidate rows.
* **Within a run**, ledger dedupe on the normalized phone and website domain, stored on the row
  *before* the create. HubSpot's search lags writes, so two candidates of one batch resolving to one
  company would both miss the portal dedupe (``dedupe.py``).
* **Against the portal**, ``find_existing_company`` (domain, then phone). A hit is flagged
  ``duplicate`` and nothing is written to it (OQ5-A).
* **After an interrupted create**, the intent is already committed (``pending_create``, carrying
  the company's ``lpe_source_url`` as its key), and the next pass resumes it before anything new:
  a company id recorded → walk its contacts (written in the same request, so no lag) for ours;
  none recorded → search companies by the key, but only once the row is ``RECOVERY_MIN_AGE`` old,
  safely past the search index's lag. A create is never retried in process (D20).

**No partial writes.** Both payloads are put through the write-gate before the company create, so
a contact that could not pass never leaves an orphan company. The ledger is committed after every
claim and every HubSpot create: it is never behind HubSpot by more than the one call in flight.

**A dry run is the real pass minus the writes:** the same reads (portal dedupe included), the same
in-run ledger dedupe (kept in memory), no ledger row, no HubSpot write, no property provisioning.
Rows a real pass would resume are counted ``recovery_pending`` instead.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.cadence.exceptions import AlreadyEnrolledError
from app.cadence.service import (
    CadenceService,
    Clock,
    HubSpotFactory,
    default_owner_id,
    utc_now,
)
from app.core.logging import get_logger
from app.promotion.client import HubSpotClient, get_hubspot_client, to_property_payload
from app.promotion.dedupe import find_existing_company, normalize_phone
from app.promotion.eligibility import check_promotable
from app.promotion.exceptions import (
    HubSpotAuthError,
    HubSpotError,
    HubSpotRateLimitError,
    PromotionRefusedError,
    RefusalReason,
    RunNotPromotableError,
    UnprovenancedWriteError,
)
from app.promotion.mapping import (
    CompanyWrite,
    ContactWrite,
    company_write,
    contact_write,
    record_url,
)
from app.promotion.models import Promotion
from app.promotion.properties import ensure_properties
from app.promotion.repository import PromotionRepository
from app.promotion.schemas import (
    PROMOTE_COUNTS,
    REVIEW_COUNTS,
    DuplicateProspect,
    ObjectType,
    PromotedProspect,
    PromotionFailure,
    PromotionReport,
    PromotionStatus,
    ReviewDecision,
    ReviewOutcome,
    ReviewStatus,
    ReviewSyncReport,
    SearchFilter,
    SearchFilterGroup,
    SearchRequest,
)
from app.qualification.service import QualificationService
from app.routing.service import RoutingService
from app.sourcing.schemas import CandidateResponse, RunStatus
from app.sourcing.service import SourcingService

logger = get_logger(__name__)

RECOVERY_MIN_AGE = timedelta(minutes=10)
"""How old a ``pending_create`` row without a company id must be before its company is searched
for. HubSpot's search lags writes by "a few moments", with no SLA; a weekly, human-run command
makes waiting free."""

_PROMOTABLE_RUNS = frozenset({RunStatus.completed, RunStatus.degraded})
_DAILY_POLICY = "DAILY"
_REVIEW_PROPERTY = "lpe_review_status"
_CONTACT_RECOVERY_PROPERTIES = ["lpe_source_url", "lastname"]


class _PassStopped(Exception):
    """Every later HubSpot call would fail the same way: a bad token, or the daily quota."""


def _stops_the_pass(exc: HubSpotError) -> bool:
    if isinstance(exc, HubSpotAuthError):
        return True
    return isinstance(exc, HubSpotRateLimitError) and exc.policy == _DAILY_POLICY


@dataclass(frozen=True)
class _Row:
    """What a review sync reads from a ledger row, copied out of the ORM object.

    ``enrol`` rolls the session back when it loses a race, which expires every loaded object; an
    expired attribute read in async SQLAlchemy raises rather than reloading. A copy cannot expire.
    """

    id: UUID
    registry_id: str
    company_id: str
    contact_id: str

    @classmethod
    def of(cls, row: Promotion) -> "_Row":
        if row.hubspot_company_id is None or row.hubspot_contact_id is None:
            raise AssertionError(f"ledger row {row.id} is {row.status} without its HubSpot ids")
        return cls(
            id=row.id,
            registry_id=row.registry_id,
            company_id=row.hubspot_company_id,
            contact_id=row.hubspot_contact_id,
        )


@dataclass
class _Writes:
    """One candidate's two writes, rendered through the gate before anything is sent."""

    candidate: CandidateResponse
    company: CompanyWrite
    contact: ContactWrite
    phone_key: str | None
    domain_key: str | None


@dataclass
class _Tally:
    """A pass's running results, rendered into the frozen report at the end."""

    counts: dict[str, int]
    created: list[PromotedProspect] = field(default_factory=list[PromotedProspect])
    duplicates: list[DuplicateProspect] = field(default_factory=list[DuplicateProspect])
    failures: list[PromotionFailure] = field(default_factory=list[PromotionFailure])
    missing: dict[str, int] = field(default_factory=dict[str, int])

    def fail(self, registry_id: str, message: str) -> None:
        self.counts["failed"] += 1
        self.failures.append(PromotionFailure(registry_id=registry_id, message=message))


class PromotionService:
    """Promotion's operations. The HubSpot client comes from a factory, as in the cadence."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        hubspot: HubSpotFactory = get_hubspot_client,
        clock: Clock = utc_now,
    ) -> None:
        self._session = session
        self._repository = PromotionRepository(session)
        self._hubspot = hubspot
        self._clock = clock

    # -- promote -------------------------------------------------------------------------------

    async def promote_run(self, run_id: UUID, *, dry_run: bool = False) -> PromotionReport:
        """Promote a finished run's qualified candidates to HubSpot at ``pending_review``.

        Raises ``SourcingRunNotFoundError`` for an unknown run and :class:`RunNotPromotableError`
        for one still running, or failed. Per-candidate failures are counted and reported, never
        raised: one bad candidate never sinks the pass.
        """
        sourcing = SourcingService(self._session)
        run = await sourcing.get_run(run_id)
        if run.status not in _PROMOTABLE_RUNS:
            raise RunNotPromotableError(
                f"run {run_id} is {run.status.value}; only completed or degraded runs are "
                "promoted (a failed run's batch is sourced again as new candidates)"
            )
        candidates = await sourcing.list_candidates(run_id)
        suppressed = await QualificationService(self._session).suppressed_registry_ids(
            run.vertical, [candidate.registry_id for candidate in candidates]
        )
        routes = await self._route_labels(run_id)
        owner_id = default_owner_id()
        tally = _Tally(counts=dict.fromkeys(PROMOTE_COUNTS, 0))
        client = self._hubspot()
        stopped: str | None = None
        try:
            if dry_run:
                pending = await self._repository.list_by_status(
                    [PromotionStatus.pending_create], vertical=run.vertical
                )
                tally.counts["recovery_pending"] = len(pending)
            else:
                await ensure_properties(client)
                await self._recover(client, run.vertical, owner_id, tally)
            seen_keys: dict[str, str] = {}
            for candidate in candidates:
                await self._promote_one(
                    client,
                    candidate,
                    vertical=run.vertical,
                    suppressed=candidate.registry_id in suppressed,
                    route_label=routes.get(candidate.id),
                    owner_id=owner_id,
                    tally=tally,
                    dry_run=dry_run,
                    seen_keys=seen_keys,
                )
        except _PassStopped as exc:
            stopped = str(exc)
            logger.warning("promotion.service.pass_stopped", run_id=str(run_id), reason=stopped)

        report = PromotionReport(
            run_id=run_id,
            vertical=run.vertical,
            dry_run=dry_run,
            counts=tally.counts,
            created=tuple(tally.created),
            duplicates=tuple(tally.duplicates),
            failures=tuple(tally.failures),
            missing_field_tally=tally.missing,
            stopped=stopped,
        )
        logger.info(
            "promotion.service.pass_finished", run_id=str(run_id), dry_run=dry_run, **tally.counts
        )
        return report

    async def _route_labels(self, run_id: UUID) -> dict[UUID, str]:
        assignments = await RoutingService(self._session).list_assignments(run_id)
        return {assignment.candidate_id: assignment.cluster_label for assignment in assignments}

    async def _promote_one(
        self,
        client: HubSpotClient,
        candidate: CandidateResponse,
        *,
        vertical: str,
        suppressed: bool,
        route_label: str | None,
        owner_id: str | None,
        tally: _Tally,
        dry_run: bool,
        seen_keys: dict[str, str],
    ) -> None:
        tally.counts["considered"] += 1
        try:
            check_promotable(candidate, suppressed=suppressed)
        except PromotionRefusedError as refused:
            tally.counts[f"refused_{refused.reason.value}"] += 1
            if refused.reason is RefusalReason.missing_fields:
                for name in refused.fields:
                    tally.missing[name] = tally.missing.get(name, 0) + 1
            return
        if await self._repository.get_by_registry_id(vertical, candidate.registry_id) is not None:
            tally.counts["already_promoted"] += 1
            return
        writes = self._writes(candidate, vertical, route_label, owner_id, tally)
        if writes is None:
            return

        if dry_run:
            await self._dry_run_one(client, writes, tally, seen_keys)
            return

        row = await self._repository.claim(
            candidate,
            vertical=vertical,
            company_key=writes.company.key,
            dedupe_phone=writes.phone_key,
            dedupe_domain=writes.domain_key,
            name_ambiguous=writes.contact.name.ambiguous,
        )
        await self._session.commit()
        if row is None:
            # Another promote claimed this business between our read and our insert.
            tally.counts["already_promoted"] += 1
            return
        await self._dedupe_then_create(client, row, writes, vertical, tally)

    def _writes(
        self,
        candidate: CandidateResponse,
        vertical: str,
        route_label: str | None,
        owner_id: str | None,
        tally: _Tally,
    ) -> _Writes | None:
        """Both writes, gated up front; ``None`` (counted ``failed``) if either cannot pass."""
        fields = candidate.fields
        company = company_write(
            fields, vertical=vertical, route_label=route_label, owner_id=owner_id
        )
        contact = contact_write(fields, vertical=vertical, owner_id=owner_id)
        try:
            to_property_payload(company.properties)
            to_property_payload(contact.properties)
        except UnprovenancedWriteError as exc:
            # Eligibility passed, so this is a bug — but one that must not leave an orphan company.
            tally.fail(candidate.registry_id, exc.message)
            return None
        if contact.name.ambiguous:
            tally.counts["name_ambiguous"] += 1
            logger.info(
                "promotion.names.split_ambiguous",
                registry_id=candidate.registry_id,
                first=contact.name.first,
                last=contact.name.last,
            )
        phone = normalize_phone(fields.phone.value) if fields.phone is not None else ""
        return _Writes(
            candidate=candidate,
            company=company,
            contact=contact,
            phone_key=phone or None,
            domain_key=company.domain,
        )

    async def _dry_run_one(
        self,
        client: HubSpotClient,
        writes: _Writes,
        tally: _Tally,
        seen_keys: dict[str, str],
    ) -> None:
        """What a real pass would do with ``writes``: the same reads, nothing written."""
        registry_id = writes.candidate.registry_id
        keys = [key for key in (writes.phone_key, writes.domain_key) if key is not None]
        earlier = next((seen_keys[key] for key in keys if key in seen_keys), None)
        if earlier is None:
            row = await self._repository.find_by_dedupe_key(
                writes.company.meta.vertical,
                phone=writes.phone_key,
                domain=writes.domain_key,
                excluding_id=uuid4(),
            )
            earlier = row.registry_id if row is not None else None
        for key in keys:
            seen_keys.setdefault(key, registry_id)
        if earlier is not None:
            tally.counts["duplicate_in_ledger"] += 1
            tally.duplicates.append(self._duplicate(writes, duplicate_of=earlier))
            return
        try:
            match = await find_existing_company(
                client, domain=writes.domain_key, phone=writes.phone_key
            )
        except HubSpotError as exc:
            self._fail_or_stop(registry_id, exc, tally)
            return
        if match is not None:
            tally.counts["duplicate_in_hubspot"] += 1
            tally.duplicates.append(self._duplicate(writes, matched_company_id=match.id))
            return
        tally.counts["created"] += 1
        tally.created.append(self._prospect(writes, company_id=None, contact_id=None))

    async def _dedupe_then_create(
        self,
        client: HubSpotClient,
        row: Promotion,
        writes: _Writes,
        vertical: str,
        tally: _Tally,
        *,
        recovered: bool = False,
    ) -> None:
        """Ledger dedupe, portal dedupe, then both creates — for a claimed row with no company."""
        registry_id = row.registry_id
        twin = await self._repository.find_by_dedupe_key(
            vertical, phone=row.dedupe_phone, domain=row.dedupe_domain, excluding_id=row.id
        )
        if twin is not None:
            await self._mark_duplicate(row, duplicate_of_id=twin.id)
            tally.counts["duplicate_in_ledger"] += 1
            tally.duplicates.append(self._duplicate(writes, duplicate_of=twin.registry_id))
            return
        try:
            match = await find_existing_company(
                client, domain=row.dedupe_domain, phone=row.dedupe_phone
            )
        except HubSpotError as exc:
            self._fail_or_stop(registry_id, exc, tally)
            return
        if match is not None:
            await self._mark_duplicate(row, matched_company_id=match.id)
            tally.counts["duplicate_in_hubspot"] += 1
            tally.duplicates.append(self._duplicate(writes, matched_company_id=match.id))
            return
        try:
            company = await client.create_company(
                writes.company.properties, meta=writes.company.meta
            )
        except HubSpotError as exc:
            # Ambiguous: HubSpot may have made it. Left `pending_create`; the next pass recovers.
            self._fail_or_stop(registry_id, exc, tally)
            return
        await self._record_company(row, company.id)
        await self._finish_contact(client, row, company.id, writes, tally, recovered=recovered)

    async def _record_company(self, row: Promotion, company_id: str) -> None:
        moved = await self._repository.transition(
            row.id,
            from_status=PromotionStatus.pending_create,
            to_status=PromotionStatus.pending_create,
            hubspot_company_id=company_id,
        )
        await self._session.commit()
        if moved is None:
            raise AssertionError(f"ledger row {row.id} left pending_create while being promoted")

    async def _finish_contact(
        self,
        client: HubSpotClient,
        row: Promotion,
        company_id: str,
        writes: _Writes,
        tally: _Tally,
        *,
        recovered: bool,
        contact_id: str | None = None,
    ) -> None:
        """Create the contact (unless recovery found it) and move the row to ``pending_review``."""
        if contact_id is None:
            try:
                contact = await client.create_contact(
                    writes.contact.properties, meta=writes.contact.meta, company_id=company_id
                )
            except HubSpotError as exc:
                self._fail_or_stop(row.registry_id, exc, tally)
                return
            contact_id = contact.id
        moved = await self._repository.transition(
            row.id,
            from_status=PromotionStatus.pending_create,
            to_status=PromotionStatus.pending_review,
            hubspot_contact_id=contact_id,
        )
        await self._session.commit()
        if moved is None:
            raise AssertionError(f"ledger row {row.id} left pending_create while being promoted")
        tally.counts["recovered" if recovered else "created"] += 1
        tally.created.append(
            self._prospect(
                writes, company_id=company_id, contact_id=contact_id, recovered=recovered
            )
        )
        logger.info(
            "promotion.service.prospect_promoted",
            registry_id=row.registry_id,
            company_id=company_id,
            contact_id=contact_id,
            recovered=recovered,
        )

    async def _mark_duplicate(
        self,
        row: Promotion,
        *,
        matched_company_id: str | None = None,
        duplicate_of_id: UUID | None = None,
    ) -> None:
        await self._repository.transition(
            row.id,
            from_status=PromotionStatus.pending_create,
            to_status=PromotionStatus.duplicate,
            matched_company_id=matched_company_id,
            duplicate_of_id=duplicate_of_id,
        )
        await self._session.commit()
        logger.info(
            "promotion.service.duplicate_found",
            registry_id=row.registry_id,
            matched_company_id=matched_company_id,
            duplicate_of_id=None if duplicate_of_id is None else str(duplicate_of_id),
        )

    def _fail_or_stop(self, registry_id: str, exc: HubSpotError, tally: _Tally) -> None:
        tally.fail(registry_id, exc.message)
        logger.warning(
            "promotion.service.create_failed",
            registry_id=registry_id,
            error=type(exc).__name__,
            detail=exc.message,
        )
        if _stops_the_pass(exc):
            raise _PassStopped(exc.message) from exc

    # -- recovery ------------------------------------------------------------------------------

    async def _recover(
        self, client: HubSpotClient, vertical: str, owner_id: str | None, tally: _Tally
    ) -> None:
        """Finish every ``pending_create`` row of this vertical an earlier pass left behind."""
        rows = await self._repository.list_by_status(
            [PromotionStatus.pending_create], vertical=vertical
        )
        for row in rows:
            candidate = await self._candidate_of(row)
            routes = await self._route_labels(row.run_id)
            writes = self._writes(candidate, vertical, routes.get(candidate.id), owner_id, tally)
            if writes is None:
                continue
            if row.hubspot_company_id is not None:
                await self._recover_contact(client, row, row.hubspot_company_id, writes, tally)
                continue
            if self._clock() - row.created_at < RECOVERY_MIN_AGE:
                tally.counts["recovery_deferred"] += 1
                logger.info("promotion.service.recovery_deferred", registry_id=row.registry_id)
                continue
            try:
                found = await self._find_company_by_key(client, row.company_key)
            except HubSpotError as exc:
                self._fail_or_stop(row.registry_id, exc, tally)
                continue
            if found is None:
                await self._dedupe_then_create(client, row, writes, vertical, tally, recovered=True)
                continue
            await self._record_company(row, found)
            await self._recover_contact(client, row, found, writes, tally)

    async def _candidate_of(self, row: Promotion) -> CandidateResponse:
        candidates = await SourcingService(self._session).list_candidates(row.run_id)
        return next(candidate for candidate in candidates if candidate.id == row.candidate_id)

    async def _find_company_by_key(self, client: HubSpotClient, key: str) -> str | None:
        """The company carrying this ``lpe_source_url`` — an interrupted create, found again."""
        response = await client.search(
            ObjectType.companies,
            SearchRequest(
                filter_groups=[
                    SearchFilterGroup(
                        filters=[SearchFilter(property_name="lpe_source_url", value=key)]
                    )
                ],
                properties=["lpe_source_url", "name"],
                limit=2,
            ),
        )
        if not response.results:
            return None
        return response.results[0].id

    async def _recover_contact(
        self,
        client: HubSpotClient,
        row: Promotion,
        company_id: str,
        writes: _Writes,
        tally: _Tally,
    ) -> None:
        """Adopt our contact if the company already has it, else create it.

        The contact was associated in its own create request, so the association walk sees it at
        once — no search, no lag (``find_task_by_key``'s rule).
        """
        try:
            linked = await client.list_associated_ids(
                ObjectType.companies, company_id, ObjectType.contacts
            )
            contacts = (
                await client.batch_read(ObjectType.contacts, linked, _CONTACT_RECOVERY_PROPERTIES)
                if linked
                else []
            )
        except HubSpotError as exc:
            self._fail_or_stop(row.registry_id, exc, tally)
            return
        ours = next(
            (
                contact.id
                for contact in contacts
                if contact.properties.get("lpe_source_url") == writes.contact.source_url
                and contact.properties.get("lastname") == writes.contact.name.last
            ),
            None,
        )
        await self._finish_contact(
            client, row, company_id, writes, tally, recovered=True, contact_id=ours
        )

    # -- report rows ---------------------------------------------------------------------------

    @staticmethod
    def _legal_name(writes: _Writes) -> str:
        legal_name = writes.candidate.fields.legal_name
        return legal_name.value if legal_name is not None else writes.candidate.registry_id

    def _prospect(
        self,
        writes: _Writes,
        *,
        company_id: str | None,
        contact_id: str | None,
        recovered: bool = False,
    ) -> PromotedProspect:
        name = writes.contact.name
        return PromotedProspect(
            registry_id=writes.candidate.registry_id,
            legal_name=self._legal_name(writes),
            company_id=company_id,
            contact_id=contact_id,
            company_url=None
            if company_id is None
            else record_url(ObjectType.companies, company_id),
            contact_url=None if contact_id is None else record_url(ObjectType.contacts, contact_id),
            route_label=writes.company.meta.route_cluster,
            first_name=name.first,
            last_name=name.last,
            name_ambiguous=name.ambiguous,
            recovered=recovered,
        )

    def _duplicate(
        self,
        writes: _Writes,
        *,
        matched_company_id: str | None = None,
        duplicate_of: str | None = None,
    ) -> DuplicateProspect:
        return DuplicateProspect(
            registry_id=writes.candidate.registry_id,
            legal_name=self._legal_name(writes),
            matched_company_id=matched_company_id,
            matched_company_url=None
            if matched_company_id is None
            else record_url(ObjectType.companies, matched_company_id),
            duplicate_of=duplicate_of,
        )

    # -- review --------------------------------------------------------------------------------

    async def sync_reviews(self, *, dry_run: bool = False) -> ReviewSyncReport:
        """Read the founder's review decisions back and act on them.

        ``accepted`` → enrolled in the cadence (its first task is the cadence's to create);
        ``rejected`` → recorded, HubSpot left alone; a company that is gone → ``withdrawn``;
        anything else → waiting. Rows an earlier sync left ``enrolling`` are retried first —
        ``enrol`` is safe to retry.
        """
        counts = dict.fromkeys(REVIEW_COUNTS, 0)
        decisions: list[ReviewDecision] = []
        failures: list[PromotionFailure] = []
        unrecognised: list[str] = []
        client = self._hubspot()
        stopped: str | None = None
        try:
            enrolling = await self._repository.list_by_status([PromotionStatus.enrolling])
            for row in [_Row.of(item) for item in enrolling]:
                await self._enrol(row, counts, decisions, failures, dry_run=dry_run)
            loaded = await self._repository.list_by_status([PromotionStatus.pending_review])
            pending = [_Row.of(item) for item in loaded]
            statuses = await self._review_statuses(client, pending)
            for row in pending:
                counts["read"] += 1
                await self._decide(
                    row,
                    statuses,
                    counts=counts,
                    decisions=decisions,
                    failures=failures,
                    unrecognised=unrecognised,
                    dry_run=dry_run,
                )
        except _PassStopped as exc:
            stopped = str(exc)
            logger.warning("promotion.service.pass_stopped", reason=stopped)
        except HubSpotError as exc:
            # The batch read itself failed: nothing was decided.
            if not _stops_the_pass(exc):
                raise
            stopped = exc.message
            logger.warning("promotion.service.pass_stopped", reason=stopped)

        logger.info("promotion.service.review_read", dry_run=dry_run, **counts)
        return ReviewSyncReport(
            dry_run=dry_run,
            counts=counts,
            decisions=tuple(decisions),
            failures=tuple(failures),
            unrecognised=tuple(sorted(set(unrecognised))),
            stopped=stopped,
        )

    async def _review_statuses(
        self, client: HubSpotClient, rows: Sequence[_Row]
    ) -> dict[str, str | None]:
        """Each live company's ``lpe_review_status``; a company absent from the map is gone."""
        if not rows:
            return {}
        ids = [row.company_id for row in rows]
        companies = await client.batch_read(ObjectType.companies, ids, [_REVIEW_PROPERTY])
        return {company.id: company.properties.get(_REVIEW_PROPERTY) for company in companies}

    async def _decide(
        self,
        row: _Row,
        statuses: dict[str, str | None],
        *,
        counts: dict[str, int],
        decisions: list[ReviewDecision],
        failures: list[PromotionFailure],
        unrecognised: list[str],
        dry_run: bool,
    ) -> None:
        company_id = row.company_id
        if company_id not in statuses:
            if not dry_run:
                await self._move(row, PromotionStatus.withdrawn)
            counts["withdrawn"] += 1
            decisions.append(self._decision(row, ReviewOutcome.withdrawn))
            return
        value = (statuses[company_id] or "").strip()
        if value == ReviewStatus.rejected.value:
            if not dry_run:
                await self._move(row, PromotionStatus.rejected)
            counts["rejected"] += 1
            decisions.append(self._decision(row, ReviewOutcome.rejected))
            return
        if value == ReviewStatus.accepted.value:
            if dry_run:
                counts["enrolled"] += 1
                decisions.append(self._decision(row, ReviewOutcome.enrolled))
                return
            claimed = await self._move(row, PromotionStatus.enrolling)
            if claimed is None:
                return  # another sync has it
            await self._enrol(claimed, counts, decisions, failures, dry_run=False)
            return
        counts["waiting"] += 1
        if value and value != ReviewStatus.pending_review.value:
            unrecognised.append(value)
            logger.warning(
                "promotion.service.status_unrecognised", registry_id=row.registry_id, value=value
            )

    async def _move(self, row: _Row, to_status: PromotionStatus) -> _Row | None:
        """``pending_review`` → a decision, stamped ``decided_at``; committed. ``None``: lost."""
        moved = await self._repository.transition(
            row.id,
            from_status=PromotionStatus.pending_review,
            to_status=to_status,
            decided_at=self._clock(),
        )
        await self._session.commit()
        return None if moved is None else _Row.of(moved)

    async def _enrol(
        self,
        row: _Row,
        counts: dict[str, int],
        decisions: list[ReviewDecision],
        failures: list[PromotionFailure],
        *,
        dry_run: bool,
    ) -> None:
        """Hand an accepted contact to the cadence, then mark the row ``enrolled``."""
        if dry_run:
            counts["enrolled"] += 1
            decisions.append(self._decision(row, ReviewOutcome.enrolled))
            return
        registry_id = row.registry_id
        cadence = CadenceService(self._session, hubspot=self._hubspot, clock=self._clock)
        first_due_at: datetime | None = None
        orphan: str | None = None
        try:
            state = await cadence.enrol(row.contact_id, company_id=row.company_id)
            first_due_at = state.due_at
        except AlreadyEnrolledError as exc:
            # The contact is in the cadence: the hand-off has happened, whoever made it.
            orphan = exc.orphan_task_id
        except HubSpotError as exc:
            counts["failed"] += 1
            failures.append(PromotionFailure(registry_id=registry_id, message=exc.message))
            logger.warning(
                "promotion.service.enrol_failed", registry_id=registry_id, detail=exc.message
            )
            if _stops_the_pass(exc):
                raise _PassStopped(exc.message) from exc
            return
        moved = await self._repository.transition(
            row.id,
            from_status=PromotionStatus.enrolling,
            to_status=PromotionStatus.enrolled,
            enrolled_at=self._clock(),
        )
        await self._session.commit()
        if moved is None:
            return
        counts["enrolled"] += 1
        decisions.append(
            self._decision(
                row, ReviewOutcome.enrolled, first_due_at=first_due_at, orphan_task_id=orphan
            )
        )
        logger.info("promotion.service.prospect_enrolled", registry_id=registry_id)

    @staticmethod
    def _decision(
        row: _Row,
        outcome: ReviewOutcome,
        *,
        first_due_at: datetime | None = None,
        orphan_task_id: str | None = None,
    ) -> ReviewDecision:
        return ReviewDecision(
            registry_id=row.registry_id,
            outcome=outcome,
            company_id=row.company_id,
            contact_id=row.contact_id,
            company_url=record_url(ObjectType.companies, row.company_id),
            first_due_at=first_due_at,
            orphan_task_id=orphan_task_id,
        )
