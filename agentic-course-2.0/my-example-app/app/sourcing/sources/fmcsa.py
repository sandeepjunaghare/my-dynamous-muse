"""FMCSA: the Company Census File (discovery), QCMobile (lookup) and Motus revocations (join).

**Census first, QCMobile second** (E10). QCMobile is a lookup keyed on a USDOT or MC number; it
cannot search by geography. A list built by querying it is how a 74-row file of BOC-3 process
agents passed for brokers. So the census file — every FMCSA registrant, refreshed daily, public
domain — is the pool, filtered at the source by the manifest's census predicates, and QCMobile is
asked only about the batch.

**Revocations come from Motus.** FMCSA moved its registration system to Motus on 2026-05-14. The
legacy "Revocation - All With History" dataset (``sa6p-acbp``, blob ``rwr4-5nkg``) has not been
updated since, so it is deliberately *not* bound: a frozen list reads as "no revocations", which is
the one wrong answer that looks right. Only Motus RevokeSuspend (``wb4f-neki``) binds. Freight v3
declares the legacy blob, so under v3 the join is unbound and a v4 is needed (see the T5 plan).

Shapes, read live 2026-10-10:

* census JSON states every value as a string and **omits empty fields**; ``dot_number`` is typed
  ``number`` but arrives as ``"7989"``; dates are ``YYYYMMDD``; ``power_units`` and the other fleet
  counts are text and are cast here (T5 ticket: "cast them at the adapter").
* Motus ``usdot_number`` is unpadded (``"1006607"``); rows repeat, and some lack a docket.
* QCMobile's field spelling (``allowToOperate`` per the docs) is **not yet verified** against a live
  response — there is no webKey yet. Values are stored exactly as returned, never renamed.

**The webKey never leaves the request.** It is a query parameter, so citations carry the carrier
URL without it, and ``sources/http.py`` logs URLs without their query strings.
"""

import asyncio
import time
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import cast

from app.core.config import Settings
from app.core.logging import get_logger
from app.manifests.schemas import DisqualifierRule
from app.shared.provenance import ProvenancedValue, RetrievalMethod
from app.sourcing.exceptions import SourceAuthError, SourceRequestError
from app.sourcing.schemas import (
    CandidateFields,
    PostalAddress,
    SourceRecord,
    SourceRow,
    SourceValue,
)
from app.sourcing.sources import http
from app.sourcing.sources.base import (
    AdapterFactory,
    DiscoveryFactory,
    DiscoveryPull,
    JoinFactory,
    LookupFactory,
    PoolRecord,
    SourceEnv,
    dataset_matcher,
    prefix_matcher,
)
from app.sourcing.sources.socrata import SodaClient, pushdown_where, soql_literal

logger = get_logger(__name__)

NAMESPACE = "usdot"
"""Registry ids are ``usdot:<n>``: namespaced before the first real write (T4 review decision 4),
so a second registry's numbers can never collide with a USDOT number in the pool or a lookup."""

CENSUS_DATASET = "data.transportation.gov/az4n-8mr2"
CENSUS_URL = "https://data.transportation.gov/d/az4n-8mr2"
REVOCATIONS_DATASET = "data.transportation.gov/wb4f-neki"
REVOCATIONS_URL = "https://data.transportation.gov/d/wb4f-neki"
QCMOBILE_PREFIX = "mobile.fmcsa.dot.gov/qc/services"
QCMOBILE_BASE = "https://mobile.fmcsa.dot.gov/qc/services"

CENSUS_INTEGER_COLUMNS = frozenset(
    {"power_units", "truck_units", "total_drivers", "total_cdl", "driver_inter_total"}
)
"""Census counts that arrive as text and are compared as numbers (``power_units > 10``)."""

