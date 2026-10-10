"""Fixtures for the cadence suite: a controllable clock and a stateful fake HubSpot.

**No test here touches the portal.** :class:`FakeHubSpot` is built on T3's :class:`MockPortal`,
which raises on any request it has no route for — so "fixtures only" stays a proven property. It
keeps a little state (tasks, logged engagements) so a test can walk three whole cycles: complete a
task, log a call, advance the clock, sync, and see what the machine did. Every body it serves has
the same shape as the recorded fixtures in ``tests/promotion/fixtures/`` — ids as strings,
``createdAt``/``updatedAt`` as ISO-8601, ``toObjectId`` as an integer on associations.
"""

import json
import re
from collections.abc import AsyncGenerator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import cast

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.cadence.adoption import Adopter
from app.cadence.schemas import ActivityKind
from app.cadence.service import CadenceService
from app.promotion.client import API_VERSION, HubSpotClient
from tests.promotion.conftest import MockPortal, load_fixture, make_client

START = datetime(2026, 10, 5, 14, 0, tzinfo=UTC)
"""Monday 2026-10-05, 9:00 in Dallas (CDT, UTC-5)."""

CONTACT = "400112233"
COMPANY = "7009876543"

_ASSOCIATIONS = re.compile(
    r"/(?P<source>contacts|tasks|companies)/(?P<object>[^/]+)/associations/(?P<kind>[a-z]+)$"
)
_TASK_TO_COMPANY = 192
"""HubSpot's task→company association type, as the gateway writes it."""
_ENGAGEMENT_BATCH = re.compile(r"/(?P<kind>calls|emails|notes|meetings)/batch/read$")


