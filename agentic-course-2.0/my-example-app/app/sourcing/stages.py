"""The pipeline's five stages, and the one stage that owns each candidate field.

**One owner per field.** A stage that owns a field may overwrite it: a retry of that stage is that
stage re-deciding its own fact. Any other stage may only *fill* the field while it is empty, and
never replaces a citation already stored. Without this the upsert is last-writer-wins across
stages, so a retried stage could silently swap a better citation for a worse one.

**Owning a field is not verifying it.** Under D13 the registry owns the address and phone, and
Places content is never stored, so verification is a separate field (``business_check``) rather
than a better copy of the address. ``CandidateFields.has_verified_address`` is the rule.

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
    and the website from a web search."""

    resolve_owner = "resolve_owner"
    """Stage 3 (T6): the owner or principal. A judgment node."""

    classify_rollup = "classify_rollup"
    """Stage 4 (T7): rollup or local. A judgment node."""

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
        # The registry's email domain fills the website until a web search replaces it.
        "website": PipelineStage.verify_business,
        # The Places check: the place ID only (D13).
        "business_check": PipelineStage.verify_business,
        # Every source's raw record (T5). The registry stage writes the whole batch's records in one
        # write per candidate, so a retry replaces them whole rather than merging stale ones in.
        "source_records": PipelineStage.search_registry,
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