CENSUS_COLUMNS = frozenset(
    {
        "add_date", "avg_drivers_leased_per_month", "bus_units", "business_org_desc",
        "business_org_id", "carrier_mailing_city", "carrier_mailing_cnty",
        "carrier_mailing_country", "carrier_mailing_state", "carrier_mailing_street",
        "carrier_mailing_und_date", "carrier_mailing_zip", "carrier_operation", "carship",
        "cell_phone", "classdef", "company_officer_1", "company_officer_2", "crgo_beverages",
        "crgo_bldgmat", "crgo_cargoothr", "crgo_cargoothr_desc", "crgo_chem", "crgo_coalcoke",
        "crgo_coldfood", "crgo_construct", "crgo_drivetow", "crgo_drybulk", "crgo_farmsupp",
        "crgo_garbage", "crgo_genfreight", "crgo_grainfeed", "crgo_household", "crgo_intermodal",
        "crgo_liqgas", "crgo_livestock", "crgo_logpole", "crgo_machlrg", "crgo_meat",
        "crgo_metalsheet", "crgo_mobilehome", "crgo_motoveh", "crgo_oilfield", "crgo_paperprod",
        "crgo_passengers", "crgo_produce", "crgo_usmail", "crgo_utility", "crgo_waterwell",
        "dba_name", "docket1", "docket1_status_code", "docket1prefix", "docket2",
        "docket2_status_code", "docket2prefix", "docket3", "docket3_status_code", "docket3prefix",
        "dot_number", "driver_inter_total", "dun_bradstreet_no", "email_address", "fax",
        "fleetsize", "hm_ind", "interstate_beyond_100_miles", "interstate_within_100_miles",
        "intrastate_beyond_100_miles", "intrastate_within_100_miles", "legal_name", "mail_barrio",
        "mail_nationality_indicator", "mcs150_date", "mcs150_mileage", "mcs150_mileage_year",
        "mcs150_update_code_id", "mcs151_mileage", "mcsipdate", "mcsipstep", "ownbus_16",
        "owncoach", "ownlimo_16", "ownlimo_1_8", "ownlimo_9_15", "ownschool_16", "ownschool_1_8",
        "ownschool_9_15", "owntract", "owntrail", "owntruck", "ownvan_1_8", "ownvan_9_15",
        "phone", "phy_barrio", "phy_city", "phy_cnty", "phy_country", "phy_nationality_indicator",
        "phy_omc_region", "phy_state", "phy_street", "phy_zip", "pointnum", "power_units",
        "prior_revoke_dot_number", "prior_revoke_flag", "recordable_crash_rate", "review_date",
        "review_id", "review_type", "safety_inv_terr", "safety_rating", "safety_rating_date",
        "status_code", "total_cars", "total_cdl", "total_drivers", "total_intrastate_drivers",
        "trmbus_16", "trmcoach", "trmlimo_16", "trmlimo_1_8", "trmlimo_9_15", "trmschool_16",
        "trmschool_1_8", "trmschool_9_15", "trmtract", "trmtrail", "trmtruck", "trmvan_1_8",
        "trmvan_9_15", "trpbus_16", "trpcoach", "trplimo_16", "trplimo_1_8", "trplimo_9_15",
        "trpschool_16", "trpschool_1_8", "trpschool_9_15", "trptract", "trptrail", "trptruck",
        "trpvan_1_8", "trpvan_9_15", "truck_units", "undeliv_phy",
    }
)  # fmt: skip
"""Every column of ``az4n-8mr2``, from ``/api/views/az4n-8mr2.json`` (read 2026-10-10). A manifest
predicate on one of these is applied in the census query; on anything else it is left for T7."""

_JOIN_CHUNK = 100
"""USDOT numbers per revocations query — keeps the ``IN (...)`` list well inside a URL's length."""

_QCMOBILE_MIN_INTERVAL_SECONDS = 0.2
"""QCMobile publishes no rate limit, so stay at five a second: 150 lookups take about 30 s."""
_QCMOBILE_MAX_ATTEMPTS = 3

_UNAUTHORIZED = 401
_FORBIDDEN = 403
_NOT_FOUND = 404


def _census_row_url(dot: str) -> str:
    return f"https://data.transportation.gov/resource/az4n-8mr2.json?dot_number={dot}"


def _revocations_url(dot: str) -> str:
    return f"https://data.transportation.gov/resource/wb4f-neki.json?usdot_number={dot}"


def _carrier_url(dot: str) -> str:
    """Where a QCMobile answer is cited — the carrier, never the request (which carries the key)."""
    return f"{QCMOBILE_BASE}/carriers/{dot}"


