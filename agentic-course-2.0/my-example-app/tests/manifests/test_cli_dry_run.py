"""``lpe manifest dry-run`` and the ``activate`` gate behind it, from the surface a person uses.

Synchronous and committed, like the rest of the CLI suite (see ``tests/manifests/conftest.py``).
"""

from pathlib import Path
from uuid import UUID

import pytest

from app.cli import main
from app.manifests.schemas import ManifestStatus
from tests.conftest import requires_db
from tests.manifests.conftest import load_committed_body

EXTRACT = Path(__file__).parents[1] / "qualification" / "fixtures" / "census_extract.csv"


@requires_db
class TestTheGate:
    def test_activate_refuses_a_manifest_never_dry_run(
        self,
        committed_draft_without_dry_run: tuple[UUID, str],
        cli_database: str,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        manifest_id, _ = committed_draft_without_dry_run

        exit_code = main(
            ["manifest", "activate", str(manifest_id), "--accept-terms", "fmcsa,places"]
        )

        assert exit_code == 1
        err = capsys.readouterr().err
        assert "has never been dry-run" in err
        assert "lpe manifest dry-run" in err
        assert "Traceback" not in err
        status, _ = load_committed_body(cli_database, manifest_id)
        assert status == ManifestStatus.draft.value

    def test_a_dry_run_unblocks_activation(
        self,
        committed_draft_without_dry_run: tuple[UUID, str],
        cli_database: str,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        manifest_id, _ = committed_draft_without_dry_run

        dry = main(["manifest", "dry-run", str(manifest_id), "--source-file", f"fmcsa={EXTRACT}"])
        out = capsys.readouterr().out

        assert dry == 0
        assert "fmcsa: census_extract.csv, 30 rows (row id: dot_number)" in out
        assert "30 rows in, 30 left after the rules" in out
        assert "FLAGGED: nothing" in out
        assert "recorded dry-run" in out
        assert str(EXTRACT.parent) not in out

        activated = main(
            ["manifest", "activate", str(manifest_id), "--accept-terms", "fmcsa,places"]
        )
        assert activated == 0
        status, _ = load_committed_body(cli_database, manifest_id)
        assert status == ManifestStatus.active.value


@requires_db
class TestRefusals:
    def test_an_undeclared_source_is_one_line_and_exit_one(
        self,
        committed_draft_without_dry_run: tuple[UUID, str],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        manifest_id, _ = committed_draft_without_dry_run
        exit_code = main(
            ["manifest", "dry-run", str(manifest_id), "--source-file", f"socrata={EXTRACT}"]
        )
        assert exit_code == 1
        err = capsys.readouterr().err
        assert "declares no source 'socrata'" in err
        assert "Traceback" not in err

    def test_a_source_given_twice(
        self,
        committed_draft_without_dry_run: tuple[UUID, str],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        manifest_id, _ = committed_draft_without_dry_run
        exit_code = main(
            [
                "manifest",
                "dry-run",
                str(manifest_id),
                "--source-file",
                f"fmcsa={EXTRACT}",
                "--source-file",
                f"fmcsa={EXTRACT}",
            ]
        )
        assert exit_code == 1
        assert "more than one --source-file for: fmcsa" in capsys.readouterr().err


class TestUsage:
    @pytest.mark.parametrize("value", ["census.csv", "=x.csv", "fmcsa=/no/such/file.csv"])
    def test_a_malformed_or_missing_source_file_is_a_usage_error(self, value: str) -> None:
        with pytest.raises(SystemExit) as exc_info:
            main(["manifest", "dry-run", str(UUID(int=1)), "--source-file", value])
        assert exc_info.value.code == 2
