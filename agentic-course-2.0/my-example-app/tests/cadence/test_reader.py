"""``OutcomeReader`` against the recorded fixtures: HubSpot's bodies in, snapshots out."""

from datetime import UTC, datetime

import httpx
import pytest

from app.cadence.schemas import ActivityKind
from app.cadence.sync import OutcomeReader, parse_hubspot_datetime, to_task_snapshot
from app.promotion.schemas import HubSpotObject
from tests.promotion.conftest import (
    MockPortal,
    Responder,
    json_responder,
    load_fixture,
    make_client,
)

EMPTY = json_responder(200, {"results": []})


def _calls_pages() -> Responder:
    def respond(request: httpx.Request) -> httpx.Response:
        name = (
            "associations_calls_page2"
            if "after=" in str(request.url)
            else "associations_calls_page1"
        )
        return httpx.Response(200, json=load_fixture(name))

    return respond


def _portal() -> MockPortal:
    return MockPortal(
        [
            ("POST", "/tasks/batch/read", json_responder(200, load_fixture("tasks_batch_read"))),
            ("POST", "/calls/batch/read", json_responder(207, load_fixture("calls_batch_read"))),
            ("GET", "/associations/calls", _calls_pages()),
            ("GET", "/associations/", EMPTY),
        ]
    )


class TestTasks:
    async def test_completed_and_open_tasks_are_told_apart(self) -> None:
        portal = _portal()
        client = make_client(portal)

        tasks = await OutcomeReader(client).tasks(["88001122", "88001123"])

        assert tasks["88001122"].completed is True
        assert tasks["88001122"].completed_at == datetime(2026, 9, 29, 16, 12, 40, tzinfo=UTC)
        assert tasks["88001123"].completed is False
        assert tasks["88001123"].completed_at is None
        await client.aclose()

    def test_a_completed_task_with_no_completion_date_falls_back_to_updated_at(self) -> None:
        record = HubSpotObject.model_validate(
            {
                "id": "88001124",
                "properties": {"hs_task_status": "COMPLETED", "hs_task_completion_date": None},
                "updatedAt": "2026-09-30T09:00:00.000Z",
            }
        )

        snapshot = to_task_snapshot(record)

        assert snapshot.completed_at == datetime(2026, 9, 30, 9, 0, tzinfo=UTC)

    async def test_a_deleted_task_is_absent(self) -> None:
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

        tasks = await OutcomeReader(client).tasks(["88001123", "88009999"])

        assert set(tasks) == {"88001123"}
        await client.aclose()


class TestActivities:
    async def test_walks_every_type_and_reads_timestamps(self) -> None:
        portal = _portal()
        client = make_client(portal)

        found = await OutcomeReader(client).activities("400112233")

        # 51230003 is associated but absent from the batch read (deleted) — it cannot be credited.
        assert [(a.kind, a.activity_id) for a in found] == [
            (ActivityKind.call, "51230002"),
            (ActivityKind.call, "51230001"),
        ]
        assert found[1].occurred_at == datetime(2026, 9, 29, 15, 0, tzinfo=UTC)
        assert portal.count("GET", "/associations/") == 5  # 2 call pages + emails, notes, meetings
        assert portal.count("POST", "/batch/read") == 1  # types with no associations are not read
        await client.aclose()

    async def test_since_drops_older_activity_but_keeps_the_tie(self) -> None:
        portal = _portal()
        client = make_client(portal)

        found = await OutcomeReader(client).activities(
            "400112233", since=datetime(2026, 9, 29, 15, 0, tzinfo=UTC)
        )

        assert [a.activity_id for a in found] == ["51230001"]
        await client.aclose()

    async def test_an_unreadable_timestamp_is_skipped(self) -> None:
        portal = MockPortal(
            [
                (
                    "GET",
                    "/associations/notes",
                    json_responder(200, load_fixture("associations_legacy_shape")),
                ),
                ("GET", "/associations/", EMPTY),
                (
                    "POST",
                    "/notes/batch/read",
                    json_responder(
                        200,
                        {"results": [{"id": "61240001", "properties": {"hs_timestamp": None}}]},
                    ),
                ),
            ]
        )
        client = make_client(portal)

        assert await OutcomeReader(client).activities("400112233") == []
        await client.aclose()


class TestParseDatetime:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("2026-09-29T15:00:00.000Z", datetime(2026, 9, 29, 15, 0, tzinfo=UTC)),
            ("2026-09-29T10:00:00-05:00", datetime(2026, 9, 29, 15, 0, tzinfo=UTC)),
            ("1790380800000", datetime(2026, 9, 26, 0, 0, tzinfo=UTC)),
            ("2026-09-29T15:00:00", datetime(2026, 9, 29, 15, 0, tzinfo=UTC)),
            (None, None),
            ("", None),
            ("not a date", None),
        ],
    )
    def test_iso_epoch_and_garbage(self, raw: str | None, expected: datetime | None) -> None:
        assert parse_hubspot_datetime(raw) == expected
