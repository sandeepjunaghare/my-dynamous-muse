"""Provisioning the six custom properties, idempotently.

None of them existed in portal 244766495 when this was written. Review happens in HubSpot, so a
record that cannot show where it came from cannot be reviewed.
"""

import json

from structlog.testing import capture_logs

from app.promotion.client import API_VERSION
from app.promotion.properties import PROVISIONING_PLAN, ensure_properties
from app.promotion.schemas import ObjectType, ReviewStatus, Verification
from tests.promotion.conftest import (
    MockPortal,
    Route,
    json_responder,
    load_fixture,
    make_client,
)

NOT_FOUND = {"status": "error", "message": "resource not found", "category": "OBJECT_NOT_FOUND"}
CONFLICT = {"status": "error", "message": "already exists", "category": "OBJECT_ALREADY_EXISTS"}

EMPTY_PORTAL: list[Route] = [
    ("GET", "groups/lpe", json_responder(404, NOT_FOUND)),
    ("POST", "/groups", json_responder(201, load_fixture("property_group_found"))),
    ("GET", "/crm/properties/", json_responder(404, NOT_FOUND)),
    ("POST", "/crm/properties/", json_responder(201, load_fixture("property_created"))),
]

FULLY_PROVISIONED: list[Route] = [
    ("GET", "groups/lpe", json_responder(200, load_fixture("property_group_found"))),
    ("GET", "/crm/properties/", json_responder(200, load_fixture("property_found"))),
]


def _property_posts(portal: MockPortal) -> list[str]:
    return [
        request.url.path
        for request in portal.requests
        if request.method == "POST" and not request.url.path.endswith("/groups")
    ]


def _property_bodies(portal: MockPortal, path: str = "/crm/properties/") -> list[dict[str, object]]:
    """Bodies of property creates only.

    The group-create path ends in `/groups` and otherwise *contains* the property path, so a plain
    fragment match would hand back the group body first and quietly test the wrong request.
    """
    return [
        json.loads(request.content)
        for request in portal.requests
        if request.method == "POST"
        and path in request.url.path
        and not request.url.path.endswith("/groups")
    ]


class TestFirstRun:
    async def test_every_placement_is_created(self) -> None:
        portal = MockPortal(EMPTY_PORTAL)
        client = make_client(portal)

        await ensure_properties(client)

        assert len(_property_posts(portal)) == len(PROVISIONING_PLAN) == 10
        await client.aclose()

    async def test_properties_land_on_the_right_object_types(self) -> None:
        """Scoring, routing and verification describe a business; provenance describes a record."""
        portal = MockPortal(EMPTY_PORTAL)
        client = make_client(portal)

        await ensure_properties(client)

        posts = _property_posts(portal)
        assert posts.count(f"/crm/properties/{API_VERSION}/companies") == 7
        assert posts.count(f"/crm/properties/{API_VERSION}/contacts") == 3
        await client.aclose()

    async def test_the_group_is_created_before_any_property(self) -> None:
        """`groupName` is required on a property create and HubSpot rejects an unknown group."""
        portal = MockPortal(EMPTY_PORTAL)
        client = make_client(portal)

        await ensure_properties(client)

        posts = [r for r in portal.requests if r.method == "POST"]
        first_group = next(i for i, r in enumerate(posts) if r.url.path.endswith("/groups"))
        first_property = next(i for i, r in enumerate(posts) if not r.url.path.endswith("/groups"))
        assert first_group < first_property
        await client.aclose()

    async def test_bodies_use_hubspot_spelling(self) -> None:
        """camelCase on the wire, snake_case in the code — the point of having the models."""
        portal = MockPortal(EMPTY_PORTAL)
        client = make_client(portal)

        await ensure_properties(client)

        body = _property_bodies(portal, f"/crm/properties/{API_VERSION}/companies")[0]
        assert "fieldType" in body
        assert "groupName" in body
        assert "field_type" not in body
        await client.aclose()

    async def test_sourced_at_uses_the_date_field_type(self) -> None:
        """There is no `datetime` fieldType in HubSpot.

        The type is `datetime`; the fieldType is `date`. Getting it the obvious way round is
        rejected at create time, and only then.
        """
        portal = MockPortal(EMPTY_PORTAL)
        client = make_client(portal)

        await ensure_properties(client)

        sourced_at = [
            body for body in _property_bodies(portal) if body.get("name") == "lpe_sourced_at"
        ]
        assert sourced_at
        assert all(body["type"] == "datetime" for body in sourced_at)
        assert all(body["fieldType"] == "date" for body in sourced_at)
        await client.aclose()

    async def test_vertical_is_a_string_not_an_enumeration(self) -> None:
        """An enum would make vertical #3 a property migration — a direct hit on M9."""
        portal = MockPortal(EMPTY_PORTAL)
        client = make_client(portal)

        await ensure_properties(client)

        vertical = [body for body in _property_bodies(portal) if body.get("name") == "lpe_vertical"]
        assert vertical
        assert all(body["type"] == "string" for body in vertical)
        await client.aclose()


class TestSecondRun:
    async def test_a_second_pass_writes_nothing(self) -> None:
        """Safe on every deploy. Ten reads, zero writes."""
        portal = MockPortal(FULLY_PROVISIONED)
        client = make_client(portal)

        with capture_logs() as captured:
            await ensure_properties(client)

        assert portal.count("POST") == 0
        events = [entry["event"] for entry in captured]
        assert events.count("promotion.hubspot.property_present") == 10
        assert events.count("promotion.hubspot.property_created") == 0
        assert events.count("promotion.hubspot.group_present") == 2
        await client.aclose()


