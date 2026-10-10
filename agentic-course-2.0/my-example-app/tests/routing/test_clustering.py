"""``cluster_points``: caps, stability and labels, proven with no database and no network."""

import math
import random

import pytest

from app.routing.clustering import cluster_points
from app.routing.schemas import RoutePoint
from tests.routing.builders import a_point, around, two_neighbourhoods


def _ids(points: tuple[RoutePoint, ...] | list[RoutePoint]) -> set[str]:
    return {point.registry_id for point in points}


class TestClusterPoints:
    def test_no_points_make_no_routes(self) -> None:
        assert cluster_points([], max_doors=12) == ()

    def test_the_e4_week_is_two_routes_of_ten(self) -> None:
        olympic, lake_highlands = two_neighbourhoods()

        clusters = cluster_points(olympic + lake_highlands, max_doors=12)

        assert [len(c.members) for c in clusters] == [10, 10]
        assert {frozenset(_ids(c.members)) for c in clusters} == {
            frozenset(_ids(olympic)),
            frozenset(_ids(lake_highlands)),
        }

    def test_routes_are_labelled_west_to_east_with_their_zip(self) -> None:
        olympic, lake_highlands = two_neighbourhoods()

        clusters = cluster_points(lake_highlands + olympic, max_doors=12)

        # Olympic Drive (Arlington) is west of Lake Highlands.
        assert [c.label for c in clusters] == ["A · 76011", "B · 75238"]
        assert [c.index for c in clusters] == [0, 1]
        assert [c.anchor_postal_code for c in clusters] == ["76011", "75238"]

    def test_members_are_in_registry_id_order(self) -> None:
        olympic, lake_highlands = two_neighbourhoods()

        for cluster in cluster_points(olympic + lake_highlands, max_doors=12):
            ids = [m.registry_id for m in cluster.members]
            assert ids == sorted(ids)

    def test_any_input_order_gives_the_same_routes(self) -> None:
        olympic, lake_highlands = two_neighbourhoods(per_side=17)
        points = olympic + lake_highlands
        expected = cluster_points(points, max_doors=12)
        shuffler = random.Random(7)

        for _ in range(5):
            shuffled = points[:]
            shuffler.shuffle(shuffled)
            assert cluster_points(shuffled, max_doors=12) == expected

    def test_the_same_input_twice_gives_the_same_routes(self) -> None:
        olympic, lake_highlands = two_neighbourhoods()
        points = olympic + lake_highlands

        assert cluster_points(points, max_doors=12) == cluster_points(points, max_doors=12)

    @pytest.mark.parametrize("max_doors", [1, 10, 12])
    def test_no_route_exceeds_the_cap_and_every_door_is_routed_once(self, max_doors: int) -> None:
        scatter = random.Random(11)
        for n in range(1, 61):
            points = [
                a_point(f"r{i:03d}", 32.6 + scatter.random() * 0.5, -97.3 + scatter.random() * 0.7)
                for i in range(n)
            ]

            clusters = cluster_points(points, max_doors=max_doors)

            assert len(clusters) == math.ceil(n / max_doors)
            assert all(1 <= len(c.members) <= max_doors for c in clusters)
            routed = [m.registry_id for c in clusters for m in c.members]
            assert sorted(routed) == sorted(p.registry_id for p in points)

    def test_sizes_are_balanced(self) -> None:
        points = around((32.80, -96.80), "b", 23, "75201")

        clusters = cluster_points(points, max_doors=12)

        assert sorted(len(c.members) for c in clusters) == [11, 12]

    def test_doors_at_one_address_still_split_under_the_cap(self) -> None:
        points = [a_point(f"s{i:02d}", 32.80, -96.80) for i in range(25)]

        clusters = cluster_points(points, max_doors=12)

        assert sorted(len(c.members) for c in clusters) == [8, 8, 9]
        assert clusters == cluster_points(list(reversed(points)), max_doors=12)

    def test_the_anchor_is_the_most_common_zip(self) -> None:
        points = [
            a_point("z1", 32.80, -96.80, "75238"),
            a_point("z2", 32.80, -96.80, "75243-1234"),
            a_point("z3", 32.80, -96.80, "75243"),
        ]

        (cluster,) = cluster_points(points, max_doors=12)

        assert cluster.anchor_postal_code == "75243"

    def test_a_zip_tie_takes_the_lowest(self) -> None:
        points = [a_point("t1", 32.80, -96.80, "75243"), a_point("t2", 32.80, -96.80, "75238")]

        (cluster,) = cluster_points(points, max_doors=12)

        assert cluster.label == "A · 75238"

    def test_labels_continue_past_z(self) -> None:
        points = [a_point(f"l{i:02d}", 32.80, -96.80 + i * 0.01) for i in range(28)]

        clusters = cluster_points(points, max_doors=1)

        assert [c.label.split(" ")[0] for c in clusters[24:]] == ["Y", "Z", "AA", "AB"]

    def test_a_duplicate_candidate_is_refused(self) -> None:
        point = a_point("d1", 32.80, -96.80)

        with pytest.raises(ValueError, match="only once"):
            cluster_points([point, point], max_doors=12)

    def test_a_cap_below_one_is_refused(self) -> None:
        with pytest.raises(ValueError, match="max_doors"):
            cluster_points([a_point("c1", 32.80, -96.80)], max_doors=0)
