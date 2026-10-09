"""``lpe manifest propose`` — the agent's proposal becomes one DRAFT row, and nothing more.

The agent is replayed from the recorded freight transcript by replacing ``query`` on the agent
module, so the CLI runs its real path — settings, options, the citation gate, ``create_draft`` —
without a model call.
"""

from decimal import Decimal

import pytest
from pydantic import JsonValue

from app.cli import main
from app.core.cost import BillableKind, RunCost
from app.manifests import agent as agent_module
from app.manifests.cli import QUALITY_REVIEW_NOTE
from app.manifests.schemas import ManifestResponse, ManifestStatus
from app.manifests.service import ManifestService
from tests.conftest import requires_db
from tests.manifests.conftest import load_committed_rows
from tests.manifests.replay import Replay, load_structured_output, load_transcript, with_result


@pytest.fixture
def replayed_agent(monkeypatch: pytest.MonkeyPatch) -> Replay:
    replay = Replay(load_transcript())
    monkeypatch.setattr(agent_module, "query", replay.as_query())
    return replay


@requires_db
class TestProposeWritesADraft:
    def test_one_draft_is_written_with_every_field_cited(
        self,
        replayed_agent: Replay,
        throwaway_vertical: str,
        cli_database: str,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        exit_code = main(
            ["manifest", "propose", "freight brokerages, DFW", "--vertical", throwaway_vertical]
        )

        assert exit_code == 0
        rows = load_committed_rows(cli_database, throwaway_vertical)
        assert len(rows) == 1
        status, body = rows[0]
        assert status == ManifestStatus.draft.value  # never ACTIVE
        assert body.source_names() == ("fmcsa",)
        assert body.terms == ()

    def test_the_output_is_a_review(
        self,
        replayed_agent: Replay,
        throwaway_vertical: str,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        main(["manifest", "propose", "freight brokerages, DFW", "--vertical", throwaway_vertical])
        out = capsys.readouterr().out

        assert f"{throwaway_vertical} v1  [draft]" in out
        assert "cited: https://safer.fmcsa.dot.gov/CompanySnapshot.aspx" in out
        assert "llm_inference" in out
        assert "fmcsa: UNDECIDED — blocks activation" in out
        assert "sources[dat_directory]: cites https://www.dat.com/load-boards" in out
        assert "https://www.fmcsa.dot.gov/policies" in out  # where to read the terms
        assert "$0.8421" in out
        assert QUALITY_REVIEW_NOTE in out

    def test_an_unreported_cost_is_shown_as_unknown_not_zero(
        self,
        monkeypatch: pytest.MonkeyPatch,
        throwaway_vertical: str,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        stream = with_result(load_structured_output(), total_cost_usd=None)
        monkeypatch.setattr(agent_module, "query", Replay(stream).as_query())

        assert main(["manifest", "propose", "freight", "--vertical", throwaway_vertical]) == 0
        out = capsys.readouterr().out
        assert "agent run cost: unknown" in out
        assert "$0 " not in out

    def test_a_second_proposal_is_a_new_draft_version(
        self, replayed_agent: Replay, throwaway_vertical: str, cli_database: str
    ) -> None:
        argv = ["manifest", "propose", "freight", "--vertical", throwaway_vertical]
        assert main(argv) == 0
        assert main(argv) == 0
        statuses = [status for status, _ in load_committed_rows(cli_database, throwaway_vertical)]
        assert statuses == [ManifestStatus.draft.value, ManifestStatus.draft.value]


class TestProposeWritesNothingOnFailure:
    """No database needed: each failure happens before a session is opened."""

    @pytest.fixture
    def no_writes(self, monkeypatch: pytest.MonkeyPatch) -> list[str]:
        calls: list[str] = []

        async def spy(self: ManifestService, vertical: str, body: object) -> ManifestResponse:
            calls.append(vertical)
            raise AssertionError("create_draft must not be reached")

        monkeypatch.setattr(ManifestService, "create_draft", spy)
        return calls

    def test_an_uncitable_required_field_exits_one(
        self,
        monkeypatch: pytest.MonkeyPatch,
        no_writes: list[str],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        proposal: JsonValue = {
            "vertical": "freight",
            "sources": [],
            "icp_band": None,
            "vocabulary": None,
        }
        monkeypatch.setattr(agent_module, "query", Replay(with_result(proposal)).as_query())

        assert main(["manifest", "propose", "freight"]) == 1
        err = capsys.readouterr().err
        assert "error: no draft written" in err
        assert "Traceback" not in err
        assert no_writes == []

    @pytest.mark.parametrize(
        ("subtype", "cap_text"),
        [
            ("error_max_turns", "error: the authoring agent hit its turn cap (60 turns)"),
            ("error_max_budget_usd", "error: the authoring agent hit its budget cap ($5.00)"),
        ],
    )
    def test_a_capped_run_exits_one_and_says_so(
        self,
        subtype: str,
        cap_text: str,
        monkeypatch: pytest.MonkeyPatch,
        no_writes: list[str],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The replay raises `ResultError` after the result, as the SDK does after a capped run."""
        messages = with_result(None, subtype=subtype, is_error=True)
        monkeypatch.setattr(agent_module, "query", Replay(messages).as_query())

        recorded: list[Decimal | None] = []
        real_record = RunCost.record

        def spy(
            self: RunCost, kind: BillableKind, count: int = 1, usd: Decimal | None = None
        ) -> None:
            recorded.append(usd)
            real_record(self, kind, count, usd)

        monkeypatch.setattr(RunCost, "record", spy)

        assert main(["manifest", "propose", "freight"]) == 1
        err = capsys.readouterr().err
        assert cap_text in err
        assert "having spent $0.84" in err
        assert "could not run" not in err  # not the generic SDK failure
        assert recorded == [Decimal("0.8421")]  # the spend goes through RunCost on this path too
        assert "Traceback" not in err
        assert no_writes == []
