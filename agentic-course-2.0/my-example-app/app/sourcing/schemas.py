"""The sourcing shapes: a brief, a run's outcome and totals, and a candidate with every field cited.

The rule running through :class:`CandidateFields` is the provenance write-gate, at rest. Each field
is a ``ProvenancedValue[T]`` or it is ``None`` — there is no third state holding a value nobody can
cite. ``None`` is what "unprovenanced" means here: the workbench may hold a candidate with absent
fields (T6 fills owner and headcount long after sourcing), and ``is_promotable`` refuses each one
until a citation exists. An uncited raw value would be the exact thing the primitive was built to
make unrepresentable.

**D13: Google Places is a check, never a source.** Its terms forbid storing Places content, so the
only thing a candidate keeps from Places is a place ID: in ``business_check`` on a match, or in
``places_verdict`` beside our own reading of a miss. Every other field refuses a Google Maps
citation, so copying Places content into a candidate fails validation rather than reaching HubSpot.
"""

import re
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Final, Self, cast
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from app.core.cost import BillableKind, RunCost
from app.manifests.schemas import SLUG_PATTERN, IcpBand
from app.shared.provenance import ProvenancedValue, RetrievalMethod
from app.sourcing.website import email_domain


class RunStatus(StrEnum):
    """Where a sourcing run is in its lifecycle."""

    running = "running"
    """Open. Candidates may be written into it; it has no finish time."""

    completed = "completed"
    """Finished normally."""

    degraded = "degraded"
    """Finished, but short of what it set out to do — e.g. the Places circuit breaker tripped (T6).

    Not a failure: the run keeps everything it already verified and says what it dropped.
    """

    failed = "failed"
    """Stopped by an error. Its counts and cost describe how far it got."""


class RunOutcome(StrEnum):
    """The terminal subset of :class:`RunStatus` — what a run may be *finished as*.

    A separate type so that finishing a run "as running" is a type error, not a runtime check.
    """

    completed = RunStatus.completed.value
    degraded = RunStatus.degraded.value
    failed = RunStatus.failed.value


REGISTRY_ID_MAX_LENGTH = 128
"""The ``candidate.registry_id`` column's width, enforced first by :class:`CandidateFields`."""

StageName = Annotated[str, Field(pattern=SLUG_PATTERN, max_length=64)]
"""A count's key — the pipeline stage that produced it, e.g. ``sourced`` or ``disqualified``."""

StageCount = Annotated[int, Field(ge=0)]


class SourcingBrief(BaseModel):
    """What a founder asks for: a vertical, an ICP band and a geography (PRD §6 step 1).

    The brief records what was *asked*. Reconciling its ICP band with the manifest's is the
    pipeline's business (T5/T7), not this shape's.
    """

    model_config = ConfigDict(frozen=True)

    vertical: str = Field(pattern=SLUG_PATTERN, max_length=64)
    geography: str = Field(pattern=SLUG_PATTERN, max_length=64)
    """A slug such as ``dfw``. Deliberately not an enum: hard-coding the one metro would be a branch
    on data, and multi-metro being a non-goal is a product decision, not a type."""

    icp_band: IcpBand


class PostalAddress(BaseModel):
    """A street address, cited as one unit — a source states an address, not four separate facts.

    Frozen because ``ProvenancedValue`` refuses to cite a value that could be edited afterwards.
    """

    model_config = ConfigDict(frozen=True)

    street: str = Field(min_length=1)
    city: str = Field(min_length=1)
    state: str = Field(min_length=1)
    postal_code: str = Field(min_length=1)


class PlaceCheck(BaseModel):
    """What a Google Places check keeps: the place ID, and the census address it checked (D13).

    The terms let a place ID be stored indefinitely and bar storing the content behind it. Its
    citation's ``retrieved_at`` is when the check ran. "Verified" means Places found this business
    from the registry's name and address; it is not a claim that the two addresses match.
    """

    model_config = ConfigDict(frozen=True)

    place_id: str = Field(min_length=1)

    checked_address: PostalAddress
    """The census address the check was run against: **our** registry data, not Places content,
    so storing it is allowed. It is what makes a later address change un-verify the check
    (``CandidateFields.has_verified_address`` compares values; PR #13 review L2)."""

    @field_validator("place_id")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("place_id must not be blank")
        return value.strip()