def _cited[T](value: T, url: str, at: datetime, method: RetrievalMethod) -> ProvenancedValue[T]:
    return ProvenancedValue(value=value, source_url=url, retrieved_at=at, retrieval_method=method)


def _text(row: Mapping[str, str], key: str) -> str | None:
    """A census value, stripped, or ``None`` when the row omits it or states it blank."""
    raw = row.get(key)
    if raw is None:
        return None
    stripped = raw.strip()
    return stripped or None


def _yyyymmdd(raw: str | None) -> date | None:
    """A census ``YYYYMMDD`` date, or ``None`` when absent or unreadable — never a guess."""
    if raw is None:
        return None
    digits = raw.strip()[:8]
    try:
        return datetime.strptime(digits, "%Y%m%d").date()
    except ValueError:
        return None


def census_source_row(row: Mapping[str, str], *, dot: str) -> SourceRow:
    """The whole census row as a ``SourceRow``, its integer columns cast.

    A count that is not a number (``"N/A"``) is left out, not zeroed: absent is what the census
    actually told us, and a zero would pass ``power_units > 10`` as if it were known.
    """
    values: list[tuple[str, SourceValue]] = []
    uncastable: list[str] = []
    for key, raw in row.items():
        if key in CENSUS_INTEGER_COLUMNS:
            try:
                values.append((key, int(raw.strip())))
            except ValueError:
                uncastable.append(key)
            continue
        values.append((key, raw))
    if uncastable:
        logger.warning("sourcing.census.value_uncastable", dot_number=dot, keys=uncastable)
    return SourceRow(values=tuple(values))


def census_pool_record(
    row: Mapping[str, str], *, source: str, retrieved_at: datetime
) -> PoolRecord | None:
    """One census row as a pool record, every field cited to that row. ``None`` without a USDOT."""
    dot = _text(row, "dot_number")
    if dot is None:
        return None
    url = _census_row_url(dot)

    def cite[T](value: T) -> ProvenancedValue[T]:
        return _cited(value, url, retrieved_at, RetrievalMethod.bulk_file)

    legal_name = _text(row, "legal_name")
    dba_name = _text(row, "dba_name")
    phone = _text(row, "phone")
    street, city = _text(row, "phy_street"), _text(row, "phy_city")
    state, postal = _text(row, "phy_state"), _text(row, "phy_zip")
    address = (
        PostalAddress(street=street, city=city, state=state, postal_code=postal)
        if street and city and state and postal
        else None
    )
    fields = CandidateFields(
        registry_id=cite(f"{NAMESPACE}:{dot}"),
        legal_name=cite(legal_name) if legal_name else None,
        dba_name=cite(dba_name) if dba_name else None,
        address=cite(address) if address else None,
        phone=cite(phone) if phone else None,
        source_records=(
            cite(SourceRecord(source=source, rows=(census_source_row(row, dot=dot),))),
        ),
    )
    return PoolRecord(
        registry_id=fields.registry_id.value,
        native_id=dot,
        fields=fields,
        currency_date=_yyyymmdd(row.get("mcs150_date")),
        has_principal=_text(row, "company_officer_1") is not None,
    )


class CensusSource:
    """The pool: the census rows that survive the manifest's census predicates."""

    def __init__(self, name: str, env: SourceEnv) -> None:
        self._name = name
        self._env = env
        self._soda = SodaClient(
            env.client,
            source=name,
            app_token=env.settings.socrata_app_token,
            backoff_seconds=env.backoff_seconds,
        )

    @property
    def name(self) -> str:
        return self._name

    async def pull(self, rules: Sequence[DisqualifierRule]) -> DiscoveryPull:
        """Query the census with the pushed-down rules; dedupe and sort what comes back."""
        pushdown = pushdown_where(
            rules, columns=CENSUS_COLUMNS, integer_columns=CENSUS_INTEGER_COLUMNS
        )
        retrieved_at = self._env.clock()
        rows = await self._soda.query(CENSUS_URL, where=pushdown.where, order="dot_number")

        records: dict[str, PoolRecord] = {}
        skipped = 0
        for row in rows:
            record = census_pool_record(row, source=self._name, retrieved_at=retrieved_at)
            if record is None:
                skipped += 1
                continue
            if record.registry_id in records:
                logger.warning("sourcing.census.duplicate_dropped", registry_id=record.registry_id)
            records[record.registry_id] = record

        logger.info(
            "sourcing.census.pull_completed",
            rows=len(rows),
            records=len(records),
            skipped_without_dot=skipped,
            pushed_down=list(pushdown.pushed_down),
        )
        return DiscoveryPull(
            records=tuple(records[key] for key in sorted(records)),
            pushed_down=pushdown.pushed_down,
            not_pushed_down=pushdown.not_pushed_down,
        )


