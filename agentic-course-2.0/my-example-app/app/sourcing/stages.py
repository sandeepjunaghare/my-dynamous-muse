"""The pipeline's five stages, and the one stage that owns each candidate field.

**One owner per field.** A stage that owns a field may overwrite it: a retry of that stage is that
stage re-deciding its own fact. Any other stage may only *fill* the field while it is empty, and
never replaces a citation already stored. Without this the upsert is last-writer-wins across
stages, so a retried stage could silently swap a better citation for a worse one.

**Owning a field is not verifying it.** Under D13 the registry owns the address and phone, and
Places content is never stored, so verification is a separate field (``business_check``) rather
than a better copy of the address. ``CandidateFields.has_verified_address`` is the rule.

**The in-batch order is free, then Places, then one judgment call, then the owner** (D8, revised
2026-10-09; the architecture's in-batch list). ``classify_rollup`` runs before ``resolve_owner``, so
the owner node never pays for a candidate the judgment rules remove, and ``verify_business`` stays
second so the Places evidence reaches the judgment call within the run.

Ownership is pipeline structure, not vertical data. The stages are the same five for every vertical
(architecture → *Five generic stages, not one per source*), so this table lives here in code and not
in a manifest. It is a table rather than branching logic, so a field's owner is read rather than
worked out. Adding a field to ``CandidateFields`` means adding its row here as well, and
``tests/sourcing/test_stages.py`` fails until you do.
"""

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Final


class PipelineStage(StrEnum):
    """The five generic stages, named as in the architecture doc, in pipeline order."""

    search_registry = "search_registry"
    """Stage 1 (T5): the manifest's declared registry, e.g. the FMCSA census file, then QCMobile."""

    verify_business = "verify_business"
    """Stage 2 (T6): a Google Places check of the registry record (D13: a check, never a source),
    and the website from a free fetch of the census email domain."""

    classify_rollup = "classify_rollup"
    """Stage 3 (T7): rollup or local. A judgment node."""

    resolve_owner = "resolve_owner"
    """Stage 4 (T6): the owner or principal. The census officer is filled by the registry stage; a
    judgment node runs only where the census names nobody *citable* (an officer ``Principal``
    refuses counts as absent)."""

    cluster_routes = "cluster_routes"
    """Stage 5 (T8): DFW route clustering. Reads fields and owns none."""


FIELD_OWNERS: Final[Mapping[str, PipelineStage]] = MappingProxyType(
    {
        # The registry is the authority on who a business legally is, and (D13) on how to reach
        # it: the census address and phone are what we may keep, and nothing replaces them.
        "registry_id": PipelineStage.search_registry,
        "legal_name": PipelineStage.search_registry,
        "dba_name": PipelineStage.search_registry,
        "address": PipelineStage.search_registry,
        "phone": PipelineStage.search_registry,
        # The registry's stated contact email (T6): an input to the website check.
        "email": PipelineStage.search_registry,
        # The homepage, fetched from the email domain by the verification stage.
        "website": PipelineStage.verify_business,
        # The Places check: the place ID only (D13).
        "business_check": PipelineStage.verify_business,
        # Our verdict on a Places miss: a kind and the place ID only (D13).
        "places_verdict": PipelineStage.verify_business,
        # The owner (T6): the census officer is filled by the registry stage (it may only fill);
        # the judgment node only where that is absent, and a retry of it re-decides its own answer.
        "owner": PipelineStage.resolve_owner,
        # The headcount band (T6, OQ-3): answered by the judgment node's one call, cited.
        "headcount_band": PipelineStage.classify_rollup,
        # Every source's raw record (T5). The registry stage writes the whole batch's records in one
        # write per candidate, so a retry replaces them whole rather than merging stale ones in.
        "source_records": PipelineStage.search_registry,
        # The judgment node's score, from cited qualifying-signal answers (T7).
        "priority": PipelineStage.classify_rollup,
    }
)
"""``CandidateFields`` field name → the one stage allowed to overwrite it."""


def owns(stage: PipelineStage, field: str) -> bool:
    """Whether ``stage`` may overwrite ``field``.

    A field with no row has no owner, so no stage may overwrite it; every stage can still fill it
    while it is empty. That is the conservative answer, and the coverage test stops it from coming
    up in practice.
    """
    return FIELD_OWNERS.get(field) is stage
