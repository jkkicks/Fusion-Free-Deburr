import math
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'FusionFreeDeburr'))
import deburr_chain as dc  # noqa: E402
import deburr_math as dm  # noqa: E402

Z = (0.0, 0.0, 1.0)
R, W = 0.3, 0.05
TOP = 'top'


def dist(a, b):
    return math.sqrt(dm.dot(dm.sub(a, b), dm.sub(a, b)))


def straight_segment(p0, p1, wall_normal, wall_id, n=1):
    """Top edge from p0 to p1 between the top face (+Z) and a wall."""
    pts = [dm.add(p0, dm.scale(dm.sub(p1, p0), k / n)) for k in range(n + 1)]
    tips = [dm.tool_tip(p, Z, wall_normal, Z, R, W) for p in pts]
    normals = {TOP: Z, wall_id: wall_normal}
    return dc.Segment(tips=tips, start=dc.End(p0, normals), end=dc.End(p1, normals),
                      start_key=p0, end_key=p1)


def square(walls_outward):
    """Four top edges of a 10x10 square. walls_outward=True: block (convex
    corners). False: pocket (walls face inward, concave corners for the path)."""
    c = [(0, 0, 0), (10, 0, 0), (10, 10, 0), (0, 10, 0)]
    sign = 1 if walls_outward else -1
    normals = [(0, -1, 0), (1, 0, 0), (0, 1, 0), (-1, 0, 0)]
    return [straight_segment(c[k], c[(k + 1) % 4], dm.scale(normals[k], sign), f'wall{k}') for k in range(4)]


def assert_connected(test, chain):
    pieces = chain.pieces
    for a, b in zip(pieces, pieces[1:]):
        test.assertLess(dist(a.points[-1], b.points[0]), 1e-9)
    if chain.closed:
        test.assertLess(dist(pieces[-1].points[-1], pieces[0].points[0]), 1e-9)


class OrderChainsTests(unittest.TestCase):
    def test_shuffled_reversed_square_forms_one_closed_chain(self):
        segs = square(True)
        shuffled = [segs[2], segs[0].reversed(), segs[3], segs[1].reversed()]
        chains = dc.order_chains(shuffled)
        self.assertEqual(len(chains), 1)
        chain, closed = chains[0]
        self.assertTrue(closed)
        self.assertEqual(len(chain), 4)
        for a, b in zip(chain, chain[1:]):
            self.assertEqual(a.end_key, b.start_key)

    def test_open_chain_starts_at_dangling_end(self):
        segs = square(True)[:3]
        chains = dc.order_chains([segs[1], segs[2], segs[0]])
        self.assertEqual(len(chains), 1)
        chain, closed = chains[0]
        self.assertFalse(closed)
        self.assertEqual(len(chain), 3)

    def test_disconnected_edges_make_separate_chains(self):
        segs = square(True)
        chains = dc.order_chains([segs[0], segs[2]])
        self.assertEqual(len(chains), 2)


class LinkChainTests(unittest.TestCase):
    def test_block_corners_roll_around_vertex(self):
        chain, closed = dc.order_chains(square(True))[0]
        linked = dc.link_chain(chain, closed, Z, R, W)
        assert_connected(self, linked)
        self.assertEqual(len(linked.pieces), 8)  # 4 edges + 4 corner rolls
        # Corner roll keeps a constant distance from the corner (it's an arc).
        roll = linked.pieces[1].points
        corner = (10, 0, 0)
        radii = [math.hypot(p[0] - corner[0], p[1] - corner[1]) for p in roll]
        self.assertAlmostEqual(max(radii), min(radii), places=9)

    def test_pocket_corners_are_trimmed_to_crossing(self):
        chain, closed = dc.order_chains(square(False))[0]
        linked = dc.link_chain(chain, closed, Z, R, W)
        assert_connected(self, linked)
        self.assertEqual(len(linked.pieces), 4)
        d = abs(linked.pieces[0].points[0][1])  # inward offset of the path
        corner = linked.pieces[0].points[-1]
        self.assertAlmostEqual(corner[0], 10 - d)
        self.assertAlmostEqual(corner[1], d)

    def test_tangent_joint_is_merged(self):
        a = straight_segment((0, 0, 0), (5, 0, 0), (0, -1, 0), 'wall')
        b = straight_segment((5, 0, 0), (10, 0, 0), (0, -1, 0), 'wall')
        linked = dc.link_chain([a, b], False, Z, R, W)
        self.assertEqual(len(linked.pieces), 2)
        assert_connected(self, linked)

    def test_open_chain_has_no_wraparound_link(self):
        chain, closed = dc.order_chains(square(True)[:2])[0]
        linked = dc.link_chain(chain, closed, Z, R, W)
        self.assertFalse(linked.closed)
        self.assertEqual(len(linked.pieces), 3)  # 2 edges + 1 roll


