"""The service boots and answers. Nothing here may require a reachable database."""

import json
import re
from unittest.mock import patch

from httpx import AsyncClient
from starlette.requests import Request

from app.core.exceptions import LocalProspectEngineError
from app.main import handle_known_error, handle_unexpected_error

UUID4 = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


class TestHealth:
    async def test_returns_ok(self, client: AsyncClient) -> None:
        response = await client.get("/health")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["version"]
        assert body["environment"] in {"dev", "prod"}

    async def test_does_not_touch_the_database(self, client: AsyncClient) -> None:
        """A liveness check that fails when Supabase blips is a readiness check in disguise.

        Asserted by making engine creation explode: if `/health` still answers 200, it never asked
        for a connection. A database probe, if ever wanted, belongs at `/health/ready`.
        """
        with patch(
            "app.core.database.create_async_engine",
            side_effect=AssertionError("/health must not open a database connection"),
        ):
            response = await client.get("/health")

        assert response.status_code == 200


class TestCorrelationId:
    async def test_client_supplied_id_is_echoed(self, client: AsyncClient) -> None:
        response = await client.get("/health", headers={"X-Request-ID": "abc123"})

        assert response.status_code == 200
        assert response.headers["x-request-id"] == "abc123"

    async def test_id_is_generated_when_absent(self, client: AsyncClient) -> None:
        response = await client.get("/health")

        generated = response.headers["x-request-id"]
        assert re.fullmatch(UUID4, generated)

    async def test_each_request_gets_its_own_id(self, client: AsyncClient) -> None:
        first = await client.get("/health")
        second = await client.get("/health")

        assert first.headers["x-request-id"] != second.headers["x-request-id"]


class TestErrorHandling:
    async def test_unknown_route_is_404(self, client: AsyncClient) -> None:
        assert (await client.get("/does-not-exist")).status_code == 404

    async def test_known_error_renders_structured_json(self) -> None:
        """A deliberate error becomes `{error, detail, code}` — never a stack trace."""
        request = Request({"type": "http", "method": "GET", "path": "/health", "headers": []})
        error = LocalProspectEngineError("registry unreachable", code="registry_unreachable")

        response = await handle_known_error(request, error)

        assert response.status_code == 500
        body = json.loads(bytes(response.body))
        assert body == {
            "error": "LocalProspectEngineError",
            "detail": "registry unreachable",
            "code": "registry_unreachable",
        }

    async def test_unexpected_error_does_not_leak_internals(self) -> None:
        """The client gets a safe message; the traceback goes to the log, not the response."""
        request = Request({"type": "http", "method": "GET", "path": "/health", "headers": []})

        response = await handle_unexpected_error(request, RuntimeError("asyncpg: password=hunter2"))

        assert response.status_code == 500
        body = json.loads(bytes(response.body))
        assert body["detail"] == "an unexpected error occurred"
        assert "hunter2" not in bytes(response.body).decode()
