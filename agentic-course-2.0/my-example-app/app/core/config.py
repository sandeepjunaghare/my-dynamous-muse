"""Application configuration — strict 12-factor, read from the environment once and cached.

Nothing here may carry a local filesystem path. That is what makes the eventual move from a local
Mac to a small VPS an env-var change and nothing else.

Required settings have no default, so a missing variable fails at startup rather than booting the
service into a surprising state. ``extra="forbid"`` turns a typo'd variable into the same loud
failure instead of a silently ignored line in ``.env``.
"""

from decimal import Decimal
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_HUBSPOT_UI_DOMAIN = "app-na2.hubspot.com"
"""The UI host for portal 244766495's data centre (na2)."""


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
    # Must use the asyncpg driver and the pooler's SESSION mode (port 5432), never its
    # TRANSACTION mode (6543), which breaks prepared statements; see .env.example.
    database_url: str

    # Observability
    log_level: str = "INFO"

    # Cost. A circuit breaker against a runaway loop, not a budget target (D9). Its consumer is
    # T6's `verify_business`: reaching it stops the Places checks and finishes the stage degraded.
    max_places_calls_per_run: int = Field(default=500, gt=0)

    # Sourcing (T5). The run works a backlog in fixed batches (D12): the batch size, not the pool
    # size, sets each run's paid bill. 150 is the starting point, to be tuned from the first runs.
    sourcing_batch_size: int = Field(default=150, gt=0)
    # A run still `running` this long after it started was killed without finishing; the next run
    # of the vertical marks it failed, which also hands its batch back to the pool.
    sourcing_stale_run_hours: int = Field(default=6, gt=0)
    # FMCSA QCMobile webKey — free, through a Login.gov developer account. Unset, a run refuses
    # before taking a batch; `--allow-degraded` records candidates without `allowedToOperate`.
    fmcsa_webkey: str | None = None
    # Socrata app token for data.transportation.gov. Optional: without it SODA throttles per IP.
    socrata_app_token: str | None = None
    # Google Places (New) API key — `verify_business`'s check, never a source (D13). Declared
    # ahead of T6 so a key placed in `.env` is not refused by `extra="forbid"`. A `SecretStr`, so
    # it never prints in a repr or a log. T6 treats a blank value as unset.
    google_places_api_key: SecretStr | None = None
    # Text Search Pro list price per call ($32 per 1,000), for cost logging only (A5). The first
    # 5,000 calls a month are free, which one run cannot see, so a run's total is an upper bound.
    places_text_search_usd: Decimal = Field(default=Decimal("0.032"), ge=0)
    # Routing (T8). The most doors one route may hold: the cadence plans ~10-12 doors an outing and
    # 20 names in two routes a week (E3, E4). A cap per route, so the number of routes follows from
    # how many candidates there are, never the other way round.
    route_max_doors: int = Field(default=12, gt=0)

    # --- Weekly run (T10) ---
    # A weekly run still `running` this long after it was requested was killed; the next start
    # marks it failed, which is what lifts the one-at-a-time guard. Kept below
    # `sourcing_stale_run_hours`, so a reaped weekly run never outlives the guard on its own
    # sourcing run (which sourcing reaps per vertical, at its own next start, anyway).
    weekly_run_stale_hours: int = Field(default=2, gt=0)
    # M7's target: brief to a pending-review list in HubSpot in under this many minutes. The Friday
    # report compares each run's measured time against it.
    weekly_run_target_minutes: int = Field(default=60, gt=0)

    # Integrations. Optional at T1 — T3 is what first needs a token.
    hubspot_private_app_token: str | None = None
    # The HubSpot owner a cadence task is assigned to when ``enrol`` names none. Unset leaves tasks
    # unassigned — out of everyone's "My tasks", which is how E15's tasks went unworked — so the
    # cadence warns about it on every enrol and sync. Blank counts as unset.
    hubspot_default_owner_id: str | None = None
    # The portal and UI host a record's URL is built from, so `lpe promotion` prints a link a
    # founder can open instead of a bare id. Blank counts as unset: a blank portal id prints the
    # bare id, and a blank UI domain falls back to the default host.
    hubspot_portal_id: str | None = None
    hubspot_ui_domain: str = DEFAULT_HUBSPOT_UI_DOMAIN

    # Manifest-authoring agent (T12). The key is optional: when unset, the Agent SDK falls back to
    # whatever credential its bundled CLI already resolves. Declared so that a key placed in `.env`
    # is not refused by `extra="forbid"`.
    anthropic_api_key: str | None = None
    manifest_agent_model: str = "claude-opus-5-5"
    # Circuit breakers against a runaway research loop, not budget targets — the same stance as
    # the Places cap (D7). The first live run (freight v2, 2026-10-09) took 36 turns and $0.78, so
    # turns, not dollars, were the binding cap; the budget is the real runaway guard.
    manifest_agent_max_turns: int = Field(default=60, gt=0)
    manifest_agent_max_budget_usd: Decimal = Field(default=Decimal("5.00"), gt=0)

    # The classify_rollup judgment node (T7): one call per candidate decides every judgment rule.
    # The ~$0.08 a call was measured on this model, so it is the cost basis; a cheaper model is the
    # lever once the live eval shows it holds the <5% false-positive bar (M6). Per-call caps and a
    # per-run call cap are circuit breakers against a runaway loop, not budget targets (D7).
    rollup_classifier_model: str = "claude-opus-5-5"
    rollup_classifier_max_turns: int = Field(default=8, gt=0)
    rollup_classifier_max_budget_usd: Decimal = Field(default=Decimal("0.50"), gt=0)
    max_judgment_calls_per_run: int = Field(default=500, gt=0)

    # The resolve_owner judgment node (T6): runs only where the census names no citable officer.
    # It mirrors the classify_rollup node: per-call and per-run caps are circuit breakers against a
    # runaway loop, not budget targets (D7). Reaching the per-run cap finishes the stage degraded.
    owner_resolver_model: str = "claude-opus-5-5"
    owner_resolver_max_turns: int = Field(default=10, gt=0)
    owner_resolver_max_budget_usd: Decimal = Field(default=Decimal("0.50"), gt=0)
    max_owner_calls_per_run: int = Field(default=500, gt=0)


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings, loaded from the environment on first call.

    ``Settings()`` would make both type checkers demand the required fields as arguments — neither
    can see that pydantic-settings fills them from the environment, and silencing that would need a
    suppression this project does not allow. ``model_validate({})`` runs the identical settings
    sources and is correctly typed.
    """
    return Settings.model_validate({})
