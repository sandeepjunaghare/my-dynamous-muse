"""Logging must produce parseable JSON carrying the correlation id, and must not eat tracebacks."""

import json
from typing import assert_type, cast

import pytest
from structlog.types import FilteringBoundLogger

from app.core.logging import (
    add_request_id,
    get_logger,
    get_request_id,
    request_id_var,
    set_request_id,
    setup_logging,
)


@pytest.fixture(autouse=True)
def _configured() -> None:
    setup_logging()


def _emit(capsys: pytest.CaptureFixture[str], event: str, **fields: object) -> dict[str, object]:
    """Emit one entry through the real configuration and parse it back."""
    get_logger("tests.core.test_logging").info(event, **fields)
    line = capsys.readouterr().out.strip().splitlines()[-1]
    return cast(dict[str, object], json.loads(line))


class TestRequestId:
    def test_generated_when_absent(self) -> None:
        generated = set_request_id()
        assert generated
        assert get_request_id() == generated

    def test_client_supplied_id_is_kept(self) -> None:
        assert set_request_id("abc123") == "abc123"
        assert get_request_id() == "abc123"

    def test_processor_stamps_the_id_onto_the_entry(self) -> None:
        set_request_id("req-42")
        event_dict = add_request_id(None, "info", {"event": "core.test.emitted"})
        assert event_dict["request_id"] == "req-42"

    def test_processor_omits_the_key_outside_a_request(self) -> None:
        """Background work has no correlation id, and must not be given a misleading one.

        Note `set_request_id("")` would *generate* one — an empty argument means "none supplied",
        which is the middleware's case. Clearing the context var is the only way to be outside a
        request.
        """
        request_id_var.set("")
        event_dict = add_request_id(None, "info", {"event": "core.test.emitted"})
        assert "request_id" not in event_dict


class TestOutput:
    def test_entry_is_json_with_level_timestamp_and_request_id(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        set_request_id("req-json")
        entry = _emit(capsys, "core.test.emitted", candidate_count=3)

        assert entry["event"] == "core.test.emitted"
        assert entry["level"] == "info"
        assert entry["request_id"] == "req-json"
        assert entry["candidate_count"] == 3
        assert "timestamp" in entry

    def test_exc_info_is_rendered_as_a_traceback_string(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """`format_exc_info` is what keeps a stack trace inside the JSON instead of losing it."""
        logger = get_logger("tests.core.test_logging")
        try:
            raise ValueError("registry returned nothing")
        except ValueError as exc:
            logger.error("core.test.failed", error=str(exc), exc_info=True)

        entry = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert entry["level"] == "error"
        assert "ValueError: registry returned nothing" in entry["exception"]
        assert "Traceback" in entry["exception"]


class TestEventNaming:
    def test_core_modules_use_domain_component_action_state(self) -> None:
        """CLAUDE.md's convention: four segments of meaning in three dots."""
        import re
        from pathlib import Path

        pattern = re.compile(r'logger\.(?:debug|info|warning|error)\(\s*"([^"]+)"')
        event_name = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+\.[a-z0-9]+_[a-z0-9]+$")

        app_dir = Path(__file__).resolve().parents[2] / "app"
        found = 0
        for source in app_dir.rglob("*.py"):
            for event in pattern.findall(source.read_text(encoding="utf-8")):
                found += 1
                assert event_name.match(event), f"{source.name}: {event!r}"

        assert found > 0, "no log events found to check"


class TestGetLogger:
    def test_return_type_is_a_bound_logger_not_any(self) -> None:
        """The reference example types this `-> Any`, which the ground rules forbid.

        The claim is a *static* one, so `assert_type` is what checks it — mypy and pyright fail
        this line if the annotation ever loosens back to `Any`. At runtime `assert_type` is a
        no-op, so the call below is what proves the object actually logs.
        """
        logger = get_logger("tests.core.test_logging")
        assert_type(logger, FilteringBoundLogger)
        logger.info("core.test.emitted")
