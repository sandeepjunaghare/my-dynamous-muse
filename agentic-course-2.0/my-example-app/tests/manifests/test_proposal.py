"""The citation gate: every field the draft carries points at a page that was read, or is absent.

Pure tests — no SDK, no database. The proposal is either the recorded freight one or a minimal one
built here with exactly one thing changed.
"""

from datetime import UTC, datetime

import pytest

from app.manifests.exceptions import ManifestProposalIncompleteError
from app.manifests.proposal import (
    AgentProposal,
    Citation,
    ProposedIcpBand,
    ProposedRule,
    ProposedSource,
    ProposedVocabulary,
    build_draft,
)
from app.manifests.schemas import RuleKind, RuleOperator, SourceKind
from app.shared.page_reads import PageRead, normalize_url
from app.shared.provenance import RetrievalMethod
from tests.manifests.replay import load_structured_output

READ_AT = datetime(2026, 10, 8, 9, 30, tzinfo=UTC)
REGISTRY = "https://registry.example.gov/licensees"
OTHER = "https://directory.example.com/listing"


def _read(*urls: str) -> list[PageRead]:
    return [PageRead(url=url, read_at=READ_AT) for url in urls]


def _cite(url: str = REGISTRY) -> Citation:
    return Citation(url=url, quote="a supporting passage")


def _proposal(**changes: object) -> AgentProposal:
    """A minimal proposal in which every field cites REGISTRY."""
    base = AgentProposal(
        vertical="widgets",
        sources=[
            ProposedSource(
                name="registry",
                kind=SourceKind.registry_api,
                description="the regulator's licensee search",
                base_url=REGISTRY,
                citation=_cite(),
            )
        ],
        icp_band=ProposedIcpBand(
            headcount_min=10, headcount_max=50, requires_office_function=True, citation=_cite()
        ),
        vocabulary=ProposedVocabulary(terms=["work orders"], citation=_cite()),
    )
    return base.model_copy(update=changes)


class TestRecordedFreightProposal:
    @pytest.fixture
    def proposal(self) -> AgentProposal:
        return AgentProposal.model_validate(load_structured_output())

    @pytest.fixture
    def reads(self) -> list[PageRead]:
        """What the recorded run fetched successfully — the 403 on freightcaviar is not here."""
        return _read(
            "https://www.fmcsa.dot.gov/registration/get-mc-number-authority-operate",
            "https://ai.fmcsa.dot.gov/SMS/Tools/Downloads.aspx",
            "https://safer.fmcsa.dot.gov/CompanySnapshot.aspx",
            "https://www.bls.gov/ooh/office-and-administrative-support/cargo-and-freight-agents.htm",
        )

    def test_names_fmcsa_and_excludes_asset_based_carriers(
        self, proposal: AgentProposal, reads: list[PageRead]
    ) -> None:
        draft = build_draft(proposal, reads)

        assert draft.body.source_names() == ("fmcsa",)
        rules = {rule.value.id: rule.value for rule in draft.body.disqualifier_rules}
        assert rules["asset_based_carrier"].kind is RuleKind.predicate
        assert rules["asset_based_carrier"].operator is RuleOperator.greater_than

    def test_every_field_cites_a_page_that_was_read(
        self, proposal: AgentProposal, reads: list[PageRead]
    ) -> None:
        draft = build_draft(proposal, reads)
        body = draft.body
        read_urls = {read.url for read in reads}

        provenance = [(c.source_url, c.retrieval_method, c.retrieved_at) for c in body.sources]
        provenance += [
            (c.source_url, c.retrieval_method, c.retrieved_at) for c in body.disqualifier_rules
        ]
        provenance += [
            (c.source_url, c.retrieval_method, c.retrieved_at) for c in body.qualifying_signals
        ]
        provenance += [
            (body.icp_band.source_url, body.icp_band.retrieval_method, body.icp_band.retrieved_at),
            (
                body.vocabulary.source_url,
                body.vocabulary.retrieval_method,
                body.vocabulary.retrieved_at,
            ),
        ]
        for source_url, method, retrieved_at in provenance:
            assert source_url in read_urls
            assert method is RetrievalMethod.llm_inference
            assert retrieved_at == READ_AT

    def test_uncitable_fields_are_absent_and_explained(
        self, proposal: AgentProposal, reads: list[PageRead]
    ) -> None:
        draft = build_draft(proposal, reads)
        omitted = {entry.label: entry.reason for entry in draft.omitted}

        assert "never successfully fetched" in omitted["sources[dat_directory]"]  # search only
        assert omitted["disqualifier_rules[freight_forwarder_only]"] == "no citation offered"
        assert "manifest schema" in omitted["disqualifier_rules[lapsed_authority]"]
        assert "never successfully fetched" in omitted["qualifying_signals[tms_and_api]"]  # 403

        kept_ids = {rule.value.id for rule in draft.body.disqualifier_rules}
        assert kept_ids == {"asset_based_carrier", "double_brokering_risk"}
        assert [s.value.id for s in draft.body.qualifying_signals] == ["manual_check_calls"]

    def test_terms_are_raised_never_answered(
        self, proposal: AgentProposal, reads: list[PageRead]
    ) -> None:
        draft = build_draft(proposal, reads)

        assert draft.body.terms == ()
        assert draft.body.undecided_sources() == ("fmcsa",)
        assert [q.source_name for q in draft.terms_questions] == ["fmcsa"]
        assert draft.terms_questions[0].terms_url == "https://www.fmcsa.dot.gov/policies"

    def test_the_reviewer_sees_the_quoted_evidence(
        self, proposal: AgentProposal, reads: list[PageRead]
    ) -> None:
        draft = build_draft(proposal, reads)
        labels = {field.label for field in draft.cited}
        assert "disqualifier_rules[asset_based_carrier]" in labels
        assert all(field.quote for field in draft.cited)


