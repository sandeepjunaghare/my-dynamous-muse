"""Adoption (T13): bring the prospects the founders were already working by hand into the cadence.

**Why it exists.** Spike 1 closed as *discipline*: 22 hand-made follow-up tasks, one ticked, nothing
logged after day 4. The machine (T11) had no live prospects; this is how those prospects reach it.

**Reconstruct, never reset.** Each contact's place in the cycle is rebuilt from the activity already
logged on it, through the same :func:`~app.cadence.sync.plan_advance` the daily sync runs — so a
prospect touched twice by hand resumes at touch three, and adoption and the next sync can never
disagree about what history means. Four decisions shape it (human, 2026-10-10):

1. **An explicit roster.** A dry run lists the candidates; a person writes a TOML roster marking
   each contact ``adopt`` or ``park`` (refusals are a judgment no rule finds), with an optional
   ``start`` that overrides the reconstructed position.
2. **Evidence from the contact only** — exactly what the sync reads afterwards. A touch logged only
   on the company is missed; the roster's ``start`` is the fix.
3. **Old hand tasks are left alone and listed.** This slice never writes to the founder's tasks.
4. **No ``candidate`` rows, and no field writes.** Adoption creates tasks and schedule rows only,
   so the provenance write-gate has nothing to check. Honest ``manual_hubspot_entry`` provenance
   for these records belongs to T9, the first place one of their fields would be written.

**The anchor rule.** A reconstruction that ends on an activity is anchored *on* it,
``(hs_timestamp, "notes:<id>")``, and the row must store the ref as well as the time, or the first
sync credits that activity a second time.

**History is not M5.** No ``cadence.sync.touch_done`` is logged here: M5 counts touches the machine
closed, and reconstructed history is not that.

**Safe to re-run.** A contact already enrolled is reported and skipped. A real run holds the sync
lock, as ``park`` does; a dry run takes no lock and writes nothing.
"""

import re
import tomllib
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.cadence.exceptions import (
    AlreadyEnrolledError,
    RosterError,
    SyncRunningError,
    UnadoptableContactError,
)
from app.cadence.machine import (
    TASK_KEY_PREFIX,
    CadencePosition,
    Touch,
    due_at_enrolment,
)
from app.cadence.repository import CadenceRepository
from app.cadence.schemas import (
    Activity,
    AdoptionPlan,
    AdoptionReport,
    AdvanceStep,
    CompanyOnlyTask,
    HandTask,
    RosterAction,
    SyncFailure,
)
from app.cadence.service import (
    OWNER_UNSET_WARNING,
    CadenceService,
    Clock,
    HubSpotFactory,
    default_owner_id,
    utc_now,
)
from app.cadence.sync import OutcomeReader, parse_hubspot_datetime, plan_advance
from app.core.logging import get_logger
from app.promotion.client import HubSpotClient, get_hubspot_client
from app.promotion.exceptions import HubSpotError
from app.promotion.schemas import (
    ObjectType,
    SearchFilter,
    SearchFilterGroup,
    SearchOperator,
    SearchRequest,
    TaskStatus,
)

logger = get_logger(__name__)

OPEN_TASK_SEARCH_LIMIT = 200
"""HubSpot's largest search page. One page is enough for a one-off listing; more is warned about."""

CONTACT_PROPERTIES = ("firstname", "lastname", "createdate")
HAND_TASK_PROPERTIES = ("hs_task_subject", "hs_task_status", "hs_task_body", "hs_timestamp")

_POSITION = re.compile(r"(?P<cycle>\d+)-(?P<touch>[a-z]+)", re.ASCII)


def parse_position(text: str) -> CadencePosition:
    """``"2-call"`` → cycle 2, the call — the suffix of a :func:`~app.cadence.machine.task_key`.

    Raises ``ValueError`` for anything that is not a real position.
    """
    match = _POSITION.fullmatch(text.strip())
    try:
        if match is None:
            raise ValueError(text)
        return CadencePosition(cycle=int(match["cycle"]), touch=Touch(match["touch"]))
    except ValueError as exc:
        raise ValueError(
            f"{text!r} is not a cadence position — write it like '2-call': cycle 1 to 3, then "
            "call, voicemail or email"
        ) from exc


