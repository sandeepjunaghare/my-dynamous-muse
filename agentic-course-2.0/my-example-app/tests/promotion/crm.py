"""``FakeCrm``: companies and contacts on top of the cadence's :class:`FakeHubSpot`. In memory.

The cadence fake already serves tasks, the contacts batch read and the association walk, which is
what ``enrol`` needs. This adds what promotion talks to, shaped like the recorded fixtures:

* **Company search** on ``domain``, ``phone`` and ``lpe_source_url`` (EQ only, as dedupe and
  recovery send it). **The index lags writes:** a company created through the API is invisible to
  search until :meth:`FakeCrm.catch_up` — the "few moments" ``dedupe.py`` warns about, made
  explicit so a test decides when it has passed. A company a test adds directly is indexed.
* **Company and contact creates**, recording inline associations: a contact created with
  ``company_id`` is linked to it at once, so the ``companies → contacts`` walk sees it (no lag).
* **The two failure modes of a create**, per object type: ``fail_*_creates`` answers 500 without
  creating (safe to retry), ``lose_*_create_responses`` creates for real and *then* answers 500
  (the ambiguous case the gateway never retries — HubSpot did the work and failed to say so).
* **Property provisioning**: groups and properties, read-then-create, so ``ensure_properties``
  runs against it and a test can see what it created.
* **What a founder does**: :meth:`set_review_status`, :meth:`delete_company`.
"""

import json
import re
from typing import cast

import httpx

from app.promotion.client import API_VERSION
from app.promotion.dedupe import normalize_phone
from tests.cadence.conftest import FakeClock, FakeHubSpot, iso
from tests.promotion.conftest import load_fixture

_COMPANIES = f"/crm/objects/{API_VERSION}/companies"
_CONTACTS = f"/crm/objects/{API_VERSION}/contacts"
_PROPERTY_PATH = re.compile(
    rf"/crm/properties/{API_VERSION}/(?P<type>companies|contacts)(?:/(?P<rest>.+))?$"
)
_CONTACT_TO_COMPANY_PRIMARY = 1
_SEARCHABLE = frozenset({"domain", "phone", "lpe_source_url"})


