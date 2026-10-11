"""``disqualification``'s constraints, against a real Postgres."""

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.qualification.models import Disqualification
from tests.conftest import requires_db
from tests.qualification.builders import ROLLUP_RULE, a_body_with, a_run_with

pytestmark = requires_db


class TestDisqualificationConstraints:
    async def test_a_candidate_from_another_run_is_refused(self, db_session: AsyncSession) -> None:
        # Issue #20: a run's disqualified candidates are read back by run_id, so a row pairing run
        # B with run A's candidate would drop that candidate from both runs' answers.
        manifest, _, (candidate,) = await a_run_with(db_session, a_body_with(), "1000001")
        _, other_run, _ = await a_run_with(db_session, a_body_with())
        db_session.add(
            Disqualification(
                candidate_id=candidate.id,
                run_id=other_run.id,
                manifest_id=manifest.id,
                vertical=manifest.vertical,
                registry_id=candidate.registry_id,
                rule_id=ROLLUP_RULE,
                rule_kind="judgment",
                source_url="https://example.gov/evidence",
                retrieval_method="web_lookup",
                evidence=None,
            )
        )

        with pytest.raises(IntegrityError, match="fk_disqualification_run_candidate"):
            await db_session.flush()
