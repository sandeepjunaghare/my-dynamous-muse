"""Drive-route clustering: deterministic geometry, no model, no randomness.

**Recursive balanced median bisection.** With ``k = ceil(n / max_doors)`` routes to make, split the
points across the wider of their two spreads (east-west or north-south), at the rank that gives the
left half ``ceil(k / 2)`` routes' share, and recurse into each half. Splitting the wider axis keeps
routes roughly square rather than slivers, which is what makes one an outing.

**No route exceeds the cap, by construction.** At every split ``n <= k * max_doors``. The left half
takes ``n_left = ceil(n * k_left / k)`` points, and since ``k_left * max_doors`` is a whole number,
``n_left <= k_left * max_doors``. The right half takes the rest, at most ``n * k_right / k <=
k_right * max_doors``. Each half therefore meets the same condition, down to ``k == 1``.

**Same input set, same routes, in any order.** The input is sorted by registry id before anything
else, and every later sort breaks ties on it. Nothing iterates a set or a dict into the output, and
no metro is hard-coded: the projection is centred on the points themselves.

Why not k-means: it needs a seed to be repeatable, still depends on input order, and does not
respect a size cap without a constrained variant. Why not group by ZIP: one ZIP can hold thirty
doors. Plan → *NOTES*.
"""

import math
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from app.routing.schemas import RouteCluster, RoutePoint

_KM_PER_DEGREE_LATITUDE = 110.574
_KM_PER_DEGREE_LONGITUDE_AT_EQUATOR = 111.320


@dataclass(frozen=True)
class _Projected:
    """A route point on a local plane, in km, so the two axes' spreads are comparable."""

    x: float
    y: float
    item: RoutePoint


def cluster_points(points: Sequence[RoutePoint], *, max_doors: int) -> tuple[RouteCluster, ...]:
    """Group ``points`` into the fewest routes of at most ``max_doors`` each, labelled A, B, …

    Routes are ordered west to east (then south to north) by centroid, and each member list is in
    registry-id order. An empty input yields no routes.
    """
    if max_doors < 1:
        raise ValueError("max_doors must be at least 1")
    if not points:
        return ()
    if len({point.candidate_id for point in points}) != len(points):
        raise ValueError("each candidate may appear in routing only once")

    ordered = sorted(points, key=lambda point: (point.registry_id, str(point.candidate_id)))
    projected = _project(ordered)
    route_count = math.ceil(len(projected) / max_doors)
    groups = _bisect(projected, route_count)

    def centroid(group: list[_Projected]) -> tuple[float, float]:
        # Rounded so float noise in the last bits cannot reorder two routes between runs.
        return (
            round(sum(p.x for p in group) / len(group), 6),
            round(sum(p.y for p in group) / len(group), 6),
        )

    groups.sort(key=lambda group: (centroid(group), group[0].item.registry_id))

    clusters: list[RouteCluster] = []
    for index, group in enumerate(groups):
        members = tuple(sorted((p.item for p in group), key=lambda m: m.registry_id))
        anchor = _anchor_postal_code(members)
        clusters.append(
            RouteCluster(
                index=index,
                label=f"{_letter(index)} · {anchor}",
                anchor_postal_code=anchor,
                members=members,
            )
        )
    return tuple(clusters)


def _project(points: Sequence[RoutePoint]) -> list[_Projected]:
    """An equirectangular projection centred on the points' mean latitude.

    Accurate to well under 1% across a metro, and the algorithm only compares spreads and ranks.
    """
    mean_latitude = sum(p.point.latitude for p in points) / len(points)
    km_per_degree_longitude = _KM_PER_DEGREE_LONGITUDE_AT_EQUATOR * math.cos(
        math.radians(mean_latitude)
    )
    return [
        _Projected(
            x=p.point.longitude * km_per_degree_longitude,
            y=p.point.latitude * _KM_PER_DEGREE_LATITUDE,
            item=p,
        )
        for p in points
    ]


def _bisect(items: list[_Projected], route_count: int) -> list[list[_Projected]]:
    """Split ``items`` into ``route_count`` groups by recursive median cuts on the wider axis."""
    if route_count == 1:
        return [items]

    x_spread = max(p.x for p in items) - min(p.x for p in items)
    y_spread = max(p.y for p in items) - min(p.y for p in items)
    along_x = x_spread >= y_spread
    ranked = sorted(items, key=lambda p: (p.x if along_x else p.y, p.item.registry_id))

    left_routes = math.ceil(route_count / 2)
    total = len(ranked)
    left_size = (total * left_routes + route_count - 1) // route_count
    return _bisect(ranked[:left_size], left_routes) + _bisect(
        ranked[left_size:], route_count - left_routes
    )


def _anchor_postal_code(members: Sequence[RoutePoint]) -> str:
    """The route's most common 5-digit ZIP; on a tie, the lowest."""
    counts = Counter(m.postal_code.strip()[:5] for m in members)
    return min(counts, key=lambda code: (-counts[code], code))


def _letter(index: int) -> str:
    """``0 → A``, ``25 → Z``, ``26 → AA``: spreadsheet column letters."""
    letters = ""
    remaining = index + 1
    while remaining:
        remaining, digit = divmod(remaining - 1, 26)
        letters = chr(ord("A") + digit) + letters
    return letters
