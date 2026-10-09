# Adding a vertical

Load this when adding or changing a vertical, a source registry, or a sourcing tool.

M9 is the test: vertical #2 in under a founder-day, no rebuild. That only holds if **vertical is data**.
Everything that differs between verticals lives in a versioned `vertical_manifest` row — the authoritative
source(s), the disqualifier rules, the qualifying signals, the ICP band, and the vocabulary. Nothing about a
vertical belongs in a feature slice, a branch on the vertical name, or a hardcoded source client. Freight's
manifest names FMCSA/SAFER, asset-based-carrier exclusion, "which TMS and does it have an API", and freight
vocabulary; Fire's names the Texas Fire Marshal registry, rollup exclusion, and inspection language. Same
code, different rows. Each source also carries its own terms of use — record that decision in the manifest
before first use (FMCSA, Google Places and any state registry each have their own).

**Google Places is a check, never a source** (D13: the Maps Platform Terms forbid storing its content). A new
vertical must name a **registry** to find candidates in: a licensing board, the Secretary of State, or a
federal dataset. "Search Places for X in DFW" is not a source; saving what it finds is an index, which the
terms ban. Places only confirms a record the registry already gave us, within the run, and only its place ID
is kept.

The weekly run is a **deterministic pipeline of five generic stages, all parameterized by the manifest**:
search a declared registry · verify a business identity · resolve the owner · classify rollup-vs-local ·
cluster routes. Two of those five need judgment and call the Agent SDK with structured output and a citation
(`resolve_owner`, `classify_rollup`); the other three are ordinary HTTP and arithmetic. Adding a source means
adding a manifest entry, not a sixth stage and not a stage per source. If a new source seems to need its own
stage, that is a signal the manifest schema is missing a field; extend the schema rather than the pipeline.

**Free before paid.** The registry pull and the free disqualifier rules run first; the Google
Places check is billed per request. Filtering first costs a little recall and saves most of the spend, and paid
calls are capped per run (500 Places calls) as a circuit breaker, not a budget target.

**Authoring a manifest is the one genuinely agentic job.** `app/manifests/` carries an agent that takes a
brief ("collision centers, DFW"), researches the authoritative registry, the disqualifiers, the ICP band and
the vocabulary, and writes a **DRAFT** `vertical_manifest` row with every proposal cited. A human activates
it on the CLI:

```
uv run lpe manifest propose "collision centers, DFW"
uv run lpe manifest show <id>
uv run lpe manifest activate <id> --accept-terms fmcsa,places
```

`activate` is where the per-source terms-of-use decision is recorded — the gate that already existed is the
human approval step, so there is no second review surface to build. A source with no recorded terms-of-use
decision cannot be marked active.

Known gaps to expect here: the manifest schema itself does not exist yet, nor does the rollup-vs-local
classifier (E7's rule currently lives in a founder's head and a hand-written skip list). Owner resolution is
the hardest problem in the system and the one M6 measures — 41% of sourced contacts currently have no person
identified at all (E18).
