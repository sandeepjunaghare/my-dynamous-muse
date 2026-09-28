"""Asserts that the deliberately-absent things stay absent.

Every item here was decided *out*, with a reason recorded in CLAUDE.md, the architecture doc or the
decision table in `_tasks/todo.md`. A test is the cheapest way to make "we decided not to" survive
contact with a later ticket that finds one of them convenient. If one of these should come back,
the decision gets reopened in the docs first and this file changes with it — that is the point.
"""

import ast
import re
import tomllib
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"

FORBIDDEN_DEPENDENCIES = ("redis", "langfuse", "celery")


def _all_dependencies() -> list[str]:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = pyproject["project"]
    groups = pyproject.get("dependency-groups", {})
    declared: list[str] = list(project.get("dependencies", []))
    for group in groups.values():
        declared.extend(group)
    return declared


def _app_sources() -> list[Path]:
    return sorted(APP.rglob("*.py"))


class TestAbsentDependencies:
    @pytest.mark.parametrize("package", FORBIDDEN_DEPENDENCIES)
    def test_decided_out_package_is_not_declared(self, package: str) -> None:
        """Redis, Langfuse and Celery were each decided out, not deferred.

        Redis: one user and one weekly run cannot race itself. Celery/queues: same reason.
        Langfuse: tracing a single-user weekly job may not earn its keep, and the question is
        recorded rather than guessed at.
        """
        for requirement in _all_dependencies():
            name = re.split(r"[\[<>=!~ ]", requirement, maxsplit=1)[0].lower()
            assert name != package, f"{package} was decided out — reopen the decision first"

    def test_agent_sdk_is_not_carried_unused(self) -> None:
        """Nothing in T1 calls a model; the SDK is added at first use (T6/T12)."""
        names = [re.split(r"[\[<>=!~ ]", r, maxsplit=1)[0].lower() for r in _all_dependencies()]
        assert "claude-agent-sdk" not in names


class TestAbsentServices:
    def test_compose_has_no_postgres_service(self) -> None:
        """Supabase is hosted (D11) — a local container would let a test write into real state."""
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        service_lines = re.findall(r"^  ([a-z0-9_-]+):", compose, flags=re.MULTILINE)
        assert "postgres" not in service_lines
        assert "db" not in service_lines
        assert "redis" not in service_lines

    def test_compose_runs_the_app_alone(self) -> None:
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        assert re.findall(r"^  ([a-z0-9_-]+):", compose, flags=re.MULTILINE) == ["app"]


class TestAbsentFrontend:
    def test_no_frontend_manifest(self) -> None:
        """Review happens in HubSpot at a `pending review` stage — there is no second login."""
        for manifest in ("package.json", "vite.config.ts", "vite.config.js"):
            assert not (ROOT / manifest).exists(), f"{manifest} — the frontend was decided out"


class TestZeroSuppressions:
    def test_no_type_ignore_comments(self) -> None:
        offenders = [
            f"{path.relative_to(ROOT)}:{number}"
            for path in _app_sources()
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
            if "# type: ignore" in line or "# pyright: ignore" in line
        ]
        assert not offenders, f"zero suppressions is a ground rule: {offenders}"

    def test_no_any_annotations(self) -> None:
        """`Any` must not be used as a type anywhere in `app/`.

        Checked through the AST rather than by grepping. A textual match fires on prose — this
        module's own docstrings discuss `-> Any` — and would either force the prose out or train
        everyone to ignore the test. The AST sees annotations and nothing else.
        """
        offenders: list[str] = []
        for path in _app_sources():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            annotations: list[ast.expr] = []
            for node in ast.walk(tree):
                if isinstance(node, ast.AnnAssign):
                    annotations.append(node.annotation)
                elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                    if node.returns is not None:
                        annotations.append(node.returns)
                    annotations.extend(
                        arg.annotation
                        for arg in [*node.args.args, *node.args.kwonlyargs, *node.args.posonlyargs]
                        if arg.annotation is not None
                    )
                elif isinstance(node, ast.arg) and node.annotation is not None:
                    annotations.append(node.annotation)

            for annotation in annotations:
                names = {n.id for n in ast.walk(annotation) if isinstance(n, ast.Name)}
                if "Any" in names:
                    offenders.append(f"{path.relative_to(ROOT)}:{annotation.lineno}")

        assert not offenders, f"no `Any` escape hatch: {sorted(set(offenders))}"


