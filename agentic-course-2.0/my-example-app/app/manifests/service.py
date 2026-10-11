"""Manifest lifecycle: the terms-of-use gate, the supersede rule, the transaction boundary.

The service is where the rules live and where the transaction is owned. The repository below it
flushes and never commits, so an operation that touches two rows — superseding the old active
manifest and activating the new one — either happens completely or not at all.
"""

from collections.abc import Mapping
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.manifests.exceptions import (
    ActiveManifestNotFoundError,
    DryRunMismatchError,
    DryRunNotRecordedError,
    ManifestNotDraftError,
    ManifestNotFoundError,
    TermsOfUseNotRecordedError,
    UnknownSourceError,
)
from app.manifests.repository import ManifestRepository
from app.manifests.schemas import (
    DryRunReport,
    DryRunResponse,
    ManifestBody,
    ManifestResponse,
    ManifestStatus,
    SourceTerms,
    TermsDecision,
)

logger = get_logger(__name__)


class ManifestService:
    """The manifests slice's business rules."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repository = ManifestRepository(session)

    async def get(self, manifest_id: UUID) -> ManifestResponse:
        """Return one manifest by id, whatever its status."""
        manifest = await self._repository.get_by_id(manifest_id)
        if manifest is None:
            raise ManifestNotFoundError(
                f"no manifest with id {manifest_id}", manifest_id=str(manifest_id)
            )
        return ManifestResponse.model_validate(manifest)

    async def get_active(self, vertical: str) -> ManifestResponse:
        """Return the ACTIVE manifest for a vertical.

        Raises rather than returning ``None``: a pipeline that silently proceeds without a manifest
        sources against nothing, which looks like an empty registry instead of a missing decision.
        """
        manifest = await self._repository.get_active(vertical)
        if manifest is None:
            raise ActiveManifestNotFoundError(
                f"no active manifest for vertical {vertical!r} — "
                "activate a draft with `lpe manifest activate`",
                vertical=vertical,
            )
        return ManifestResponse.model_validate(manifest)

    async def list(
        self,
        *,
        vertical: str | None = None,
        status: ManifestStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ManifestResponse]:
        """List manifests, newest version first within each vertical."""
        manifests = await self._repository.list(
            vertical=vertical, status=status, limit=limit, offset=offset
        )
        return [ManifestResponse.model_validate(manifest) for manifest in manifests]

    async def create_draft(self, vertical: str, body: ManifestBody) -> ManifestResponse:
        """Create a DRAFT manifest at the next version for this vertical.

        The only write path into the table. T12's authoring agent calls this; nothing here can
        create an ACTIVE row, because ``activate`` is the single door and a human holds the key.
        """
        manifest = await self._repository.create_draft(vertical, body)
        await self._session.commit()
        return ManifestResponse.model_validate(manifest)

    async def record_dry_run(
        self, manifest_id: UUID, report: DryRunReport, actor: str
    ) -> DryRunResponse:
        """Record a dry-run of one manifest version — what ``activate`` then requires.

        Any status may be dry-run; the record matters on a DRAFT, where it unblocks activation.
        """
        if report.manifest_id != manifest_id:
            raise DryRunMismatchError(
                f"the report is for manifest {report.manifest_id}, not {manifest_id}",
                manifest_id=str(manifest_id),
            )
        if await self._repository.get_by_id(manifest_id) is None:
            raise ManifestNotFoundError(
                f"no manifest with id {manifest_id}", manifest_id=str(manifest_id)
            )
        dry_run = await self._repository.record_dry_run(manifest_id, report, actor)
        await self._session.commit()
        return DryRunResponse.model_validate(dry_run)

    async def activate(
        self,
        manifest_id: UUID,
        accept_terms: frozenset[str],
        actor: str,
        licenses: Mapping[str, str] | None = None,
    ) -> ManifestResponse:
        """Record a terms-of-use decision per declared source and flip the draft ACTIVE.

        The order is the gate. Nothing is written until a dry-run has been recorded (T7) and every
        declared source has been named in ``accept_terms``, and the cited content is copied through
        untouched — activation records a human's decision, it does not re-retrieve anything.
        """
        manifest = await self._repository.get_by_id(manifest_id)
        if manifest is None:
            raise ManifestNotFoundError(
                f"no manifest with id {manifest_id}", manifest_id=str(manifest_id)
            )

        logger.info(
            "manifests.service.activation_started",
            manifest_id=str(manifest_id),
            vertical=manifest.vertical,
            version=manifest.version,
        )

        if manifest.status != ManifestStatus.draft.value:
            logger.warning(
                "manifests.service.activation_refused",
                manifest_id=str(manifest_id),
                vertical=manifest.vertical,
                reason="not_draft",
                status=manifest.status,
            )
            raise ManifestNotDraftError(
                f"manifest {manifest_id} is {manifest.status}, and only a draft can be activated",
                manifest_id=str(manifest_id),
                status=manifest.status,
            )

        if await self._repository.latest_dry_run(manifest_id) is None:
            logger.warning(
                "manifests.service.activation_refused",
                manifest_id=str(manifest_id),
                vertical=manifest.vertical,
                reason="dry_run_missing",
            )
            raise DryRunNotRecordedError(
                f"manifest {manifest_id} has never been dry-run; run "
                f"`lpe manifest dry-run {manifest_id} --source-file <source>=<extract.csv>`, read "
                "the funnel and its flags, then activate",
                manifest_id=str(manifest_id),
            )

        body = ManifestBody.model_validate(manifest.body)
        declared = set(body.source_names())

        unknown = tuple(sorted(accept_terms - declared))
        if unknown:
            logger.warning(
                "manifests.service.activation_refused",
                manifest_id=str(manifest_id),
                vertical=manifest.vertical,
                reason="unknown_sources",
                unknown_sources=list(unknown),
            )
            raise UnknownSourceError(
                f"manifest {manifest_id} does not declare source(s): {', '.join(unknown)}",
                vertical=manifest.vertical,
                unknown_sources=unknown,
            )

        missing = tuple(sorted(declared - accept_terms))
        if missing:
            logger.warning(
                "manifests.service.activation_refused",
                manifest_id=str(manifest_id),
                vertical=manifest.vertical,
                reason="terms_undecided",
                missing_sources=list(missing),
            )
            raise TermsOfUseNotRecordedError(
                "cannot activate without a terms-of-use decision for every declared source; "
                f"missing: {', '.join(missing)}",
                vertical=manifest.vertical,
                missing_sources=missing,
            )

        decided_at = datetime.now(UTC)
        accepted_terms = tuple(
            SourceTerms(
                source_name=name,
                decision=TermsDecision.accepted,
                license=None if licenses is None else licenses.get(name),
                decided_by=actor,
                decided_at=decided_at,
            )
            for name in body.source_names()
        )
        # The cited fields are carried through as the very objects that were cited. Rebuilding them
        # would mint fresh citations claiming the sources were retrieved at activation time.
        activated_body = ManifestBody(
            sources=body.sources,
            disqualifier_rules=body.disqualifier_rules,
            qualifying_signals=body.qualifying_signals,
            icp_band=body.icp_band,
            vocabulary=body.vocabulary,
            terms=accepted_terms,
        )

        # Supersede first, then activate, in one transaction: the partial unique index refuses a
        # second active row, and two commits would leave a window with no active manifest at all.
        previous = await self._repository.get_active(manifest.vertical)
        superseded_version = None
        if previous is not None and previous.id != manifest.id:
            await self._repository.mark_superseded(previous)
            superseded_version = previous.version

        await self._repository.mark_active(manifest, activated_body, actor)
        await self._session.commit()

        logger.info(
            "manifests.service.activation_succeeded",
            manifest_id=str(manifest_id),
            vertical=manifest.vertical,
            version=manifest.version,
            superseded_version=superseded_version,
            sources=list(body.source_names()),
        )
        return ManifestResponse.model_validate(manifest)


def parse_accept_terms(raw: str | None) -> frozenset[str]:
    """Split a ``--accept-terms a,b`` argument into source names.

    Trailing commas, stray whitespace and duplicates are all ordinary typing, not errors: an empty
    segment carries no meaning to lose. A name the manifest does not declare *is* an error, and it
    is caught in :meth:`ManifestService.activate` where the declared set is known.
    """
    if raw is None:
        return frozenset()
    return frozenset(segment.strip() for segment in raw.split(",") if segment.strip())
