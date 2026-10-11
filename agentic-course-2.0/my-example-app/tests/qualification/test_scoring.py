"""The priority score from admitted signal answers: both axes cited, or no score at all."""

from datetime import UTC, datetime

from app.manifests.schemas import ScoreAxis
from app.qualification.schemas import AdmittedSignal
from app.qualification.scoring import score
from app.shared.provenance import ProvenancedValue, RetrievalMethod
from app.sourcing.schemas import ScoreBand
from tests.qualification.builders import a_body_with, signal

BODY = a_body_with(
    signals=(
        signal("compliance_load", ScoreAxis.intensity),
        signal("manual_dispatch", ScoreAxis.intensity),
        signal("tms_and_api", ScoreAxis.automatable),
    )
)


def _answer(
    signal_id: str, axis_score: int, axis: ScoreAxis = ScoreAxis.intensity
) -> AdmittedSignal:
    return AdmittedSignal(
        signal_id=signal_id,
        axis=axis,
        axis_score=axis_score,
        answer=ProvenancedValue(
            value="yes",
            source_url="https://example.test/about",
            retrieved_at=datetime(2026, 10, 10, tzinfo=UTC),
            retrieval_method=RetrievalMethod.llm_inference,
        ),
    )


class TestScore:
    def test_both_axes_cited_gives_the_product(self) -> None:
        result = score(BODY, [_answer("compliance_load", 4), _answer("tms_and_api", 5)])
        assert result is not None
        assert (result.intensity, result.automatable, result.score) == (4, 5, 20)
        assert result.band is ScoreBand.live

    def test_an_axis_without_a_cited_answer_gives_no_score(self) -> None:
        """Freight v3 may declare no intensity signal; then it scores nothing, honestly."""
        assert score(BODY, [_answer("tms_and_api", 5)]) is None
        assert score(BODY, []) is None

    def test_an_axis_is_the_mean_rounded_half_up(self) -> None:
        answers = [_answer("compliance_load", 4), _answer("manual_dispatch", 5)]
        result = score(BODY, [*answers, _answer("tms_and_api", 2)])
        assert result is not None
        assert result.intensity == 5

    def test_the_axis_comes_from_the_manifest_not_the_answer(self) -> None:
        """A model that files an automatable signal under intensity does not move the score."""
        mislabelled = _answer("tms_and_api", 3, axis=ScoreAxis.intensity)
        result = score(BODY, [_answer("compliance_load", 3), mislabelled])
        assert result is not None
        assert (result.intensity, result.automatable) == (3, 3)

    def test_an_unknown_signal_is_ignored(self) -> None:
        result = score(
            BODY, [_answer("compliance_load", 2), _answer("tms_and_api", 4), _answer("made_up", 5)]
        )
        assert result is not None
        assert result.score == 8
        assert result.band is ScoreBand.dead