class TestTheGate:
    def test_a_fully_cited_proposal_becomes_a_draft(self) -> None:
        draft = build_draft(_proposal(), _read(REGISTRY))
        assert draft.vertical == "widgets"
        assert draft.omitted == ()

    def test_a_citation_to_an_unread_page_is_dropped(self) -> None:
        proposal = _proposal(
            disqualifier_rules=[
                ProposedRule(
                    id="rollup", kind=RuleKind.judgment, description="d", citation=_cite(OTHER)
                )
            ]
        )
        draft = build_draft(proposal, _read(REGISTRY))
        assert draft.body.disqualifier_rules == ()
        assert draft.omitted[0].label == "disqualifier_rules[rollup]"
        assert (
            draft.omitted[0].reason == f"cites {OTHER}, which the agent never successfully fetched"
        )

    @pytest.mark.parametrize("bad_url", ["http://[x", "https://[::1/registry"])
    def test_a_malformed_citation_url_is_omitted_not_fatal(self, bad_url: str) -> None:
        """`urlsplit` raises on these; one bad field must not cost the whole paid-for proposal."""
        proposal = _proposal(
            disqualifier_rules=[
                ProposedRule(
                    id="rollup", kind=RuleKind.judgment, description="d", citation=_cite(bad_url)
                )
            ]
        )
        draft = build_draft(proposal, _read(REGISTRY))
        assert draft.body.disqualifier_rules == ()
        assert draft.omitted[0].label == "disqualifier_rules[rollup]"
        assert draft.omitted[0].reason == f"citation URL {bad_url!r} is not parseable"

    def test_an_unparseable_read_is_skipped_not_fatal(self) -> None:
        draft = build_draft(_proposal(), _read("http://[x", REGISTRY))
        assert draft.omitted == ()

    def test_the_stored_url_is_the_one_fetched(self) -> None:
        """A cited URL differing only by slash or fragment matches; the fetched spelling is kept."""
        proposal = _proposal(
            vocabulary=ProposedVocabulary(terms=["w"], citation=_cite(f"{REGISTRY}/#terms"))
        )
        draft = build_draft(proposal, _read(REGISTRY))
        assert draft.body.vocabulary.source_url == REGISTRY

    def test_the_first_read_of_a_page_is_the_one_cited(self) -> None:
        later = datetime(2026, 10, 8, 10, 0, tzinfo=UTC)
        reads = [*_read(REGISTRY), PageRead(url=REGISTRY, read_at=later)]
        assert build_draft(_proposal(), reads).body.icp_band.retrieved_at == READ_AT

    def test_a_duplicate_rule_id_keeps_the_first(self) -> None:
        rule = ProposedRule(id="dup", kind=RuleKind.judgment, description="d", citation=_cite())
        draft = build_draft(_proposal(disqualifier_rules=[rule, rule]), _read(REGISTRY))
        assert len(draft.body.disqualifier_rules) == 1
        assert draft.omitted[0].reason == "duplicate rule id"

    def test_an_invalid_source_slug_is_omitted_not_fatal_when_another_survives(self) -> None:
        bad = ProposedSource(
            name="Not A Slug",
            kind=SourceKind.web_lookup,
            description="d",
            base_url=OTHER,
            citation=_cite(),
        )
        proposal = _proposal()
        proposal = proposal.model_copy(update={"sources": [*proposal.sources, bad]})
        draft = build_draft(proposal, _read(REGISTRY))
        assert draft.body.source_names() == ("registry",)
        assert draft.omitted[0].label == "sources[Not A Slug]"
        assert draft.omitted[0].reason.startswith("refused by the manifest schema:")

    def test_the_vertical_override_wins(self) -> None:
        assert build_draft(_proposal(), _read(REGISTRY), vertical_override="gadgets").vertical == (
            "gadgets"
        )


