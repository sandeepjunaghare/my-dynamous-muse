"""Typed models for everything this slice sends to, or parses from, HubSpot.

Two rules run through the file, and they point in opposite directions on purpose:

* **Response models set** ``extra="ignore"``. HubSpot adds fields to its payloads without warning,
  and a new key upstream must never fail a weekly run.
* **Request models set** ``extra="forbid"``. A typo'd key in a body we construct would be accepted
  by HubSpot and silently ignored, so the property would simply never appear in the portal. Failing
  here turns that into an error at the one place someone is looking.

Everything is serialized with ``model_dump(by_alias=True, exclude_none=True)``: sending ``null``
for a property is not the same as omitting it — HubSpot reads the former as "clear this field".

Aliases are split by direction — ``validation_alias`` on what we parse, ``serialization_alias``
on what we send — rather than the bare ``alias`` that does both. A bare alias renames the
constructor argument too, so ``PropertyDefinition(field_type=...)`` stops type-checking and
every call site has to spell HubSpot's camelCase. Splitting it keeps camelCase on the wire and
snake_case in the code, which is the whole point of having these models.

The three task enums were read from portal 244766495 on 2026-09-27 rather than from the docs, which
contradict themselves on ``hs_task_status``.
"""

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

_RESPONSE_CONFIG = ConfigDict(populate_by_name=True, extra="ignore")
_REQUEST_CONFIG = ConfigDict(populate_by_name=True, extra="forbid")


class ObjectType(StrEnum):
    """The CRM object types this gateway touches. The path segment and the value are the same.

    The four engagement types are **read-only** here: T11 reads them to decide whether a cadence
    touch was done (D5). Nothing in this gateway creates a call, email, note or meeting.
    """

    companies = "companies"
    contacts = "contacts"
    tasks = "tasks"
    calls = "calls"
    emails = "emails"
    notes = "notes"
    meetings = "meetings"


class SearchOperator(StrEnum):
    """Search operators the MVP uses.

    Members as callers need them, deliberately: ``eq`` for dedupe, which matches exact values on
    default-searchable properties, and ``neq`` for adoption's open-task listing. An operator we do
    not use is an operator nobody has tested.
    """

    eq = "EQ"
    neq = "NEQ"


class TaskType(StrEnum):
    """The task types we create.

    The live property also offers ``LINKED_IN_CONNECT`` and ``LINKED_IN_MESSAGE``. They are
    **deliberately absent**: PRD section 8 makes LinkedIn automation a non-goal — it violates Sales
    Navigator's terms and risks the single seat Compumatrice has — so the write vocabulary does not
    offer them. This is a request-side enum; reading a task back does not parse it.
    """

    email = "EMAIL"
    call = "CALL"
    todo = "TODO"


class TaskPriority(StrEnum):
    """Task priority. ``NONE`` is a real member of the live property, not an absence."""

    none = "NONE"
    low = "LOW"
    medium = "MEDIUM"
    high = "HIGH"


class TaskStatus(StrEnum):
    """Task status, as portal 244766495 actually defines it.

    HubSpot's own documentation contradicts itself here — the property table lists
    ``NOT_STARTED``/``COMPLETED``, while the example on the same page uses ``WAITING``. The live
    property has all five below. T11 reads :attr:`completed` as one half of the done-signal (D5).
    """

    not_started = "NOT_STARTED"
    in_progress = "IN_PROGRESS"
    waiting = "WAITING"
    completed = "COMPLETED"
    deferred = "DEFERRED"


class HubSpotObject(BaseModel):
    """Any CRM record coming back from HubSpot.

    ``properties`` stays a flat ``dict[str, str | None]`` rather than a typed per-object model:
    which properties come back is chosen by the caller's request, so a fixed shape would be a lie.
    """

    model_config = _RESPONSE_CONFIG

    id: str
    properties: dict[str, str | None] = Field(default_factory=dict[str, str | None])
    created_at: datetime | None = Field(default=None, validation_alias="createdAt")
    updated_at: datetime | None = Field(default=None, validation_alias="updatedAt")