class FakeCrm(FakeHubSpot):
    """A portal with companies, contacts, tasks and engagements — everything T9 and T11 touch."""

    def __init__(self, clock: FakeClock) -> None:
        super().__init__(clock)
        self.companies: dict[str, dict[str, object]] = {}
        self.indexed: set[str] = set()
        """Companies search can see. A created company joins it only on :meth:`catch_up`."""
        self.company_bodies: list[dict[str, object]] = []
        self.contact_bodies: list[dict[str, object]] = []
        self.fail_company_creates = 0
        self.lose_company_create_responses = 0
        self.fail_contact_creates = 0
        self.lose_contact_create_responses = 0
        self.groups: set[str] = set()
        self.provisioned: dict[tuple[str, str], dict[str, object]] = {}
        self.portal.prepend(
            ("POST", f"{_COMPANIES}/search", self._search_companies),
            ("POST", f"{_COMPANIES}/batch/read", self._read_companies),
            ("POST", _COMPANIES, self._create_company),
            ("GET", "/crm/properties/", self._read_property),
            ("POST", "/crm/properties/", self._create_property),
        )
        # After the cadence fake's `/contacts/batch/read`, which this fragment would also catch.
        self.portal.extend(("POST", _CONTACTS, self._create_contact))

    # -- what a founder (or the portal's history) does ----------------------------------------

    def add_company(self, **properties: str) -> str:
        """A company already in the portal — hand-typed, auto-created, adopted — and indexed."""
        company_id = self._new_id()
        self.companies[company_id] = self._record(company_id, dict(properties))
        self.indexed.add(company_id)
        return company_id

    def catch_up(self) -> None:
        """The search index catches up with every write so far."""
        self.indexed |= set(self.companies)

    def set_review_status(self, company_id: str, value: str | None) -> None:
        _props(self.companies[company_id])["lpe_review_status"] = value

    def delete_company(self, company_id: str) -> None:
        del self.companies[company_id]
        self.indexed.discard(company_id)

    # -- what a test reads back ---------------------------------------------------------------

    def company_property(self, company_id: str, name: str) -> object:
        return _props(self.companies[company_id]).get(name)

    def contact_property(self, contact_id: str, name: str) -> object:
        return _props(self.contacts[contact_id]).get(name)

    def contacts_of(self, company_id: str) -> list[str]:
        return [
            contact
            for contact, companies in self.contact_companies.items()
            if company_id in companies
        ]

    def writes(self) -> int:
        """Every request that could change the portal — creates and PATCHes, any object type."""
        return sum(1 for request in self.portal.requests if _is_write(request))

    # -- the routes ---------------------------------------------------------------------------

    def _record(self, object_id: str, properties: dict[str, object]) -> dict[str, object]:
        now = iso(self.clock())
        return {
            "id": object_id,
            "properties": {**properties, "hs_object_id": object_id, "createdate": now},
            "createdAt": now,
            "updatedAt": now,
            "archived": False,
        }

    def _search_companies(self, request: httpx.Request) -> httpx.Response:
        body = cast(dict[str, list[dict[str, list[dict[str, str]]]]], json.loads(request.content))
        (only,) = body["filterGroups"][0]["filters"]
        name, value = only["propertyName"], only["value"]
        assert name in _SEARCHABLE and only["operator"] == "EQ", only
        found = [
            record
            for company_id, record in self.companies.items()
            if company_id in self.indexed and _matches(_props(record).get(name), name, value)
        ]
        return httpx.Response(200, json={"total": len(found), "results": found})

    def _read_companies(self, request: httpx.Request) -> httpx.Response:
        return self._batch(request, self.companies)

    def _create_company(self, request: httpx.Request) -> httpx.Response:
        if self.fail_company_creates:
            self.fail_company_creates -= 1
            return httpx.Response(500, json=load_fixture("error_server"))
        body: dict[str, object] = json.loads(request.content)
        assert "associations" not in body, "a company create carries no associations"
        self.company_bodies.append(body)
        company_id = self._new_id()
        record = self._record(company_id, dict(_props(body)))
        self.companies[company_id] = record
        if self.lose_company_create_responses:
            self.lose_company_create_responses -= 1
            return httpx.Response(500, json=load_fixture("error_server"))
        return httpx.Response(201, json=record)

    def _create_contact(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path == _CONTACTS, request.url.path
        if self.fail_contact_creates:
            self.fail_contact_creates -= 1
            return httpx.Response(500, json=load_fixture("error_server"))
        body: dict[str, object] = json.loads(request.content)
        self.contact_bodies.append(body)
        contact_id = self._new_id()
        record = self._record(contact_id, dict(_props(body)))
        self.contacts[contact_id] = record
        for association in cast(list[dict[str, object]], body.get("associations", [])):
            target = cast(dict[str, str], association["to"])
            types = cast(list[dict[str, int]], association["types"])
            assert types[0]["associationTypeId"] == _CONTACT_TO_COMPANY_PRIMARY, types
            self.link_company(contact_id, target["id"])
        if self.lose_contact_create_responses:
            self.lose_contact_create_responses -= 1
            return httpx.Response(500, json=load_fixture("error_server"))
        return httpx.Response(201, json=record)

    def _read_property(self, request: httpx.Request) -> httpx.Response:
        match = _PROPERTY_PATH.search(request.url.path)
        assert match is not None and match["rest"], request.url.path
        object_type, rest = match["type"], match["rest"]
        if rest.startswith("groups/"):
            found = object_type in self.groups
            return httpx.Response(200 if found else 404, json={"name": rest[len("groups/") :]})
        definition = self.provisioned.get((object_type, rest))
        if definition is None:
            return httpx.Response(404, json={"status": "error", "message": "not found"})
        return httpx.Response(200, json=definition)

    def _create_property(self, request: httpx.Request) -> httpx.Response:
        match = _PROPERTY_PATH.search(request.url.path)
        assert match is not None, request.url.path
        body: dict[str, object] = json.loads(request.content)
        if match["rest"] == "groups":
            self.groups.add(match["type"])
            return httpx.Response(201, json=body)
        self.provisioned[(match["type"], str(body["name"]))] = body
        return httpx.Response(201, json=body)


def _matches(stored: object, name: str, value: str) -> bool:
    if stored is None:
        return False
    if name == "phone":
        return normalize_phone(str(stored)) == value
    return str(stored) == value


def _is_write(request: httpx.Request) -> bool:
    path = request.url.path
    if request.method == "PATCH":
        return True
    if request.method != "POST" or path.endswith(("/search", "/batch/read")):
        return False
    return path.startswith("/crm/objects/")


def _props(record: dict[str, object]) -> dict[str, object]:
    properties = record["properties"]
    assert isinstance(properties, dict)
    return cast(dict[str, object], properties)
