"""The contract every pipeline stage meets, and the registry the runner reads them from.

**A stage registers by being a file.** ``app/tools/<stage>.py``, named for its ``PipelineStage``
value, exports ``STAGE``. There is no list to append to: T5, T7 and T8 are built in parallel, and
three branches each appending a line at the same spot in one file is a merge conflict every time.
Adding a file is not.

The pipeline order comes from ``PipelineStage``, never from the file system, so the run is the same
whatever order the modules were written or listed in. A missing module means the stage is not built
yet; a module that is present but broken is an error, never a skipped stage.

The runner itself is T5's (``app/sourcing/``). This module only states what a stage is.
"""

import importlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cost import RunCost
from app.manifests.schemas import ManifestResponse
from app.sourcing.stages import PipelineStage

STAGES_PACKAGE = "app.tools"


@dataclass(frozen=True, kw_only=True)
class StageContext:
    """What every stage is handed: the run, the manifest it runs under, and where to record cost.

    No candidate list: stage 1 selects the run's batch, so later stages read it back by ``run_id``.
    """

    run_id: UUID
    manifest: ManifestResponse
    session: AsyncSession
    cost: RunCost


@dataclass(frozen=True, kw_only=True)
class StageResult:
    """What a stage reports back: its counts, merged into ``sourcing_run.counts`` by the runner."""

    counts: Mapping[str, int]
    degraded_reason: str | None = None
    """Set when the stage finished but short of its goal: a source it could not use, lookups that
    failed. The runner then finishes the run ``degraded`` with every stage's reason."""


@runtime_checkable
class Stage(Protocol):
    """One of the five generic stages. A stage that fails raises; the runner decides the outcome."""

    @property
    def stage(self) -> PipelineStage:
        """Which stage this is. It must match the module's name."""
        ...

    async def run(self, context: StageContext) -> StageResult:
        """Do this stage's work for one run."""
        ...


def registered_stages(package: str = STAGES_PACKAGE) -> tuple[Stage, ...]:
    """Every stage built so far, in pipeline order.

    ``package`` exists for tests; the pipeline always reads ``app.tools``.
    """
    stages: list[Stage] = []
    for pipeline_stage in PipelineStage:
        module_name = f"{package}.{pipeline_stage.value}"
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            # Only *this* module being absent means "not built yet". A stage module that exists but
            # imports something missing is broken, and skipping it would drop a stage from the run.
            if exc.name != module_name:
                raise
            continue

        exported: object = getattr(module, "STAGE", None)
        if not isinstance(exported, Stage):
            raise TypeError(f"{module_name} must export STAGE, meeting the Stage contract")
        # The Protocol check only proves ``stage`` exists, not that it is the enum: a bare string
        # would pass it and then fail on ``.value`` below with an AttributeError that says nothing.
        declared: object = getattr(exported, "stage", None)
        if not isinstance(declared, PipelineStage):
            raise TypeError(f"{module_name}: STAGE.stage must be a PipelineStage, got {declared!r}")
        if declared is not pipeline_stage:
            raise TypeError(
                f"{module_name} exports the {declared.value} stage; "
                f"a stage module is named for the stage it exports"
            )
        stages.append(exported)
    return tuple(stages)
