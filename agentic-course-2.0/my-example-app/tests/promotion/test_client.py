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
    HubSpotResponseShapeError,
    HubSpotTransportError,
    UnprovenancedWriteError,
)
from app.promotion.schemas import (
    ObjectType,
    RecordMeta,
    ReviewStatus,
    SearchFilter,
    SearchFilterGroup,
    SearchRequest,
    TaskCreate,
    TaskType,
    Verification,
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
CONTACTS = ("POST", "/crm/objects/2026-09/contacts")
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

    async def test_a_locked_response_on_a_create_is_not_retried(
        self, no_sleep: list[float]
    ) -> None:
        """A 423 is as ambiguous as a 5xx — the lock says nothing about whether our write landed.

        And a duplicate made this way cannot be caught within the same run: the search index lags
        the write, so `dedupe.py` would not see it until a human did.
        """
        portal = MockPortal([(*COMPANIES, json_responder(423, load_fixture("error_server")))])
        client = make_client(portal)

        with pytest.raises(HubSpotResponseError) as raised:
            await client.create_company(DOMAIN)

        assert portal.count("POST") == 1
        assert raised.value.status_code_received == 423
        assert no_sleep == []
        await client.aclose()

    async def test_a_locked_read_gets_one_retry_and_no_more(self, no_sleep: list[float]) -> None:
        """`_LOCKED_MAX_ATTEMPTS`, not the shared `_MAX_ATTEMPTS` budget."""
        portal = MockPortal([(*PROPERTY_GET, json_responder(423, load_fixture("error_server")))])
        client = make_client(portal)

        with pytest.raises(HubSpotResponseError) as raised:
            await client.get_property(ObjectType.companies, "lpe_source_url")

        assert portal.count("GET") == 2
        assert raised.value.status_code_received == 423
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


META = RecordMeta(
    vertical="freight",
    review_status=ReviewStatus.pending_review,
    route_cluster="A · 75238",
    verification=Verification.none,
    owner_id="81234567",
)


class TestRecordMeta:
    """OQ6: the closed carve-out for engine metadata — facts about our process, not the prospect."""

    def test_it_holds_exactly_five_fields(self) -> None:
        """Widening the carve-out must be a visible diff, never a side door for a prospect field.

        ``verification`` widened it on 2026-10-16: which of our checks found the business.
        """
        assert set(RecordMeta.model_fields) == {
            "vertical",
            "review_status",
            "route_cluster",
            "verification",
            "owner_id",
        }

    def test_a_company_gets_all_five_properties(self) -> None:
        assert META.to_properties(ObjectType.companies) == {
            "lpe_vertical": "freight",
            "lpe_review_status": "pending_review",
            "lpe_route_cluster": "A · 75238",
            "lpe_verification": "none",
            "hubspot_owner_id": "81234567",
        }

    def test_a_contact_gets_no_review_status_route_or_verification(self) -> None:
        """All three exist on COMPANY only; on a contact they would be a 400."""
        assert META.to_properties(ObjectType.contacts) == {
            "lpe_vertical": "freight",
            "hubspot_owner_id": "81234567",
        }

    def test_absent_values_are_omitted_not_sent_blank(self) -> None:
        """A blank sent to HubSpot clears the property; absence leaves it alone."""
        assert RecordMeta(vertical="freight").to_properties(ObjectType.companies) == {
            "lpe_vertical": "freight"
        }

    def test_an_unknown_field_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            RecordMeta.model_validate({"vertical": "freight", "phone": "2145550100"})

    def test_it_is_frozen(self) -> None:
        with pytest.raises(ValidationError):
            META.vertical = "fire"


class TestObjectWrites:
    async def test_meta_is_merged_after_the_gate(self) -> None:
        portal = MockPortal([(*COMPANIES, json_responder(201, load_fixture("company_created")))])
        client = make_client(portal)

        await client.create_company(DOMAIN, meta=META)

        assert portal.bodies("POST", "/companies")[0]["properties"] == {
            "domain": "bluebonnetlogistics.com",
            "lpe_vertical": "freight",
            "lpe_review_status": "pending_review",
            "lpe_route_cluster": "A · 75238",
            "lpe_verification": "none",
            "hubspot_owner_id": "81234567",
        }
        await client.aclose()

    async def test_a_company_create_sends_no_associations_key(self) -> None:
        """`None`, not `[]`: a plain company create's body is byte-identical to T3's."""
        portal = MockPortal([(*COMPANIES, json_responder(201, load_fixture("company_created")))])
        client = make_client(portal)

        await client.create_company(DOMAIN, meta=META)

        assert "associations" not in portal.bodies("POST", "/companies")[0]
        await client.aclose()

    async def test_a_contact_is_created_with_a_primary_association_to_its_company(self) -> None:
        """Written in the same request, so the association walk finds it at once (recovery)."""
        portal = MockPortal([(*CONTACTS, json_responder(201, {"id": "400112233"}))])
        client = make_client(portal)

        created = await client.create_contact(
            {"lastname": cited("MUSGRAVES")}, meta=META, company_id="7009876543"
        )

        assert created.id == "400112233"
        body = portal.bodies("POST", "/contacts")[0]
        assert body["associations"] == [
            {
                "to": {"id": "7009876543"},
                "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 1}],
            }
        ]
        assert body["properties"] == {
            "lastname": "MUSGRAVES",
            "lpe_vertical": "freight",
            "hubspot_owner_id": "81234567",
        }
        await client.aclose()

    async def test_a_contact_without_a_company_sends_no_associations_key(self) -> None:
        portal = MockPortal([(*CONTACTS, json_responder(201, {"id": "400112233"}))])
        client = make_client(portal)

        await client.create_contact({"lastname": cited("MUSGRAVES")})

        assert "associations" not in portal.bodies("POST", "/contacts")[0]
        await client.aclose()

    async def test_a_gate_refusal_with_meta_sends_nothing(self) -> None:
        """Meta cannot carry an uncitable prospect field past the gate; nothing reaches the wire."""
        portal = MockPortal()
        client = make_client(portal)

        with pytest.raises(UnprovenancedWriteError) as raised:
            await client.create_contact({"lastname": None}, meta=META, company_id="7009876543")

        assert raised.value.fields == ("lastname",)
        assert portal.requests == []
        await client.aclose()

    async def test_a_key_claimed_by_both_halves_is_refused_before_any_request(self) -> None:
        """A programming error: one property, two owners. Neither silently wins."""
        portal = MockPortal()
        client = make_client(portal)

        with pytest.raises(ValueError, match="lpe_vertical"):
            await client.create_company({"lpe_vertical": cited("freight")}, meta=META)

        assert portal.requests == []
        await client.aclose()

    async def test_an_update_merges_meta_too(self) -> None:
        portal = MockPortal(
            [
                (
                    "PATCH",
                    "/companies/7009876543",
                    json_responder(200, load_fixture("company_created")),
                )
            ]
        )
        client = make_client(portal)

        await client.update_company("7009876543", DOMAIN, meta=RecordMeta(vertical="freight"))

        assert portal.bodies("PATCH")[0] == {
            "properties": {"domain": "bluebonnetlogistics.com", "lpe_vertical": "freight"}
        }
        await client.aclose()

    async def test_a_create_logs_the_new_object(self) -> None:
        portal = MockPortal([(*COMPANIES, json_responder(201, load_fixture("company_created")))])
        client = make_client(portal)

        with capture_logs() as captured:
            await client.create_company(DOMAIN)

        created = [e for e in captured if e["event"] == "promotion.hubspot.object_created"]
        assert created == [
            {
                "event": "promotion.hubspot.object_created",
                "log_level": "info",
                "object_type": "companies",
                "object_id": "7009876543",
            }
        ]
        await client.aclose()


class TestAnUnreadableBody:
    """A 2xx whose body is not what we asked for is a typed error, never a traceback (PR #33 L1)."""

    async def test_a_create_answered_with_html_is_a_shape_error(self) -> None:
        portal = MockPortal([(*COMPANIES, text_responder(201, "<html>proxy</html>"))])
        client = make_client(portal)

        with pytest.raises(HubSpotResponseShapeError):
            await client.create_company(DOMAIN)

        assert portal.count("POST", "/companies") == 1

    async def test_a_record_without_an_id_is_a_shape_error(self) -> None:
        portal = MockPortal([(*COMPANIES, json_responder(201, {"properties": {}}))])
        client = make_client(portal)

        with pytest.raises(HubSpotResponseShapeError):
            await client.create_company(DOMAIN)

    async def test_a_batch_read_answered_with_html_is_a_shape_error(self) -> None:
        portal = MockPortal(
            [("POST", "/companies/batch/read", text_responder(200, "<html>proxy</html>"))]
        )
        client = make_client(portal)

        with pytest.raises(HubSpotResponseShapeError):
            await client.batch_read(ObjectType.companies, ["7001"], ["name"])


class TestBatchReadNotFound:
    """Only an explicit ``OBJECT_NOT_FOUND`` says a record is gone (PR #33 M3)."""

    async def test_not_found_ids_are_reported_apart_from_the_results(self) -> None:
        payload = {
            "status": "COMPLETE",
            "results": [{"id": "7001", "properties": {"name": "Acme"}}],
            "numErrors": 2,
            "errors": [
                {
                    "status": "error",
                    "category": "OBJECT_NOT_FOUND",
                    "message": "Could not get some COMPANY objects.",
                    "context": {"ids": ["7002"]},
                },
                {"status": "error", "category": "INTERNAL_ERROR", "message": "try again"},
            ],
        }
        portal = MockPortal([("POST", "/companies/batch/read", json_responder(207, payload))])
        client = make_client(portal)

        read = await client.batch_read_with_not_found(
            ObjectType.companies, ["7001", "7002", "7003"], ["name"]
        )

        assert [record.id for record in read.results] == ["7001"]
        assert read.not_found == frozenset({"7002"})
