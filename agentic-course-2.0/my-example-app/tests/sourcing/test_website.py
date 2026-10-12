"""The website check: the email domain, a free homepage fetch, the pool's shared domains (T6)."""

from collections.abc import MutableMapping, Sequence

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.testing import capture_logs

from app.shared.provenance import ProvenancedValue, RetrievalMethod
from app.sourcing.service import SourcingService
from app.sourcing.website import HomepageChecker, email_domain
from tests.conftest import requires_db
from tests.sourcing.builders import a_brief, an_active_manifest
from tests.sourcing.conftest import RUN_CLOCK, MockWeb, a_clock, census_rows
from tests.sourcing.test_pool import pool_records


class TestEmailDomain:
    def test_a_business_domain_lowercased(self) -> None:
        assert email_domain("Ops@AcmeFreight.COM") == "acmefreight.com"

    @pytest.mark.parametrize(
        "email", ["owner@gmail.com", "x@yahoo.com", "y@SBCGLOBAL.NET", "z@proton.me"]
    )
    def test_free_mail_is_no_website(self, email: str) -> None:
        assert email_domain(email) is None

    @pytest.mark.parametrize("email", ["n/a", "@acme.com", "a@", "a@b@acme.com", "a@localhost"])
    def test_a_malformed_address_is_no_domain(self, email: str) -> None:
        assert email_domain(email) is None


async def _check(web: MockWeb, domain: str = "acme.test") -> ProvenancedValue[str] | None:
    async with httpx.AsyncClient(transport=web.transport()) as client:
        return await HomepageChecker(client, clock=a_clock()).check(domain)


def _reasons(logs: Sequence[MutableMapping[str, object]]) -> list[object]:
    return [
        log.get("reason") for log in logs if log["event"] == "sourcing.website.homepage_unreachable"
    ]


class TestHomepageChecker:
    async def test_a_200_html_page_is_the_cited_origin(self) -> None:
        web = MockWeb()
        web.html("https://acme.test/")
        cited = await _check(web)
        assert cited is not None
        assert cited.value == "https://acme.test"
        assert cited.source_url == "https://acme.test/"
        assert cited.retrieval_method is RetrievalMethod.web_lookup
        assert cited.retrieved_at == RUN_CLOCK

    async def test_a_redirect_to_www_on_the_same_domain_is_accepted(self) -> None:
        web = MockWeb()
        web.redirect("https://acme.test/", "https://www.acme.test/")
        web.html("https://www.acme.test/")
        cited = await _check(web)
        assert cited is not None
        assert cited.value == "https://www.acme.test"
        assert cited.source_url == "https://www.acme.test/"

    async def test_a_redirect_to_another_host_is_not_this_business(self) -> None:
        """A parked-domain service or a franchisor's site is not the business's own."""
        web = MockWeb()
        web.redirect("https://acme.test/", "https://acme-logistics.test/")
        web.html("https://acme-logistics.test/")
        with capture_logs() as logs:
            assert await _check(web) is None
        assert _reasons(logs) == ["other_host"]

    async def test_a_redirect_loop_gives_up(self) -> None:
        web = MockWeb()
        web.redirect("https://acme.test/", "https://acme.test/a")
        web.redirect("https://acme.test/a", "https://acme.test/b")
        web.redirect("https://acme.test/b", "https://acme.test/c")
        web.redirect("https://acme.test/c", "https://acme.test/")
        with capture_logs() as logs:
            assert await _check(web) is None
        assert _reasons(logs) == ["too_many_redirects"]

    async def test_a_404_is_none(self) -> None:
        web = MockWeb()
        web.status("https://acme.test/", 404)
        with capture_logs() as logs:
            assert await _check(web) is None
        assert _reasons(logs) == ["status_404"]

    async def test_a_pdf_is_not_a_homepage(self) -> None:
        web = MockWeb()
        web.status("https://acme.test/", 200, content_type="application/pdf")
        with capture_logs() as logs:
            assert await _check(web) is None
        assert _reasons(logs) == ["not_html"]

    async def test_a_timeout_is_none_and_never_raises(self) -> None:
        web = MockWeb()

        def times_out(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("slow", request=request)

        web.pages["https://acme.test/"] = times_out
        with capture_logs() as logs:
            assert await _check(web) is None
        assert _reasons(logs) == ["timeout"]

    async def test_a_tls_or_connection_error_is_none(self) -> None:
        web = MockWeb()

        def refuses(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("tls", request=request)

        web.pages["https://acme.test/"] = refuses
        with capture_logs() as logs:
            assert await _check(web) is None
        assert _reasons(logs) == ["transport_ConnectError"]


@requires_db
class TestSharedEmailDomains:
    """A domain three or more pool businesses share is a service's, named by no code."""

    async def test_three_entries_on_one_domain_are_shared_and_two_are_not(
        self, db_session: AsyncSession
    ) -> None:
        _, vertical = await an_active_manifest(db_session)
        service = SourcingService(db_session)
        run = await service.start_run(a_brief(vertical))
        rows = census_rows()
        shared = {"email_address": "agent@processagents.test"}
        pair = {"email_address": "ops@twoshops.test"}
        overrides = [shared, shared, shared, pair, pair, {}, {}, {}]
        records = pool_records(
            [{**row, **extra} for row, extra in zip(rows, overrides, strict=True)]
        )
        await service.refresh_pool_and_select(
            run.id, records, size=1, currency_cutoff=RUN_CLOCK.date()
        )

        assert await service.shared_email_domains(vertical) == frozenset({"processagents.test"})

    async def test_another_verticals_pool_is_not_counted(self, db_session: AsyncSession) -> None:
        _, vertical = await an_active_manifest(db_session)
        _, other = await an_active_manifest(db_session)
        service = SourcingService(db_session)
        run = await service.start_run(a_brief(other))
        rows = [{**row, "email_address": "agent@processagents.test"} for row in census_rows()]
        await service.refresh_pool_and_select(
            run.id, pool_records(rows), size=1, currency_cutoff=RUN_CLOCK.date()
        )

        assert await service.shared_email_domains(vertical) == frozenset()
