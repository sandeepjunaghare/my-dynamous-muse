# `app/routing/` — drive routes from verified addresses

**Stage 5 of 5 (`cluster_routes`, T8).** It takes a run's candidates that have a verified address,
geocodes each census address, and groups the doors into drive routes of at most `ROUTE_MAX_DOORS`
(default 12). That is the Monday-afternoon job in E4: "Two geographic clusters, 20 names." It is
deterministic geometry with no model. The same candidates give the same routes, in any order.

The stage module is `app/tools/cluster_routes.py`, and the work lives here.

## Who gets routed

**Only candidates with a verified address**, by `CandidateFields.has_verified_address()`: the census
address **and** a `business_check`. A candidate without one is counted and never geocoded, because a
door placed from a guess sends a founder to the wrong street.

> **Follow-up for T9/T10.** T8 routes every candidate with a verified address, disqualified or not.
> T7's `disqualification` table was built in parallel, so this slice does not read it (decided
> 2026-10-10). Before routes reach a founder, T9/T10 must narrow them to candidates that qualified.
> They must also decide whether routing re-runs after that. Re-running is cheap, because the stage
> replaces its rows.

## Where the coordinates come from (D13)

From the **US Census Geocoder** (free, public domain, no key), one address per request:
`/geocoder/locations/address`, benchmark `Public_AR_Current`. **Never from Google Places.** Places
lat/lng may be cached for only 30 days and is barred as input to point-in-polygon analysis.

- **Exactly one match** is a point, cited `web_lookup` to the request URL. Census `x` is longitude
  and `y` is latitude.
- **No match** is *unmatched*, and **two or more** is *ambiguous*. Both are left out and counted. We
  never pick one of several matches.
- **A geocode failing** for one address (unreachable after 3 tries, or an unreadable body) leaves
  that candidate out as `routing_geocode_failed`. If **every** address fails, for any mix of those
  reasons, the stage raises **before writing anything**. A geocoder outage cannot pass as a quiet
  week, and a failed re-run keeps the run's earlier routes.

## The algorithm (`clustering.py`)

The algorithm makes k = ⌈n / max_doors⌉ routes by **recursive balanced median bisection**. Each step
splits the points across the wider of their two spreads, on a local km projection centred on the
points. The left half gets ⌈k/2⌉ routes' share, and the algorithm recurses into each half.

- **No route exceeds the cap**, by construction. The two-line proof is in the module docstring.
- **Sizes are balanced.** 20 → 10 + 10, and 23 → 12 + 11.
- **Stable.** The input is sorted by registry id first, and every later sort breaks ties on it.
- **Labels** run `A`, `B`, … from west to east, then south to north, by centroid. Each label adds the
  route's most common 5-digit ZIP: `A · 76011`, `B · 75238`. A tie goes to the lowest ZIP.

Why not k-means or grouping by ZIP: see the plan's *NOTES* (`.claude/plans/t8-route-clustering.md`).

## The table

| `route_assignment` | |
|---|---|
| `run_id` → `sourcing_run` · `candidate_id` → `candidate` (unique) | which candidate, which run |
| `cluster_index` (≥ 0) · `cluster_label` | which route |
| `point` (JSONB) | the cited Census `GeoPoint` |

One row per clustered candidate. A re-run of the stage **replaces** the run's rows: delete, then
insert, in the service's one commit. Routing writes only this table. `cluster_routes` owns no
candidate field (`app/sourcing/stages.py`).

## Counts it reports

The stage reports these to `sourcing_run.counts`, every key every time:
- `routing_eligible`, `routing_unverified_address`
- `routing_geocode_unmatched`, `routing_geocode_ambiguous`, `routing_geocode_failed`
- `routing_clustered`, `routing_clusters`
