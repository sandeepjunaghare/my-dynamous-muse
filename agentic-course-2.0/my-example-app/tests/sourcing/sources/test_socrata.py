"""The rule→SoQL pushdown and the SODA client — no database, no network."""

import httpx
import pytest

from app.manifests.schemas import DisqualifierRule, RuleKind, RuleOperator
from app.sourcing.exceptions import SourceRequestError
from app.sourcing.sources.socrata import (
    SodaClient,
    pushdown_where,
    resource_url,
    soql_literal,
)
from tests.sourcing.builders import freight_v3_rules

COLUMNS = frozenset({"status_code", "phy_state", "phy_cnty", "carship", "power_units", "name"})
INTEGERS = frozenset({"power_units"})
CENSUS = "https://data.transportation.gov/d/az4n-8mr2"


def rule(
    operator: RuleOperator,
    value: str | tuple[str, ...] | int | bool | None,
    field: str = "carship",
    rule_id: str = "r",
) -> DisqualifierRule:
    return DisqualifierRule(
        id=rule_id,
        kind=RuleKind.predicate,
        description="test",
        field=field,
        operator=operator,
        value=value,
    )


def where_for(*rules: DisqualifierRule) -> str | None:
    return pushdown_where(rules, columns=COLUMNS, integer_columns=INTEGERS).where


class TestPushdownOperators:
    """Each clause is the rule's negation — a row is *kept* when the rule does not fire."""

    @pytest.mark.parametrize(
        ("operator", "value", "field", "expected"),
        [
            (
                RuleOperator.equals,
                "I",
                "status_code",
                "(status_code IS NULL OR status_code != 'I')",
            ),
            (RuleOperator.not_equals, "TX", "phy_state", "(phy_state = 'TX')"),
            (
                RuleOperator.in_set,
                ("C", "S"),
                "carship",
                "(carship IS NULL OR carship NOT IN ('C', 'S'))",
            ),
            (RuleOperator.not_in_set, ("085", "113"), "phy_cnty", "(phy_cnty IN ('085', '113'))"),
            (
                RuleOperator.contains,
                "C",
                "carship",
                "(carship IS NULL OR NOT contains(carship, 'C'))",
            ),
            (
                RuleOperator.not_contains,
                "B",
                "carship",
                "(carship IS NOT NULL AND contains(carship, 'B'))",
            ),
            (
                RuleOperator.greater_than,
                10,
                "power_units",
                "(power_units IS NULL OR power_units::number <= 10)",
            ),
            (
                RuleOperator.less_than,
                1,
                "power_units",
                "(power_units IS NULL OR power_units::number >= 1)",
            ),
        ],
    )
    def test_each_operator_becomes_its_keep_clause(
        self, operator: RuleOperator, value: str | tuple[str, ...] | int, field: str, expected: str
    ) -> None:
        assert where_for(rule(operator, value, field)) == expected

    def test_exclude_by_absence_fires_on_a_missing_field_and_the_rest_do_not(self) -> None:
        """The null contract with T7, stated as SQL: only these three omit ``IS NULL OR``."""
        for operator, value in (
            (RuleOperator.not_equals, "TX"),
            (RuleOperator.not_in_set, ("TX",)),
            (RuleOperator.not_contains, "B"),
        ):
            assert "IS NULL OR" not in (where_for(rule(operator, value)) or "")
        for operator, value in (
            (RuleOperator.equals, "I"),
            (RuleOperator.in_set, ("C",)),
            (RuleOperator.contains, "C"),
        ):
            assert "IS NULL OR" in (where_for(rule(operator, value)) or "")

    def test_a_quote_in_a_value_is_doubled(self) -> None:
        assert soql_literal("O'NEIL") == "'O''NEIL'"
        assert where_for(rule(RuleOperator.equals, "O'NEIL", "name")) == (
            "(name IS NULL OR name != 'O''NEIL')"
        )

    def test_clauses_join_with_and_in_rule_order(self) -> None:
        where = where_for(
            rule(RuleOperator.not_equals, "TX", "phy_state", "a"),
            rule(RuleOperator.equals, "I", "status_code", "b"),
        )
        assert where == "(phy_state = 'TX') AND (status_code IS NULL OR status_code != 'I')"

    def test_no_rules_means_no_where(self) -> None:
        assert where_for() is None


class TestPushdownRefusals:
    """A rule this dataset cannot express is reported and left for T7 — never mistranslated."""

    @pytest.mark.parametrize(
        "unexpressible",
        [
            rule(RuleOperator.is_true, True, "carship"),
            rule(RuleOperator.greater_than, True, "power_units"),
            rule(RuleOperator.greater_than, "10", "power_units"),
            rule(RuleOperator.greater_than, 10, "carship"),
            rule(RuleOperator.equals, ("A", "B"), "carship"),
            rule(RuleOperator.in_set, "A", "carship"),
            rule(RuleOperator.not_in_set, (), "carship"),
        ],
    )
    def test_an_unexpressible_rule_is_not_pushed_down(
        self, unexpressible: DisqualifierRule
    ) -> None:
        pushdown = pushdown_where([unexpressible], columns=COLUMNS, integer_columns=INTEGERS)
        assert pushdown.where is None
        assert pushdown.not_pushed_down == ("r",)
        assert pushdown.pushed_down == ()

    def test_judgment_rules_and_foreign_fields_are_not_this_datasets(self) -> None:
        judgment = DisqualifierRule(id="j", kind=RuleKind.judgment, description="rollup")
        foreign = rule(RuleOperator.equals, "N", "allowToOperate", "q")
        pushdown = pushdown_where([judgment, foreign], columns=COLUMNS)
        assert pushdown == pushdown_where([], columns=COLUMNS)