def _scalars(carrier: Mapping[object, object]) -> SourceRow:
    """A QCMobile carrier's ``str`` and ``int`` values, unchanged. Booleans, nulls and nested
    objects are skipped by key and logged — converting them would be inventing a spelling."""
    values: list[tuple[str, SourceValue]] = []
    skipped: list[str] = []
    for key, value in carrier.items():
        if not isinstance(key, str):
            continue
        if isinstance(value, str) or (isinstance(value, int) and not isinstance(value, bool)):
            values.append((key, value))
        elif value is not None:
            skipped.append(key)
    if skipped:
        logger.info("sourcing.qcmobile.values_skipped", keys=sorted(skipped))
    return SourceRow(values=tuple(values))


def _as_mapping(value: object) -> Mapping[object, object] | None:
    """``value`` as a mapping when it is a dict. Narrowing an ``object`` to ``dict`` leaves its key
    and value types unknown to Pyright strict; the cast states what is true of any dict, the way
    ``shared/provenance.py`` does for tuples."""
    return cast(Mapping[object, object], value) if isinstance(value, dict) else None


def _as_list(value: object) -> list[object] | None:
    """``value`` as a list of objects when it is a list (same reasoning as :func:`_as_mapping`)."""
    return cast(list[object], value) if isinstance(value, list) else None


class QcMobileSource:
    """One QCMobile ``/carriers/{dot}`` call per batch candidate: ``allowToOperate`` and friends."""

    def __init__(self, name: str, env: SourceEnv) -> None:
        self._name = name
        self._env = env
        self._webkey = (env.settings.fmcsa_webkey or "").strip()
        self._lock = asyncio.Lock()
        self._last_call_at: float | None = None

    @property
    def name(self) -> str:
        return self._name

    async def _pace(self) -> None:
        if not self._env.throttle:
            return
        if self._last_call_at is not None:
            elapsed = time.monotonic() - self._last_call_at
            if elapsed < _QCMOBILE_MIN_INTERVAL_SECONDS:
                await asyncio.sleep(_QCMOBILE_MIN_INTERVAL_SECONDS - elapsed)
        self._last_call_at = time.monotonic()

    async def lookup(self, record: PoolRecord) -> ProvenancedValue[SourceRecord] | None:
        """QCMobile's carrier record, ``rows=()`` when it does not know the USDOT, ``None`` on
        failure. Raises :class:`SourceAuthError` when the key is refused — no later call will do
        better, so the caller stops asking."""
        dot = record.native_id
        url = _carrier_url(dot)
        async with self._lock:
            await self._pace()
            try:
                response = await http.get(
                    self._env.client,
                    url,
                    source=self._name,
                    params={"webKey": self._webkey},
                    allow_status=frozenset({_UNAUTHORIZED, _FORBIDDEN, _NOT_FOUND}),
                    max_attempts=_QCMOBILE_MAX_ATTEMPTS,
                    backoff_seconds=self._env.backoff_seconds,
                )
            except SourceRequestError as exc:
                logger.warning(
                    "sourcing.qcmobile.lookup_failed",
                    registry_id=record.registry_id,
                    status=exc.status,
                )
                return None
        retrieved_at = self._env.clock()

        if response.status_code in (_UNAUTHORIZED, _FORBIDDEN):
            raise SourceAuthError(
                f"{self._name} refused the webKey ({response.status_code}) — check FMCSA_WEBKEY",
                source=self._name,
                status=response.status_code,
            )

        try:
            body: object = response.json()
        except ValueError:
            body = None
        envelope = _as_mapping(body)
        content: object = envelope.get("content") if envelope is not None else None
        # A missing key comes back as a 404 whose content is a sentence, not as a 401.
        if isinstance(content, str) and "webkey" in content.lower():
            raise SourceAuthError(
                f"{self._name} says the webKey is missing or invalid — set FMCSA_WEBKEY",
                source=self._name,
                status=response.status_code,
            )

        listed = _as_list(content)
        if listed:
            content = listed[0]
        carrier_holder = _as_mapping(content)
        carrier = _as_mapping(carrier_holder.get("carrier")) if carrier_holder else None

        if carrier is not None:
            rows: tuple[SourceRow, ...] = (_scalars(carrier),)
        elif response.status_code == _NOT_FOUND or content is None or listed == []:
            rows = ()
        else:
            logger.warning(
                "sourcing.qcmobile.shape_unexpected",
                registry_id=record.registry_id,
                status_code=response.status_code,
                content_type=type(content).__name__,
            )
            return None

        logger.info(
            "sourcing.qcmobile.lookup_completed", registry_id=record.registry_id, found=bool(rows)
        )
        return _cited(
            SourceRecord(source=self._name, rows=rows),
            url,
            retrieved_at,
            RetrievalMethod.registry_api,
        )


