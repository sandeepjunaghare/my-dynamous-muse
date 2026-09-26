"""Application configuration — strict 12-factor, read from the environment once and cached.

Nothing here may carry a local filesystem path. That is what makes the eventual move from a local
Mac to a small VPS an env-var change and nothing else.

Required settings have no default, so a missing variable fails at startup rather than booting the
service into a surprising state. ``extra="forbid"`` turns a typo'd variable into the same loud
failure instead of a silently ignored line in ``.env``.
"""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application-wide configuration."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="forbid",
    )

    # Application
    app_name: str = "local-prospect-engine"
    version: str = "0.1.0"
    environment: Literal["dev", "prod"] = "dev"
    debug: bool = False

    # Database — hosted Supabase, one project per environment.
    # Must use the asyncpg driver and the DIRECT port (5432), not the pooler; see .env.example.
    database_url: str

    # Observability
    log_level: str = "INFO"

    # Cost. A circuit breaker against a runaway loop, not a budget target (D9).
    max_places_calls_per_run: int = 500

    # Integrations. Optional at T1 — T3 is what first needs a token.
    hubspot_private_app_token: str | None = None


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings, loaded from the environment on first call.

    ``Settings()`` would make both type checkers demand the required fields as arguments — neither
    can see that pydantic-settings fills them from the environment, and silencing that would need a
    suppression this project does not allow. ``model_validate({})`` runs the identical settings
    sources and is correctly typed.
    """
    return Settings.model_validate({})
