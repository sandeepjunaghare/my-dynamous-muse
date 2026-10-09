"""``lpe manifest propose`` — the agent's proposal becomes one DRAFT row, and nothing more.

The agent is replayed from the recorded freight transcript by replacing ``query`` on the agent
module, so the CLI runs its real path — settings, options, the citation gate, ``create_draft`` —
without a model call.
"""

import pytest
from pydantic import JsonValue

from app.cli import main
from app.manifests import agent as agent_module
from app.manifests.cli import QUALITY_REVIEW_NOTE
from app.manifests.schemas import ManifestResponse, ManifestStatus
from app.manifests.service import ManifestService
from tests.conftest import requires_db
from tests.manifests.conftest import load_committed_rows
from tests.manifests.replay import Replay, load_transcript, with_result


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

    def test_a_failed_agent_run_exits_one(
        self,
        monkeypatch: pytest.MonkeyPatch,
        no_writes: list[str],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        messages = with_result(None, subtype="error_max_turns", is_error=True)
        monkeypatch.setattr(agent_module, "query", Replay(messages).as_query())

        assert main(["manifest", "propose", "freight"]) == 1
        err = capsys.readouterr().err
        assert "error: the authoring agent's run failed (error_max_turns)" in err
        assert no_writes == []