class ExistingProperty(BaseModel):
    """A property definition read back from HubSpot, for the idempotency check."""

    model_config = _RESPONSE_CONFIG

    name: str
    label: str | None = None
    type: str | None = None
    field_type: str | None = Field(default=None, validation_alias="fieldType")
    group_name: str | None = Field(default=None, validation_alias="groupName")


class PropertyGroupDefinition(BaseModel):
    """A property group we create. The group must exist before any property naming it."""

    model_config = _REQUEST_CONFIG

    name: str
    label: str
    display_order: int = Field(default=-1, serialization_alias="displayOrder")


class PropertyDefinition(BaseModel):
    """A property we create.

    ``group_name`` serializes to ``groupName`` and carries the group's **name**, not its id — a
    distinction HubSpot's error message for getting it wrong does not make clear.
    """

    model_config = _REQUEST_CONFIG

    name: str
    label: str
    type: str
    field_type: str = Field(serialization_alias="fieldType")
    group_name: str = Field(serialization_alias="groupName")
    description: str | None = None


class SearchFilter(BaseModel):
    """One ``propertyName`` / ``operator`` / ``value`` triple."""

    model_config = _REQUEST_CONFIG

    property_name: str = Field(serialization_alias="propertyName")
    operator: SearchOperator = SearchOperator.eq
    value: str


class SearchFilterGroup(BaseModel):
    """Filters within a group are ANDed; groups are ORed. Dedupe uses exactly one group."""

    model_config = _REQUEST_CONFIG

    filters: list[SearchFilter]


class SearchRequest(BaseModel):
    """A CRM search body.

    HubSpot caps this at 5 filter groups, 6 filters per group, 18 filters total and a ``limit`` of
    200. Nothing here approaches those, so they are documented rather than validated.
    """

    model_config = _REQUEST_CONFIG

    filter_groups: list[SearchFilterGroup] = Field(serialization_alias="filterGroups")
    properties: list[str]
    limit: int = 10


class SearchResponse(BaseModel):
    """A page of search results."""

    model_config = _RESPONSE_CONFIG

    total: int = 0
    results: list[HubSpotObject] = Field(default_factory=list[HubSpotObject])


class HubSpotErrorBody(BaseModel):
    """HubSpot's error envelope — or rather, both of them.

    The standard shape carries ``category``; the 429 shape carries ``errorType`` and ``policyName``
    instead. Every field is optional because HubSpot documents the envelope as varying per
    endpoint, and because a proxy's HTML error page must not turn a 503 into a ``ValidationError``.
    """

    model_config = _RESPONSE_CONFIG

    status: str | None = None
    message: str | None = None
    category: str | None = None
    error_type: str | None = Field(default=None, validation_alias="errorType")
    policy_name: str | None = Field(default=None, validation_alias="policyName")
    correlation_id: str | None = Field(default=None, validation_alias="correlationId")


class ObjectWriteRequest(BaseModel):
    """The body of a company or contact create/update: a flat map of rendered property values."""

    model_config = _REQUEST_CONFIG

    properties: dict[str, str]


class AssociationTarget(BaseModel):
    """The record an association points at."""

    model_config = _REQUEST_CONFIG

    id: str


class AssociationType(BaseModel):
    """A ``HUBSPOT_DEFINED`` association type id. 204 is task→contact, 192 is task→company."""

    model_config = _REQUEST_CONFIG

    association_category: str = Field(
        default="HUBSPOT_DEFINED", serialization_alias="associationCategory"
    )
    association_type_id: int = Field(serialization_alias="associationTypeId")


class Association(BaseModel):
    """One inline association on a create body."""

    model_config = _REQUEST_CONFIG

    to: AssociationTarget
    types: list[AssociationType]


class TaskCreateRequest(BaseModel):
    """The wire body for a task create, associations included."""

    model_config = _REQUEST_CONFIG

    properties: dict[str, str]
    associations: list[Association] = Field(default_factory=list[Association])


