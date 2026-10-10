"""``app/tools/search_registry.py`` is registered by being a file (the seam PR's contract).

Its behaviour is tested in ``tests/sourcing/`` — the pipeline acceptance tests and portability —
where the database fixtures live.
"""

from app.sourcing.stages import PipelineStage
from app.tools import search_registry
from app.tools.registry import registered_stages
from app.tools.search_registry import SearchRegistryStage


class TestRegistration:
    def test_the_real_package_registers_search_registry(self) -> None:
        """Membership, not equality: T6, T7 and T8 add their stages beside it."""
        stages = registered_stages()
        assert search_registry.STAGE in stages
        assert stages[0].stage is PipelineStage.search_registry

    def test_the_exported_stage_reads_fmcsa_by_default(self) -> None:
        assert isinstance(search_registry.STAGE, SearchRegistryStage)
        assert search_registry.STAGE.stage is PipelineStage.search_registry
