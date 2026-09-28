"""Builders shared by the manifests suites — a valid body, cheaply, with one thing changed.

Throwaway vertical names come from :func:`a_vertical`: reusing ``freight`` or ``fire`` would
couple these tests to whatever the seed migration happens to contain.
"""

from datetime import UTC, datetime
from uuid import uuid4

from app.manifests.schemas import (
    DisqualifierRule,
    IcpBand,
    ManifestBody,
    ManifestSource,
    QualifyingSignal,
    RuleKind,
    ScoreAxis,
    SourceKind,
    Vocabulary,
)
from app.shared.provenance import ProvenancedValue, RetrievalMethod

RETRIEVED_AT = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
SOURCE_URL = "https://safer.fmcsa.dot.gov/"


def a_vertical() -> str:
    """A vertical name no other test and no seed row uses."""
    return f"test_{uuid4().hex[:8]}"


def cited[T](value: T, source_url: str = SOURCE_URL) -> ProvenancedValue[T]:
    """Wrap a value in a citation, the way the seeds and the authoring agent both must."""
    return ProvenancedValue(
        value=value,
        source_url=source_url,
        retrieved_at=RETRIEVED_AT,
        retrieval_method=RetrievalMethod.manual_research,
    )


def a_body(*source_names: str) -> ManifestBody:
    """A valid body declaring the named sources (``fmcsa`` when none are given)."""
    names = source_names or ("fmcsa",)
    return ManifestBody(
        sources=tuple(
            cited(
                ManifestSource(
                    name=name,
                    kind=SourceKind.bulk_file,
                    description=f"the {name} source",
                    base_url=SOURCE_URL,
                )
            )
            for name in names
        ),
        disqualifier_rules=(
            cited(
                DisqualifierRule(
                    id="national_rollup",
                    kind=RuleKind.judgment,
                    description="a national rollup with no local owner",
                )
            ),
        ),
        qualifying_signals=(
            cited(QualifyingSignal(id="tms", question="which TMS", feeds=ScoreAxis.automatable)),
        ),
        icp_band=cited(IcpBand(headcount_min=20, headcount_max=200, requires_office_function=True)),
        vocabulary=cited(Vocabulary(terms=("loads", "lanes"))),
    )