class MissKind(StrEnum):
    """What a Places miss at the census address says about that address (website-verification OQ-2).

    Our reading of the result, never Google's words: the kind is ours, so storing it is allowed.
    """

    mail_drop = "mail_drop"
    """A post office, business centre or shipping store sits at the address: a door that isn't
    there. Kept off the routes (website-verification OQ-3)."""

    no_listing = "no_listing"
    """Only the address itself is listed: nothing is listed at the address."""

    other_business = "other_business"
    """Another business, sharing no distinguishing word with ours, is listed at the address."""


class PlacesVerdict(BaseModel):
    """**Our** verdict on a Places miss: a kind, the place ID it rests on, and the address checked.

    Recorded only when the check found no match but did find something at the census address
    (website-verification OQ-1). The place ID may be stored indefinitely (D13); the kind is ours;
    the address is our registry's. No Places name, address or type is kept.
    """

    model_config = ConfigDict(frozen=True)

    kind: MissKind
    place_id: str = Field(min_length=1)

    checked_address: PostalAddress
    """The census address the check was run against, as on :class:`PlaceCheck`: a later address
    change makes the verdict stale rather than wrong."""

    @field_validator("place_id")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("place_id must not be blank")
        return value.strip()


_NOT_AFTER_A_LETTER = r"(?<![^\W\d_])"
_NOT_BEFORE_A_LETTER = r"(?![^\W\d_])"
r"""Word edges by *letter*, Unicode-aware: ``[^\W\d_]`` is any letter, accented ones included."""

_ENTITY_DESIGNATOR = re.compile(
    _NOT_AFTER_A_LETTER
    + r"(L\.?L\.?C|L\.?L\.?P|L\.?P|P\.?L\.?L\.?C|INC|INCORPORATED|CORP|CORPORATION|LTD|LIMITED)\.?"
    + _NOT_BEFORE_A_LETTER,
    re.IGNORECASE,
)
"""A legal-entity designator as a whole word. ``LLOYD INCE`` and ``Vincent Corpus`` are people.

Changing this list changes which *stored* owners load, because candidates are validated on read:
widen it only with a check of what is already stored. CO, LC and PC are left out on purpose, since
"Co" is a surname and refusing a real owner costs more than a rare ``ACME CO`` reaching review."""


def has_entity_designator(name: str) -> bool:
    """Whether ``name`` carries a legal-entity designator as a whole word (LLC, INC, CORP…)."""
    return _ENTITY_DESIGNATOR.search(name) is not None


_NAME_STOP_WORDS = frozenset({"THE", "AND", "OF"})
"""Words too common to tell two businesses apart. Nothing vertical: "FREIGHT" or "LOGISTICS" here
would be vertical-specific code. A trade word is defused by :data:`MIN_SHARED_WORDS` instead."""

MIN_SHARED_WORDS = 2
"""A name of up to this many distinguishing words must share all of them; a longer one, at least
this many. So a neighbour in one building that shares only a trade word (``LOGISTICS``) is told
apart **when the census name has two or more distinguishing words** the neighbour lacks one of
(PR #32 H3), and two still survive a storefront shortening a long legal name. **Known limit**
(round-2 M3, the option stands): a name that reduces to one word (``AJ LOGISTICS LLC`` is
``LOGISTICS``) matches any neighbour carrying it, and a longer name sharing two trade words with
a neighbour matches it too.

Shared by the Places matcher and the owner gate's page tie (website-verification OQ-4), which is
why it lives here rather than in the Places adapter, which only ``verify_business`` may import."""

_MIN_TOKEN_LENGTH = 3
_LETTERS = re.compile(r"[^\W\d_]+")


