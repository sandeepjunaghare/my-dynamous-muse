"""Config must fail loudly. A silent default is how a run points at the wrong database."""

from pathlib import Path

import pytest
from pydantic import ValidationError
from pydantic_settings import SettingsConfigDict

from app.core.config import Settings, get_settings

_MANAGED_VARS = (
    "APP_NAME",
    "VERSION",
    "DATABASE_URL",
    "ENVIRONMENT",
    "DEBUG",
    "LOG_LEVEL",
    "MAX_PLACES_CALLS_PER_RUN",
    "HUBSPOT_PRIVATE_APP_TOKEN",
)

VALID_URL = "postgresql+asyncpg://u:p@db.abc.supabase.co:5432/postgres"


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """A clean environment with no ``.env`` behind it.

    Without detaching ``env_file``, these tests would read whatever ``.env`` the developer running
    them happens to have, and "missing required variable" would pass or fail by accident.
    """
    detached: SettingsConfigDict = {**Settings.model_config, "env_file": None}
    monkeypatch.setattr(Settings, "model_config", detached)
    for key in _MANAGED_VARS:
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


class TestRequiredSettings:
    def test_missing_database_url_raises(self, env: pytest.MonkeyPatch) -> None:
        with pytest.raises(ValidationError) as exc_info:
            Settings.model_validate({})
        assert "database_url" in str(exc_info.value).lower()

    def test_database_url_is_read_from_the_environment(self, env: pytest.MonkeyPatch) -> None:
        env.setenv("DATABASE_URL", VALID_URL)
        assert Settings.model_validate({}).database_url == VALID_URL


class TestStrictness:
    def test_unknown_variable_in_env_file_is_rejected(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """A typo'd line in `.env` is a startup failure, not a silently ignored line.

        Scoped to the `.env` file on purpose. `extra="forbid"` cannot police the *process*
        environment — that holds PATH, HOME and every other unrelated variable, none of which are
        settings. `.env` is the surface a person edits, so `.env` is where a typo must be caught.
        """
        env_file = tmp_path / ".env"
        env_file.write_text(
            f"DATABASE_URL={VALID_URL}\nMAX_PLACES_CALLS_PER_RUNN=10\n",
            encoding="utf-8",
        )
        pinned: SettingsConfigDict = {**Settings.model_config, "env_file": str(env_file)}
        monkeypatch.setattr(Settings, "model_config", pinned)
        for key in _MANAGED_VARS:
            monkeypatch.delenv(key, raising=False)

        with pytest.raises(ValidationError) as exc_info:
            Settings.model_validate({})
        assert "extra" in str(exc_info.value).lower()

    def test_invalid_environment_is_rejected(self, env: pytest.MonkeyPatch) -> None:
        env.setenv("DATABASE_URL", VALID_URL)
        env.setenv("ENVIRONMENT", "staging")
        with pytest.raises(ValidationError):
            Settings.model_validate({})


class TestDefaults:
    def test_defaults_land_where_expected(self, env: pytest.MonkeyPatch) -> None:
        env.setenv("DATABASE_URL", VALID_URL)
        settings = Settings.model_validate({})

        assert settings.app_name == "local-prospect-engine"
        assert settings.environment == "dev"
        assert settings.debug is False
        assert settings.log_level == "INFO"
        assert settings.max_places_calls_per_run == 500
        assert settings.hubspot_private_app_token is None

    def test_no_setting_carries_a_local_path(self, env: pytest.MonkeyPatch) -> None:
        """Strict 12-factor: local Mac to VPS must be env vars only, never a path in code."""
        env.setenv("DATABASE_URL", VALID_URL)
        for name, value in Settings.model_validate({}).model_dump().items():
            if isinstance(value, str):
                assert not value.startswith("/"), f"{name} looks like an absolute path"
                assert "\\" not in value, f"{name} looks like a Windows path"


class TestCaching:
    def test_get_settings_is_cached(self, env: pytest.MonkeyPatch) -> None:
        env.setenv("DATABASE_URL", VALID_URL)
        assert get_settings() is get_settings()
