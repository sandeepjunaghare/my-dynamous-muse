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

    def test_agent_sdk_is_imported_only_at_its_decided_call_sites(self) -> None:
        """D1: the weekly run is a deterministic pipeline, not an agent loop.

        The SDK arrived with T12's manifest-authoring agent. It is allowed exactly where the
        architecture put it — that agent, the `classify_rollup` judgment node (T7), and
        `resolve_owner`'s node, `app/sourcing/owner/judgment.py` (T6). `app/shared/agent_reads.py`
        imports the SDK's *message types* only, to tell a verified fetch from a claimed one for all
        three; it never starts a run. An import
        anywhere else is the pipeline quietly growing an agent loop.
        """
        allowed = {
            "app/manifests/agent.py",
            "app/qualification/judgment.py",
            "app/shared/agent_reads.py",
            "app/sourcing/owner/judgment.py",
        }
        importers: set[str] = set()
        for path in _app_sources():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                modules: list[str] = []
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    modules = [node.module]
                if any(module.split(".")[0] == "claude_agent_sdk" for module in modules):
                    importers.add(str(path.relative_to(ROOT)))

        assert importers <= allowed, f"Agent SDK outside its call sites: {importers - allowed}"


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


PLACES_MODULE = "app/sourcing/sources/google_places.py"
PLACES_PRO_FIELDS = frozenset(
    {
        "places.id",
        "places.displayName",
        "places.formattedAddress",
        "places.businessStatus",
        "places.types",
    }
)
"""The Text Search Pro triggers (``places.id`` alone is Essentials). Anything else is Enterprise:
billed at the higher rate for the whole request, and content D13 forbids storing anyway."""


def _imports_from(tree: ast.AST, module: str, *, package: str) -> bool:
    """Whether ``tree``, a module in ``package``, imports ``module`` in any spelling: ``import m``,
    ``from m import x``, ``from parent import m``, ``from parent import *``, or any of those
    relative (PR #32 round-2 M2, round-3 L1).

    Known limit: an import of a package followed by attribute access (``from app.sourcing import
    sources``, then ``sources.google_places``) is not seen. Nothing in ``app/`` reaches an
    adapter that way; a test pins the limit so widening the matcher is a visible change.
    """
    parent = module.rpartition(".")[0]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name == module for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            parts = package.split(".")
            base = parts[: len(parts) - node.level + 1] if node.level else []
            source = ".".join([*base, *([node.module] if node.module else [])])
            if source == module or any(f"{source}.{alias.name}" == module for alias in node.names):
                return True
            # `google_places` has no `__all__`, so a star import of its package exposes it.
            if source == parent and any(alias.name == "*" for alias in node.names):
                return True
    return False


def _package_of(path: Path) -> str:
    """The dotted package a source file's relative imports resolve against."""
    return ".".join(path.relative_to(ROOT).parent.parts)


class TestImportsFrom:
    """The import matcher the D13 guard rests on, checked spelling by spelling: a matcher that
    misses one spelling lets a second importer through with the guard still green."""

    PLACES = "app.sourcing.sources.google_places"

    @pytest.mark.parametrize(
        ("source", "package"),
        [
            ("import app.sourcing.sources.google_places", "app.tools"),
            ("from app.sourcing.sources.google_places import PlaceResult", "app.tools"),
            ("from app.sourcing.sources import google_places", "app.tools"),
            ("from app.sourcing.sources import http, google_places as gp", "app.tools"),
            ("from .google_places import PlaceResult", "app.sourcing.sources"),
            ("from . import google_places", "app.sourcing.sources"),
            ("from .sources import google_places", "app.sourcing"),
            ("from ..sources.google_places import PlaceResult", "app.sourcing.channel"),
            ("from app.sourcing.sources import *", "app.tools"),
            ("from . import *", "app.sourcing.sources"),
        ],
    )
    def test_every_spelling_of_the_import_is_seen(self, source: str, package: str) -> None:
        assert _imports_from(ast.parse(source), self.PLACES, package=package)

    @pytest.mark.parametrize(
        ("source", "package"),
        [
            ("from app.sourcing.sources import http", "app.tools"),
            ("from app.sourcing.sources.google_places_extra import x", "app.tools"),
            ("from . import google_places", "app.tools"),
            ("import app.sourcing.sources", "app.tools"),
            ("from app.sourcing import *", "app.tools"),
        ],
    )
    def test_a_neighbouring_import_is_not_the_adapter(self, source: str, package: str) -> None:
        assert not _imports_from(ast.parse(source), self.PLACES, package=package)

    def test_attribute_access_through_a_package_is_a_known_limit(self) -> None:
        """Round-3 L1: pinned, not fixed. If the matcher learns this, flip the assertion."""
        source = "from app.sourcing import sources\nsources.google_places.PlaceResult"
        assert not _imports_from(ast.parse(source), self.PLACES, package="app.tools")