def name_words(name: str) -> frozenset[str]:
    """Distinguishing words: upper-cased letters only, no designators, no stop words, 3+ long."""
    return frozenset(
        token
        for token in _LETTERS.findall(name.upper())
        if len(token) >= _MIN_TOKEN_LENGTH
        and token not in _NAME_STOP_WORDS
        and not has_entity_designator(token)
    )


def shares_name(ours: frozenset[str], theirs: frozenset[str]) -> bool:
    """All of a short name's distinguishing words, or :data:`MIN_SHARED_WORDS` of a longer one.

    Vacuously true when ``ours`` is empty; a caller with a name that may have no distinguishing
    word must refuse that case itself.
    """
    shared = len(ours & theirs)
    return shared >= min(len(ours), MIN_SHARED_WORDS)


class Principal(BaseModel):
    """The owner or principal of a business: the person a HubSpot Contact is created for (T9).

    ``name`` is kept as the source stated it. The census gives one string (``company_officer_1``),
    and splitting it into first and last is a guess, so T9 does it at write time rather than
    storing a split behind a census citation.

    **E18 guard:** a name carrying a legal-entity designator is a company in the person field,
    and is refused. Name equality with ``legal_name`` would be the wrong test: a sole proprietor's
    legal name *is* the owner's name (the counts are in ticket T6).
    """

    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1)
    title: str | None = None

    @field_validator("name")
    @classmethod
    def _a_person_not_a_company(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("name must not be blank")
        if has_entity_designator(stripped):
            raise ValueError(
                f"{stripped!r} carries a legal-entity designator; a company name is not a person "
                "(E18). Leave the owner absent rather than cite a company as one"
            )
        return stripped

    @field_validator("title")
    @classmethod
    def _blank_title_is_absent(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None


def principal_or_none(name: str | None, title: str | None = None) -> Principal | None:
    """A :class:`Principal`, or ``None`` when the name is absent, blank or refused.

    The one test of "names a citable person" (PR #31 L1): the backlog's ``has_principal`` and the
    census adapter both ask it, so neither can drift from what ``Principal`` accepts. It catches the
    refusal itself (PR #31 M2), so a company in the officer column never fails a pull.
    """
    if name is None or not name.strip():
        return None
    try:
        return Principal(name=name, title=title)
    except ValidationError:
        return None


class HeadcountBand(BaseModel):
    """How many people work at a business, as an inclusive range the source stated (M8).

    ``"11-50 employees"`` is ``(11, 50)``; ``"a team of 12"`` is ``(12, 12)``; ``"50+"`` is
    ``(50, None)``. A range rather than an enum of buckets, because sources state ranges and
    numbers; bucketing is T9's HubSpot-mapping concern. Frozen, because it is cited.
    """

    model_config = ConfigDict(frozen=True)

    low: int = Field(ge=1)
    high: int | None = Field(default=None, ge=1)
    """``None`` is open-ended (``50+``)."""

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.high is not None and self.high < self.low:
            raise ValueError(f"headcount band {self.low}-{self.high} is inverted")
        return self

    @property
    def label(self) -> str:
        """``"11-50"``, ``"50+"`` or ``"12"``."""
        if self.high is None:
            return f"{self.low}+"
        if self.high == self.low:
            return str(self.low)
        return f"{self.low}-{self.high}"


class ScoreBand(StrEnum):
    """Where a priority score falls (E9: >=16 live, <9 dead)."""

    live = "live"
    middle = "middle"
    dead = "dead"


LIVE_SCORE_MIN = 16
DEAD_SCORE_BELOW = 9


class PriorityScore(BaseModel):
    """Intensity (1-5) x Automatable (1-5) = 1-25 (E9), as the ``classify_rollup`` node scored it.

    Lives here, not in ``qualification/``, because ``CandidateFields`` holds it and sourcing must
    not import the slice that reads it. Frozen, because it is cited.
    """

    model_config = ConfigDict(frozen=True)

    intensity: int = Field(ge=1, le=5)
    automatable: int = Field(ge=1, le=5)

    @property
    def score(self) -> int:
        return self.intensity * self.automatable

    @property
    def band(self) -> ScoreBand:
        if self.score >= LIVE_SCORE_MIN:
            return ScoreBand.live
        if self.score < DEAD_SCORE_BELOW:
            return ScoreBand.dead
        return ScoreBand.middle


type SourceValue = str | int
"""One value as a source stated it: its text, or an integer the adapter cast (census numerics)."""


class SourceRow(BaseModel):
    """One record a source returned — a census row, a QCMobile carrier, a revocation — as pairs.

    Pairs rather than a dict because a citation may only hold immutable values, and **sorted by
    key** so the same record always serialises the same way: same input, same stored bytes.
    """

    model_config = ConfigDict(frozen=True)

    values: tuple[tuple[str, SourceValue], ...]

    @field_validator("values")
    @classmethod
    def _sorted_and_unique(
        cls, value: tuple[tuple[str, SourceValue], ...]
    ) -> tuple[tuple[str, SourceValue], ...]:
        keys = [key for key, _ in value]
        if any(not key.strip() for key in keys):
            raise ValueError("a source row key must not be blank")
        duplicates = sorted({key for key in keys if keys.count(key) > 1})
        if duplicates:
            raise ValueError(f"duplicate source row keys: {duplicates}")
        return tuple(sorted(value, key=lambda pair: pair[0]))

    def get(self, key: str) -> SourceValue | None:
        """The value stated for ``key``, or ``None`` when the source did not state one."""
        for name, value in self.values:
            if name == key:
                return value
        return None


class SourceRecord(BaseModel):
    """What one declared source said about one candidate, as zero or more rows.

    ``source`` is the **manifest's** source name, so a rule's field can be traced to the source it
    came from (T7). ``rows == ()`` means *looked, found nothing* — no revocations, an unknown USDOT
    — which is a different fact from a record being absent altogether (never looked, or the lookup
    failed). An evaluator must not read the second as the first.
    """

    model_config = ConfigDict(frozen=True)

    source: str = Field(pattern=SLUG_PATTERN, max_length=64)
    rows: tuple[SourceRow, ...]


_PLACES_ARTEFACTS: Final = frozenset({"business_check", "places_verdict"})
"""The only ``CandidateFields`` members allowed to cite Google Maps: each holds a place ID and
nothing of Places' content (D13)."""

_GOOGLE_MAPS_HOSTS = frozenset(
    {"places.googleapis.com", "maps.googleapis.com", "maps.google.com", "maps.app.goo.gl"}
)


def is_google_maps(url: str) -> bool:
    """Whether ``url`` points at Google Maps Platform content.

    Google *search* is not Maps: a website found by a web search may well be cited to google.com.
    Only Maps hosts, and the ``/maps`` paths of google.com and goo.gl, count.

    A tripwire for our own adapters, so it is lenient about form: a URL without a scheme is parsed
    as a host, and the host's case, port, userinfo and trailing dot are ignored. It is a deny-list
    of known Maps hosts, so a Maps hostname not on it would pass.
    """
    stripped = url.strip()
    parts = urlsplit(stripped if "//" in stripped else f"//{stripped}")
    host = (parts.hostname or "").rstrip(".")
    path = parts.path.lower()
    if host in _GOOGLE_MAPS_HOSTS:
        return True
    is_google = host == "google.com" or host.endswith(".google.com")
    return (is_google or host == "goo.gl") and (path == "/maps" or path.startswith("/maps/"))


class PlacesEvidence(BaseModel):
    """What ``verify_business``'s Places check saw, handed to the judgment node within the run.

    Never a ``CandidateFields`` member, never stored, never logged (D13): the Maps Platform Terms
    bar storing it. Built by ``verify_business``, read by ``classify_rollup`` in the same run
    through ``StageContext.places``, and dropped with the run. Context for the model, never a
    citation.
    """

    model_config = ConfigDict(frozen=True)

    display_name: str | None = None
    business_status: str | None = None
    types: tuple[str, ...] = ()


class CandidateFields(BaseModel):
    """A sourced business's identity, every field carrying its own provenance.

    Stored whole as the ``candidate.fields`` JSONB. Only ``registry_id`` is required: a candidate
    exists *because* a registry named it, so its id is the one field that cannot be absent. Every
    other field may be ``None`` — storable, and not promotable (see the module docstring).

    Because the column is JSONB, adding a field is a schema change with no migration, and rows
    written before it existed load with it absent — which is honestly what they are.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    registry_id: ProvenancedValue[str]
    """The authoritative registry's own identifier for this business (e.g. a USDOT number)."""

    legal_name: ProvenancedValue[str] | None = None
    dba_name: ProvenancedValue[str] | None = None
    address: ProvenancedValue[PostalAddress] | None = None
    phone: ProvenancedValue[str] | None = None
    email: ProvenancedValue[str] | None = None
    """The registry's stated contact email (T6), cited ``bulk_file`` to the census row, lowercased.
    Filled by the registry adapter; ``verify_business`` reads its domain for the website check.
    Whether it reaches HubSpot is T9's call."""

    website: ProvenancedValue[str] | None = None
    """The business's own homepage origin, from ``verify_business``'s free fetch of the email
    domain, cited ``web_lookup`` to the page it fetched."""

    business_check: ProvenancedValue[PlaceCheck] | None = None
    """The Google Places check of this record (T6): the place ID, cited to Places. Absent means
    never checked, or checked and not found."""

    places_verdict: ProvenancedValue[PlacesVerdict] | None = None
    """What a Places miss told us (website-verification OQ-1): our kind and the place ID it rests
    on, cited to that place. Recorded only on a miss with something at the census address; absent
    when the check matched, was never run, or found nothing there."""

    owner: ProvenancedValue[Principal] | None = None
    """The owner or principal (T6, ``resolve_owner``). The census ``company_officer_1`` first, cited
    ``bulk_file``; the judgment node only where that is absent, cited ``llm_inference`` to the page
    it read. Absent means nobody could be cited, and T9 does not promote such a candidate: the
    cadence is keyed on a contact."""

    headcount_band: ProvenancedValue[HeadcountBand] | None = None
    """How many people work there (M8), as a range a page stated, owned by ``classify_rollup``
    (its one call answers it, cited to a page it fetched). Absent when nothing citable states it,
    which is common for small businesses and honest: a guessed band is E18."""

    source_records: tuple[ProvenancedValue[SourceRecord], ...] = ()
    """Every source's raw record for this candidate, each with its own citation (T5).

    The census row, the QCMobile carrier, the revocation rows. This is where T7's evaluator finds a
    rule's field, and where T6 finds ``company_officer_1`` and the email domain. One record per
    source. The typed fields above are what a person reads; these are what the rules read.
    """

    @field_validator("source_records")
    @classmethod
    def _one_record_per_source(
        cls, value: tuple[ProvenancedValue[SourceRecord], ...]
    ) -> tuple[ProvenancedValue[SourceRecord], ...]:
        """Two records for one source would make "what did fmcsa_qcmobile say?" ambiguous."""
        names = [record.value.source for record in value]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"more than one source record for: {duplicates}")
        return value

    priority: ProvenancedValue[PriorityScore] | None = None
    """The priority score (T7), cited ``llm_inference`` to a page the judgment node read. Absent
    when either axis had no cited signal answer: an absent score is honest, a guessed one is E18.
    """

    @field_validator("registry_id")
    @classmethod
    def _normalise_registry_id(cls, value: ProvenancedValue[str]) -> ProvenancedValue[str]:
        """Strip the id, and refuse one that is blank or longer than its column.

        It is half the upsert key, so ``" 555 "`` and ``"555"`` must be the same key or one carrier
        becomes two rows. A blank id would collapse every blank record into one row. And an id the
        ``String(128)`` column cannot hold must fail *here*, as a ``ValidationError`` before any
        write, not as a ``DBAPIError`` that aborts the rest of an atomic batch.
        """
        stripped = value.value.strip()
        if not stripped:
            raise ValueError("registry_id must not be blank")
        if len(stripped) > REGISTRY_ID_MAX_LENGTH:
            raise ValueError(f"registry_id must be at most {REGISTRY_ID_MAX_LENGTH} characters")
        if stripped == value.value:
            return value
        # The citation is unchanged — the same source said the same thing, minus its padding.
        return value.model_copy(update={"value": stripped})

    @model_validator(mode="after")
    def _no_places_content(self) -> Self:
        """D13: only ``business_check`` and ``places_verdict`` may cite Google Maps; any other
        field doing so is copied Places content, which the Maps Platform Terms §3.2.3(a) forbid us
        to store."""
        for name in type(self).model_fields:
            if name in _PLACES_ARTEFACTS:
                continue
            for cited in self._citations(name):
                if is_google_maps(cited.source_url):
                    raise ValueError(
                        f"{name} is cited to Google Maps ({cited.source_url}); Places content "
                        "cannot be stored (D13). Keep only the place ID, in business_check or "
                        "places_verdict"
                    )
        return self

    def _citations(self, name: str) -> tuple[ProvenancedValue[object], ...]:
        """Every citation a field holds: none, one, or one per element of a tuple field."""
        held: object = getattr(self, name)
        if isinstance(held, ProvenancedValue):
            return (cast(ProvenancedValue[object], held),)
        if isinstance(held, tuple):
            return tuple(
                cast(ProvenancedValue[object], item)
                for item in cast(tuple[object, ...], held)
                if isinstance(item, ProvenancedValue)
            )
        return ()

    def to_stored(self) -> dict[str, object]:
        """The JSONB form: absent fields are absent keys — ``None`` *and* an empty tuple.

        The candidate upsert merges by key, so a key present means "this write states this field".
        ``exclude_none`` drops ``None``, but an empty ``source_records`` would still be written as
        ``[]`` — a key every stage's write carries, stating nothing.
        """
        dumped = self.model_dump(mode="json", exclude_none=True)
        return {name: value for name, value in dumped.items() if value != []}

    def has_verified_address(self) -> bool:
        """Whether this candidate has an address **and** a business check of *that* address.

        The rule T8 clusters on: a candidate without one is excluded, never geocoded from a guess.
        The address is the registry's copy either way (D13); verification is the separate fact
        that Places found the business there. The check's ``checked_address`` must equal the
        current address — values, never ``retrieved_at``, which every registry refresh bumps
        (PR #13 review L2) — so a moved business is unverified until it is checked again.
        """
        return (
            self.address is not None
            and self.business_check is not None
            and self.business_check.value.checked_address == self.address.value
        )

    def has_own_domain_website(self) -> bool:
        """Whether the website is the homepage ``verify_business`` fetched on the email's domain.

        Website-verification B2: cited ``web_lookup`` (the free fetch, not the ``llm_inference``
        homepage ``classify_rollup`` may fill, which would make verification circular), and its host
        without ``www.`` equal to the email's domain. Values are compared, never a stage name. A
        free-mail email has no domain, so it never verifies.
        """
        if self.website is None or self.email is None:
            return False
        if self.website.retrieval_method is not RetrievalMethod.web_lookup:
            return False
        domain = email_domain(self.email.value)
        if domain is None:
            return False
        host = (urlsplit(self.website.value).hostname or "").removeprefix("www.")
        return host == domain

    def is_verified(self) -> bool:
        """Places found the business at its current address, or it answers on its own domain.

        Website-verification B1: the one test every paid-step skip and T9's gate use.
        """
        return self.has_verified_address() or self.has_own_domain_website()

    def has_current_verdict(self) -> bool:
        """Whether a Places verdict exists for the *current* address (website-verification B4)."""
        return (
            self.address is not None
            and self.places_verdict is not None
            and self.places_verdict.value.checked_address == self.address.value
        )

    def has_mail_drop_here(self) -> bool:
        """Whether the current address's verdict is a mail drop: a door that isn't there."""
        return (
            self.has_current_verdict()
            and self.places_verdict is not None
            and self.places_verdict.value.kind is MissKind.mail_drop
        )

    def has_contradicting_verdict(self) -> bool:
        """Whether the current address's verdict says the business is not at that door.

        A mail drop or another business, recorded for *this* address: positive evidence against it,
        unlike ``no_listing`` or no verdict at all, which are only an absence of evidence. T9's
        gate refuses an *unverified* candidate on this (founder's decision, 2026-10-16); a verified
        one is exempt, since a website verifies the business, not the door (website-verification
        OQ-3).
        """
        return (
            self.has_current_verdict()
            and self.places_verdict is not None
            and self.places_verdict.value.kind in {MissKind.mail_drop, MissKind.other_business}
        )

    def has_routable_address(self) -> bool:
        """Whether T8 may put this candidate on a route (website-verification OQ-3).

        Places-verified at this address, or website-verified with no mail-drop verdict for it. A
        website verifies the business, not the door, so a known mail drop stays off the routes;
        ``no_listing`` and ``other_business`` stay routable.
        """
        if self.has_verified_address():
            return True
        return (
            self.address is not None
            and self.has_own_domain_website()
            and not self.has_mail_drop_here()
        )

    def unprovenanced_fields(self) -> tuple[str, ...]:
        """The fields with no citation, in declaration order.

        What T9's gate names when it refuses a write. Which of these are *required* for promotion is
        T9's decision, not this model's. ``source_records`` is never listed: a tuple is never
        ``None``, and it is evidence for the rules rather than a field a person reads.
        """
        return tuple(name for name in type(self).model_fields if getattr(self, name) is None)


class RunCostSummary(BaseModel):
    """What a run spent, per billable kind — the persisted form of a :class:`RunCost`.

    USD stays ``Decimal`` end to end; in JSONB it is a string, which is what keeps it exact.
    """

    calls: dict[BillableKind, int] = Field(default_factory=dict[BillableKind, int])
    usd: dict[BillableKind, Decimal] = Field(default_factory=dict[BillableKind, Decimal])

    @classmethod
    def from_run_cost(cls, cost: RunCost) -> Self:
        """Snapshot a run's accumulator. Every kind is present, so the stored shape is stable."""
        return cls(
            calls={kind: cost.total_calls(kind) for kind in BillableKind},
            usd={kind: cost.total_usd(kind) for kind in BillableKind},
        )

    def total_usd(self) -> Decimal:
        """Dollars across every kind. Computed, not stored — one fact, one home."""
        return sum(self.usd.values(), Decimal("0"))


class RunTotals(BaseModel):
    """What ``finish_run`` writes: validated stage counts and the cost snapshot."""

    counts: dict[StageName, StageCount]
    cost: RunCostSummary


class SourcingRunResponse(BaseModel):
    """One ``sourcing_run`` row."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    manifest_id: UUID
    vertical: str
    geography: str
    icp_band: IcpBand
    status: RunStatus
    status_detail: str | None
    started_at: datetime
    finished_at: datetime | None
    counts: dict[str, int]
    cost: RunCostSummary


class CandidateResponse(BaseModel):
    """One ``candidate`` row, its fields validated back into citations."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    run_id: UUID
    registry_id: str
    fields: CandidateFields
    created_at: datetime
    updated_at: datetime


class PoolEntry(BaseModel):
    """One backlog entry a run took into its batch: its key and the registry's cited fields."""

    model_config = ConfigDict(frozen=True)

    registry_id: str
    fields: CandidateFields


class PoolSelection(BaseModel):
    """What refreshing the backlog and taking a batch came to, for one run (D12)."""

    model_config = ConfigDict(frozen=True)

    seen: int = Field(ge=0)
    """Records the registry returned this run."""

    new: int = Field(ge=0)
    """Of those, entries the pool had never held."""

    available: int = Field(ge=0)
    """Entries this run could take, before it took any."""

    selected: tuple[PoolEntry, ...]
    """The batch, best first."""