class TestConflictsAndDrift:
    async def test_a_conflict_on_create_is_treated_as_success(self) -> None:
        """Something created it between our read and our write. Not an error.

        The conflict body is community-reported rather than documented, so the status is what is
        matched — never the message string.
        """
        portal = MockPortal(
            [
                ("GET", "groups/lpe", json_responder(200, load_fixture("property_group_found"))),
                ("GET", "/crm/properties/", json_responder(404, NOT_FOUND)),
                ("POST", "/crm/properties/", json_responder(409, CONFLICT)),
            ]
        )
        client = make_client(portal)

        with capture_logs() as captured:
            await ensure_properties(client)

        events = [entry["event"] for entry in captured]
        assert events.count("promotion.hubspot.property_present") == 10
        assert events.count("promotion.hubspot.property_created") == 0
        await client.aclose()

    async def test_a_type_mismatch_warns_and_changes_nothing(self) -> None:
        """A human may have changed it deliberately. Overwriting that silently is not our call."""
        drifted = {**load_fixture("property_found"), "type": "enumeration"}
        portal = MockPortal(
            [
                ("GET", "groups/lpe", json_responder(200, load_fixture("property_group_found"))),
                ("GET", "/crm/properties/", json_responder(200, drifted)),
            ]
        )
        client = make_client(portal)

        with capture_logs() as captured:
            await ensure_properties(client)

        mismatches = [
            entry for entry in captured if entry["event"] == "promotion.hubspot.property_drifted"
        ]
        assert mismatches
        assert mismatches[0]["found"] == "enumeration"
        assert portal.count("PATCH") == 0
        assert portal.count("POST") == 0
        await client.aclose()


class TestProvisioningPlan:
    def test_the_plan_covers_the_ticket_five_the_review_status_and_verification(self) -> None:
        names = {definition.name for _, definition in PROVISIONING_PLAN}
        assert names == {
            "lpe_source_url",
            "lpe_sourced_at",
            "lpe_vertical",
            "lpe_priority_score",
            "lpe_route_cluster",
            "lpe_review_status",
            "lpe_verification",
        }

    def test_scoring_and_routing_are_company_only(self) -> None:
        contact_names = {
            definition.name
            for object_type, definition in PROVISIONING_PLAN
            if object_type is ObjectType.contacts
        }
        assert "lpe_priority_score" not in contact_names
        assert "lpe_route_cluster" not in contact_names

    def test_review_status_is_company_only(self) -> None:
        """OQ2: the company list view is the review surface; a second permanent name is unwanted."""
        placements = [
            object_type
            for object_type, definition in PROVISIONING_PLAN
            if definition.name == "lpe_review_status"
        ]
        assert placements == [ObjectType.companies]


class TestReviewStatus:
    async def test_it_is_a_select_with_three_options_in_order(self) -> None:
        """OQ2-A. The option values are what `lpe promotion sync` reads back, so they are exact."""
        portal = MockPortal(EMPTY_PORTAL)
        client = make_client(portal)

        await ensure_properties(client)

        (body,) = [b for b in _property_bodies(portal) if b.get("name") == "lpe_review_status"]
        assert body["type"] == "enumeration"
        assert body["fieldType"] == "select"
        assert body["groupName"] == "lpe"
        assert body["options"] == [
            {"label": "Pending review", "value": "pending_review", "displayOrder": 0},
            {"label": "Accepted", "value": "accepted", "displayOrder": 1},
            {"label": "Rejected", "value": "rejected", "displayOrder": 2},
        ]
        await client.aclose()

    async def test_a_string_property_body_carries_no_options_key(self) -> None:
        """`exclude_none`: the five T3 properties are created exactly as before."""
        portal = MockPortal(EMPTY_PORTAL)
        client = make_client(portal)

        await ensure_properties(client)

        selects = {"lpe_review_status", "lpe_verification"}
        others = [b for b in _property_bodies(portal) if b.get("name") not in selects]
        assert others
        assert all("options" not in body for body in others)
        await client.aclose()

    def test_the_option_values_are_the_review_status_enum(self) -> None:
        (definition,) = [d for _, d in PROVISIONING_PLAN if d.name == "lpe_review_status"]
        assert definition.options is not None
        assert [option.value for option in definition.options] == [s.value for s in ReviewStatus]


class TestVerification:
    async def test_it_is_a_company_select_with_three_options_in_order(self) -> None:
        portal = MockPortal(EMPTY_PORTAL)
        client = make_client(portal)

        await ensure_properties(client)

        (body,) = [b for b in _property_bodies(portal) if b.get("name") == "lpe_verification"]
        assert body["type"] == "enumeration"
        assert body["fieldType"] == "select"
        assert body["groupName"] == "lpe"
        assert body["options"] == [
            {"label": "Google Places", "value": "places", "displayOrder": 0},
            {"label": "Own-domain website", "value": "website", "displayOrder": 1},
            {"label": "Not verified", "value": "none", "displayOrder": 2},
        ]
        placements = [o for o, d in PROVISIONING_PLAN if d.name == "lpe_verification"]
        assert placements == [ObjectType.companies]
        await client.aclose()

    def test_the_option_values_are_the_verification_enum(self) -> None:
        (definition,) = [d for _, d in PROVISIONING_PLAN if d.name == "lpe_verification"]
        assert definition.options is not None
        assert [option.value for option in definition.options] == [v.value for v in Verification]