class RosterEntry(BaseModel):
    """One contact in the roster, and what to do with it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    contact: str
    """The HubSpot contact id."""
    action: RosterAction
    start: str | None = None
    """``"2-call"``: start here instead of where the evidence says. ``adopt`` only."""
    company: str | None = None
    """The company the cadence tasks are associated with, when the contact has none or several."""

    @field_validator("contact", "company", mode="before")
    @classmethod
    def _id_as_text(cls, value: object) -> object:
        """An id written unquoted in TOML is an integer; it is the same id."""
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
        return value

    @field_validator("contact", "company")
    @classmethod
    def _digits_only(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not (stripped.isascii() and stripped.isdigit()):
            raise ValueError(f"{value!r} is not a HubSpot id — ids are digits only")
        return stripped

    @field_validator("start")
    @classmethod
    def _a_real_position(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parse_position(value)
        return value.strip()

    @model_validator(mode="after")
    def _start_only_when_adopting(self) -> Self:
        if self.start is not None and self.action is not RosterAction.adopt:
            raise ValueError("`start` applies only to `adopt` — a parked contact has no next touch")
        return self

    @property
    def start_position(self) -> CadencePosition | None:
        """``start``, parsed."""
        return None if self.start is None else parse_position(self.start)


class Roster(BaseModel):
    """The human's decision, per contact: adopt or park."""

    model_config = ConfigDict(extra="forbid")

    prospect: list[RosterEntry] = Field(min_length=1)

    @model_validator(mode="after")
    def _one_entry_per_contact(self) -> Self:
        seen: set[str] = set()
        for entry in self.prospect:
            if entry.contact in seen:
                raise ValueError(f"contact {entry.contact} appears more than once")
            seen.add(entry.contact)
        return self


def load_roster(path: Path) -> Roster:
    """Read and validate a roster file. Every way it can be wrong is one :class:`RosterError`."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RosterError(f"roster {path}: cannot be read ({exc.strerror or exc})") from exc
    except UnicodeDecodeError as exc:
        raise RosterError(f"roster {path}: not UTF-8 text — save it as UTF-8") from exc
    try:
        return Roster.model_validate(tomllib.loads(text))
    except tomllib.TOMLDecodeError as exc:
        raise RosterError(f"roster {path}: not valid TOML — {exc}") from exc
    except ValidationError as exc:
        raise RosterError(f"roster {path}: {_describe(exc)}") from exc


def _describe(exc: ValidationError) -> str:
    """The first few problems, located the way the person sees the file: ``prospect 2.start``."""
    errors = exc.errors()
    problems: list[str] = []
    for error in errors[:3]:
        where = _where(error["loc"])
        message = error["msg"].removeprefix("Value error, ")
        problems.append(f"{where}: {message}" if where else message)
    more = len(errors) - len(problems)
    return "; ".join(problems) + (f" (and {more} more)" if more > 0 else "")


def _where(loc: tuple[int | str, ...]) -> str:
    """``("prospect", 1, "start")`` → ``"prospect 2.start"``: entries are numbered from one."""
    parts: list[str] = []
    for part in loc:
        if isinstance(part, int) and parts:
            parts[-1] = f"{parts[-1]} {part + 1}"
        else:
            parts.append(str(part))
    return ".".join(parts)


@dataclass(frozen=True)
class Reconstruction:
    """Where one contact's logged history puts it, and what adoption would store."""

    steps: tuple[AdvanceStep, ...]
    """The touches the history closes — shown as evidence even when overridden."""
    start: CadencePosition | None
    """The touch to start at; ``None`` when all nine are already done, and the contact parks."""
    due_at: datetime | None
    anchor_at: datetime
    anchor_ref: str | None
    overridden: bool

    @property
    def finished(self) -> bool:
        """All nine touches are already logged."""
        return self.start is None

    @property
    def last_position(self) -> CadencePosition:
        """The position a parked row records: the start, or else the last touch closed."""
        if self.start is not None:
            return self.start
        return self.steps[-1].position if self.steps else CadencePosition.first()