class TestNothingIsWrittenWithoutTheRequiredFields:
    def test_no_page_read_means_no_draft(self) -> None:
        with pytest.raises(ManifestProposalIncompleteError) as exc_info:
            build_draft(_proposal(), [])
        assert exc_info.value.missing_fields == ("sources", "icp_band", "vocabulary")

    def test_an_uncited_icp_band_refuses_the_draft(self) -> None:
        band = ProposedIcpBand(headcount_min=1, headcount_max=5, requires_office_function=False)
        with pytest.raises(ManifestProposalIncompleteError) as exc_info:
            build_draft(_proposal(icp_band=band), _read(REGISTRY))
        assert exc_info.value.missing_fields == ("icp_band",)
        assert "icp_band: no citation offered" in exc_info.value.message

    def test_a_missing_vocabulary_refuses_the_draft(self) -> None:
        with pytest.raises(ManifestProposalIncompleteError) as exc_info:
            build_draft(_proposal(vocabulary=None), _read(REGISTRY))
        assert exc_info.value.missing_fields == ("vocabulary",)

    def test_an_invalid_vertical_slug_is_refused(self) -> None:
        with pytest.raises(ManifestProposalIncompleteError) as exc_info:
            build_draft(_proposal(vertical="Collision Centers"), _read(REGISTRY))
        assert exc_info.value.missing_fields == ("vertical",)

    def test_a_vertical_slug_too_long_for_its_column_is_refused(self) -> None:
        """Refused before the write, not as a database error after the run was paid for."""
        with pytest.raises(ManifestProposalIncompleteError) as exc_info:
            build_draft(_proposal(vertical="w" * 65), _read(REGISTRY))
        assert exc_info.value.missing_fields == ("vertical",)
        assert build_draft(_proposal(vertical="w" * 64), _read(REGISTRY)).vertical == "w" * 64


class TestNormalizeUrl:
    @pytest.mark.parametrize(
        ("left", "right"),
        [
            ("https://a.gov/x", "https://a.gov/x/"),
            ("https://A.gov/x", "https://a.gov/x"),
            ("https://a.gov/x#part", "https://a.gov/x"),
            (" https://a.gov/x ", "https://a.gov/x"),
        ],
    )
    def test_spellings_of_one_page_match(self, left: str, right: str) -> None:
        assert normalize_url(left) == normalize_url(right)

    def test_a_query_string_names_a_different_page(self) -> None:
        assert normalize_url("https://a.gov/x?id=1") != normalize_url("https://a.gov/x?id=2")
