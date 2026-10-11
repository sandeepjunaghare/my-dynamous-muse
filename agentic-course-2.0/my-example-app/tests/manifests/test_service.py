"""The lifecycle rules: the terms-of-use gate, the supersede rule, and what activation must not do.

This is the suite that guards the ticket's reason for existing — **a source with no recorded
terms-of-use decision cannot be marked active** — and the quieter property underneath it: activating
records a human's decision and must leave every citation byte-identical, because a citation is a
claim about when a value was *retrieved*.
"""

from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.manifests.exceptions import (
    ActiveManifestNotFoundError,
    DryRunMismatchError,
    DryRunNotRecordedError,
    ManifestNotDraftError,
    ManifestNotFoundError,
    TermsOfUseNotRecordedError,
    UnknownSourceError,
)
from app.manifests.schemas import ManifestStatus, TermsDecision
from app.manifests.service import ManifestService, parse_accept_terms
from tests.conftest import requires_db
from tests.manifests.builders import a_body, a_dry_run, a_vertical, dry_run_recorded


@requires_db
class TestTheTermsGate:
    async def test_activation_is_refused_while_a_source_is_undecided(
        self, db_session: AsyncSession
    ) -> None:
        """The ticket's named test — and the refusal has to say *which* source."""
        service = ManifestService(db_session)
        vertical = a_vertical()
        draft = await service.create_draft(vertical, a_body("fmcsa", "places"))
        await dry_run_recorded(db_session, draft.id)

        with pytest.raises(TermsOfUseNotRecordedError) as exc_info:
            await service.activate(draft.id, frozenset({"fmcsa"}), "sandeep")

        assert exc_info.value.missing_sources == ("places",)
        assert "places" in exc_info.value.message

        unchanged = await service.get(draft.id)
        assert unchanged.status is ManifestStatus.draft

    async def test_an_unknown_source_is_refused(self, db_session: AsyncSession) -> None:
        """Accepting terms for a source that is not declared is a typo, not a decision."""
        service = ManifestService(db_session)
        draft = await service.create_draft(a_vertical(), a_body("fmcsa"))
        await dry_run_recorded(db_session, draft.id)

        with pytest.raises(UnknownSourceError) as exc_info:
            await service.activate(draft.id, frozenset({"fmcsa", "fcmsa"}), "sandeep")

        assert exc_info.value.unknown_sources == ("fcmsa",)

    async def test_accepting_every_source_activates(self, db_session: AsyncSession) -> None:
        service = ManifestService(db_session)
        draft = await service.create_draft(a_vertical(), a_body("fmcsa", "places"))
        await dry_run_recorded(db_session, draft.id)

        activated = await service.activate(
            draft.id, frozenset({"fmcsa", "places"}), "sandeep", {"fmcsa": "CC PDM 1.0"}
        )

        assert activated.status is ManifestStatus.active
        assert activated.activated_by == "sandeep"
        assert activated.activated_at is not None
        assert activated.body.undecided_sources() == ()
        decisions = {entry.source_name: entry for entry in activated.body.terms}
        assert decisions["fmcsa"].decision is TermsDecision.accepted
        assert decisions["fmcsa"].license == "CC PDM 1.0"
        assert decisions["places"].license is None
        assert decisions["places"].decided_by == "sandeep"


@requires_db
class TestActivationPreconditions:
    async def test_an_unknown_id_is_a_not_found(self, db_session: AsyncSession) -> None:
        service = ManifestService(db_session)
        with pytest.raises(ManifestNotFoundError):
            await service.activate(uuid4(), frozenset(), "sandeep")

    async def test_an_already_active_manifest_cannot_be_activated(
        self, db_session: AsyncSession
    ) -> None:
        service = ManifestService(db_session)
        draft = await service.create_draft(a_vertical(), a_body())
        await dry_run_recorded(db_session, draft.id)
        await service.activate(draft.id, frozenset({"fmcsa"}), "sandeep")

        with pytest.raises(ManifestNotDraftError) as exc_info:
            await service.activate(draft.id, frozenset({"fmcsa"}), "sandeep")

        assert exc_info.value.status == ManifestStatus.active.value

    async def test_a_superseded_manifest_cannot_be_reactivated(
        self, db_session: AsyncSession
    ) -> None:
        service = ManifestService(db_session)
        vertical = a_vertical()
        first = await service.create_draft(vertical, a_body())
        await dry_run_recorded(db_session, first.id)
        second = await service.create_draft(vertical, a_body())
        await dry_run_recorded(db_session, second.id)
        await service.activate(first.id, frozenset({"fmcsa"}), "sandeep")
        await service.activate(second.id, frozenset({"fmcsa"}), "sandeep")

        with pytest.raises(ManifestNotDraftError):
            await service.activate(first.id, frozenset({"fmcsa"}), "sandeep")


