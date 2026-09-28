"""The request funnel: auth, versioned paths, the throttle, and the retry policy.

The retry policy is the part of this slice most likely to be got wrong, and its worst failure is
silent: a create retried after a 5xx can leave two companies in a portal nobody is auditing. Each
row of the plan's retry table has a test here.
"""

import json
import time
from datetime import UTC, datetime

import httpx
import pytest
from pydantic import ValidationError
from structlog.testing import capture_logs

from app.core.config import get_settings
from app.core.exceptions import ConfigurationError
from app.promotion.client import (
    API_VERSION,
    HubSpotClient,
    aclose_hubspot_client,
    get_hubspot_client,
    is_initialised,
)
from app.promotion.exceptions import (
    HubSpotAuthError,
    HubSpotRateLimitError,
    HubSpotResponseError,
    HubSpotTransportError,
)
from app.promotion.schemas import (
    ObjectType,
    SearchFilter,
    SearchFilterGroup,
    SearchRequest,
    TaskCreate,
    TaskType,
)
from tests.promotion.conftest import (
    TEST_TOKEN,
    MockPortal,
    cited,
    json_responder,
    load_fixture,
    make_client,
    sequence_responder,
    text_responder,
)

COMPANIES = ("POST", "/crm/objects/2026-09/companies")
PROPERTY_GET = ("GET", "/crm/properties/")
TASKS = ("POST", "/tasks")

DOMAIN = {"domain": cited("bluebonnetlogistics.com")}


def _search_request() -> SearchRequest:
    return SearchRequest(
        filter_groups=[
            SearchFilterGroup(filters=[SearchFilter(property_name="domain", value="acme.com")])
        ],
        properties=["domain"],
    )


class TestRequestShape:
    async def test_the_token_is_sent_as_a_bearer_header(self) -> None:
        portal = MockPortal([(*COMPANIES, json_responder(201, load_fixture("company_created")))])
        client = make_client(portal)

        await client.create_company(DOMAIN)

        assert portal.requests[0].headers["Authorization"] == f"Bearer {TEST_TOKEN}"
        await client.aclose()

    async def test_paths_carry_the_dated_api_version_not_the_legacy_one(self) -> None:
        """Legacy `v3` URLs still work but are the frozen surface — starting there owes a
        migration on day one."""
        portal = MockPortal([(*COMPANIES, json_responder(201, load_fixture("company_created")))])
        client = make_client(portal)

        await client.create_company(DOMAIN)

        path = portal.requests[0].url.path
        assert path == f"/crm/objects/{API_VERSION}/companies"
        assert "/v3/" not in path
        await client.aclose()

    async def test_the_token_never_reaches_a_log_line(self) -> None:
        portal = MockPortal([(*COMPANIES, json_responder(201, load_fixture("company_created")))])
        client = make_client(portal)

        with capture_logs() as captured:
            await client.create_company(DOMAIN)

        assert captured, "the request funnel must log something"
        assert TEST_TOKEN not in json.dumps(captured, default=str)
        await client.aclose()


class TestRateLimiting:
    async def test_a_rolling_rate_limit_is_retried_and_then_succeeds(
        self, no_sleep: list[float]
    ) -> None:
        """A 429 is retried even on a create: the request never executed, so nothing duplicates."""
        portal = MockPortal(
            [
                (
                    *COMPANIES,
                    sequence_responder(
                        (429, load_fixture("error_rate_limit_rolling")),
                        (201, load_fixture("company_created")),
                    ),
                )
            ]
        )
        client = make_client(portal)

        created = await client.create_company(DOMAIN)

        assert created.id == "7009876543"
        assert portal.count("POST") == 2
        assert len(no_sleep) == 1
        await client.aclose()

    async def test_exhausted_retries_raise_rather_than_loop(self, no_sleep: list[float]) -> None:
        portal = MockPortal(
            [(*COMPANIES, json_responder(429, load_fixture("error_rate_limit_rolling")))]
        )
        client = make_client(portal)

        with pytest.raises(HubSpotRateLimitError) as raised:
            await client.create_company(DOMAIN)

        assert raised.value.attempts == 4
        assert raised.value.policy == "TEN_SECONDLY_ROLLING"
        assert portal.count("POST") == 4
        await client.aclose()

    async def test_a_daily_quota_rejection_fails_fast(self, no_sleep: list[float]) -> None:
        """250,000 calls in a day is a bug, not load — sleeping on it just hides the bug."""
        portal = MockPortal(
            [(*COMPANIES, json_responder(429, load_fixture("error_rate_limit_daily")))]
        )
        client = make_client(portal)

        with pytest.raises(HubSpotRateLimitError) as raised:
            await client.create_company(DOMAIN)

        assert raised.value.policy == "DAILY"
        assert raised.value.attempts == 1
        assert portal.count("POST") == 1
        assert no_sleep == [], "a daily rejection must not be waited out"
        await client.aclose()

    async def test_a_thin_remaining_budget_engages_the_throttle(
        self, no_sleep: list[float]
    ) -> None:
        """HubSpot's guidance is to stay under the limit, not to react to being rejected."""
        portal = MockPortal(
            [
                (
                    *COMPANIES,
                    json_responder(
                        201,
                        load_fixture("company_created"),
                        headers={
                            "X-HubSpot-RateLimit-Remaining": "2",
                            "X-HubSpot-RateLimit-Interval-Milliseconds": "10000",
                        },
                    ),
                )
            ]
        )
        client = make_client(portal)

        await client.create_company(DOMAIN)

        assert no_sleep == [10.0]
        await client.aclose()

    async def test_search_calls_are_spaced_for_the_five_per_second_ceiling(self) -> None:
        """Search responses carry no rate-limit headers, so pacing is the only mechanism.

        Deliberately not using the `no_sleep` fixture: the interval is the behaviour under test,
        and a recorded sleep duration would prove only that a number was passed to `sleep`.
        """
        portal = MockPortal(
            [("POST", "companies/search", json_responder(200, load_fixture("company_search_miss")))]
        )
        client = make_client(portal)

        started = time.perf_counter()
        await client.search(ObjectType.companies, _search_request())
        await client.search(ObjectType.companies, _search_request())
        elapsed = time.perf_counter() - started

        assert elapsed >= 0.2
        assert portal.count("POST") == 2
        await client.aclose()


