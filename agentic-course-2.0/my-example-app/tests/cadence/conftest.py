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
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from typing import cast

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.cadence.schemas import ActivityKind
from app.cadence.service import CadenceService
from app.promotion.client import API_VERSION, HubSpotClient
from tests.promotion.conftest import MockPortal, load_fixture, make_client

START = datetime(2026, 10, 5, 14, 0, tzinfo=UTC)
"""Monday 2026-10-05, 9:00 in Dallas (CDT, UTC-5)."""

CONTACT = "400112233"
COMPANY = "7009876543"

_ASSOCIATIONS = re.compile(r"/contacts/(?P<contact>[^/]+)/associations/(?P<kind>[a-z]+)$")
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
        """Answer this many task creates with a 500 before succeeding again."""
        self._next_id = 88_000_000
        self.portal = MockPortal(
            [
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

    # -- what a test reads back ---------------------------------------------------------------

    def created_subjects(self) -> list[str]:
        return [str(_properties(body)["hs_task_subject"]) for body in self.created]

    def created_types(self) -> list[str]:
        return [str(_properties(body)["hs_task_type"]) for body in self.created]

    def last_task_id(self) -> str:
        return list(self.tasks)[-1]

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
        return httpx.Response(201, json=record)

    def _read_tasks(self, request: httpx.Request) -> httpx.Response:
        return self._batch(request, self.tasks)

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
        linked = [
            {
                "toObjectId": int(activity_id),
                "associationTypes": [{"category": "HUBSPOT_DEFINED", "typeId": 0, "label": None}],
            }
            for kind, activity_id in self.contact_engagements.get(match["contact"], [])
            if kind == match["kind"]
        ]
        return httpx.Response(200, json={"results": linked})


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