class SplitBoundaryTests(unittest.TestCase):
    def test_edges_between_same_faces_are_bridged_without_warning(self):
        a = straight_segment((0, 0, 0), (5, 0, 0), (0, -1, 0), 'wall')
        b = straight_segment((5, 0, 0), (10, 0, 0), (0, -1, 0), 'wall')
        b.tips[0] = dm.add(b.tips[0], (0.001, 0, 0))  # small gap from normal evaluation
        linked = dc.link_chain([a, b], False, Z, R, W)
        self.assertEqual(linked.notes, [])
        assert_connected(self, linked)


class SpikeCornerTests(unittest.TestCase):
    """A needle of material pointing +Y: edge a runs up to the tip along +Y with
    a wall facing -X, edge b runs back down along -Y with a 45 deg chamfer facing
    +X. Both edges leave the tip in the same direction (-Y), so the shortest
    turn between the side normals would go over the top face."""

    def segments(self):
        s = math.sqrt(0.5)
        tip = (0.0, 0.0, 0.0)
        a_normals = {TOP: Z, 'wall': (-1.0, 0.0, 0.0)}
        b_normals = {TOP: Z, 'chamfer': (s, 0.0, s)}
        a_pts, b_pts = [(0.0, -5.0, 0.0), tip], [tip, (0.0, -5.0, 0.0)]
        a = dc.Segment(tips=[dm.tool_tip(p, Z, a_normals['wall'], Z, R, W) for p in a_pts],
                       start=dc.End(a_pts[0], a_normals, (0.0, 1.0, 0.0)),
                       end=dc.End(tip, a_normals, (0.0, -1.0, 0.0)), start_key='a0', end_key='tip')
        b = dc.Segment(tips=[dm.tool_tip(p, Z, b_normals['chamfer'], Z, R, W) for p in b_pts],
                       start=dc.End(tip, b_normals, (0.0, -1.0, 0.0)),
                       end=dc.End(b_pts[1], b_normals, (0.0, 1.0, 0.0)), start_key='tip', end_key='b1')
        return a, b

    def test_roll_goes_around_the_tip_not_over_the_top(self):
        a, b = self.segments()
        linked = dc.link_chain([a, b], False, Z, R, W)
        self.assertEqual(linked.notes, [])
        assert_connected(self, linked)
        roll = linked.pieces[1].points
        self.assertGreater(len(roll), 30)  # ~180 deg in 5 deg steps
        # Halfway round, the ball is past the tip (+Y), clear of the needle.
        self.assertGreater(roll[len(roll) // 2][1], 0.0)
        # Ball stays on the top face: center never lower than on the straight edges.
        lowest_edge = min(a.tips[-1][2], b.tips[0][2])
        for p in roll:
            self.assertGreaterEqual(p[2], lowest_edge - 1e-9)


class CornerChoiceTests(unittest.TestCase):
    def test_vertex_crossing_keeps_distance_from_vertex(self):
        a, b = SpikeCornerTests().segments()
        tip = (0.0, 0.0, 0.0)
        pts = dc.vertex_crossing(a.tips[-1], b.tips[0], tip, Z, R)
        self.assertEqual(pts[0], a.tips[-1])
        self.assertLess(dist(pts[-1], b.tips[0]), 1e-12)
        centers = [dm.add(p, dm.scale(Z, R)) for p in pts]
        d0, d1 = dist(centers[0], tip), dist(centers[-1], tip)
        for k, c in enumerate(centers):
            self.assertAlmostEqual(dist(c, tip), d0 + (d1 - d0) * k / (len(centers) - 1), places=9)
        # Goes over the vertex, not out past it.
        self.assertTrue(all(abs(p[1]) < 1e-9 for p in pts))

    def test_depth_function_picks_the_shallower_candidate(self):
        a, b = SpikeCornerTests().segments()
        # Material continues past the tip (+Y): anything out there cuts deep.
        depth = lambda tips: max(1.0 if p[1] > 1e-6 else 0.0 for p in tips)
        linked = dc.link_chain([a, b], False, Z, R, W, depth)
        self.assertEqual(linked.notes, [])
        self.assertTrue(all(p[1] <= 1e-6 for p in linked.pieces[1].points))
        # Nothing beyond the tip: the roll around the outside is kept.
        linked = dc.link_chain([a, b], False, Z, R, W, lambda tips: 0.0)
        self.assertGreater(linked.pieces[1].points[len(linked.pieces[1].points) // 2][1], 0.0)

    def test_note_when_best_corner_still_cuts_deep(self):
        a, b = SpikeCornerTests().segments()
        ends = (a.tips[-1], b.tips[0])
        depth = lambda tips: 0.0 if all(p in ends for p in tips) else 1.0
        linked = dc.link_chain([a, b], False, Z, R, W, depth)
        self.assertEqual(len(linked.notes), 1)
        self.assertIn('corner 1', linked.notes[0])


class PolylineTests(unittest.TestCase):
    def test_closed_chain_polyline_ends_on_start_without_duplicates(self):
        segs = square(walls_outward=True)
        linked = dc.link_chain(segs, True, Z, R, W)
        pts = dc.chain_points(linked)
        self.assertEqual(pts[-1], pts[0])
        for a, b in zip(pts, pts[1:]):
            self.assertGreater(dist(a, b), dc.JOIN_TOL)

    def test_open_chain_polyline_keeps_ends(self):
        segs = square(walls_outward=True)[:2]
        linked = dc.link_chain(segs, False, Z, R, W)
        pts = dc.chain_points(linked)
        self.assertEqual(pts[0], linked.pieces[0].points[0])
        self.assertEqual(pts[-1], linked.pieces[-1].points[-1])

    def test_knots_are_clamped_arc_length(self):
        pts = [(0, 0, 0), (3, 0, 0), (3, 4, 0)]
        self.assertEqual(dc.polyline_knots(pts), [0.0, 0.0, 3.0, 7.0, 7.0])
        self.assertEqual(len(dc.polyline_knots(pts)), len(pts) + 2)  # degree 1


class CheckedSpanTests(unittest.TestCase):
    LINE = [(0.0, 0, 0), (4.0, 0, 0), (10.0, 0, 0)]
    LIMIT, STEP = .01, .5

    def spans(self, excess):
        # A move is clear only if every tip on it is: check its worst point.
        def swept(a, b):
            n = 50
            return all(excess(dm.add(a, dm.scale(dm.sub(b, a), k / n))) <= self.LIMIT
                       for k in range(n + 1))
        return dc.checked_spans(self.LINE, excess, swept, self.LIMIT, self.STEP)

    def test_clear_path_is_unchanged(self):
        spans, removed = self.spans(lambda p: 0.0)
        self.assertEqual(spans, [self.LINE])
        self.assertEqual(removed, [])

    def test_blocked_end_is_trimmed_back_to_the_limit(self):
        # The ball cuts 2 too far at x=0, easing to nothing at x=2.
        spans, removed = self.spans(lambda p: max(0.0, 2 - p[0]))
        self.assertEqual(len(spans), 1)
        self.assertAlmostEqual(spans[0][0][0], 2 - self.LIMIT, delta=2 * dc.TRIM_TOL)
        self.assertEqual(spans[0][-1], self.LINE[-1])
        self.assertIn(self.LINE[1], spans[0])  # path points after the trim are kept
        self.assertEqual(len(removed), 1)
        self.assertAlmostEqual(removed[0][0], 2 - self.LIMIT, delta=2 * dc.TRIM_TOL)
        self.assertEqual(removed[0][1], 2)

    def test_blocked_middle_splits_the_path(self):
        spans, removed = self.spans(lambda p: 1.0 if 5 < p[0] < 6 else 0.0)
        self.assertEqual(len(spans), 2)
        self.assertAlmostEqual(spans[0][-1][0], 5, delta=2 * dc.TRIM_TOL)
        self.assertAlmostEqual(spans[1][0][0], 6, delta=2 * dc.TRIM_TOL)
        self.assertIn(self.LINE[1], spans[0])
        self.assertAlmostEqual(removed[0][0], 1, delta=4 * dc.TRIM_TOL)

    def test_narrow_block_between_samples_is_found(self):
        # Clear at every 0.5 step; only the whole-move check sees it.
        excess = lambda p: 1.0 if 7.2 < p[0] < 7.3 else 0.0
        spans, removed = self.spans(excess)
        self.assertEqual(len(spans), 2)
        self.assertLess(spans[0][-1][0], 7.2 + 2 * dc.TRIM_TOL)
        self.assertGreater(spans[1][0][0], 7.3 - 2 * dc.TRIM_TOL)

    def test_every_trimmed_end_is_clear(self):
        excess = lambda p: max(0.0, 2 - p[0], p[0] - 8)
        spans, _ = self.spans(excess)
        for span in spans:
            for point in (span[0], span[-1]):
                self.assertLessEqual(excess(point), self.LIMIT)

    def test_fully_blocked_path_is_removed(self):
        spans, removed = self.spans(lambda p: .5)
        self.assertEqual(spans, [])
        self.assertEqual(len(removed), 1)
        self.assertAlmostEqual(removed[0][0], 10)

    def test_trim_next_to_a_path_point_keeps_the_path_point(self):
        # Review case: clean-up must not swap the checked corner for a shortcut.
        corner = [(0.0, 0, 0), (1.0, 0, 0), (1.0, 1.0, 0)]
        excess = lambda p: 1.0 if p[1] == 0 and p[0] < 1 - 5e-5 else 0.0
        swept = lambda a, b: excess(a) == 0 and excess(b) == 0 and excess(dm.scale(dm.add(a, b), .5)) == 0
        spans, _ = dc.checked_spans(corner, excess, swept, .01, .5)
        self.assertEqual(spans, [[(1.0, 0, 0), (1.0, 1.0, 0)]])

    def test_near_limit_path_is_kept_without_endless_splitting(self):
        # Review case: every point passes, but the whole-move check never
        # does; the path must not be dropped as a gouge.
        calls = []

        def swept(a, b):
            calls.append(1)
            return False
        spans, removed = dc.checked_spans([(0.0, 0, 0), (2.0, 0, 0)], lambda p: .0099, swept,
                                          .01, .01, .01 / 8)
        self.assertEqual(spans, [[(0.0, 0, 0), (2.0, 0, 0)]])
        self.assertEqual(removed, [])
        self.assertLessEqual(len(calls), 200 * 15)   # 200 moves, split at most three times

    def test_shorten_ends_moves_failing_ends_inward(self):
        line = [(0.0, 0, 0), (4.0, 0, 0), (10.0, 0, 0)]
        excess = lambda p: 1.0 if p[0] < 2.5 or p[0] > 9 else 0.0
        points, removed = dc.shorten_ends(line, excess, .01, .5, 1e-3)
        self.assertAlmostEqual(points[0][0], 2.5, delta=1e-3)
        self.assertEqual(points[1], (4.0, 0, 0))           # path points kept
        self.assertAlmostEqual(points[-1][0], 9, delta=1e-3)
        self.assertEqual(len(removed), 2)
        self.assertAlmostEqual(removed[0][0], 2.5, delta=1e-3)
        self.assertAlmostEqual(removed[1][0], 1, delta=1e-3)

    def test_shorten_ends_exports_the_end_it_checked(self):
        # Review case: the new end lands within JOIN_TOL of a path point that
        # itself fails; clean-up must keep the checked end, not that point.
        line = [(0.0, 0, 0), (.5, 0, 0), (1.0, 0, 0)]
        bad = lambda p: p[0] > .5 + 3e-5 or abs(p[0] - .5) < 1e-12
        excess = lambda p: 1.0 if bad(p) else 0.0
        points, _ = dc.shorten_ends(line, excess, .01, .1, 1e-6)
        self.assertEqual(excess(points[-1]), 0.0)
        self.assertEqual(points, dc._clean(points))       # nothing left for clean-up to drop

    def test_shorten_ends_keeps_clear_ends_and_drops_hopeless_paths(self):
        line = [(0.0, 0, 0), (1.0, 0, 0)]
        self.assertEqual(dc.shorten_ends(line, lambda p: 0.0, .01, .1), (line, []))
        points, removed = dc.shorten_ends(line, lambda p: 1.0, .01, .1)
        self.assertIsNone(points)
        self.assertEqual(len(removed), 1)

    def test_cut_back_removes_the_end_of_a_path(self):
        line = [(0.0, 0, 0), (1.0, 0, 0), (1.0, 1.0, 0)]
        self.assertEqual(dc.cut_back(line, .25), [(0.0, 0, 0), (1.0, 0, 0), (1.0, .75, 0)])
        self.assertEqual(dc.cut_back(line, 1.5), [(0.0, 0, 0), (.5, 0, 0)])
        self.assertIsNone(dc.cut_back(line, 2))

    def test_open_loop_continues_past_its_start(self):
        loop = [(0.0, 0, 0), (1.0, 0, 0), (1.0, 1.0, 0), (0.0, 1.0, 0), (0.0, 0, 0)]
        opened = dc.open_loop(loop, .25)
        self.assertEqual(opened[:5], loop)
        self.assertEqual(opened[-1], (.25, 0, 0))
        line = [(0.0, 0, 0), (1.0, 0, 0)]
        self.assertEqual(dc.open_loop(line, .25), line)
        # A loop shorter than twice the overlap still gets two distinct ends.
        tiny = [(0.0, 0, 0), (.01, 0, 0), (.01, .01, 0), (0.0, .01, 0), (0.0, 0, 0)]
        opened = dc.open_loop(tiny, .25)
        self.assertGreater(dist(opened[0], opened[-1]), .005)

    @staticmethod
    def box_surface(size, n):
        """Points and outward normals on the surface of a box from the origin."""
        nodes, normals = [], []
        for i in range(n + 1):
            for j in range(n + 1):
                a, b = i / n, j / n
                for p, normal in (((a * size[0], b * size[1], 0), (0, 0, -1)),
                                  ((a * size[0], b * size[1], size[2]), (0, 0, 1)),
                                  ((a * size[0], 0, b * size[2]), (0, -1, 0)),
                                  ((a * size[0], size[1], b * size[2]), (0, 1, 0)),
                                  ((0, a * size[1], b * size[2]), (-1, 0, 0)),
                                  ((size[0], a * size[1], b * size[2]), (1, 0, 0))):
                    nodes.append(p)
                    normals.append(normal)
        return nodes, normals

    def test_inward_points_reach_deep_inside_only(self):
        nodes, normals = self.box_surface((1, 1, 1), 20)
        deep = dc.inward_points(nodes, normals, nodes, .2)
        self.assertTrue(deep)
        self.assertTrue(any(dist(p, (.5, .5, .5)) < .2 for p in deep))
        self.assertTrue(all(min(dist(p, q) for q in nodes) > .2 for p in deep))
        # A slab thinner than twice the depth has no point that deep.
        nodes, normals = self.box_surface((1, 1, .3), 20)
        inside = [p for p in dc.inward_points(nodes, normals, nodes, .2)
                  if all(0 <= p[k] <= (1, 1, .3)[k] for k in range(3))]
        self.assertEqual(inside, [])

    def test_limit_matches_corner_check(self):
        self.assertEqual(dc.excess_limit(.0254), .00254)
        self.assertEqual(dc.excess_limit(.001), .001)


class DistanceTests(unittest.TestCase):
    def test_polyline_distance_stops_at_the_ends(self):
        line = [(0.0, 0, 0), (1.0, 0, 0)]
        self.assertAlmostEqual(dc.polyline_distance((.5, 2, 0), line), 2)
        self.assertAlmostEqual(dc.polyline_distance((4, 0, 0), line), 3)
        self.assertAlmostEqual(dc.polyline_distance((-3, 4, 0), line), 5)

    def test_farthest_finds_the_worst_sample(self):
        segments = dc.polyline_segments([(0.0, 0, 0), (1.0, 0, 0)])
        samples = [(x / 100, 0.01 * (x % 7), 0) for x in range(100)] + [(2.0, 0, 0)]
        self.assertAlmostEqual(dc.farthest(samples, segments, 10), 1.0)
        self.assertEqual(dc.farthest(samples, segments, .5), .5)   # capped
        self.assertEqual(dc.farthest(samples, [], .5), .5)          # nothing in reach
        self.assertEqual(dc.farthest([], segments, .5), 0.0)

    def test_farthest_stops_beyond_enough(self):
        segments = dc.polyline_segments([(0.0, 0, 0), (1.0, 0, 0)])
        samples = [(.5, .1, 0), (.5, .9, 0), (.5, .5, 0)]
        self.assertGreater(dc.farthest(samples, segments, 10, enough=.3), .3)
        self.assertLessEqual(dc.farthest([(.5, .1, 0), (.5, .2, 0)], segments, 10, enough=.3), .3)

    def test_segment_index_matches_a_full_scan(self):
        import random
        rng = random.Random(4)
        points = [(rng.uniform(-2, 2), rng.uniform(-2, 2), rng.uniform(-1, 1)) for _ in range(60)]
        segments = dc.polyline_segments(points)
        index = dc.SegmentIndex(segments, .3)
        for _ in range(200):
            center = (rng.uniform(-2, 2), rng.uniform(-2, 2), rng.uniform(-1, 1))
            reach = rng.uniform(.05, 1)
            found = set(map(id, index.within(center, reach)))
            # Every segment truly within reach is found; nothing found is beyond its box.
            truly = {id(s) for s in segments if dc._segment_distance(center, s[0], s[1]) <= reach}
            self.assertLessEqual(truly, found)
            self.assertLessEqual(found, set(map(id, dc.segments_within(segments, center, reach))))
            hi = tuple(c + .2 for c in center)
            found = set(map(id, index.near_box(center, hi, reach)))
            self.assertLessEqual(found, set(map(id, dc.segments_near_box(segments, center, hi, reach))))
            middle = tuple(c + .1 for c in center)
            near_middle = {id(s) for s in segments if dc._segment_distance(middle, s[0], s[1]) <= reach}
            self.assertLessEqual(near_middle, found)

    def test_search_along_a_long_slanted_path_finds_what_is_near_it(self):
        # Review case: a long plunge on a tilted tool axis; its box holds
        # far more cells than the plunge comes near.
        import random
        rng = random.Random(7)
        points = [(rng.uniform(0, 20), rng.uniform(0, 20), rng.uniform(0, 20)) for _ in range(40)]
        segments = dc.polyline_segments(points)
        index = dc.SegmentIndex(segments, .05)
        a, b = (0.0, 0.0, 0.0), (20.0, 20.0, 20.0)
        found = set(map(id, index.near_path(a, b, .5)))
        for s in segments:
            # A segment passing within reach of a point on the path is found.
            for k in range(101):
                p = dm.add(a, dm.scale(dm.sub(b, a), k / 100))
                if dc._segment_distance(p, s[0], s[1]) <= .3:
                    self.assertIn(id(s), found)
                    break

    def test_long_diagonal_segment_fills_only_the_cells_it_crosses(self):
        # Review case: a 10 cm diagonal edge with a 1 mm ball would file its
        # whole bounding box, millions of cells.
        segment = dc.polyline_segments([(0.0, 0, 0), (10.0, 10.0, 10.0)])
        index = dc.SegmentIndex(segment, .05)
        self.assertLess(len(index.cells), 20000)
        self.assertEqual(len(index.within((5, 5, 5), .01)), 1)
        self.assertEqual(len(index.within((5, 0, 5), 1)), 0)

    def test_segments_within_reach(self):
        segments = dc.polyline_segments([(0.0, 0, 0), (1.0, 0, 0), (5.0, 0, 0)])
        self.assertEqual(len(dc.segments_within(segments, (0.5, .2, 0), .3)), 1)
        self.assertEqual(len(dc.segments_within(segments, (0.5, .2, 0), 10)), 2)


if __name__ == '__main__':
    unittest.main()
