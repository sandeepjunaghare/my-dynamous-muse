"""The shared request loop: ``post`` rides ``get``'s retries, and never logs a header (T6)."""

import json

import httpx
import pytest
from structlog.testing import capture_logs

from app.sourcing.exceptions import SourceRequestError
from app.sourcing.sources import http

URL = "https://places.googleapis.com/v1/places:searchText"
SECRET = "places-key-fixture-not-a-secret"


class _Scripted:
    """Answers each request with the next scripted status, and keeps what it was sent."""

    def __init__(self, *statuses: int) -> None:
        self._statuses = list(statuses)
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self._statuses:
            raise AssertionError(f"unexpected request: {request.method} {request.url}")
        return httpx.Response(self._statuses.pop(0), json={"ok": True})


async def _post(scripted: _Scripted) -> httpx.Response:
    async with httpx.AsyncClient(transport=httpx.MockTransport(scripted.handler)) as client:
        return await http.post(
            client,
            URL,
            source="places",
            json={"textQuery": "Acme Freight", "pageSize": 5},
            headers={"X-Goog-Api-Key": SECRET},
            backoff_seconds=0.0,
        )


class TestPost:
    async def test_a_503_is_retried_then_succeeds(self) -> None:
        scripted = _Scripted(503, 200)
        response = await _post(scripted)
        assert response.status_code == 200
        assert len(scripted.requests) == 2
        assert all(request.method == "POST" for request in scripted.requests)

    async def test_a_400_is_not_retried_and_names_its_status(self) -> None:
        scripted = _Scripted(400)
        with pytest.raises(SourceRequestError) as raised:
            await _post(scripted)
        assert raised.value.status == 400
        assert len(scripted.requests) == 1

    async def test_the_body_is_the_json_given(self) -> None:
        scripted = _Scripted(200)
        await _post(scripted)
        sent: object = json.loads(scripted.requests[0].content)
        assert sent == {"textQuery": "Acme Freight", "pageSize": 5}

    async def test_no_header_value_reaches_a_log_line(self) -> None:
        """The Places key travels as a header; the log carries the URL and never the headers."""
        scripted = _Scripted(503, 400)
        with capture_logs() as logs, pytest.raises(SourceRequestError) as raised:
            await _post(scripted)
        assert logs, "the loop logs every attempt"
        assert SECRET not in str(logs)
        assert SECRET not in str(raised.value)


class TestGetStillWorks:
    async def test_a_get_sends_its_params_and_logs_no_query(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json=[])

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with capture_logs() as logs:
                await http.get(client, URL, source="s", params={"webKey": SECRET})
        assert seen[0].method == "GET"
        assert seen[0].url.params["webKey"] == SECRET
        assert SECRET not in str(logs)
