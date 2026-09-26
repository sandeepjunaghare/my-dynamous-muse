"""Global FastAPI dependencies.

Annotated aliases rather than bare ``Depends`` calls at each use site, so a handler signature reads
as types and a change to how something is provided happens in exactly one place.
"""

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.cost import RunCost
from app.core.database import get_db


def get_run_cost() -> RunCost:
    """Provide a fresh cost accumulator.

    One per request today. When T10 introduces the weekly run, the run — not the request — becomes
    the scope, and this is the single place that changes.
    """
    return RunCost()


SettingsDep = Annotated[Settings, Depends(get_settings)]
DbSession = Annotated[AsyncSession, Depends(get_db)]
RunCostDep = Annotated[RunCost, Depends(get_run_cost)]
