"""The M6 accuracy bar for the ``classify_rollup`` node, against the real model. **Paid; opt-in.**

``LPE_LIVE_EVAL=1 uv run pytest tests/qualification/test_live_eval.py -s`` — about $0.08 a company.
Never part of ``/piv-validate``. The replay suite proves the wiring; only this proves the judgment:

* the four known rollups (E7: Impact Fire, Summit Fire, Century Fire, Control Systems) fire the
  rollup rule, with a citation that survives the gate;
* fewer than 5% of known-local companies do (M6). That half needs ≥20 labelled locals in
  ``fixtures/rollup_eval_set.json``, which a founder supplies; until then it skips.

Decided 2026-10-10: replay offline plus this opt-in eval.
"""

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import BaseModel

from app.core.cost import RunCost, format_usd
from app.manifests.schemas import ScoreAxis, SourceKind
from app.qualification.judgment import admit_judgment, evidence_reads, run_judgment
from app.shared.provenance import ProvenancedValue, RetrievalMethod
from app.sourcing.schemas import CandidateFields
from tests.qualification.builders import ROLLUP_RULE, a_body_with, judgment, signal

EVAL_SET = Path(__file__).parent / "fixtures" / "rollup_eval_set.json"
MIN_LOCALS = 20
MAX_FALSE_POSITIVE_RATE = 0.05

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.getenv("LPE_LIVE_EVAL") != "1", reason="paid live eval; set LPE_LIVE_EVAL=1 to run"
    ),
]

FIRE_BODY = a_body_with(
    judgment(
        ROLLUP_RULE,
        "National rollups with no local owner cannot buy a $999 assessment locally (E7). The call "
        "needs judgment about ownership structure.",
    ),
    sources=(("tx_fire_marshal", SourceKind.registry_api),),
    signals=(signal("owner_accessible", ScoreAxis.intensity),),
)


class _Company(BaseModel):
    legal_name: str
    city: str
    state: str


class _EvalSet(BaseModel):
    rollups: list[_Company]
    locals: list[_Company]


def _eval_set() -> _EvalSet:
    return _EvalSet.model_validate(json.loads(EVAL_SET.read_text(encoding="utf-8")))


def _candidate(index: int, company: _Company) -> CandidateFields:
    """The company as a labelled record — cited to the eval set, never to the web."""

    def labelled(value: str) -> ProvenancedValue[str]:
        return ProvenancedValue(
            value=value,
            source_url=f"file:{EVAL_SET.name}",
            retrieved_at=datetime(2026, 10, 10, tzinfo=UTC),
            retrieval_method=RetrievalMethod.manual_research,
        )

    return CandidateFields(
        registry_id=labelled(f"eval-{index}"),
        legal_name=labelled(f"{company.legal_name} — {company.city}, {company.state}"),
    )


async def _fires(index: int, company: _Company, cost: RunCost) -> bool:
    fields = _candidate(index, company)
    run = await run_judgment(fields, FIRE_BODY, cost=cost)
    admitted = admit_judgment(
        run.answer, FIRE_BODY, reads=run.reads, evidence=evidence_reads(fields)
    )
    fired = any(rule.rule_id == ROLLUP_RULE for rule in admitted.fired)
    print(f"  {company.legal_name}: {'ROLLUP' if fired else 'local'} ({len(run.reads)} reads)")
    return fired


async def test_the_four_known_rollups_fire() -> None:
    cost = RunCost()
    rollups = _eval_set().rollups
    results = [await _fires(i, company, cost) for i, company in enumerate(rollups)]
    print(f"  cost: {format_usd(cost.total_usd())}")
    assert all(results), [c.legal_name for c, hit in zip(rollups, results, strict=True) if not hit]


async def test_false_positives_on_known_locals_are_under_five_percent() -> None:
    locals_ = _eval_set().locals
    if len(locals_) < MIN_LOCALS:
        pytest.skip(f"labelled locals not supplied ({len(locals_)} of {MIN_LOCALS} needed)")
    cost = RunCost()
    results = [await _fires(i, company, cost) for i, company in enumerate(locals_)]
    rate = sum(results) / len(results)
    print(f"  false positives: {sum(results)}/{len(results)}; cost: {format_usd(cost.total_usd())}")
    assert rate < MAX_FALSE_POSITIVE_RATE
