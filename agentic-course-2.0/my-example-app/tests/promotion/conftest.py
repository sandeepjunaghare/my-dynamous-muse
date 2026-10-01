"""Fixtures for the HubSpot gateway suite.

**No test here touches the portal.** The seam is an injected ``httpx.MockTransport``, matching how
the existing ``client`` / ``error_client`` fixtures inject an ``ASGITransport`` one directory up —
which is also why this does not reach for ``pytest-httpx`` or ``respx``. Both work by patching
httpx globally for the duration of a test, and the suite next door is already running a different
transport through the same library; a global patch is a live collision risk for about fifteen
lines of saved setup.

:class:`MockPortal` raises on any request it has no route for, so a test that reaches for a real
endpoint fails loudly and by name instead of hanging on a socket. That is what makes "fixtures
only" a proven property rather than an intention.
"""

import json
from collections.abc import AsyncGenerator, Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from app.promotion.client import HubSpotClient, aclose_hubspot_client
from app.shared.provenance import ProvenancedValue, RetrievalMethod

FIXTURES = Path(__file__).parent / "fixtures"

# Shaped like a real private app token so nothing about the tests depends on it being odd, but it
# is not one. The suite asserts this string never reaches a log line.
TEST_TOKEN = "pat-na2-0000000-fixture-token-not-a-secret"

type Responder = Callable[[httpx.Request], httpx.Response]
type Route = tuple[str, str, Responder]


def load_fixture(name: str) -> dict[str, object]:
    """Read a recorded response body. Fixtures are synthetic but schema-accurate."""
    payload: dict[str, object] = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return payload


def json_responder(
    status_code: int, payload: object, headers: dict[str, str] | None = None
) -> Responder:
    """Always answer with this status and body."""

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=payload, headers=headers)

    return respond


def text_responder(status_code: int, body: str) -> Responder:
    """Answer with something that is not JSON — a proxy's HTML error page, say."""

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, text=body, headers={"Content-Type": "text/html"})

    return respond


def sequence_responder(*responses: tuple[int, object]) -> Responder:
    """Answer differently on each successive call; the last entry repeats.

    This is how a retry is tested honestly — the second attempt has to see something the first did
    not, or the test proves only that the code called twice.
    """
    calls = {"n": 0}

    def respond(request: httpx.Request) -> httpx.Response:
        index = min(calls["n"], len(responses) - 1)
        calls["n"] += 1
        status_code, payload = responses[index]
        return httpx.Response(status_code, json=payload)

    return respond


class MockPortal:
    """A mock HubSpot, plus the log of what was actually sent to it.

    Routes are matched in order on ``(method, path fragment)``, so a more specific route goes
    first. An unmatched request is an :class:`AssertionError`, never a network call.
    """

    def __init__(self, routes: Sequence[Route] | None = None) -> None:
        self._routes: list[Route] = list(routes or [])
        self.requests: list[httpx.Request] = []
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        for method, fragment, responder in self._routes:
            if request.method == method and fragment in request.url.path:
                return responder(request)
        raise AssertionError(
            f"unmatched HubSpot request: {request.method} {request.url.path} — every test runs "
            "against fixtures, so nothing may reach the network"
        )

    def count(self, method: str, fragment: str = "") -> int:
        """How many requests matched this method and path fragment."""
        return len([r for r in self.requests if r.method == method and fragment in r.url.path])

    def bodies(self, method: str, fragment: str = "") -> list[dict[str, object]]:
        """The decoded JSON bodies of the matching requests."""
        decoded: list[dict[str, object]] = []
        for request in self.requests:
            if request.method == method and fragment in request.url.path:
                decoded.append(json.loads(request.content))
        return decoded


def make_client(portal: MockPortal) -> HubSpotClient:
    """A client wired to a mock portal instead of the network."""
    return HubSpotClient(token=TEST_TOKEN, transport=portal.transport)


@pytest.fixture(autouse=True)
async def _reset_hubspot_client() -> AsyncGenerator[None, None]:
    """Drop the client singleton around every test.

    Mirrors ``_reset_database_state`` in the parent conftest, and for the same reason: a test that
    monkeypatches the token after an earlier test already built the client would otherwise reuse
    the stale one, and that failure reads like witchcraft.
    """
    await aclose_hubspot_client()
    yield
    await aclose_hubspot_client()


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Replace ``asyncio.sleep`` in the client with a recorder.

    Returns the list of requested durations, so a test can assert not just *that* the code backed
    off but *that it did not* — "fails fast rather than sleeping" is otherwise unobservable.
    """
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr("app.promotion.client.asyncio.sleep", fake_sleep)
    return slept


SOURCE_URL = "https://safer.fmcsa.dot.gov/query.asp?query_string=MC1234567"
RETRIEVED_AT = datetime(2026, 9, 27, 14, 30, tzinfo=UTC)


def cited[T](value: T) -> ProvenancedValue[T]:
    """A value with an honest citation attached — the only kind the write-gate accepts."""
    return ProvenancedValue(
        value=value,
        source_url=SOURCE_URL,
        retrieved_at=RETRIEVED_AT,
        retrieval_method=RetrievalMethod.registry_api,
    )