def iso(moment: datetime) -> str:
    """HubSpot's wire format for datetimes: UTC, milliseconds, ``Z``."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + (
        f"{moment.microsecond // 1000:03d}Z"
    )


class FakeClock:
    """A clock a test moves by hand. Shared by the service and the fake portal."""

    def __init__(self, now: datetime = START) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> datetime:
        self.now = self.now + timedelta(**delta)
        return self.now


class FakeHubSpot:
    """Just enough of a HubSpot portal to run the cadence against, all of it in memory."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.tasks: dict[str, dict[str, object]] = {}
        self.created: list[dict[str, object]] = []
        """Every task-create body received, in order — what a test asserts the machine asked for."""
        self.engagements: dict[str, dict[str, dict[str, object]]] = {
            kind.value: {} for kind in ActivityKind
        }
        self.contact_engagements: dict[str, list[tuple[str, str]]] = {}
        self.fail_creates = 0
        """Answer this many task creates with a 500 **without** creating them — the unambiguous
        failure, where retrying would be safe."""
        self.lose_create_responses = 0
        """Create this many tasks for real, then answer each with a 500 — the ambiguous failure
        the gateway refuses to retry, because HubSpot did the work and failed to say so."""
        self.malformed_associations_for: set[str] = set()
        """Contacts whose association reads answer a body that fails validation."""
        self.unrecognised_associations_for: set[str] = set()
        """Contacts whose association reads answer a well-formed body of an unknown shape."""
        self.contacts: dict[str, dict[str, object]] = {}
        self.contact_companies: dict[str, list[str]] = {}
        self.task_companies: dict[str, list[str]] = {}
        self.search_total_extra = 0
        """Report this many more open tasks than the page carries — a truncated search."""
        self._next_id = 88_000_000
        self.portal = MockPortal(
            [
                ("POST", "/tasks/search", self._search_tasks),
                ("POST", "/contacts/batch/read", self._read_contacts),
                ("POST", "/tasks/batch/read", self._read_tasks),
                ("POST", "/batch/read", self._read_engagements),
                ("POST", f"/crm/objects/{API_VERSION}/tasks", self._create_task),
                ("GET", "/associations/", self._associations),
            ]
        )

    # -- what a founder does in HubSpot -------------------------------------------------------

    def complete_task(self, task_id: str, at: datetime | None = None) -> None:
        moment = at or self.clock()
        record = self.tasks[task_id]
        properties = _properties(record)
        properties["hs_task_status"] = "COMPLETED"
        properties["hs_task_completion_date"] = iso(moment)
        record["updatedAt"] = iso(moment)

    def delete_task(self, task_id: str) -> None:
        del self.tasks[task_id]

    def log(self, kind: ActivityKind, at: datetime | None = None, contact: str = CONTACT) -> str:
        """Log a call/email/note/meeting on a contact, as a person would in the HubSpot UI."""
        moment = at or self.clock()
        activity_id = self._new_id()
        self.engagements[kind.value][activity_id] = {
            "id": activity_id,
            "properties": {
                "hs_object_id": activity_id,
                "hs_timestamp": iso(moment),
                "hs_createdate": iso(self.clock()),
            },
            "createdAt": iso(self.clock()),
            "updatedAt": iso(self.clock()),
            "archived": False,
        }
        self.contact_engagements.setdefault(contact, []).append((kind.value, activity_id))
        return activity_id

    def add_contact(
        self,
        contact_id: str = CONTACT,
        created_at: datetime | None = None,
        *,
        first: str | None = None,
        last: str | None = None,
    ) -> str:
        """A contact as HubSpot stores it. ``created_at=None`` omits ``createdAt`` altogether."""
        record: dict[str, object] = {
            "id": contact_id,
            "properties": {
                "hs_object_id": contact_id,
                "firstname": first,
                "lastname": last,
                "createdate": None if created_at is None else iso(created_at),
            },
            "archived": False,
        }
        if created_at is not None:
            record["createdAt"] = iso(created_at)
            record["updatedAt"] = iso(created_at)
        self.contacts[contact_id] = record
        return contact_id

    def link_company(self, contact: str, company: str) -> None:
        self.contact_companies.setdefault(contact, []).append(company)

    def add_hand_task(
        self,
        subject: str,
        *,
        contact: str | None = None,
        company: str | None = None,
        completed_at: datetime | None = None,
        due: datetime | None = None,
    ) -> str:
        """A task a founder typed by hand: no ``Ref:`` line, so not one of ours."""
        task_id = self._new_id()
        now = iso(self.clock())
        self.tasks[task_id] = {
            "id": task_id,
            "properties": {
                "hs_object_id": task_id,
                "hs_task_subject": subject,
                "hs_task_body": "Follow up — see the notes.",
                "hs_task_status": "NOT_STARTED" if completed_at is None else "COMPLETED",
                "hs_task_completion_date": None if completed_at is None else iso(completed_at),
                "hs_timestamp": iso(due or self.clock()),
                "hs_createdate": now,
            },
            "createdAt": now,
            "updatedAt": now if completed_at is None else iso(completed_at),
            "archived": False,
        }
        if contact is not None:
            self.contact_engagements.setdefault(contact, []).append(("tasks", task_id))
        if company is not None:
            self.task_companies.setdefault(task_id, []).append(company)
        return task_id

    # -- what a test reads back ---------------------------------------------------------------

    def open_tasks(self, contact: str = CONTACT) -> list[str]:
        """Subjects of the contact's tasks that are not completed — what the founder sees."""
        return [
            str(_properties(self.tasks[task_id])["hs_task_subject"])
            for kind, task_id in self.contact_engagements.get(contact, [])
            if kind == "tasks"
            and task_id in self.tasks
            and _properties(self.tasks[task_id])["hs_task_status"] != "COMPLETED"
        ]

    def task_creates_attempted(self) -> int:
        return sum(
            1
            for request in self.portal.requests
            if request.method == "POST" and request.url.path.endswith("/tasks")
        )

    def created_subjects(self) -> list[str]:
        return [str(_properties(body)["hs_task_subject"]) for body in self.created]

    def created_bodies(self) -> list[str]:
        return [str(_properties(body)["hs_task_body"]) for body in self.created]

    def created_owners(self) -> list[str | None]:
        owners: list[str | None] = []
        for body in self.created:
            owner = _properties(body).get("hubspot_owner_id")
            owners.append(None if owner is None else str(owner))
        return owners

    def created_types(self) -> list[str]:
        return [str(_properties(body)["hs_task_type"]) for body in self.created]

    def last_task_id(self) -> str:
        return list(self.tasks)[-1]

    def task_status(self, task_id: str) -> str:
        return str(_properties(self.tasks[task_id])["hs_task_status"])

    def created_company_ids(self) -> list[list[str]]:
        """Per created task, the companies its create body associated it with."""
        return [
            [target for target, type_id in _targets(body) if type_id == _TASK_TO_COMPANY]
            for body in self.created
        ]

    # -- the routes ---------------------------------------------------------------------------

    def _new_id(self) -> str:
        self._next_id += 1
        return str(self._next_id)

    def _create_task(self, request: httpx.Request) -> httpx.Response:
        if self.fail_creates:
            self.fail_creates -= 1
            return httpx.Response(500, json=load_fixture("error_server"))
        body: dict[str, object] = json.loads(request.content)
        self.created.append(body)
        task_id = self._new_id()
        now = iso(self.clock())
        properties = dict(_properties(body))
        record: dict[str, object] = {
            "id": task_id,
            "properties": {**properties, "hs_object_id": task_id, "hs_createdate": now},
            "createdAt": now,
            "updatedAt": now,
            "archived": False,
        }
        self.tasks[task_id] = record
        for target, type_id in _targets(body):
            if type_id == _TASK_TO_COMPANY:
                self.task_companies.setdefault(task_id, []).append(target)
            else:
                self.contact_engagements.setdefault(target, []).append(("tasks", task_id))
        if self.lose_create_responses:
            self.lose_create_responses -= 1
            return httpx.Response(500, json=load_fixture("error_server"))
        return httpx.Response(201, json=record)

    def _read_tasks(self, request: httpx.Request) -> httpx.Response:
        return self._batch(request, self.tasks)

    def _read_contacts(self, request: httpx.Request) -> httpx.Response:
        return self._batch(request, self.contacts)

    def _search_tasks(self, request: httpx.Request) -> httpx.Response:
        """Only the one filter adoption sends: ``hs_task_status NEQ <value>``."""
        body = cast(dict[str, list[dict[str, list[dict[str, str]]]]], json.loads(request.content))
        (only,) = body["filterGroups"][0]["filters"]
        assert (only["propertyName"], only["operator"]) == ("hs_task_status", "NEQ"), only
        found = [
            record
            for record in self.tasks.values()
            if _properties(record)["hs_task_status"] != only["value"]
        ]
        return httpx.Response(
            200, json={"total": len(found) + self.search_total_extra, "results": found}
        )

    def _read_engagements(self, request: httpx.Request) -> httpx.Response:
        match = _ENGAGEMENT_BATCH.search(request.url.path)
        assert match is not None, request.url.path
        return self._batch(request, self.engagements[match["kind"]])

    def _batch(self, request: httpx.Request, store: dict[str, dict[str, object]]) -> httpx.Response:
        body: dict[str, list[dict[str, str]]] = json.loads(request.content)
        ids = [item["id"] for item in body["inputs"]]
        found = [store[object_id] for object_id in ids if object_id in store]
        missing = [object_id for object_id in ids if object_id not in store]
        payload: dict[str, object] = {"status": "COMPLETE", "results": found}
        if missing:
            payload["numErrors"] = 1
            payload["errors"] = [
                {
                    "status": "error",
                    "category": "OBJECT_NOT_FOUND",
                    "message": "Could not get some objects, they may be deleted or not exist.",
                    "context": {"ids": missing},
                }
            ]
        return httpx.Response(207 if missing else 200, json=payload)

    def _associations(self, request: httpx.Request) -> httpx.Response:
        match = _ASSOCIATIONS.search(request.url.path)
        assert match is not None, request.url.path
        source, object_id, kind = match["source"], match["object"], match["kind"]
        if object_id in self.malformed_associations_for:
            return httpx.Response(200, json={"results": "not a list"})
        if object_id in self.unrecognised_associations_for:
            return httpx.Response(200, json={"results": [{"objectRef": {"value": 1}}]})
        if source == "tasks" and kind == "contacts":
            ids = [
                contact
                for contact, linked in self.contact_engagements.items()
                if ("tasks", object_id) in linked
            ]
        elif source == "tasks" and kind == "companies":
            ids = self.task_companies.get(object_id, [])
        elif source == "companies" and kind == "contacts":
            ids = [
                contact
                for contact, companies in self.contact_companies.items()
                if object_id in companies
            ]
        elif kind == "companies":
            ids = self.contact_companies.get(object_id, [])
        else:
            ids = [
                activity_id
                for engagement, activity_id in self.contact_engagements.get(object_id, [])
                if engagement == kind
            ]
        linked = [
            {
                "toObjectId": int(linked_id),
                "associationTypes": [{"category": "HUBSPOT_DEFINED", "typeId": 0, "label": None}],
            }
            for linked_id in ids
        ]
        return httpx.Response(200, json={"results": linked})