class TestRetryPolicy:
    async def test_a_server_error_on_a_create_is_never_retried(self, no_sleep: list[float]) -> None:
        """The single most expensive bug available in this slice.

        HubSpot may have created the company and failed to answer; a retry makes a second one, and
        nothing downstream would ever notice.
        """
        portal = MockPortal([(*COMPANIES, json_responder(500, load_fixture("error_server")))])
        client = make_client(portal)

        with pytest.raises(HubSpotResponseError) as raised:
            await client.create_company(DOMAIN)

        assert portal.count("POST") == 1
        assert raised.value.status_code_received == 500
        assert no_sleep == []
        await client.aclose()

    async def test_a_server_error_on_a_read_is_retried(self, no_sleep: list[float]) -> None:
        portal = MockPortal(
            [
                (
                    *PROPERTY_GET,
                    sequence_responder(
                        (500, load_fixture("error_server")),
                        (200, load_fixture("property_found")),
                    ),
                )
            ]
        )
        client = make_client(portal)

        found = await client.get_property(ObjectType.companies, "lpe_source_url")

        assert found is not None
        assert portal.count("GET") == 2
        await client.aclose()

    async def test_a_timeout_on_a_create_is_not_retried(self, no_sleep: list[float]) -> None:
        """Same ambiguity as the 5xx: we cannot know whether the request arrived."""

        def time_out(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("connect timed out")

        portal = MockPortal([(*COMPANIES, time_out)])
        client = make_client(portal)

        with pytest.raises(HubSpotTransportError):
            await client.create_company(DOMAIN)

        assert portal.count("POST") == 1
        await client.aclose()

    async def test_a_migration_is_raised_rather_than_waited_out(
        self, no_sleep: list[float]
    ) -> None:
        """A 477 can last up to 24 hours. Sitting in a retry loop would hang the weekly run."""
        portal = MockPortal([(*PROPERTY_GET, json_responder(477, load_fixture("error_migration")))])
        client = make_client(portal)

        with pytest.raises(HubSpotResponseError) as raised:
            await client.get_property(ObjectType.companies, "lpe_source_url")

        assert raised.value.status_code_received == 477
        assert portal.count("GET") == 1
        assert no_sleep == []
        await client.aclose()

    async def test_a_locked_response_is_retried_once(self, no_sleep: list[float]) -> None:
        portal = MockPortal(
            [
                (
                    *PROPERTY_GET,
                    sequence_responder(
                        (423, load_fixture("error_server")),
                        (200, load_fixture("property_found")),
                    ),
                )
            ]
        )
        client = make_client(portal)

        found = await client.get_property(ObjectType.companies, "lpe_source_url")

        assert found is not None
        assert portal.count("GET") == 2
        assert no_sleep == [2.0]
        await client.aclose()


class TestErrorMapping:
    async def test_an_unauthorized_response_raises_an_auth_error(self) -> None:
        portal = MockPortal([(*COMPANIES, json_responder(401, load_fixture("error_server")))])
        client = make_client(portal)

        with pytest.raises(HubSpotAuthError):
            await client.create_company(DOMAIN)

        assert portal.count("POST") == 1, "a scope problem is not fixed by asking again"
        await client.aclose()

    async def test_a_forbidden_response_carries_the_scope_that_is_missing(self) -> None:
        portal = MockPortal([(*COMPANIES, json_responder(403, load_fixture("error_forbidden")))])
        client = make_client(portal)

        with pytest.raises(HubSpotAuthError) as raised:
            await client.create_company(DOMAIN)

        assert raised.value.missing_scope == "crm.schemas.companies.write"
        await client.aclose()

    async def test_the_correlation_id_reaches_the_exception(self) -> None:
        """It is the only handle support has on a specific failed call."""
        portal = MockPortal([(*COMPANIES, json_responder(400, load_fixture("error_validation")))])
        client = make_client(portal)

        with pytest.raises(HubSpotResponseError) as raised:
            await client.create_company(DOMAIN)

        assert raised.value.correlation_id == "c3e9a710-8b42-4d6f-95a1-0f7c2b84e93d"
        assert raised.value.category == "VALIDATION_ERROR"
        await client.aclose()

    async def test_a_non_json_error_body_does_not_become_a_validation_error(self) -> None:
        """A proxy in front of HubSpot answers with HTML. That must stay an upstream failure."""
        portal = MockPortal(
            [(*COMPANIES, text_responder(503, "<html><body>503 Service Unavailable</body></html>"))]
        )
        client = make_client(portal)

        with pytest.raises(HubSpotResponseError) as raised:
            await client.create_company(DOMAIN)

        assert raised.value.status_code_received == 503
        assert raised.value.category is None
        await client.aclose()


class TestTasksAreUngated:
    async def test_a_task_is_created_for_a_record_with_nothing_to_cite(self) -> None:
        """The T13 guarantee. If this fails, adopting the existing 22 is impossible.

        The write-gate governs prospect field writes, not task creation — a task is something we
        are asking a human to do, not a claim about a business.
        """
        portal = MockPortal([(*TASKS, json_responder(201, load_fixture("task_created")))])
        client = make_client(portal)

        created = await client.create_task(
            TaskCreate(
                subject="Call - Bluebonnet Logistics",
                due_at=datetime(2026, 10, 1, 15, 0, tzinfo=UTC),
                task_type=TaskType.call,
            ),
            contact_id="400112233",
        )

        assert created.id == "88001122"
        await client.aclose()

    async def test_a_task_carries_its_associations_inline(self) -> None:
        portal = MockPortal([(*TASKS, json_responder(201, load_fixture("task_created")))])
        client = make_client(portal)

        await client.create_task(
            TaskCreate(
                subject="Knock - Bluebonnet Logistics",
                due_at=datetime(2026, 10, 1, 15, 0, tzinfo=UTC),
            ),
            contact_id="400112233",
            company_id="7001234567",
        )

        body = portal.bodies("POST", "/tasks")[0]
        assert body["associations"] == [
            {
                "to": {"id": "400112233"},
                "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 204}],
            },
            {
                "to": {"id": "7001234567"},
                "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 192}],
            },
        ]
        await client.aclose()

    def test_a_naive_due_date_is_refused(self) -> None:
        """A naive datetime takes the local zone, so the touch lands on the wrong day silently."""
        with pytest.raises(ValidationError):
            TaskCreate(subject="Call", due_at=datetime(2026, 10, 1, 15, 0))


class TestTierAwareness:
    def test_the_client_offers_no_sequences_method(self) -> None:
        """Spike 3: portal 244766495 is Sales Hub Starter — there is no sequences API to call.

        The absence is the assertion. `app/cadence/` owns the three-touch state machine precisely
        because there is nothing in HubSpot to hand it to.
        """
        assert [name for name in dir(HubSpotClient) if "sequence" in name.lower()] == []


class TestLifecycle:
    async def test_a_missing_token_is_a_configuration_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Settings keeps the token optional so the service — and `/health` — boots without it."""
        settings = get_settings().model_copy(update={"hubspot_private_app_token": None})
        monkeypatch.setattr("app.promotion.client.get_settings", lambda: settings)

        with pytest.raises(ConfigurationError) as raised:
            get_hubspot_client()

        assert "HUBSPOT_PRIVATE_APP_TOKEN" in str(raised.value)

    async def test_the_singleton_is_built_once_and_closed_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = get_settings().model_copy(update={"hubspot_private_app_token": TEST_TOKEN})
        monkeypatch.setattr("app.promotion.client.get_settings", lambda: settings)

        assert is_initialised() is False
        first = get_hubspot_client()
        assert is_initialised() is True
        assert get_hubspot_client() is first

        await aclose_hubspot_client()
        assert is_initialised() is False