def reconstruct(
    activities: Sequence[Activity],
    *,
    since: datetime,
    now: datetime,
    override: CadencePosition | None,
) -> Reconstruction:
    """A contact's starting row, from its logged history. Pure.

    The history runs through :func:`plan_advance` from touch one, anchored at ``since`` (when the
    contact was created) with no task, so notes count exactly as the sync counts them.

    * nothing matched → the first call, due today, anchored at ``since``
    * touches closed → the next touch, due as the sync would schedule it, anchored on the
      activity that closed the last one (its ref included)
    * all nine closed → finished: the contact parks
    * an ``override`` → that touch, due today, anchored at ``now`` — "from here, from now"
    """
    plan = plan_advance(CadencePosition.first(), since, None, None, activities, now)
    if override is not None:
        return Reconstruction(
            steps=plan.steps,
            start=override,
            due_at=due_at_enrolment(now),
            anchor_at=now,
            anchor_ref=None,
            overridden=True,
        )
    if not plan.changed:
        return Reconstruction(
            steps=(),
            start=CadencePosition.first(),
            due_at=due_at_enrolment(now),
            anchor_at=since,
            anchor_ref=None,
            overridden=False,
        )
    return Reconstruction(
        steps=plan.steps,
        start=plan.next_position,
        due_at=plan.next_due_at,
        anchor_at=plan.anchor_at or since,
        anchor_ref=plan.anchor_ref,
        overridden=False,
    )


