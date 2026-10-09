# `app/sourcing/` — runs, and the candidates they find

**A run is one execution of a brief; a candidate is one business it found, every field cited.** This
slice is the sourcing workbench in Supabase: machine state, never human-edited. Nothing here reaches
HubSpot. A candidate gets there only once it qualifies *and* a human accepts it (T9).

T4 built the data model and the run lifecycle. The pipeline that drives it is T5's. So are the stages
and the source adapters.

## The two tables

| Table | Columns (keyed, constrained, joined) | JSONB (read whole) |
|---|---|---|
| `sourcing_run` | `id` · `manifest_id` → `vertical_manifest` · `vertical` · `geography` · `status` · `status_detail` · `started_at` · `finished_at` | `icp_band` · `counts` · `cost` |
| `candidate` | `id` · `run_id` → `sourcing_run` · `registry_id` · `created_at` · `updated_at` | `fields` |

- **The brief** is `vertical` · `icp_band` · `geography` (`SourcingBrief`).
- **`manifest_id`** names the exact manifest *version* the run sourced under. Without it, a rule id
  in a later `disqualification` row (T7) does not say which rule it was.
- **`counts`** is `{stage_name: n}`. Each stage ticket adds its own key, so nobody edits this table.
- **`cost`** is a snapshot of the run's `RunCost`: calls and USD for every `BillableKind`. USD is
  stored as a string, which keeps it exact. The total is computed, not stored.

## Lifecycle

```
start_run ──▶ RUNNING ──finish_run──▶ COMPLETED | DEGRADED | FAILED
                 │
         record_candidates   (idempotent; refused once the run has finished)
```

- `start_run` resolves the vertical's **ACTIVE** manifest and refuses with
  `ActiveManifestNotFoundError` when only drafts exist. Sourcing against a proposal nobody accepted
  would hit sources whose terms of use nobody decided.
- `degraded` is for a run that finished short of its goal, for example when the 500-call Places
  breaker trips (T6). It is not a failure: the run keeps what it already verified.
- `RunOutcome` holds only the terminal statuses, so finishing a run "as running" is a type error.
- `ck_sourcing_run_finished_at_matches_status` means a running run has no finish time and a
  finished run always has one.

## Every field cited, at rest

`CandidateFields` wraps each field in `ProvenancedValue[T]`. Only `registry_id` is required; every
other field is `ProvenancedValue[T] | None`.

**"Unprovenanced" means absent.** There is no slot for a raw value without a citation. That is the
state T1's primitive made unrepresentable, and T6's rule is that a field nobody can cite is *left
absent, not inferred*. So a candidate with a missing phone is **storable but not promotable**:

```python
candidate.fields.phone  # None
is_promotable(candidate.fields.phone)  # False — HubSpot may not receive it
candidate.fields.unprovenanced_fields()  # ("phone", "website")
```

T9 decides which fields promotion requires. This slice only reports which fields are missing.

`registry_id` is stored twice, as the key column and as the cited `fields.registry_id`.
`ck_candidate_registry_id_is_cited` stops the two from ever disagreeing, and refuses a row whose
registry id is missing, JSON `null` or uncited (no source URL, retrieval time or method).

**The rest of the guarantee is held by types, not by the database.** Only `registry_id` is checked in
SQL. Write candidates through `SourcingService` / `SourcingRepository` and nothing else. A `Candidate`
built directly through the ORM can store a raw, uncited value, and that one row then makes
`list_candidates` raise `ValidationError` for the whole run.

## The upsert: keyed on `(run_id, registry_id)`, merged by field ownership

`upsert_candidate(run_id, fields, *, stage)` is `INSERT … ON CONFLICT (run_id, registry_id) DO UPDATE`.
`stage` is required, and names the pipeline stage doing the writing.

**Each field has exactly one owning stage** (`stages.py`, `FIELD_OWNERS`):

| Field | Owner |
|---|---|
| `registry_id` · `legal_name` · `dba_name` | `search_registry` |
| `address` · `phone` · `website` | `verify_business` |

`resolve_owner`, `classify_rollup` and `cluster_routes` own none yet. T6's owner fields add their rows.

- The **owning** stage overwrites its own field. A retried stage re-decides its own fact.
- **Any other** stage only *fills* a field that is empty. It never replaces a stored citation, so a
  retried registry stage cannot downgrade a Places-verified phone.
- A field the incoming write does not carry **keeps its citation**. Absent fields are dumped as absent
  keys (`exclude_none=True`), never as JSON `null`, and that is what makes the merge safe.
- In SQL the stored value becomes `fill || existing || owned`: last key wins, so owned fields beat what
  is stored, and what is stored beats fill-only fields.
- The registry id is stripped and capped at 128 characters by `CandidateFields`, so `" 555 "` and
  `"555"` are one row, and an over-long id fails validation before the batch starts.
- The same input twice leaves the same row with the same content.
- The same registry id in two runs gives two rows: each run keeps its own record of what it saw.
- `populate_existing=True` refreshes an object the session already holds. Without it, the second
  upsert returns the stale first object. A test proves this by failing when the option is removed.

## Adding a field

Add it to `CandidateFields` as `ProvenancedValue[T] | None = None`. No migration is needed, because
`fields` is JSONB. Rows written earlier load with the new field absent, which is accurate. A frozen
model is required for any structured value, since `ProvenancedValue` refuses to cite anything
editable.

## Deliberately absent

- **Pipeline, stages, source adapters.** These are T5's (`app/tools/`, `sources/fmcsa`).
- **Enrichment fields** (owner, headcount band, years in business) belong to T6 and need no
  migration.
- **`disqualification` and an index on `registry_id` alone.** These are T7's, which adds the index
  together with the cross-run query that uses it.
- **Routes and CLI.** No reader needs one yet; T10's report is the first.
- **A run-lock** (one running run per vertical). A run-lock is the stated trigger for reopening the
  Redis decision, so it is not this slice's to add quietly.

## Cross-slice reads

- **Reads** `manifests`: `ManifestService.get_active` and the `IcpBand` type. It never writes there.
- **Read by** T7 (FKs `disqualification` → `candidate`), T8 (reads `fields.address`) and T9 (FKs the
  promotion ledger → `candidate` and gates every field on `is_promotable`).
