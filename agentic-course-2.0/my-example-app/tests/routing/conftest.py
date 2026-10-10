"""Fixtures for the routing suites: a mock Census Geocoder that answers by street.

**No test here reaches the real geocoder.** The seam is an injected ``httpx.MockTransport``, for the
reason ``tests/promotion/conftest.py`` gives (no global patching). :class:`MockCensus` raises on a
request to any other host, or for a street it has no answer for, so "recorded fixtures only" is a
proven property rather than an intention.
"""

import json
from collections import deque
from collections.abc import AsyncGenerator, Callable
from pathlib import Path

import httpx
import pytest

from app.routing.geocoder import BASE_URL, CensusGeocoder

FIXTURES = Path(__file__).parent / "fixtures"

type Answer = Callable[[httpx.Request], httpx.Response]


def load_fixture(name: str) -> dict[str, object]:
    """Read a recorded response body. Synthetic, but in the live endpoint's exact shape."""
    payload: dict[str, object] = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return payload


def a_match(latitude: float, longitude: float, postal_code: str = "75238") -> dict[str, object]:
    """A one-match body at a given place, built from the recorded match fixture."""
    return {
        "result": {
            "input": {},
            "addressMatches": [
                {
                    "coordinates": {"x": longitude, "y": latitude},
                    "matchedAddress": f"MATCHED, TX, {postal_code}",
                }
            ],
        }
    }


def json_answer(status_code: int, payload: object) -> Answer:
    """Always answer with this status and body."""

    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=payload)

    return answer


def text_answer(status_code: int, body: str) -> Answer:
    """Answer with something that is not JSON."""

    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, text=body)

    return answer


def transport_error() -> Answer:
    """Fail the way a dropped connection does."""

    def answer(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    return answer


class MockCensus:
    """A Census Geocoder that answers by ``street``, recording every request it sees.

    Each street holds a queue of answers; the last one repeats, so a single answer serves any
    number of calls and ``[503, 503, match]`` scripts a retry.
    """

    def __init__(self) -> None:
        self._answers: dict[str, deque[Answer]] = {}
        self._geocoders: list[CensusGeocoder] = []
        self.requests: list[httpx.Request] = []

    def on(self, street: str, *answers: Answer) -> None:
        """Script the answers for one street."""
        self._answers[street] = deque(answers)

    def streets_requested(self) -> list[str]:
        """The ``street`` parameter of every request, in order."""
        return [request.url.params["street"] for request in self.requests]

    def handle(self, request: httpx.Request) -> httpx.Response:
        expected = httpx.URL(BASE_URL)
        if (request.url.host, request.url.path) != (expected.host, expected.path):
            raise AssertionError(f"unexpected request to {request.url}")
        self.requests.append(request)
        street = request.url.params.get("street", "")
        answers = self._answers.get(street)
        if not answers:
            raise AssertionError(f"MockCensus has no answer for street {street!r}")
        answer = answers.popleft() if len(answers) > 1 else answers[0]
        return answer(request)

    def geocoder(self) -> CensusGeocoder:
        """A geocoder wired to this mock, with retries that do not wait."""

        async def no_sleep(seconds: float) -> None:
            return None

        geocoder = CensusGeocoder(transport=httpx.MockTransport(self.handle), sleep=no_sleep)
        self._geocoders.append(geocoder)
        return geocoder

    async def aclose(self) -> None:
        """Close every geocoder handed out. A service never closes one it was given."""
        for geocoder in self._geocoders:
            await geocoder.aclose()


@pytest.fixture
async def census() -> AsyncGenerator[MockCensus, None]:
    """A :class:`MockCensus` whose geocoders are closed when the test ends."""
    mock = MockCensus()
    yield mock
    await mock.aclose()
