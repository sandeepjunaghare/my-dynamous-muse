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

    One per request. The weekly run (T10) does not use it: ``SourcingPipeline`` creates one
    accumulator per run, which is the scope a run's cost belongs to.
    """
    return RunCost()


SettingsDep = Annotated[Settings, Depends(get_settings)]
DbSession = Annotated[AsyncSession, Depends(get_db)]
RunCostDep = Annotated[RunCost, Depends(get_run_cost)]
