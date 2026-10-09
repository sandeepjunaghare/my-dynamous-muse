"""The CLI is the review surface, so what it prints and what it exits with are the contract.

Three properties: ``show`` renders **every** citation and says plainly when terms are undecided;
a deliberate failure is one readable line and exit 1, never a traceback; and ``activate`` is the
only thing that flips a row to ACTIVE.

These tests are synchronous because ``lpe`` runs its own ``asyncio.run`` — see
``tests/manifests/conftest.py`` for why they commit their fixtures instead of using ``db_session``.
"""

from uuid import UUID, uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from app.cli import main
from app.manifests.schemas import ManifestResponse, ManifestStatus, TermsDecision
from app.manifests.service import ManifestService
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
            main(["manifest", "frobnicate"])
        assert exc_info.value.code == 2

    def test_propose_is_offered(self, capsys: pytest.CaptureFixture[str]) -> None:
        """T12 shipped `propose`; it belongs in --help now that it does something."""
        with pytest.raises(SystemExit):
            main(["manifest", "--help"])
        assert "propose" in capsys.readouterr().out

    def test_propose_without_a_brief_exits_two(self) -> None:
        with pytest.raises(SystemExit) as exc_info:
            main(["manifest", "propose"])
        assert exc_info.value.code == 2

    def test_propose_refuses_a_vertical_that_is_not_a_slug(self) -> None:
        with pytest.raises(SystemExit) as exc_info:
            main(["manifest", "propose", "collision centers", "--vertical", "Collision Centers"])
        assert exc_info.value.code == 2


class TestDatabaseRefusals:
    def test_an_integrity_error_is_one_line_not_a_traceback(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """ "Never a traceback" is unconditional, so it must hold for unmodelled refusals too.

        Found in the PR #6 review: the CLI caught only `LocalProspectEngineError`, so a losing
        activation race — the partial unique index refusing a second ACTIVE row — escaped as a
        raw Python traceback, against this CLI's own stated contract.

        No database needed: the refusal is injected at the service boundary, which is where a real
        one would surface from.
        """

        def refuse(*args: object, **kwargs: object) -> ManifestResponse:
            raise IntegrityError("INSERT INTO vertical_manifest ...", {}, Exception("duplicate"))

        monkeypatch.setattr(ManifestService, "activate", refuse)

        exit_code = main(
            ["manifest", "activate", str(uuid4()), "--accept-terms", "fmcsa"],
        )

        captured = capsys.readouterr()
        assert exit_code == 1
        assert "Traceback" not in captured.err
        assert "error: the database refused the write" in captured.err
        # The exception's own text is a multi-line SQL dump — it must not be what gets printed.
        assert "INSERT INTO" not in captured.err

    def test_the_refusal_message_names_no_command_or_table(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The handler covers every slice's commands, so its wording must fit all of them.

        Found in the PR #6 round-2 review. The first version of this message named *activation* and
        advised `manifest show <id>` — correct for the only write path that existed, wrong for the
        next one. `create_draft` reaches a different constraint through a racy select-max-then-
        insert, and T12's `propose` calls it: that caller would have been told an activation landed
        first, and pointed at an id it does not have.
        """

        def refuse(*args: object, **kwargs: object) -> ManifestResponse:
            raise IntegrityError("INSERT INTO vertical_manifest ...", {}, Exception("duplicate"))

        monkeypatch.setattr(ManifestService, "activate", refuse)
        main(["manifest", "activate", str(uuid4()), "--accept-terms", "fmcsa"])

        # Only the error line, not the whole stream: stderr is also where the CLI's logs go, and a
        # future log field would otherwise fail this test for a reason it is not about.
        message = next(
            line for line in capsys.readouterr().err.splitlines() if line.startswith("error:")
        )
        for command_specific in ("activation", "activate", "manifest show", "<id>", "vertical"):
            assert command_specific not in message, (
                f"{command_specific!r} makes this message wrong for some other command that "
                "reaches the same handler"
            )


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
