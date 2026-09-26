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

The agent loop calls **five generic tools, all parameterized by the manifest**: search a declared registry ·
verify a business identity · resolve the owner · classify rollup-vs-local · cluster routes. Adding a source
means adding a manifest entry, not a sixth tool and not a tool per source — "fewer, smarter tools", inherited
from the `base-research-agent` template. If a new source seems to need its own tool, that is a signal the
manifest schema is missing a field; extend the schema rather than the toolchain.

Known gaps to expect here: the manifest schema itself does not exist yet, nor does the rollup-vs-local
classifier (E7's rule currently lives in a founder's head and a hand-written skip list). Owner resolution is
the hardest problem in the system and the one M6 measures — 41% of sourced contacts currently have no person
identified at all (E18).
