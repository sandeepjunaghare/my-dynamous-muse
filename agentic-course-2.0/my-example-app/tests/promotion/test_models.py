"""The ledger's CHECK constraints name every status the code can write, and nothing else."""

import re

from app.promotion.models import STATUS_IN_SET
from app.promotion.schemas import PromotionStatus


def test_the_status_check_is_exactly_the_enum() -> None:
    """A status added to the enum but not the CHECK is a runtime IntegrityError; the reverse is a
    state no code can reach. Either way the two must move together."""
    assert set(re.findall(r"'([a-z_]+)'", STATUS_IN_SET)) == {s.value for s in PromotionStatus}
