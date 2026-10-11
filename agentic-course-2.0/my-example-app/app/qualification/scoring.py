"""The priority score: Intensity (1-5) x Automatable (1-5) = 1-25 (E9), from cited answers only.

Pure. An axis's score is the rounded mean (half up) of the admitted signal answers that feed it, and
the axis a signal feeds is read from the manifest, not from what the model said. If either axis has
no admitted answer there is **no score** — an absent priority is honest, a guessed one is E18 again.
"""

from collections.abc import Sequence
from decimal import ROUND_HALF_UP, Decimal

from app.core.logging import get_logger
from app.manifests.schemas import ManifestBody, ScoreAxis
from app.qualification.schemas import AdmittedSignal
from app.sourcing.schemas import PriorityScore

logger = get_logger(__name__)


def _rounded_mean(scores: Sequence[int]) -> int:
    mean = Decimal(sum(scores)) / Decimal(len(scores))
    return int(mean.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def score(body: ManifestBody, signals: Sequence[AdmittedSignal]) -> PriorityScore | None:
    """The priority score these admitted answers support, or ``None`` if an axis has none."""
    feeds = {cited.value.id: cited.value.feeds for cited in body.qualifying_signals}
    by_axis: dict[ScoreAxis, list[int]] = {axis: [] for axis in ScoreAxis}
    for signal in signals:
        axis = feeds.get(signal.signal_id)
        if axis is None:
            logger.warning("qualification.scoring.signal_ignored", signal_id=signal.signal_id)
            continue
        by_axis[axis].append(signal.axis_score)

    missing = [axis.value for axis, scores in by_axis.items() if not scores]
    if missing:
        logger.info("qualification.scoring.score_absent", missing_axes=missing)
        return None
    return PriorityScore(
        intensity=_rounded_mean(by_axis[ScoreAxis.intensity]),
        automatable=_rounded_mean(by_axis[ScoreAxis.automatable]),
    )
