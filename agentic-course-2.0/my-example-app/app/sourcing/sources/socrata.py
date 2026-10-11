"""SODA, the query API in front of every dataset on ``data.transportation.gov``, and the pushdown.

**Why push rules down.** The FMCSA census holds about 4.5 million rows. Freight's free predicates
leave about 4,450 (measured 2026-10-09, and again at 4,464 on 2026-10-10 through this exact
translation). Asking SODA for the 4,450 is one page; downloading the 4.5 million to filter here
is not a weekly job. So each predicate rule on a column the dataset holds is translated into a
SoQL **keep** clause — the rule's negation — and the dataset does the filtering.

**One rule language, two evaluators.** T7 evaluates the same rules in Python, on candidates. The
two must agree about a field the record does not state, so the null semantics are a written
contract (``app/sourcing/README.md`` → *Contract with T7*):

* an *exclude-by-absence* operator (``not_equals``, ``not_in_set``, ``not_contains``) **fires**
  on an absent field — an absent county is not one of the eleven;
* every other operator **does not** — an absent ``power_units`` is not "more than 10".

A rule this module cannot express (``is_true``, a mismatched value type, a numeric compare on a
column it does not know to be numeric) is not pushed down. It is reported, logged, and left for
T7 — the pool is then larger than it might be, never smaller than it should be.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import cast

import httpx

from app.core.logging import get_logger
from app.manifests.schemas import DisqualifierRule, RuleKind, RuleOperator
from app.sourcing.exceptions import SourceRequestError
from app.sourcing.sources import http
from app.sourcing.sources.base import normalise_dataset_url

logger = get_logger(__name__)

DEFAULT_PAGE_SIZE = 5000
_DATASET_ID = re.compile(r"^[a-z0-9]{4}-[a-z0-9]{4}$")


@dataclass(frozen=True, kw_only=True)
class Pushdown:
    """A ``$where`` built from a manifest's rules, and which rules it does and does not cover."""

    where: str | None
    pushed_down: tuple[str, ...]
    not_pushed_down: tuple[str, ...]


def resource_url(dataset_url: str) -> str:
    """The SODA endpoint for a dataset, from any URL form a manifest might cite."""
    identity = normalise_dataset_url(dataset_url)
    host, _, dataset_id = identity.partition("/")
    if not host or not _DATASET_ID.match(dataset_id):
        raise ValueError(f"not a Socrata dataset URL: {dataset_url!r}")
    return f"https://{host}/resource/{dataset_id}.json"


def soql_literal(value: str) -> str:
    """A SoQL string literal: single-quoted, with any single quote inside doubled."""
    return "'" + value.replace("'", "''") + "'"


def _keep_clause(rule: DisqualifierRule, integer_columns: frozenset[str]) -> str | None:
    """The SoQL condition a row must meet for ``rule`` *not* to fire, or ``None`` if inexpressible.

    ``rule.field`` has already been checked against the dataset's columns, so it is never
    interpolated as anything but a known column name.
    """
    column = rule.field
    value = rule.value
    if column is None or rule.operator is None:
        return None

    match rule.operator:
        case RuleOperator.equals if isinstance(value, str):
            return f"({column} IS NULL OR {column} != {soql_literal(value)})"
        case RuleOperator.not_equals if isinstance(value, str):
            return f"({column} = {soql_literal(value)})"
        case RuleOperator.in_set if isinstance(value, tuple) and value:
            members = ", ".join(soql_literal(member) for member in value)
            return f"({column} IS NULL OR {column} NOT IN ({members}))"
        case RuleOperator.not_in_set if isinstance(value, tuple) and value:
            members = ", ".join(soql_literal(member) for member in value)
            return f"({column} IN ({members}))"
        case RuleOperator.contains if isinstance(value, str):
            return f"({column} IS NULL OR NOT contains({column}, {soql_literal(value)}))"
        case RuleOperator.not_contains if isinstance(value, str):
            return f"({column} IS NOT NULL AND contains({column}, {soql_literal(value)}))"
        case RuleOperator.greater_than | RuleOperator.less_than:
            # `bool` is an `int`; a rule saying "greater than True" is a mistake, not a number.
            if not isinstance(value, int) or isinstance(value, bool):
                return None
            if column not in integer_columns:
                return None
            keep = "<=" if rule.operator is RuleOperator.greater_than else ">="
            return f"({column} IS NULL OR {column}::number {keep} {int(value)})"
        case _:
            return None