class TaskCreate(BaseModel):
    """A cadence touch, in our vocabulary rather than HubSpot's.

    **Plain values, no provenance** — deliberately. A task is a thing we are asking a human to do,
    not a claim about a prospect, so it carries nothing that needs citing. See
    :class:`~app.promotion.exceptions.UnprovenancedWriteError` for why that carve-out exists.
    """

    model_config = _REQUEST_CONFIG

    subject: str
    due_at: datetime
    body: str | None = None
    task_type: TaskType = TaskType.todo
    priority: TaskPriority = TaskPriority.medium
    status: TaskStatus = TaskStatus.not_started
    owner_id: str | None = None

    @field_validator("due_at")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        """Reject a naive due date.

        ``datetime.timestamp()`` on a naive value silently assumes the *local* zone. A task built
        on a laptop in Chicago and one built on a UTC VPS would land five or six hours apart from
        identical code — and the symptom is a touch due on the wrong day, not an error.
        """
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise ValueError("due_at must be timezone-aware")
        return value.astimezone(UTC)

    def to_properties(self) -> dict[str, str]:
        """Render to HubSpot's property names. ``hs_timestamp`` is epoch milliseconds."""
        properties = {
            "hs_timestamp": str(int(self.due_at.timestamp() * 1000)),
            "hs_task_subject": self.subject,
            "hs_task_type": self.task_type.value,
            "hs_task_priority": self.priority.value,
            "hs_task_status": self.status.value,
        }
        if self.body is not None:
            properties["hs_task_body"] = self.body
        if self.owner_id is not None:
            properties["hubspot_owner_id"] = self.owner_id
        return properties


class BatchReadInput(BaseModel):
    """One record to read in a batch."""

    model_config = _REQUEST_CONFIG

    id: str


class BatchReadRequest(BaseModel):
    """The body of a ``batch/read``. HubSpot caps ``inputs`` at 100 per call."""

    model_config = _REQUEST_CONFIG

    properties: list[str]
    inputs: list[BatchReadInput]


class BatchReadResponse(BaseModel):
    """A batch read's results.

    An id that does not exist — or was archived — is simply **absent** from ``results`` (HubSpot
    answers 207 and lists it under ``errors``, which is ignored here). The caller decides what an
    absent record means; for a cadence task it means a human deleted it.
    """

    model_config = _RESPONSE_CONFIG

    results: list[HubSpotObject] = Field(default_factory=list[HubSpotObject])


class AssociatedObject(BaseModel):
    """One associated record, as the dated associations endpoint returns it.

    The dated surface follows v4 (``toObjectId``, an integer); v3 answered ``id``, a string. This
    shape has not been exercised against the live portal, so both are accepted and normalized to a
    string id rather than one of them failing a weekly run.
    """

    model_config = _RESPONSE_CONFIG

    to_object_id: str | None = Field(default=None, validation_alias="toObjectId")
    legacy_id: str | None = Field(default=None, validation_alias="id")

    @field_validator("to_object_id", "legacy_id", mode="before")
    @classmethod
    def _stringify(cls, value: object) -> object:
        """HubSpot ids are int64 on this endpoint and strings everywhere else."""
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
        return value

    @property
    def object_id(self) -> str | None:
        """The associated record's id, whichever key carried it."""
        return self.to_object_id if self.to_object_id is not None else self.legacy_id


class PagingNext(BaseModel):
    """The cursor to the next page."""

    model_config = _RESPONSE_CONFIG

    after: str | None = None


class Paging(BaseModel):
    """HubSpot's paging envelope."""

    model_config = _RESPONSE_CONFIG

    next: PagingNext | None = None


class AssociationsPage(BaseModel):
    """One page of a record's associations to one object type."""

    model_config = _RESPONSE_CONFIG

    results: list[AssociatedObject] = Field(default_factory=list[AssociatedObject])
    paging: Paging | None = None

    @property
    def next_after(self) -> str | None:
        """The cursor for the next page, or ``None`` on the last one."""
        if self.paging is None or self.paging.next is None:
            return None
        return self.paging.next.after