def _targets(body: dict[str, object]) -> list[tuple[str, int]]:
    """A create body's associations as ``(target id, association type id)`` pairs."""
    associations = cast(list[dict[str, object]], body.get("associations", []))
    pairs: list[tuple[str, int]] = []
    for association in associations:
        target = cast(dict[str, str], association["to"])
        types = cast(list[dict[str, int]], association["types"])
        pairs.append((target["id"], types[0]["associationTypeId"]))
    return pairs


def _properties(body: dict[str, object]) -> dict[str, object]:
    """A record's ``properties``. The cast states the shape every body in this module has."""
    properties = body["properties"]
    assert isinstance(properties, dict)
    return cast(dict[str, object], properties)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def hubspot(clock: FakeClock) -> FakeHubSpot:
    return FakeHubSpot(clock)


@pytest.fixture
async def hubspot_client(hubspot: FakeHubSpot) -> AsyncGenerator[HubSpotClient, None]:
    client = make_client(hubspot.portal)
    yield client
    await client.aclose()


@pytest.fixture
def service(
    db_session: AsyncSession, hubspot_client: HubSpotClient, clock: FakeClock
) -> CadenceService:
    """The service under test: the rolled-back session, the fake portal, the hand-moved clock."""
    return CadenceService(db_session, hubspot=lambda: hubspot_client, clock=clock)


@pytest.fixture
def adopter(db_session: AsyncSession, hubspot_client: HubSpotClient, clock: FakeClock) -> Adopter:
    """T13's adopter, on the same session, portal and clock as :func:`service`."""
    return Adopter(db_session, hubspot=lambda: hubspot_client, clock=clock)


def raced_by_another_run[**P, R](real: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    """Wrap ``CadenceRepository.create`` so another run's row lands first, inside the same write.

    The first call inserts the row; the second meets ``uq_cadence_state_contact`` — the race an
    enrolment loses after it has already created its task. The rollback removes both.
    """

    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        await real(*args, **kwargs)
        return await real(*args, **kwargs)

    return wrapper
