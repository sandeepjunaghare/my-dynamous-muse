# `app/manifests/` — the vertical manifest slice

**Everything that differs between one vertical and the next is a row in `vertical_manifest`, never a
branch in a slice.** A manifest declares, for one vertical: the authoritative source(s) to search, the
rules that disqualify a candidate, the signals that qualify one, the ICP headcount band, and the
vocabulary to speak in. The five pipeline stages read it; none of them knows what "freight" means.

That is the M9 lever — *vertical #2 in under a founder-day, no rebuild*. `tests/test_structure.py::
test_no_branch_on_the_vertical_name` is what keeps it true: an `if vertical == "freight"` anywhere in
`app/` fails the build.

## The shape of a row

Identity and lifecycle are **columns** (`vertical`, `version`, `status`) because that is what the
database indexes and constrains. The researched content is one JSONB **`body`**, whose shape
`ManifestBody` owns, because the manifest is always read whole and nothing queries inside it.

Every researched field in the body is wrapped in `ProvenancedValue[T]` — source URL, retrieval
timestamp, retrieval method — so a manifest field nobody can cite cannot be represented. Everything
inside a citation is immutable (tuples, frozen models); the primitive rejects anything else at
construction.

## Lifecycle

```
          create_draft                 activate --accept-terms <every source>
  (none) ──────────────▶ DRAFT ──────────────────────────────────────────▶ ACTIVE
                          ▲                                                  │
                          │                        a later version activates │
                    T12's agent                                              ▼
                     proposes                                           SUPERSEDED
```

- **DRAFT** — proposed. Never returned by `get_active()`; the pipeline must not source against a
  proposal nobody accepted.
- **ACTIVE** — a human accepted it, and a terms-of-use decision exists for **every** declared source.
- **SUPERSEDED** — replaced by a later version. The row is retained, never overwritten.

Two guards, deliberately doubled:

| Guard | Where | What it is for |
|---|---|---|
| terms recorded for every source | `ManifestService.activate` | the good error message, naming what is missing |
| at most one ACTIVE per vertical | partial unique index in `0002` | still true when a future slice writes by a path nobody anticipated |

This is the same argument provenance already makes: a check can be forgotten by a new slice; a
constraint cannot be written around.

## Terms of use sit outside the citations

A citation says *when and how a value was obtained*. `activate` does not re-obtain the source
declaration — it records what a human **decided** about it. If the decision lived inside the cited
value, activation would have to mint a fresh `ProvenancedValue`, and the manifest would then claim
FMCSA's base URL was "retrieved" at the moment someone typed `--accept-terms`. So `SourceTerms` is a
sibling of the cited sources, with `decided_by` / `decided_at` as its own provenance.

Undecided is the **absence** of a record, not a third enum member. A `rejected` decision blocks
activation exactly as a missing one does — the question was asked and the answer was no.

## The review surface

The CLI is the whole review UI. No frontend, no second login.

```bash
uv run lpe manifest list [--vertical freight] [--status draft]
uv run lpe manifest propose "collision centers, DFW"   # the authoring agent writes a cited DRAFT
uv run lpe manifest show <id>                          # every field with its citation
uv run lpe manifest activate <id> --accept-terms fmcsa,places [--actor you]
```

## The authoring agent (T12)

`propose` is the one place an agent drives control flow at run time. Three modules, one boundary each:

| Module | Job |
|---|---|
| `agent.py` | The Agent SDK loop. Two tools only (`WebSearch`, `WebFetch`), no filesystem settings, `dontAsk`. Records which pages were **actually fetched**, and the run's cost via `RunCost`. Never writes. |
| `proposal.py` | The structured-output contract and `build_draft` — the citation gate. Pure. |
| `prompts.py` | The system prompt. Names no vertical. |

**The citation gate.** A proposed field is written only if its citation URL is a page the agent
fetched successfully in this run; it is stored as `llm_inference` with that URL and the time the
fetch result was observed. "Successfully" means proven: the CLI's structured result reports a 2xx
status for the host that was requested. The CLI returns an HTTP 403/404 or a cross-host redirect as an
ordinary result, not a tool error, so anything short of that proof (an error status, a redirect, a
missing or unattributable status) is not a read. Neither is a search-result snippet or a remembered
URL. That field is left **absent** and listed under "left out" with the reason, and so is a field whose
citation URL does not parse. If a field the
manifest cannot exist without (a source, the ICP band, the vocabulary) does not survive, nothing is
written and the command exits 1.

**Terms of use are raised, never answered.** The agent may point at where a source publishes its
terms; it has no field in which to judge them. The draft's `terms` is always empty, and `propose`
prints one question per source for the person who will run `activate`.

**Quality review is still open.** Nothing checks that a proposed disqualifier is right or that the
proposed source is the authoritative one (architecture → *Open questions*). `propose` says so on
every run, and the activator is the reviewer until that question is decided.

**Cost is logged on every path.** It is recorded the moment the result arrives, so a run that hits
the turn or budget cap (after which the SDK raises `ResultError`) still logs its spend, and the CLI
names the cap that stopped it.

**Tests never call a model.** `tests/manifests/replay.py` feeds raw CLI stream-json through the SDK's
real `query()` and message parser via a fake `Transport`, and raises after an error result as the
real CLI exit does. `fixtures/freight_proposal_transcript.json` is the acceptance fixture; replace it
with the first live run's recording.

Exit codes: `0` success · `1` a deliberate failure, printed as one line, never a traceback · `2`
argparse's own usage error.

## Conventions this slice sets for the ones after it

T2 is the first feature slice, so its shape is the house pattern whether or not anyone intends it.
Three decisions worth copying deliberately:

1. **The repository flushes; the service commits.** A repository method may write and flush so that
   defaults land and constraints fire early, but the transaction boundary belongs to the service —
   the only layer that knows whether an operation is finished. Superseding and activating happen in
   one transaction for exactly this reason.
2. **Slice exceptions derive from `LocalProspectEngineError` and are never caught in handlers.**
   `app/main.py` renders any of them as structured JSON at the exception's own status code; a local
   `try/except` would produce a second, inconsistent error shape.
3. **Tests live in `tests/manifests/`**, mirroring `app/`, and database-backed tests carry
   `requires_db` so the suite stays runnable with no infrastructure.

## Adding a vertical

See `.claude/references/adding-a-vertical.md`. In short: a manifest row, not a feature. If a new
source seems to need its own pipeline stage, the schema is missing a field — extend the schema, not
the pipeline.

## Deliberately absent

- **A rule evaluator.** `DisqualifierRule` is a declarative shape; **T7** runs it. There is no
  `matches()` here, and the rule schema deliberately resists growing into a DSL: `RuleKind.judgment`
  is the escape hatch for anything that needs real judgment, which is a `classify_rollup` call.
- **Write routes.** Creation is T12's; activation is the CLI's. A `POST /manifests` would be a second
  door into ACTIVE that bypasses the gate.