def pushdown_where(
    rules: Sequence[DisqualifierRule],
    *,
    columns: frozenset[str],
    integer_columns: frozenset[str] = frozenset(),
) -> Pushdown:
    """Translate every predicate rule on one of ``columns`` into a ``$where``, in rule order.

    Judgment rules and rules on other fields (``allowToOperate`` lives in QCMobile, not the
    census) are not this dataset's to apply, and appear in neither list.
    """
    clauses: list[str] = []
    pushed: list[str] = []
    skipped: list[str] = []
    for rule in rules:
        if rule.kind is not RuleKind.predicate or rule.field not in columns:
            continue
        clause = _keep_clause(rule, integer_columns)
        if clause is None:
            skipped.append(rule.id)
            logger.warning(
                "sourcing.socrata.pushdown_skipped",
                rule_id=rule.id,
                field=rule.field,
                operator=rule.operator.value if rule.operator is not None else None,
            )
            continue
        clauses.append(clause)
        pushed.append(rule.id)

    where = " AND ".join(clauses) if clauses else None
    logger.info(
        "sourcing.socrata.where_built",
        rule_ids=pushed,
        not_pushed_down=skipped,
        clauses=len(clauses),
    )
    return Pushdown(where=where, pushed_down=tuple(pushed), not_pushed_down=tuple(skipped))


def _string_row(row: object, *, source: str) -> dict[str, str] | None:
    """Keep a row's string values.

    SODA's JSON states every value as a string. Anything else is something this client does not
    understand, so it is dropped and logged rather than guessed at.
    """
    if not isinstance(row, dict):
        return None
    kept: dict[str, str] = {}
    dropped: list[str] = []
    # Narrowing `object` to `dict` leaves its key and value types unknown, which Pyright strict
    # rejects; the cast states what is already true of any dict, as `shared/provenance.py` does.
    for key, value in cast(dict[object, object], row).items():
        if isinstance(key, str) and isinstance(value, str):
            kept[key] = value
        else:
            dropped.append(str(key))
    if dropped:
        logger.warning("sourcing.socrata.values_dropped", source=source, keys=sorted(dropped))
    return kept


class SodaClient:
    """Pages through one SODA query. Read-only, so every failure is retried before it is raised."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        source: str,
        app_token: str | None,
        page_size: int = DEFAULT_PAGE_SIZE,
        backoff_seconds: float = http.DEFAULT_BACKOFF_SECONDS,
    ) -> None:
        self._client = client
        self._source = source
        token = (app_token or "").strip()
        self._headers: Mapping[str, str] = {"X-App-Token": token} if token else {}
        self._page_size = page_size
        self._backoff_seconds = backoff_seconds

    async def query(
        self,
        dataset_url: str,
        *,
        where: str | None,
        order: str,
        select: str | None = None,
    ) -> list[dict[str, str]]:
        """Every row matching ``where``, ordered by ``order``, fetched a page at a time.

        ``$limit`` is always sent: SODA's default is 1,000, which would silently truncate a pool.
        Paging stops on the first short page, which needs a stable ``order`` to be correct.
        """
        url = resource_url(dataset_url)
        rows: list[dict[str, str]] = []
        offset = 0
        while True:
            params: dict[str, str] = {
                "$limit": str(self._page_size),
                "$offset": str(offset),
                "$order": order,
            }
            if where is not None:
                params["$where"] = where
            if select is not None:
                params["$select"] = select

            response = await http.get(
                self._client,
                url,
                source=self._source,
                params=params,
                headers=self._headers,
                backoff_seconds=self._backoff_seconds,
            )
            body = http.json_body(response, source=self._source)
            if not isinstance(body, list):
                raise SourceRequestError(
                    f"{self._source} answered with a {type(body).__name__}, not a list of rows",
                    source=self._source,
                    status=response.status_code,
                )
            listed = cast(list[object], body)
            page: list[dict[str, str]] = []
            for raw in listed:
                row = _string_row(raw, source=self._source)
                if row is not None:
                    page.append(row)
            rows.extend(page)
            logger.info(
                "sourcing.socrata.page_fetched",
                source=self._source,
                dataset=url,
                offset=offset,
                rows=len(page),
            )
            if len(listed) < self._page_size:
                return rows
            offset += self._page_size