class RevocationsSource:
    """Motus RevokeSuspend, joined to the batch on the USDOT number."""

    def __init__(self, name: str, env: SourceEnv) -> None:
        self._name = name
        self._env = env
        self._soda = SodaClient(
            env.client,
            source=name,
            app_token=env.settings.socrata_app_token,
            backoff_seconds=env.backoff_seconds,
        )

    @property
    def name(self) -> str:
        return self._name

    async def join(
        self, records: Sequence[PoolRecord]
    ) -> Mapping[str, ProvenancedValue[SourceRecord]]:
        """One record per batch candidate: its revocation and suspension rows, ``()`` for none.

        Joined on ``usdot_number`` with leading zeros stripped, never on the docket: Motus writes
        ``MC-424836`` and the census ``docket1prefix`` + ``docket1``, which do not compare.
        A failed query raises — storing "no revocations" for a chunk nobody read is worse than
        failing the run.
        """
        found: dict[str, set[tuple[tuple[str, SourceValue], ...]]] = {}
        native = [record.native_id for record in records]
        for start in range(0, len(native), _JOIN_CHUNK):
            chunk = native[start : start + _JOIN_CHUNK]
            members = ", ".join(soql_literal(dot) for dot in chunk)
            rows = await self._soda.query(
                REVOCATIONS_URL,
                where=f"usdot_number IN ({members})",
                order="usdot_number,:id",
            )
            for row in rows:
                dot = (row.get("usdot_number") or "").strip().lstrip("0")
                if not dot:
                    continue
                pairs: tuple[tuple[str, SourceValue], ...] = tuple(sorted(row.items()))
                found.setdefault(dot, set()).add(pairs)
        retrieved_at = self._env.clock()

        joined: dict[str, ProvenancedValue[SourceRecord]] = {}
        for record in records:
            key = record.native_id.lstrip("0")
            matched = sorted(found.get(key, set()))
            joined[record.registry_id] = _cited(
                SourceRecord(
                    source=self._name,
                    rows=tuple(SourceRow(values=pairs) for pairs in matched),
                ),
                _revocations_url(record.native_id),
                retrieved_at,
                RetrievalMethod.bulk_file,
            )
        logger.info(
            "sourcing.revocations.join_completed",
            batch=len(records),
            matched=sum(1 for record in joined.values() if record.value.rows),
        )
        return joined


def _needs_webkey(settings: Settings) -> str | None:
    """QCMobile cannot run without a key; blank counts as unset."""
    return None if (settings.fmcsa_webkey or "").strip() else "FMCSA_WEBKEY is not set"


FMCSA_ADAPTERS: tuple[AdapterFactory, ...] = (
    DiscoveryFactory(matches=dataset_matcher(CENSUS_DATASET), build=CensusSource),
    LookupFactory(
        matches=prefix_matcher(QCMOBILE_PREFIX),
        build=QcMobileSource,
        unavailable=_needs_webkey,
    ),
    JoinFactory(matches=dataset_matcher(REVOCATIONS_DATASET), build=RevocationsSource),
)
"""Every FMCSA source this stage can read, matched on each declared source's ``base_url``."""