class TestFreightV3:
    def test_freight_v3_pushes_its_census_rules_and_leaves_allow_to_operate(self) -> None:
        """The exact ``$where`` that returned 4,464 rows from the live census on 2026-10-10
        (4,449 on 2026-10-09 by hand) — if this string changes, re-run Level 4 of the T5 plan."""
        from app.sourcing.sources.fmcsa import CENSUS_COLUMNS, CENSUS_INTEGER_COLUMNS

        pushdown = pushdown_where(
            freight_v3_rules(), columns=CENSUS_COLUMNS, integer_columns=CENSUS_INTEGER_COLUMNS
        )
        assert pushdown.pushed_down == (
            "inactive_registration",
            "outside_texas",
            "outside_dfw_metro",
            "no_broker_entity_type",
            "asset_based_carrier",
        )
        assert pushdown.not_pushed_down == ()
        assert pushdown.where == (
            "(status_code IS NULL OR status_code != 'I')"
            " AND (phy_state = 'TX')"
            " AND (phy_cnty IN ('085', '113', '121', '139', '231', '251', '257', '367', '397',"
            " '439', '497'))"
            " AND (carship IS NOT NULL AND contains(carship, 'B'))"
            " AND (power_units IS NULL OR power_units::number <= 10)"
        )


class TestResourceUrl:
    @pytest.mark.parametrize(
        "url",
        [
            "https://data.transportation.gov/d/az4n-8mr2",
            "https://data.transportation.gov/resource/az4n-8mr2.json",
            "https://DATA.transportation.gov/api/views/az4n-8mr2/",
        ],
    )
    def test_every_form_resolves_to_the_resource_endpoint(self, url: str) -> None:
        assert resource_url(url) == "https://data.transportation.gov/resource/az4n-8mr2.json"

    def test_a_url_that_is_not_a_dataset_is_refused(self) -> None:
        with pytest.raises(ValueError, match="not a Socrata dataset"):
            resource_url("https://safer.fmcsa.dot.gov/CompanySnapshot.aspx")


def _client(handler: httpx.MockTransport, *, token: str | None = None, page: int = 2) -> SodaClient:
    return SodaClient(
        httpx.AsyncClient(transport=handler),
        source="census",
        app_token=token,
        page_size=page,
        backoff_seconds=0.0,
    )


class TestSodaClient:
    async def test_pages_until_a_short_page_and_always_sends_limit(self) -> None:
        rows = [{"dot_number": str(n)} for n in range(5)]
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            offset, limit = int(request.url.params["$offset"]), int(request.url.params["$limit"])
            return httpx.Response(200, json=rows[offset : offset + limit])

        got = await _client(httpx.MockTransport(handler)).query(
            CENSUS, where="(x = 'y')", order="dot_number"
        )
        assert got == rows
        assert [request.url.params["$offset"] for request in seen] == ["0", "2", "4"]
        assert all(request.url.params["$limit"] == "2" for request in seen)
        assert all(request.url.params["$where"] == "(x = 'y')" for request in seen)
        assert all(request.url.params["$order"] == "dot_number" for request in seen)

    async def test_an_exact_multiple_ends_on_an_empty_page(self) -> None:
        rows = [{"dot_number": str(n)} for n in range(4)]

        def handler(request: httpx.Request) -> httpx.Response:
            offset = int(request.url.params["$offset"])
            return httpx.Response(200, json=rows[offset : offset + 2])

        assert (
            len(await _client(httpx.MockTransport(handler)).query(CENSUS, where=None, order="x"))
            == 4
        )

    @pytest.mark.parametrize(("token", "sent"), [("abc", True), (None, False), ("  ", False)])
    async def test_the_app_token_is_sent_only_when_set(self, token: str | None, sent: bool) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json=[])

        await _client(httpx.MockTransport(handler), token=token).query(
            CENSUS, where=None, order="x"
        )
        assert ("X-App-Token" in captured[0].headers) is sent

    async def test_a_429_then_a_200_is_retried(self) -> None:
        answers = [httpx.Response(429, json={}), httpx.Response(200, json=[{"a": "1"}])]
        got = await _client(httpx.MockTransport(lambda request: answers.pop(0))).query(
            CENSUS, where=None, order="x"
        )
        assert got == [{"a": "1"}]

    async def test_a_400_is_a_source_error_naming_the_source(self) -> None:
        transport = httpx.MockTransport(lambda request: httpx.Response(400, json={"code": "bad"}))
        with pytest.raises(SourceRequestError, match="census answered 400") as raised:
            await _client(transport).query(CENSUS, where="(bad)", order="x")
        assert raised.value.status == 400

    async def test_a_body_that_is_not_a_list_is_refused(self) -> None:
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"error": True}))
        with pytest.raises(SourceRequestError, match="not a list"):
            await _client(transport).query(CENSUS, where=None, order="x")

    async def test_non_string_values_are_dropped_not_guessed(self) -> None:
        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, json=[{"a": "1", "b": 2, "c": None}])
        )
        assert await _client(transport).query(CENSUS, where=None, order="x") == [{"a": "1"}]

    async def test_persistent_5xx_gives_up_after_the_retry_budget(self) -> None:
        calls: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(503, json={})

        with pytest.raises(SourceRequestError, match="503"):
            await _client(httpx.MockTransport(handler)).query(CENSUS, where=None, order="x")
        assert len(calls) == 4