class Adopter:
    """Discovers hand-worked prospects, rehearses a roster, and applies it.

    Every decision in a run is made from **one** ``now`` (the single-moment rule from PR #12), and
    each contact is read and planned before anything is written for it.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        hubspot: HubSpotFactory = get_hubspot_client,
        clock: Clock = utc_now,
    ) -> None:
        self._session = session
        self._repository = CadenceRepository(session)
        self._service = CadenceService(session, hubspot=hubspot, clock=clock)
        self._hubspot = hubspot
        self._clock = clock

    async def discover(self) -> AdoptionReport:
        """The dry run with no roster: every contact on an open hand task, planned. Reads only.

        Open tasks come from search, whose index lags writes by seconds — harmless for a one-off
        listing. Our own tasks (carrying the key) are left out; a task with no contact is listed
        under ``company_only``, since a cadence needs a contact, with the contacts its company
        already has — so a duplicate task is told apart from a company that truly has nobody.
        """
        now = self._clock()
        report = self._report(now, dry_run=True)
        client = self._hubspot()
        found = await client.search(
            ObjectType.tasks,
            SearchRequest(
                filter_groups=[
                    SearchFilterGroup(
                        filters=[
                            SearchFilter(
                                property_name="hs_task_status",
                                operator=SearchOperator.neq,
                                value=TaskStatus.completed.value,
                            )
                        ]
                    )
                ],
                properties=["hs_task_subject", "hs_task_body"],
                limit=OPEN_TASK_SEARCH_LIMIT,
            ),
        )
        if found.total > len(found.results):
            report.warnings.append(
                f"more than {OPEN_TASK_SEARCH_LIMIT} open tasks ({found.total}) — the listing is "
                "truncated; name the missing contacts in the roster by hand"
            )
        contacts: dict[str, None] = {}
        for task in found.results:
            if TASK_KEY_PREFIX in (task.properties.get("hs_task_body") or ""):
                continue
            linked = await client.list_associated_ids(
                ObjectType.tasks, task.id, ObjectType.contacts
            )
            for contact in linked:
                contacts.setdefault(contact)
            if not linked:
                companies = await client.list_associated_ids(
                    ObjectType.tasks, task.id, ObjectType.companies
                )
                company_contacts: dict[str, None] = {}
                for company in companies:
                    for contact in await client.list_associated_ids(
                        ObjectType.companies, company, ObjectType.contacts
                    ):
                        company_contacts.setdefault(contact)
                report.company_only.append(
                    CompanyOnlyTask(
                        task_id=task.id,
                        subject=task.properties.get("hs_task_subject") or "",
                        company_ids=companies,
                        company_contacts=list(company_contacts),
                    )
                )
        logger.info(
            "cadence.adoption.run_started", mode="discover", contacts=len(contacts), dry_run=True
        )
        entries = [RosterEntry(contact=contact, action=RosterAction.adopt) for contact in contacts]
        await self._plan_all(entries, now, report, OutcomeReader(client), client)
        return self._completed(report, mode="discover")

    async def rehearse(self, roster: Roster) -> AdoptionReport:
        """What applying ``roster`` would do. Reads only: no lock, no task, no row."""
        now = self._clock()
        report = self._report(now, dry_run=True)
        logger.info("cadence.adoption.run_started", mode="rehearse", contacts=len(roster.prospect))
        client = self._hubspot()
        await self._plan_all(roster.prospect, now, report, OutcomeReader(client), client)
        return self._completed(report, mode="rehearse")

    async def apply(self, roster: Roster) -> AdoptionReport:
        """Adopt or park every roster entry, under the sync lock.

        Each entry is its own unit of work: an entry already enrolled is skipped, and a HubSpot,
        validation or database failure on one is rolled back and reported while the rest carry
        on. Re-running the same roster adopts nothing new.
        """
        now = self._clock()
        report = self._report(now, dry_run=False)
        async with self._repository.sync_lock() as acquired:
            if not acquired:
                raise SyncRunningError(
                    "a cadence sync is running — adopt again when it has finished"
                )
            logger.info("cadence.adoption.run_started", mode="apply", contacts=len(roster.prospect))
            client = self._hubspot()
            reader = OutcomeReader(client)
            for entry in roster.prospect:

                async def adopt_one(entry: RosterEntry = entry) -> None:
                    plan = await self._plan_contact(entry, now, reader, client)
                    await self._apply(plan, report)
                    report.plans.append(plan)

                await self._guarded(report, entry.contact, adopt_one)
        return self._completed(report, mode="apply")

    # -- one contact -------------------------------------------------------------------------

    async def _plan_all(
        self,
        entries: Sequence[RosterEntry],
        now: datetime,
        report: AdoptionReport,
        reader: OutcomeReader,
        client: HubSpotClient,
    ) -> None:
        for entry in entries:

            async def plan_one(entry: RosterEntry = entry) -> None:
                report.plans.append(await self._plan_contact(entry, now, reader, client))

            await self._guarded(report, entry.contact, plan_one)

    async def _plan_contact(
        self,
        entry: RosterEntry,
        now: datetime,
        reader: OutcomeReader,
        client: HubSpotClient,
    ) -> AdoptionPlan:
        """Read one contact and decide its starting row. Reads only."""
        contact = entry.contact
        if await self._repository.get_by_contact(contact) is not None:
            raise AlreadyEnrolledError(
                f"contact {contact} already has a cadence", contact_id=contact
            )
        records = await client.batch_read(ObjectType.contacts, [contact], CONTACT_PROPERTIES)
        if not records:
            raise UnadoptableContactError(
                f"contact {contact} does not exist in HubSpot — deleted or merged? Fix the roster",
                contact_id=contact,
                code="contact_not_found",
            )
        record = records[0]
        since = record.created_at or parse_hubspot_datetime(record.properties.get("createdate"))
        if since is None:
            raise UnadoptableContactError(
                f"contact {contact} has no creation date in HubSpot, so there is nothing to count "
                "its evidence from",
                contact_id=contact,
                code="contact_created_at_missing",
            )
        activities = await reader.activities(contact)
        hand_tasks = await self._hand_tasks(client, contact)
        company_id, warnings = await self._company(client, entry)
        result = reconstruct(activities, since=since, now=now, override=entry.start_position)
        name = " ".join(
            part
            for part in (record.properties.get("firstname"), record.properties.get("lastname"))
            if part
        )
        return AdoptionPlan(
            hubspot_contact_id=contact,
            display_name=name or None,
            company_id=company_id,
            action=entry.action,
            steps=list(result.steps),
            start=None if entry.action is RosterAction.park else result.start,
            due_at=None if entry.action is RosterAction.park else result.due_at,
            anchor_at=result.anchor_at,
            anchor_ref=result.anchor_ref,
            overridden=result.overridden,
            finished=result.finished,
            hand_tasks=hand_tasks,
            warnings=warnings,
            parked_position=(
                result.last_position
                if entry.action is RosterAction.park or result.finished
                else None
            ),
        )

    async def _hand_tasks(self, client: HubSpotClient, contact: str) -> list[HandTask]:
        """The contact's tasks that are not ours — open or ticked — for the founder to see.

        Read through associations, not search, so a task made a moment ago is not missed.
        """
        ids = await client.list_associated_ids(ObjectType.contacts, contact, ObjectType.tasks)
        if not ids:
            return []
        records = await client.batch_read(ObjectType.tasks, ids, HAND_TASK_PROPERTIES)
        return [
            HandTask(
                task_id=record.id,
                subject=record.properties.get("hs_task_subject") or "",
                completed=record.properties.get("hs_task_status") == TaskStatus.completed.value,
                due_at=parse_hubspot_datetime(record.properties.get("hs_timestamp")),
            )
            for record in sorted(records, key=lambda record: (len(record.id), record.id))
            if TASK_KEY_PREFIX not in (record.properties.get("hs_task_body") or "")
        ]

    async def _company(
        self, client: HubSpotClient, entry: RosterEntry
    ) -> tuple[str | None, list[str]]:
        """The roster's company, else the contact's only one. None or several is a warning."""
        if entry.company is not None:
            return entry.company, []
        ids = await client.list_associated_ids(
            ObjectType.contacts, entry.contact, ObjectType.companies
        )
        if len(ids) == 1:
            return ids[0], []
        if not ids:
            return None, ["no company associated — the cadence tasks will carry the contact only"]
        return None, [
            f"{len(ids)} companies associated ({', '.join(ids)}) — set `company` in the roster "
            "to choose one; until then the tasks carry the contact only"
        ]

    async def _apply(self, plan: AdoptionPlan, report: AdoptionReport) -> None:
        contact = plan.hubspot_contact_id
        if plan.parks:
            state = await self._service.adopt_parked(
                contact,
                company_id=plan.company_id,
                position=plan.parked_position or CadencePosition.first(),
                anchor_at=plan.anchor_at,
                anchor_ref=plan.anchor_ref,
            )
            report.parked.append(state)
            logger.info(
                "cadence.adoption.prospect_parked",
                contact_id=contact,
                finished=plan.finished,
                cycle=state.cycle,
                touch=state.touch.value,
            )
            return
        state = await self._service.enrol(
            contact,
            company_id=plan.company_id,
            start=plan.start,
            anchor_at=plan.anchor_at,
            anchor_ref=plan.anchor_ref,
            due_at=plan.due_at,
        )
        report.adopted.append(state)
        logger.info(
            "cadence.adoption.prospect_adopted",
            contact_id=contact,
            cycle=state.cycle,
            touch=state.touch.value,
            task_id=state.hubspot_task_id,
            evidence=len(plan.steps),
            overridden=plan.overridden,
        )

    async def _guarded(
        self, report: AdoptionReport, contact: str, work: Callable[[], Awaitable[None]]
    ) -> None:
        """Run one contact's work; an expected failure is that contact's, and the run goes on.

        Mirrors the sync's per-prospect handling: roll back what was not committed, report it.
        """
        try:
            await work()
        except AlreadyEnrolledError as exc:
            report.already_enrolled.append(contact)
            if exc.orphan_task_id is not None:
                report.orphaned_tasks.append(exc.orphan_task_id)
            logger.info("cadence.adoption.already_enrolled", contact_id=contact)
        except UnadoptableContactError as exc:
            await self._fail(report, contact, exc.code, exc.message)
        except HubSpotError as exc:
            await self._fail(report, contact, exc.code, exc.message)
        except ValidationError as exc:
            await self._fail(
                report,
                contact,
                "invalid_hubspot_response",
                f"HubSpot answered a body that failed validation ({exc.title}, "
                f"{exc.error_count()} error(s))",
            )
        except SQLAlchemyError as exc:
            await self._fail(report, contact, "database_error", str(exc).splitlines()[0])

    async def _fail(self, report: AdoptionReport, contact: str, code: str, message: str) -> None:
        await self._session.rollback()
        logger.error(
            "cadence.adoption.prospect_failed", contact_id=contact, code=code, error=message
        )
        report.failures.append(SyncFailure(hubspot_contact_id=contact, error=message, code=code))

    # -- the report --------------------------------------------------------------------------

    def _report(self, now: datetime, *, dry_run: bool) -> AdoptionReport:
        report = AdoptionReport(ran_at=now, dry_run=dry_run)
        if default_owner_id() is None:
            report.warnings.append(OWNER_UNSET_WARNING)
        return report

    def _completed(self, report: AdoptionReport, *, mode: str) -> AdoptionReport:
        logger.info(
            "cadence.adoption.run_completed",
            mode=mode,
            dry_run=report.dry_run,
            planned=len(report.plans),
            adopted=len(report.adopted),
            parked=len(report.parked),
            already_enrolled=len(report.already_enrolled),
            failures=len(report.failures),
        )
        return report
