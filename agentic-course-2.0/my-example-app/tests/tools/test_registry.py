"""The stage registry: a stage registers by being a file named for its ``PipelineStage``.

Each case builds a throwaway package on ``sys.path`` rather than touching ``app/tools/``, so the
tests describe the contract and stay true as T5 to T8 add the real stage modules.
"""

import importlib
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from uuid import uuid4

import pytest

from app.sourcing.stages import PipelineStage
from app.tools.registry import registered_stages

type PackageFactory = Callable[[dict[str, str]], str]
"""Builds a package of the given ``{module: source}`` files and returns its importable name."""

_STAGE_MODULE = """
from app.sourcing.stages import PipelineStage
from app.tools.registry import StageContext, StageResult


class _Stage:
    stage = PipelineStage.{stage}

    async def run(self, context: StageContext) -> StageResult:
        return StageResult(counts={{}})


STAGE = _Stage()
"""


@pytest.fixture
def make_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[PackageFactory]:
    """A factory for a uniquely named package of stage modules, importable for the test only."""
    monkeypatch.setattr(sys, "path", [str(tmp_path), *sys.path])
    created: list[str] = []

    def factory(modules: dict[str, str]) -> str:
        name = f"stages_{uuid4().hex}"
        package = tmp_path / name
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        for module, source in modules.items():
            (package / f"{module}.py").write_text(source, encoding="utf-8")
        created.append(name)
        importlib.invalidate_caches()
        return name

    yield factory

    for name in created:
        for module in [m for m in sys.modules if m == name or m.startswith(f"{name}.")]:
            del sys.modules[module]


def _stage_source(stage: PipelineStage) -> str:
    return _STAGE_MODULE.format(stage=stage.value)


def test_app_tools_registers_only_built_stages() -> None:
    """The real package: whatever is registered is a known stage, in pipeline order."""
    stages = [registered.stage for registered in registered_stages()]
    assert stages == sorted(stages, key=list(PipelineStage).index)


def test_a_package_with_no_stage_modules_registers_nothing(make_package: PackageFactory) -> None:
    assert registered_stages(make_package({})) == ()


def test_stages_come_back_in_pipeline_order_not_file_order(make_package: PackageFactory) -> None:
    """Order is the pipeline's, from ``PipelineStage``, whatever order the files were written in."""
    package = make_package(
        {
            "cluster_routes": _stage_source(PipelineStage.cluster_routes),
            "search_registry": _stage_source(PipelineStage.search_registry),
            "classify_rollup": _stage_source(PipelineStage.classify_rollup),
        }
    )

    assert [registered.stage for registered in registered_stages(package)] == [
        PipelineStage.search_registry,
        PipelineStage.classify_rollup,
        PipelineStage.cluster_routes,
    ]


def test_a_module_that_exports_no_stage_is_refused(make_package: PackageFactory) -> None:
    """A file named for a stage that forgot ``STAGE`` would otherwise silently skip that stage."""
    package = make_package({"cluster_routes": "VALUE = 1\n"})

    with pytest.raises(TypeError, match="cluster_routes"):
        registered_stages(package)


def test_a_module_whose_stage_claims_another_name_is_refused(
    make_package: PackageFactory,
) -> None:
    """``cluster_routes.py`` exporting the ``classify_rollup`` stage would run a stage twice."""
    package = make_package({"cluster_routes": _stage_source(PipelineStage.classify_rollup)})

    with pytest.raises(TypeError, match="classify_rollup"):
        registered_stages(package)


def test_an_import_error_inside_a_stage_module_is_not_swallowed(
    make_package: PackageFactory,
) -> None:
    """A stage that exists but cannot import is broken, not absent, and must not be skipped."""
    package = make_package({"search_registry": "import a_module_that_does_not_exist\n"})

    with pytest.raises(ModuleNotFoundError, match="a_module_that_does_not_exist"):
        registered_stages(package)


def test_unrelated_modules_in_the_package_are_ignored(make_package: PackageFactory) -> None:
    """Helpers beside the stages, such as the registry itself, are not stages."""
    package = make_package(
        {
            "registry": "VALUE = 1\n",
            "search_registry": _stage_source(PipelineStage.search_registry),
        }
    )

    assert [registered.stage for registered in registered_stages(package)] == [
        PipelineStage.search_registry,
    ]