@requires_db
class TestSupersede:
    async def test_activating_a_new_version_retires_the_old_one(
        self, db_session: AsyncSession
    ) -> None:
        """Exactly one active row, and the old version still readable at its own version."""
        service = ManifestService(db_session)
        vertical = a_vertical()
        first = await service.create_draft(vertical, a_body())
        await dry_run_recorded(db_session, first.id)
        second = await service.create_draft(vertical, a_body())
        await dry_run_recorded(db_session, second.id)

        await service.activate(first.id, frozenset({"fmcsa"}), "sandeep")
        await service.activate(second.id, frozenset({"fmcsa"}), "sandeep")

        assert (await service.get(first.id)).status is ManifestStatus.superseded
        assert (await service.get(second.id)).status is ManifestStatus.active
        assert (await service.get_active(vertical)).version == 2

    async def test_activation_does_not_bump_the_version(self, db_session: AsyncSession) -> None:
        """A bump is how a manifest *changes*; activation is how a version is *accepted*."""
        service = ManifestService(db_session)
        draft = await service.create_draft(a_vertical(), a_body())
        await dry_run_recorded(db_session, draft.id)

        activated = await service.activate(draft.id, frozenset({"fmcsa"}), "sandeep")

        assert activated.version == draft.version


@requires_db
class TestCitationsSurviveActivation:
    async def test_the_cited_fields_are_identical_before_and_after(
        self, db_session: AsyncSession
    ) -> None:
        """Activation records a decision; it must not re-date a single citation."""
        service = ManifestService(db_session)
        body = a_body("fmcsa", "places")
        draft = await service.create_draft(a_vertical(), body)
        await dry_run_recorded(db_session, draft.id)

        activated = await service.activate(draft.id, frozenset({"fmcsa", "places"}), "sandeep")

        assert activated.body.sources == body.sources
        assert activated.body.disqualifier_rules == body.disqualifier_rules
        assert activated.body.qualifying_signals == body.qualifying_signals
        assert activated.body.icp_band == body.icp_band
        assert activated.body.vocabulary == body.vocabulary


@requires_db
class TestReads:
    async def test_get_active_raises_when_only_drafts_exist(self, db_session: AsyncSession) -> None:
        """A pipeline must stop loudly, not source against nothing."""
        service = ManifestService(db_session)
        vertical = a_vertical()
        await service.create_draft(vertical, a_body())

        with pytest.raises(ActiveManifestNotFoundError) as exc_info:
            await service.get_active(vertical)

        assert exc_info.value.vertical == vertical

    async def test_get_raises_for_an_unknown_id(self, db_session: AsyncSession) -> None:
        with pytest.raises(ManifestNotFoundError):
            await ManifestService(db_session).get(uuid4())


class TestParseAcceptTerms:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("fmcsa", {"fmcsa"}),
            ("fmcsa,places", {"fmcsa", "places"}),
            (" fmcsa , places ", {"fmcsa", "places"}),
            ("fmcsa,,places,", {"fmcsa", "places"}),
            ("fmcsa,fmcsa", {"fmcsa"}),
            ("", set[str]()),
            (None, set[str]()),
        ],
    )
    def test_ordinary_typing_is_not_an_error(self, raw: str | None, expected: set[str]) -> None:
        """Trailing commas and stray spaces carry no meaning to lose."""
        assert parse_accept_terms(raw) == frozenset(expected)


@requires_db
class TestTheDryRunGate:
    """Decided 2026-10-09: ``activate`` requires a recorded dry-run (T7)."""

    async def test_activation_without_a_dry_run_is_refused_and_writes_nothing(
        self, db_session: AsyncSession
    ) -> None:
        service = ManifestService(db_session)
        draft = await service.create_draft(a_vertical(), a_body("fmcsa"))

        with pytest.raises(DryRunNotRecordedError) as exc_info:
            await service.activate(draft.id, frozenset({"fmcsa"}), "sandeep")

        assert "lpe manifest dry-run" in exc_info.value.message
        unchanged = await service.get(draft.id)
        assert unchanged.status is ManifestStatus.draft
        assert unchanged.body.terms == ()

    async def test_the_dry_run_is_checked_before_the_terms(self, db_session: AsyncSession) -> None:
        """Both missing: the person is sent to read the data before the legal question."""
        service = ManifestService(db_session)
        draft = await service.create_draft(a_vertical(), a_body("fmcsa", "places"))
        with pytest.raises(DryRunNotRecordedError):
            await service.activate(draft.id, frozenset(), "sandeep")

    async def test_a_recorded_dry_run_unblocks_activation(self, db_session: AsyncSession) -> None:
        service = ManifestService(db_session)
        draft = await service.create_draft(a_vertical(), a_body("fmcsa"))

        recorded = await service.record_dry_run(draft.id, a_dry_run(draft.id), "sandeep")
        activated = await service.activate(draft.id, frozenset({"fmcsa"}), "sandeep")

        assert recorded.manifest_id == draft.id
        assert recorded.ran_by == "sandeep"
        assert activated.status is ManifestStatus.active

    async def test_a_report_for_another_manifest_is_refused(self, db_session: AsyncSession) -> None:
        service = ManifestService(db_session)
        draft = await service.create_draft(a_vertical(), a_body("fmcsa"))
        with pytest.raises(DryRunMismatchError):
            await service.record_dry_run(draft.id, a_dry_run(uuid4()), "sandeep")

    async def test_a_dry_run_of_an_unknown_manifest_is_a_not_found(
        self, db_session: AsyncSession
    ) -> None:
        missing = uuid4()
        with pytest.raises(ManifestNotFoundError):
            await ManifestService(db_session).record_dry_run(missing, a_dry_run(missing), "x")