class TestPlacesIsACheckNeverASource:
    """D13 at the structure level (T6 ticket: "a structure guard should reject one").

    With the stored-row assertion in ``tests/tools/test_verify_business.py`` and the
    ``CandidateFields`` validator, this covers "no Places field other than the place ID is ever
    written to a candidate" at three layers.
    """

    def test_one_module_calls_places_and_one_stage_uses_it(self) -> None:
        """Only the adapter constructs a result or names the endpoint, and only verify_business
        imports the adapter anywhere in ``app/``: a second call site is a second way to store it
        (widened from ``app/tools/`` by PR #32 M3)."""
        callers: set[str] = set()
        importers: set[str] = set()
        for path in _app_sources():
            relative = str(path.relative_to(ROOT))
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                constructs = (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "PlaceResult"
                )
                names_url = isinstance(node, ast.Name) and node.id == "PLACES_SEARCH_URL"
                literal = (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and "places:searchText" in node.value
                )
                if constructs or names_url or literal:
                    callers.add(relative)
            if _imports_from(tree, "app.sourcing.sources.google_places", package=_package_of(path)):
                importers.add(relative)

        assert callers == {PLACES_MODULE}
        assert importers == {"app/tools/verify_business.py"}

    def test_a_stored_check_holds_the_place_id_and_our_own_address_only(self) -> None:
        """``business_check`` is the one stored Places artefact, so its shape is pinned: adding
        ``display_name`` or a Places address to it would store content D13 forbids (PR #32 M3)."""
        from app.sourcing.schemas import PlaceCheck

        assert set(PlaceCheck.model_fields) == {"place_id", "checked_address"}

    def test_a_stored_verdict_holds_a_kind_the_place_id_and_our_own_address_only(self) -> None:
        """``places_verdict`` is the second stored Places artefact (website-verification OQ-1): our
        kind, the place ID and the census address. A Places name, type or address on it would
        store content D13 forbids."""
        from app.sourcing.schemas import PlacesVerdict

        assert set(PlacesVerdict.model_fields) == {"kind", "place_id", "checked_address"}

    def test_the_field_mask_is_pro_tier_only(self) -> None:
        from app.sourcing.sources.google_places import PLACES_FIELD_MASK

        assert set(PLACES_FIELD_MASK) <= PLACES_PRO_FIELDS

    def test_places_content_has_no_home_in_a_candidate_or_a_table(self) -> None:
        """Neither in-memory Places shape is a candidate field type, and no column is named for
        Places content."""
        from app.cadence import models as cadence_models
        from app.core.database import Base
        from app.manifests import models as manifest_models
        from app.qualification import models as qualification_models
        from app.routing import models as routing_models
        from app.sourcing import models as sourcing_models
        from app.sourcing.schemas import CandidateFields

        assert all(
            (cadence_models, manifest_models, qualification_models, routing_models, sourcing_models)
        )
        for name, field in CandidateFields.model_fields.items():
            annotation = str(field.annotation)
            assert "PlacesEvidence" not in annotation, name
            assert "PlaceResult" not in annotation, name

        content_columns = {"display_name", "formatted_address", "business_status"}
        for table in Base.metadata.tables.values():
            assert not content_columns & set(table.columns.keys()), table.name
