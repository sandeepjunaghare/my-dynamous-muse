"""Field ownership, without a database: one owner per field, and every writer names its stage."""

import inspect

import pytest

from app.sourcing.repository import SourcingRepository
from app.sourcing.schemas import CandidateFields
from app.sourcing.service import SourcingService
from app.sourcing.stages import FIELD_OWNERS, PipelineStage, owns


class TestFieldOwners:
    def test_every_candidate_field_has_an_owner(self) -> None:
        """Adding a field to ``CandidateFields`` without naming its owner fails here."""
        assert set(FIELD_OWNERS) == set(CandidateFields.model_fields)

    def test_each_field_has_exactly_one_owner(self) -> None:
        for field in CandidateFields.model_fields:
            assert sum(owns(stage, field) for stage in PipelineStage) == 1, field

    def test_an_unknown_field_is_owned_by_no_stage(self) -> None:
        """Conservative: an undeclared field can be filled by anyone and overwritten by no one."""
        assert not any(owns(stage, "owner_name") for stage in PipelineStage)

    @pytest.mark.parametrize("field", ["registry_id", "legal_name", "dba_name", "address", "phone"])
    def test_the_registry_owns_identity_and_contact(self, field: str) -> None:
        """D13: the census is the source of address and phone; nothing overwrites it."""
        assert owns(PipelineStage.search_registry, field)

    @pytest.mark.parametrize("field", ["website", "business_check"])
    def test_verification_owns_the_check_and_the_website(self, field: str) -> None:
        assert owns(PipelineStage.verify_business, field)

    def test_the_stages_are_the_architecture_docs_five_in_order(self) -> None:
        assert [stage.value for stage in PipelineStage] == [
            "search_registry",
            "verify_business",
            "resolve_owner",
            "classify_rollup",
            "cluster_routes",
        ]


class TestTheWritingStageIsRequired:
    @pytest.mark.parametrize(
        "method",
        [SourcingRepository.upsert_candidate, SourcingService.record_candidates],
        ids=["repository.upsert_candidate", "service.record_candidates"],
    )
    def test_stage_is_keyword_only_with_no_default(self, method: object) -> None:
        """A caller cannot forget to say which stage is writing: omitting it is a TypeError."""
        assert callable(method)
        stage = inspect.signature(method).parameters["stage"]
        assert stage.kind is inspect.Parameter.KEYWORD_ONLY
        assert stage.default is inspect.Parameter.empty
        assert stage.annotation in (PipelineStage, "PipelineStage")