class TestAbsentAuth:
    def test_settings_carry_no_auth_fields(self) -> None:
        """Single internal user — no auth, no RLS. Both are Non-goals, not "later"."""
        from app.core.config import Settings

        for field in ("secret_key", "algorithm", "access_token_expire_minutes", "jwt_secret"):
            assert field not in Settings.model_fields, f"{field} implies an auth story we excluded"

    def test_no_rls_policies_in_migrations(self) -> None:
        versions = ROOT / "alembic" / "versions"
        for migration in versions.glob("*.py"):
            body = migration.read_text(encoding="utf-8").lower()
            assert "row level security" not in body
            assert "create policy" not in body


class TestStructure:
    def test_slices_are_not_scaffolded_ahead_of_their_tickets(self) -> None:
        """No package under `app/` may exist without code in it.

        The rule is *an empty slice dir is scaffolding, not design* — a directory laid out for a
        ticket nobody has started tells a later reader the slice exists when it does not.

        This asserts that rule rather than a snapshot of which slices happen to exist today. The
        snapshot version (`present == {"core", "shared"}`) went red the moment any ticket added a
        slice, which meant every parallel branch had to edit this same line and then conflict with
        its sibling on merge. Stating the intent instead means slices land without touching it.
        """
        scaffolding = [
            str(path.relative_to(ROOT))
            for path in sorted(APP.rglob("*"))
            if path.is_dir()
            and path.name != "__pycache__"
            and not [
                module
                for module in path.rglob("*.py")
                if module.name != "__init__.py" and "__pycache__" not in module.parts
            ]
        ]
        assert not scaffolding, f"empty package(s) — scaffolding, not design: {scaffolding}"

    def test_core_and_shared_exist(self) -> None:
        """The two T1 packages every slice builds on."""
        for package in ("core", "shared"):
            assert (APP / package / "__init__.py").exists(), f"app/{package}/ is missing"

    def test_migrations_have_exactly_one_head(self) -> None:
        """Alembic must never grow a second head.

        Two branches that each add a migration will both set ``down_revision`` to whatever was tip
        when they forked. Git merges that cleanly — the files do not overlap — and the damage only
        surfaces at ``alembic upgrade head``, which refuses to pick between two heads. That is a
        deploy-time failure caused by a merge-time mistake, so it is caught here instead.

        On failure: rebase the later migration's ``down_revision`` onto the other head. Reach for
        ``alembic merge`` only if both have already been applied somewhere real.
        """
        script = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
        heads = script.get_heads()
        assert len(heads) == 1, (
            f"{len(heads)} migration heads: {heads} — rebase the later migration's "
            "down_revision onto the other head so the chain stays linear"
        )

    def test_provenance_does_not_depend_on_core(self) -> None:
        """`shared/provenance.py` is pure domain vocabulary — it imports nothing from `core/`.

        That independence is what let Phase 3 be built in parallel with Phase 2, and what keeps the
        primitive consumable by a slice that has no application context at all.
        """
        tree = ast.parse((APP / "shared" / "provenance.py").read_text(encoding="utf-8"))
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)

        assert not [m for m in imported if m.startswith("app.core")], imported

    def test_no_branch_on_the_vertical_name(self) -> None:
        """Vertical is data, not code — nothing in `app/` may branch on a vertical's name.

        The moment `if vertical == "freight"` exists, M9 ("vertical #2 in under a founder-day") is
        quietly impossible: adding a vertical becomes editing slices instead of writing a row.

        AST, not grep, for the reason `test_no_any_annotations` gives: a textual match fires on
        this project's own prose about the rule, and a guard that cries wolf gets muted. Comparing
        two *variables* (`manifest.vertical == vertical`) is legitimate and must not fire — only a
        comparison against a literal is the thing the rule forbids.
        """

        def _is_vertical(node: ast.expr) -> bool:
            if isinstance(node, ast.Name):
                return node.id == "vertical"
            return isinstance(node, ast.Attribute) and node.attr == "vertical"

        def _is_string_literal(node: ast.expr) -> bool:
            if isinstance(node, ast.Constant):
                return isinstance(node.value, str)
            if isinstance(node, ast.Tuple | ast.List | ast.Set):
                return bool(node.elts) and all(_is_string_literal(item) for item in node.elts)
            return False

        offenders: list[str] = []
        for path in _app_sources():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Compare):
                    sides = [node.left, *node.comparators]
                    vertical_side = any(_is_vertical(side) for side in sides)
                    literal_side = any(_is_string_literal(side) for side in sides)
                    if vertical_side and literal_side:
                        offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
                elif isinstance(node, ast.Match) and _is_vertical(node.subject):
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")

        assert not offenders, f"vertical is data, not code: {sorted(set(offenders))}"
