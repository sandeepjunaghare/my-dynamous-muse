"""The two read methods T11 added: ``batch_read`` and ``list_associated_ids``.

Transport only — what a completed task or a logged call *means* is tested in ``tests/cadence/``.
What is pinned here is the wire: dated paths, body shape, chunking, paging, and the one retry
decision that differs from a create (a batch read is a POST, and it *is* retried on a 5xx).
"""

import json

import httpx

from app.promotion.client import API_VERSION
from app.promotion.schemas import ObjectType
from tests.promotion.conftest import (
    MockPortal,
    Responder,
    json_responder,
    load_fixture,
    make_client,
    sequence_responder,
)

TASK_PROPERTIES = ("hs_task_status", "hs_task_completion_date", "hs_timestamp")


def _paged_associations() -> Responder:
    """First page carries a cursor, the second does not — and only the cursor selects it."""

    def respond(request: httpx.Request) -> httpx.Response:
        if "after=" in str(request.url):
            return httpx.Response(200, json=load_fixture("associations_calls_page2"))
        return httpx.Response(200, json=load_fixture("associations_calls_page1"))

    return respond


class TestBatchRead:
    async def test_reads_on_the_dated_batch_path_with_ids_and_properties(self) -> None:
        portal = MockPortal(
            [("POST", "/tasks/batch/read", json_responder(200, load_fixture("tasks_batch_read")))]
        )
        client = make_client(portal)

        results = await client.batch_read(
            ObjectType.tasks, ["88001122", "88001123"], TASK_PROPERTIES
        )

        assert portal.requests[0].url.path == f"/crm/objects/{API_VERSION}/tasks/batch/read"
        body = portal.bodies("POST")[0]
        assert body == {
            "properties": list(TASK_PROPERTIES),
            "inputs": [{"id": "88001122"}, {"id": "88001123"}],
        }
        assert [task.id for task in results] == ["88001122", "88001123"]
        assert results[0].properties["hs_task_status"] == "COMPLETED"
        await client.aclose()

    async def test_no_ids_means_no_request(self) -> None:
        portal = MockPortal()
        client = make_client(portal)

        assert await client.batch_read(ObjectType.calls, [], ["hs_timestamp"]) == []
        assert portal.requests == []
        await client.aclose()

    async def test_more_than_one_hundred_ids_are_chunked(self) -> None:
        """HubSpot caps a batch at 100 inputs; 250 ids is three calls of 100, 100 and 50."""
        portal = MockPortal([("POST", "/calls/batch/read", json_responder(200, {"results": []}))])
        client = make_client(portal)

        await client.batch_read(ObjectType.calls, [str(n) for n in range(250)], ["hs_timestamp"])

        sizes = [len(json.loads(r.content)["inputs"]) for r in portal.requests]
        assert sizes == [100, 100, 50]
        await client.aclose()

    async def test_duplicate_ids_are_read_once(self) -> None:
        portal = MockPortal([("POST", "/calls/batch/read", json_responder(200, {"results": []}))])
        client = make_client(portal)

        await client.batch_read(ObjectType.calls, ["1", "2", "1"], ["hs_timestamp"])

        assert portal.bodies("POST")[0]["inputs"] == [{"id": "1"}, {"id": "2"}]
        await client.aclose()

    async def test_a_missing_id_is_absent_rather_than_an_error(self) -> None:
        """A deleted task comes back as a 207 with an ``errors`` entry, not a failure."""
        portal = MockPortal(
            [
                (
                    "POST",
                    "/tasks/batch/read",
                    json_responder(207, load_fixture("tasks_batch_read_partial")),
                )
            ]
        )
        client = make_client(portal)

        results = await client.batch_read(
            ObjectType.tasks, ["88001123", "88009999"], TASK_PROPERTIES
        )

        assert [task.id for task in results] == ["88001123"]
        await client.aclose()

    async def test_a_server_error_is_retried_because_a_read_cannot_duplicate(
        self, no_sleep: list[float]
    ) -> None:
        """Unlike a create: reading twice cannot duplicate anything, so a 5xx is retried."""
        portal = MockPortal(
            [
                (
                    "POST",
                    "/tasks/batch/read",
                    sequence_responder(
                        (502, load_fixture("error_server")),
                        (200, load_fixture("tasks_batch_read")),
                    ),
                )
            ]
        )
        client = make_client(portal)

        results = await client.batch_read(ObjectType.tasks, ["88001122"], TASK_PROPERTIES)

        assert len(results) == 2
        assert portal.count("POST") == 2
        assert len(no_sleep) == 1
        await client.aclose()


class TestAssociations:
    async def test_walks_every_page_and_returns_string_ids(self) -> None:
        portal = MockPortal(
            [("GET", "/contacts/400112233/associations/calls", _paged_associations())]
        )
        client = make_client(portal)

        ids = await client.list_associated_ids(ObjectType.contacts, "400112233", ObjectType.calls)

        assert ids == ["51230001", "51230002", "51230003"]
        assert portal.count("GET") == 2
        first, second = portal.requests
        assert first.url.path == (
            f"/crm/objects/{API_VERSION}/contacts/400112233/associations/calls"
        )
        assert first.url.params["limit"] == "500"
        assert "after" not in first.url.params
        assert "after=MjAyNi0wOS0yOQ" in str(second.url)
        await client.aclose()

    async def test_the_older_id_shape_is_tolerated(self) -> None:
        """The dated endpoint is v4-shaped; an ``id``-keyed body must not fail a weekly run."""
        portal = MockPortal(
            [
                (
                    "GET",
                    "/associations/notes",
                    json_responder(200, load_fixture("associations_legacy_shape")),
                )
            ]
        )
        client = make_client(portal)

        ids = await client.list_associated_ids(ObjectType.contacts, "400112233", ObjectType.notes)

        assert ids == ["61240001"]
        await client.aclose()

    async def test_no_associations_is_an_empty_list(self) -> None:
        portal = MockPortal([("GET", "/associations/emails", json_responder(200, {"results": []}))])
        client = make_client(portal)

        ids = await client.list_associated_ids(ObjectType.contacts, "400112233", ObjectType.emails)

        assert ids == []
        await client.aclose()

    async def test_a_cursor_that_never_ends_is_capped(self) -> None:
        """A circuit breaker, not a limit anyone should reach: the walk stops at 20 pages."""
        portal = MockPortal(
            [
                (
                    "GET",
                    "/associations/calls",
                    json_responder(200, load_fixture("associations_calls_page1")),
                )
            ]
        )
        client = make_client(portal)

        ids = await client.list_associated_ids(ObjectType.contacts, "400112233", ObjectType.calls)

        assert portal.count("GET") == 20
        assert ids == ["51230001", "51230002"]
        await client.aclose()


class TestReadOnly:
    def test_the_gateway_still_cannot_create_an_engagement(self) -> None:
        """Engagement types were added to read them. Nothing may log a call or send an email."""
        from app.promotion.client import HubSpotClient

        for name in ("create_call", "create_email", "send_email", "create_note", "log_activity"):
            assert not hasattr(HubSpotClient, name), name
