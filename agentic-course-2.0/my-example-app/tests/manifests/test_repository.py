"""The repository's contract, against a real Postgres.

Two of these are load-bearing beyond the method they name: ``get_active`` must never hand back a
DRAFT (the lifecycle is meaningless otherwise), and a second ACTIVE row for one vertical must be
refused **by the database** — a service check can be forgotten by a slice nobody has written yet.
"""

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.manifests.models import VerticalManifest
from app.manifests.repository import ManifestRepository
from app.manifests.schemas import ManifestBody, ManifestStatus
from tests.conftest import requires_db
from tests.manifests.builders import a_body, a_vertical

pytestmark = requires_db


class TestVersioning:
    async def test_drafts_take_successive_versions(self, db_session: AsyncSession) -> None:
        repository = ManifestRepository(db_session)
        vertical = a_vertical()

        first = await repository.create_draft(vertical, a_body())
        second = await repository.create_draft(vertical, a_body())

        assert (first.version, second.version) == (1, 2)

    async def test_the_old_version_is_retained(self, db_session: AsyncSession) -> None:
        """AC6: a bump writes a new row; it does not overwrite the previous one."""
        repository = ManifestRepository(db_session)
        vertical = a_vertical()

        first = await repository.create_draft(vertical, a_body())
        await repository.create_draft(vertical, a_body("places"))

        found = await repository.get(vertical, 1)
        assert found is not None
        assert found.id == first.id

    async def test_next_version_starts_at_one(self, db_session: AsyncSession) -> None:
        repository = ManifestRepository(db_session)
        assert await repository.next_version(a_vertical()) == 1

    async def test_the_same_version_cannot_be_written_twice(self, db_session: AsyncSession) -> None:
        repository = ManifestRepository(db_session)
        vertical = a_vertical()
        await repository.create_draft(vertical, a_body())

        db_session.add(
            VerticalManifest(
                vertical=vertical,
                version=1,
                status=ManifestStatus.draft.value,
                body=a_body().model_dump(mode="json"),
            )
        )
        with pytest.raises(IntegrityError):
            await db_session.flush()


class TestGetActive:
    async def test_a_draft_is_never_returned(self, db_session: AsyncSession) -> None:
        """The ticket's named test. A proposal nobody accepted is not "the manifest"."""
        repository = ManifestRepository(db_session)
        vertical = a_vertical()
        await repository.create_draft(vertical, a_body())

        assert await repository.get_active(vertical) is None

    async def test_an_active_row_is_returned(self, db_session: AsyncSession) -> None:
        repository = ManifestRepository(db_session)
        vertical = a_vertical()
        manifest = await repository.create_draft(vertical, a_body())
        await repository.mark_active(manifest, a_body(), "sandeep")

        found = await repository.get_active(vertical)
        assert found is not None
        assert found.id == manifest.id
        assert found.activated_by == "sandeep"

    async def test_two_active_rows_for_one_vertical_are_refused_by_the_database(
        self, db_session: AsyncSession
    ) -> None:
        """AC4: the invariant is an index, not an intention.

        Written through the repository twice on purpose — this is the path a future slice would
        take if it forgot the supersede step, and the database has to be what stops it.
        """
        repository = ManifestRepository(db_session)
        vertical = a_vertical()
        first = await repository.create_draft(vertical, a_body())
        second = await repository.create_draft(vertical, a_body())
        await repository.mark_active(first, a_body(), "sandeep")

        with pytest.raises(IntegrityError):
            await repository.mark_active(second, a_body(), "sandeep")


class TestStatusIsConstrained:
    async def test_an_unknown_status_is_refused_by_the_database(
        self, db_session: AsyncSession
    ) -> None:
        """`status` must be one of the three lifecycle values, enforced by a CHECK constraint.

        Found in the PR #6 review. Without the constraint this insert *succeeded*: the partial
        unique index keys on the literal `'active'`, so a row saying `'Active'` was exempt from
        "one active per vertical" while also being invisible to every reader filtering on
        `'active'` — constrained by nothing and visible to nothing.
        """
        db_session.add(
            VerticalManifest(
                vertical=a_vertical(),
                version=1,
                status="Active",
                body=a_body().model_dump(mode="json"),
            )
        )
        with pytest.raises(IntegrityError) as exc_info:
            await db_session.flush()

        assert "ck_vertical_manifest_status" in str(exc_info.value)

    async def test_every_lifecycle_value_is_accepted(self, db_session: AsyncSession) -> None:
        """The constraint must not be narrower than the enum it mirrors."""
        for index, status in enumerate(ManifestStatus, start=1):
            db_session.add(
                VerticalManifest(
                    vertical=a_vertical(),
                    version=index,
                    status=status.value,
                    body=a_body().model_dump(mode="json"),
                )
            )
        await db_session.flush()


class TestListing:
    async def test_ordering_is_stable_and_filters_apply(self, db_session: AsyncSession) -> None:
        repository = ManifestRepository(db_session)
        vertical = a_vertical()
        await repository.create_draft(vertical, a_body())
        await repository.create_draft(vertical, a_body())
        third = await repository.create_draft(vertical, a_body())
        await repository.mark_active(third, a_body(), "sandeep")

        listed = await repository.list(vertical=vertical)
        assert [manifest.version for manifest in listed] == [3, 2, 1]

        active_only = await repository.list(vertical=vertical, status=ManifestStatus.active)
        assert [manifest.version for manifest in active_only] == [3]

    async def test_limit_and_offset_page(self, db_session: AsyncSession) -> None:
        repository = ManifestRepository(db_session)
        vertical = a_vertical()
        for _ in range(3):
            await repository.create_draft(vertical, a_body())

        page = await repository.list(vertical=vertical, limit=2, offset=1)
        assert [manifest.version for manifest in page] == [2, 1]


class TestBodyRoundTrip:
    async def test_provenance_survives_persist_and_load(self, db_session: AsyncSession) -> None:
        """The JSONB round trip is where a `datetime` either survives or becomes a bare string."""
        repository = ManifestRepository(db_session)
        body = a_body()
        manifest = await repository.create_draft(a_vertical(), body)

        await db_session.refresh(manifest)

        assert ManifestBody.model_validate(manifest.body) == body
