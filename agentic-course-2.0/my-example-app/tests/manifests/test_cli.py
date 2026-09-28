"""The CLI is the review surface, so what it prints and what it exits with are the contract.

Three properties: ``show`` renders **every** citation and says plainly when terms are undecided;
a deliberate failure is one readable line and exit 1, never a traceback; and ``activate`` is the
only thing that flips a row to ACTIVE.

These tests are synchronous because ``lpe`` runs its own ``asyncio.run`` — see
``tests/manifests/conftest.py`` for why they commit their fixtures instead of using ``db_session``.
"""

from uuid import UUID, uuid4

import pytest

from app.cli import main
from app.manifests.schemas import ManifestStatus, TermsDecision
from tests.conftest import requires_db
from tests.manifests.conftest import load_committed_body


class TestUsageErrors:
    def test_activate_without_accept_terms_exits_two(self) -> None:
        """argparse's own usage error. Nobody activates anything by forgetting the flag."""
        with pytest.raises(SystemExit) as exc_info:
            main(["manifest", "activate", str(uuid4())])
        assert exc_info.value.code == 2

    def test_an_unknown_command_exits_two(self) -> None:
        with pytest.raises(SystemExit) as exc_info:
            main(["manifest", "propose", "collision centers, DFW"])
        assert exc_info.value.code == 2

    def test_propose_is_not_offered(self, capsys: pytest.CaptureFixture[str]) -> None:
        """T12 owns `propose`; an empty command in --help reads as a broken feature."""
        with pytest.raises(SystemExit):
            main(["manifest", "--help"])
        assert "propose" not in capsys.readouterr().out


@requires_db
class TestShow:
    def test_it_prints_every_citation_and_the_undecided_terms(
        self,
        committed_draft: tuple[UUID, str],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        manifest_id, vertical = committed_draft

        assert main(["manifest", "show", str(manifest_id)]) == 0

        out = capsys.readouterr().out
        assert vertical in out
        # One citation line per cited field: two sources, one rule, one signal, band, vocabulary.
        assert out.count("cited:") == 6
        assert "manual_research" in out
        assert "fmcsa: UNDECIDED — blocks activation" in out
        assert "places: UNDECIDED — blocks activation" in out

    def test_an_unknown_id_is_one_line_and_exit_one(
        self,
        cli_database: str,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        assert main(["manifest", "show", str(uuid4())]) == 1

        captured = capsys.readouterr()
        assert captured.err.strip().splitlines()[-1].startswith("error: no manifest with id")
        assert "Traceback" not in captured.err


@requires_db
class TestActivate:
    def test_a_missing_terms_decision_exits_one_without_a_traceback(
        self,
        committed_draft: tuple[UUID, str],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The gate, from the surface a person actually uses."""
        manifest_id, _ = committed_draft

        assert main(["manifest", "activate", str(manifest_id), "--accept-terms", "fmcsa"]) == 1

        captured = capsys.readouterr()
        assert "error: cannot activate without a terms-of-use decision" in captured.err
        assert "places" in captured.err
        assert "Traceback" not in captured.err

    def test_accepting_every_source_flips_the_row(
        self,
        committed_draft: tuple[UUID, str],
        cli_database: str,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        manifest_id, _ = committed_draft

        exit_code = main(
            [
                "manifest",
                "activate",
                str(manifest_id),
                "--accept-terms",
                "fmcsa, places,",
                "--actor",
                "sandeep",
            ]
        )

        assert exit_code == 0
        assert "activated" in capsys.readouterr().out

        status, body = load_committed_body(cli_database, manifest_id)
        assert status == ManifestStatus.active.value
        assert body.undecided_sources() == ()
        assert {entry.decision for entry in body.terms} == {TermsDecision.accepted}
        assert {entry.decided_by for entry in body.terms} == {"sandeep"}


@requires_db
class TestList:
    def test_it_shows_the_row_with_its_sources(
        self,
        committed_draft: tuple[UUID, str],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _, vertical = committed_draft

        assert main(["manifest", "list", "--vertical", vertical]) == 0

        out = capsys.readouterr().out
        assert vertical in out
        assert "fmcsa, places" in out
        assert "draft" in out
