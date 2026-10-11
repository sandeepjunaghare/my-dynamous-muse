"""The read-only endpoints, including that slice errors render through the centralized handler.

The error shape matters as much as the status code: handlers carry no ``try/except``, so a 404 here
proves ``app.main``'s ``LocalProspectEngineError`` handler is what rendered it.
"""

from uuid import uuid4

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.manifests.service import ManifestService
from tests.conftest import requires_db
from tests.manifests.builders import a_body, a_vertical, dry_run_recorded

pytestmark = requires_db


class TestListManifests:
    async def test_it_lists_a_verticals_versions_newest_first(
        self, client_with_db: AsyncClient, db_session: AsyncSession
    ) -> None:
        service = ManifestService(db_session)
        vertical = a_vertical()
        await service.create_draft(vertical, a_body())
        await service.create_draft(vertical, a_body())

        response = await client_with_db.get("/manifests", params={"vertical": vertical})

        assert response.status_code == 200
        payload = response.json()
        assert [item["version"] for item in payload] == [2, 1]
        assert payload[0]["body"]["sources"][0]["value"]["name"] == "fmcsa"

    async def test_the_status_filter_applies(
        self, client_with_db: AsyncClient, db_session: AsyncSession
    ) -> None:
        service = ManifestService(db_session)
        vertical = a_vertical()
        draft = await service.create_draft(vertical, a_body())
        await dry_run_recorded(db_session, draft.id)
        await service.activate(draft.id, frozenset({"fmcsa"}), "sandeep")

        drafts = await client_with_db.get(
            "/manifests", params={"vertical": vertical, "status": "draft"}
        )
        assert drafts.json() == []

        active = await client_with_db.get(
            "/manifests", params={"vertical": vertical, "status": "active"}
        )
        assert [item["version"] for item in active.json()] == [1]

    async def test_an_out_of_range_limit_is_rejected(self, client_with_db: AsyncClient) -> None:
        """Collections are paginated, with a cap the client cannot talk its way past."""
        response = await client_with_db.get("/manifests", params={"limit": 500})
        assert response.status_code == 422


class TestGetActive:
    async def test_it_returns_the_active_manifest(
        self, client_with_db: AsyncClient, db_session: AsyncSession
    ) -> None:
        service = ManifestService(db_session)
        vertical = a_vertical()
        draft = await service.create_draft(vertical, a_body())
        await dry_run_recorded(db_session, draft.id)
        await service.activate(draft.id, frozenset({"fmcsa"}), "sandeep")

        response = await client_with_db.get(f"/manifests/active/{vertical}")

        assert response.status_code == 200
        assert response.json()["status"] == "active"

    async def test_only_drafts_is_a_structured_404(
        self, client_with_db: AsyncClient, db_session: AsyncSession
    ) -> None:
        vertical = a_vertical()
        await ManifestService(db_session).create_draft(vertical, a_body())

        response = await client_with_db.get(f"/manifests/active/{vertical}")

        assert response.status_code == 404
        payload = response.json()
        assert payload["code"] == "active_manifest_not_found"
        assert payload["error"] == "ActiveManifestNotFoundError"
        assert vertical in payload["detail"]


class TestGetById:
    async def test_it_returns_one_manifest(
        self, client_with_db: AsyncClient, db_session: AsyncSession
    ) -> None:
        draft = await ManifestService(db_session).create_draft(a_vertical(), a_body())

        response = await client_with_db.get(f"/manifests/{draft.id}")

        assert response.status_code == 200
        assert response.json()["id"] == str(draft.id)

    async def test_an_unknown_id_is_a_structured_404(self, client_with_db: AsyncClient) -> None:
        response = await client_with_db.get(f"/manifests/{uuid4()}")

        assert response.status_code == 404
        assert response.json()["code"] == "manifest_not_found"

    async def test_a_malformed_id_is_a_422_not_a_500(self, client_with_db: AsyncClient) -> None:
        """Typing the path as `UUID` is what keeps a bad id out of the database."""
        response = await client_with_db.get("/manifests/not-a-uuid")
        assert response.status_code == 422
