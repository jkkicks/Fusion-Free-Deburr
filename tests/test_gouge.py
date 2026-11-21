"""Ball-path gouge check: every bit of material the ball removes must lie
within the break width (plus the limit) of a selected edge.

`RuleTests` checks that rule on small analytic parts, including the cases
found in review. `ExcessTests` and `TrimTests` check the Fusion wiring with
the Fusion API mocked."""
import math
import unittest
from types import SimpleNamespace as NS
from unittest.mock import MagicMock, patch

from test_safety import adsk, dc, fusion

dm = dc.dm
Z = (0.0, 0.0, 1.0)


# --- A tiny solid model: the material a ball removes, sampled on its boundary

def plane_face(origin, normal, u, v):
    return ('plane', origin, dm.normalize(normal), dm.normalize(u), dm.normalize(v))


def removed_samples(center, radius, inside, faces, spacing=.003, sphere_points=6000):
    """Points on the boundary of (ball and part): the ball's surface inside
    the part, and the part's faces inside the ball."""
    eps = 1e-6
    samples = []
    golden = math.pi * (3 - math.sqrt(5))
    for k in range(sphere_points):
        zc = 1 - 2 * (k + .5) / sphere_points
        rc = math.sqrt(1 - zc * zc)
        p = dm.add(center, dm.scale((rc * math.cos(golden * k), rc * math.sin(golden * k), zc), radius))
        if inside(p):
            samples.append(p)
    for face in faces:
        if face[0] == 'plane':
            _, origin, n, u, v = face
            height = dm.dot(dm.sub(center, origin), n)
            if abs(height) >= radius:
                continue
            foot = dm.sub(center, dm.scale(n, height))
            rho = math.sqrt(radius * radius - height * height)
            steps = int(rho / spacing)
            for i in range(-steps, steps + 1):
                for j in range(-steps, steps + 1):
                    p = dm.add(foot, dm.add(dm.scale(u, i * spacing), dm.scale(v, j * spacing)))
                    if dc.dist(p, center) <= radius and inside(dm.sub(p, dm.scale(n, eps))) \
                            and not inside(dm.add(p, dm.scale(n, eps))):
                        samples.append(p)
        else:                           # ('hole', radius): a vertical hole on the z axis
            hole = face[1]
            for i in range(720):
                a = 2 * math.pi * i / 720
                for j in range(int(radius / spacing) + 1):
                    p = (hole * math.cos(a), hole * math.sin(a), -j * spacing)
                    if dc.dist(p, center) <= radius:
                        samples.append(p)
    return samples


def rule_excess(tip, radius, width, inside, faces, edges):
    center = dm.add(tip, dm.scale(Z, radius))
    segments = [s for e in edges for s in dc.polyline_segments(e)]
    far = dc.farthest(removed_samples(center, radius, inside, faces), segments, radius + width + 1)
    return far - width


class RuleTests(unittest.TestCase):
    """Material removed farther than the break width from every selected edge."""
    SAMPLING = .003 + 1e-4        # face grid spacing, plus a little

    def limit(self, width):
        return dc.excess_limit(width) + fusion.PATH_TOL

    def test_straight_break_and_outside_corner_end_pass(self):
        r, w = .3175, .0254
        # Block x <= 0, z <= 0, y <= 0: an edge along y ends at an outside corner.
        inside = lambda p: p[0] <= 0 and p[2] <= 0 and -2 <= p[1] <= 0
        faces = [plane_face((0, 0, 0), (0, 0, 1), (1, 0, 0), (0, 1, 0)),
                 plane_face((0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, 1)),
                 plane_face((0, 0, 0), (0, 1, 0), (1, 0, 0), (0, 0, 1))]
        edge = [(0, -2, 0), (0, 0, 0)]
        for y in (-1.0, 0.0):            # mid-edge, and the very end at the corner
            tip = dm.tool_tip((0, y, 0), Z, (1, 0, 0), Z, r, w)
            with self.subTest(y=y):
                self.assertLessEqual(rule_excess(tip, r, w, inside, faces, [edge]),
                                     self.limit(w) + self.SAMPLING)

    def test_ramp_running_into_a_floor_is_caught(self):
        r, w = .3175, .0254
        slope = math.radians(20)
        # Front face y = 0 (part at y >= 0); a ramp coming down to a floor at z = 0, x >= 0.
        top = lambda x: max(0.0, -x * math.tan(slope))
        inside = lambda p: 0 <= p[1] <= 2 and p[2] <= top(p[0])
        ramp_n = (math.sin(slope), 0, math.cos(slope))
        faces = [plane_face((0, 0, 0), (0, -1, 0), (1, 0, 0), (0, 0, 1)),
                 plane_face((0, 0, 0), (0, 0, 1), (1, 0, 0), (0, 1, 0)),
                 plane_face((0, 0, 0), ramp_n, (math.cos(slope), 0, -math.sin(slope)), (0, 1, 0))]
        edge = [(-1, 0, math.tan(slope)), (0, 0, 0)]
        at_floor = dm.tool_tip((0, 0, 0), (0, -1, 0), ramp_n, Z, r, w)
        up_ramp = dm.tool_tip((-.8, 0, .8 * math.tan(slope)), (0, -1, 0), ramp_n, Z, r, w)
        self.assertGreater(rule_excess(at_floor, r, w, inside, faces, [edge]), 5 * self.limit(w))
        self.assertLessEqual(rule_excess(up_ramp, r, w, inside, faces, [edge]),
                             self.limit(w) + self.SAMPLING)

    def slot(self, half):
        inside = lambda p: p[2] <= 0 and abs(p[0]) >= half and -2 <= p[1] <= 2
        faces = [plane_face((0, 0, 0), (0, 0, 1), (1, 0, 0), (0, 1, 0)),
                 plane_face((-half, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, 1)),
                 plane_face((half, 0, 0), (-1, 0, 0), (0, 1, 0), (0, 0, 1))]
        edges = [[(-half, -2, 0), (-half, 2, 0)], [(half, -2, 0), (half, 2, 0)]]
        return inside, faces, edges

    def test_slot_lip_breaking_into_the_opposite_lip_is_caught(self):
        # Review case: 6 mm ball, 0.5 mm break, 3.5 mm slot, both lips
        # selected. The ball for one lip cuts the other lip deeper than its break.
        r, w = .3, .05
        inside, faces, edges = self.slot(.175)
        tip = dm.tool_tip((-.175, 0, 0), Z, (1, 0, 0), Z, r, w)
        self.assertGreater(rule_excess(tip, r, w, inside, faces, edges), self.limit(w) + self.SAMPLING)

    def test_slot_lip_touching_only_the_other_lips_break_passes(self):
        # Review case: 4 mm ball, 0.6 mm break, 3 mm slot, both lips selected.
        # The ball reaches down the opposite wall, but only within that lip's
        # own break, which its own pass removes anyway.
        r, w = .2, .06
        inside, faces, edges = self.slot(.15)
        tip = dm.tool_tip((-.15, 0, 0), Z, (1, 0, 0), Z, r, w)
        self.assertLessEqual(rule_excess(tip, r, w, inside, faces, edges),
                             self.limit(w) + self.SAMPLING)

    def test_ball_swallowing_a_feature_inside_its_edges_is_caught(self):
        # Review case: a ball swallows a small cube whose edges are all
        # selected. Every point of the cube's surface lies within the
        # allowance of an edge; its middle does not.
        r, w = .3, .05
        side = .1
        inside = lambda p: all(0 <= c <= side for c in p)
        corners = [(x, y, z) for x in (0, side) for y in (0, side) for z in (0, side)]
        edges = [[a, b] for a in corners for b in corners
                 if a < b and sum(abs(a[k] - b[k]) > 0 for k in range(3)) == 1]
        n = 40
        surface, normals = [], []
        for i in range(n + 1):
            for j in range(n + 1):
                u, v = side * i / n, side * j / n
                for p, normal in (((u, v, 0), (0, 0, -1)), ((u, v, side), (0, 0, 1)),
                                  ((u, 0, v), (0, -1, 0)), ((u, side, v), (0, 1, 0)),
                                  ((0, u, v), (-1, 0, 0)), ((side, u, v), (1, 0, 0))):
                    surface.append(p)
                    normals.append(normal)
        segments = [s for e in edges for s in dc.polyline_segments(e)]
        limit = self.limit(w)
        on_surface = dc.farthest(surface, segments, 1) - w
        self.assertLessEqual(on_surface, limit)          # the surface alone passes
        deep = [p for p in dc.inward_points(surface, normals, surface, w / 2) if inside(p)]
        self.assertGreater(dc.farthest(surface + deep, segments, 1) - w, limit)

    def test_swallowed_feature_under_two_widths_across_is_caught(self):
        # Review case: a boss 0.9 mm across with a 0.5 mm break, all its edges
        # selected. Its core is less than one width deep: half-width steps
        # never reach a point far enough from the surface; quarter steps do.
        w, side = .05, .09
        inside = lambda p: all(0 <= c <= side for c in p)
        corners = [(x, y, z) for x in (0, side) for y in (0, side) for z in (0, side)]
        edges = [[a, b] for a in corners for b in corners
                 if a < b and sum(abs(a[k] - b[k]) > 0 for k in range(3)) == 1]
        segments = [s for e in edges for s in dc.polyline_segments(e)]
        n = 36
        surface, normals = [], []
        for i in range(n + 1):
            for j in range(n + 1):
                u, v = side * i / n, side * j / n
                for p, normal in (((u, v, 0), (0, 0, -1)), ((u, v, side), (0, 0, 1)),
                                  ((u, 0, v), (0, -1, 0)), ((u, side, v), (0, 1, 0)),
                                  ((0, u, v), (-1, 0, 0)), ((side, u, v), (1, 0, 0))):
                    surface.append(p)
                    normals.append(normal)
        limit = self.limit(w)
        self.assertLessEqual(dc.farthest(surface, segments, 1) - w, limit)
        old = [p for p in dc.inward_points(surface, normals, surface, w / 2, (1, 2, 4)) if inside(p)]
        self.assertLessEqual(dc.farthest(surface + old, segments, 1) - w, limit)   # missed before
        deep = [p for p in dc.inward_points(surface, normals, surface, w * fusion.INSIDE_STEP)
                if inside(p)]
        self.assertGreater(dc.farthest(surface + deep, segments, 1) - w, limit)

    def test_ball_bigger_than_a_small_hole_is_caught(self):
        # Review case: 4 mm ball, 0.254 mm break, 1.4 mm hole: the ball's
        # center crosses the hole's axis and cuts far back from the rim.
        r, w, hole = .2, .0254, .07
        inside = lambda p: p[2] <= 0 and math.hypot(p[0], p[1]) >= hole and abs(p[0]) < 2 and abs(p[1]) < 2
        faces = [plane_face((0, 0, 0), (0, 0, 1), (1, 0, 0), (0, 1, 0)), ('hole', hole)]
        rim = [(hole * math.cos(a), hole * math.sin(a), 0)
               for a in (2 * math.pi * k / 720 for k in range(721))]
        tip = dm.tool_tip((hole, 0, 0), Z, (-1, 0, 0), Z, r, w)
        self.assertGreater(rule_excess(tip, r, w, inside, faces, [rim]), .1)


# --- The Fusion wiring, with the API mocked ------------------------------------

def point(p):
    return NS(x=p[0], y=p[1], z=p[2])


class Body:
    def __init__(self, lo=(-5, -5, -5), hi=(5, 5, 5), contains_center=False):
        self.boundingBox = NS(minPoint=point(lo), maxPoint=point(hi))
        self.contains_center = contains_center

    def pointContainment(self, p):
        containment = adsk.fusion.PointContainment
        return (containment.PointInsidePointContainment if self.contains_center
                else containment.PointOutsidePointContainment)


class ExcessTests(unittest.TestCase):
    """`_excess` intersects a ball with each body and measures the samples."""
    R, W = .3, .05
    EDGE = dc.SegmentIndex(dc.polyline_segments([(0.0, 0, 0), (1.0, 0, 0)]), .3)

    def setUp(self):
        self.temp = adsk.fusion.TemporaryBRepManager.get()
        self.spheres = []

        def sphere(center, radius):
            cut = MagicMock()
            cut.faces.count = 1
            cut.center, cut.radius = center, radius
            self.spheres.append(cut)
            return cut
        for p in (patch.object(adsk.core.Point3D, 'create', side_effect=lambda *p: p),
                  patch.object(self.temp, 'createSphere', side_effect=sphere),
                  patch.object(self.temp, 'booleanOperation', return_value=True)):
            p.start()
            self.addCleanup(p.stop)

    def excess(self, removed, bodies=None, margin=0.0, tip=(.5, 0, -.05)):
        with patch.object(fusion, '_cut_samples', side_effect=lambda cut, width, *rest: removed(cut)):
            return fusion._excess(bodies or [Body()], self.EDGE, self.R, self.W, Z, .01)(tip, margin)

    def test_material_within_the_break_has_no_excess(self):
        self.assertEqual(self.excess(lambda cut: [(.5, -.04, 0), (.5, 0, -.05)]), 0)

    def test_material_beyond_the_break_counts_how_far(self):
        self.assertAlmostEqual(self.excess(lambda cut: [(.5, -.04, 0), (.5, 0, -.2)]), .2 - self.W)

    def test_past_the_end_of_a_straight_edge_counts_from_its_end(self):
        self.assertAlmostEqual(self.excess(lambda cut: [(1.3, 0, 0)]), .3 - self.W)

    def test_margin_grows_the_ball(self):
        self.excess(lambda cut: [], margin=.01)
        self.assertAlmostEqual(self.spheres[0].radius, self.R + .01)
        self.assertEqual(self.spheres[0].center, (.5, 0, .25))

    def test_every_body_is_intersected_and_the_worst_counts(self):
        far = {}

        def removed(cut):
            # The second body's cut reaches 0.2 below the edge.
            return [(.5, 0, -.2)] if len(self.spheres) == 2 else [(.5, 0, -.01)]
        self.assertAlmostEqual(self.excess(removed, bodies=[Body(), Body()]), .2 - self.W)
        self.assertEqual(len(self.spheres), 2)

    def test_bodies_out_of_reach_are_not_intersected(self):
        self.assertEqual(self.excess(lambda cut: [(9, 9, 9)], bodies=[Body((5, 5, 5), (6, 6, 6))]), 0)
        self.assertEqual(self.spheres, [])

    def test_check_stops_once_a_move_has_failed(self):
        # Two bodies: the first already fails, so the second is not intersected.
        self.excess(lambda cut: [(.5, 0, -.2)], bodies=[Body(), Body()])
        self.assertEqual(len(self.spheres), 2)
        self.spheres.clear()
        with patch.object(fusion, '_cut_samples', side_effect=lambda cut, width, *rest: [(.5, 0, -.2)]):
            fusion._excess([Body(), Body()], self.EDGE, self.R, self.W, Z, .01)((.5, 0, -.05), 0, .01)
        self.assertEqual(len(self.spheres), 1)

    def test_ball_center_inside_the_part_fails_outright(self):
        # Review case: a ball swallowing a small feature whose selected edges
        # surround it; its middle is farther from the edges than its surface.
        # The center is removed material: its own distance, .25 from the
        # edge, less the width, is reported (a lower bound), never a pass.
        self.assertAlmostEqual(self.excess(lambda cut: [], bodies=[Body(contains_center=True)]), .25 - self.W)
        self.assertEqual(self.spheres, [])
        # Right at an edge it still fails, with no depth to report.
        self.assertEqual(self.excess(lambda cut: [], bodies=[Body(contains_center=True)],
                                     tip=(.5, 0, -self.R)), math.inf)

    def test_no_intersection_has_no_excess(self):
        def removed(cut):
            raise AssertionError('an empty cut is not sampled')
        with patch.object(self.temp, 'createSphere',
                          side_effect=lambda c, r: MagicMock(faces=NS(count=0))):
            self.assertEqual(self.excess(removed), 0)

    def test_failed_intersection_is_an_error(self):
        with patch.object(self.temp, 'booleanOperation', return_value=False):
            with self.assertRaises(dc.dm.DeburrError):
                self.excess(lambda cut: [])

    def column(self, bodies, removed=lambda cut: [(.5, 0, .03)], limit=.01):
        cylinders = []

        def cylinder(a, ra, b, rb):
            cut = MagicMock()
            cut.faces.count = 1
            cylinders.append((a, ra, b, rb))
            return cut
        for p in (patch.object(self.temp, 'createCylinderOrCone', side_effect=cylinder),
                  patch.object(fusion, '_cut_samples',
                               side_effect=lambda cut, width, *a: removed(cut))):
            p.start()
            self.addCleanup(p.stop)
        return fusion._column_excess(bodies, self.EDGE, self.R, self.W, Z, limit), cylinders

    def test_plunge_column_runs_from_the_ball_center_up_past_the_part(self):
        check, cylinders = self.column([Body((-5, -5, -5), (5, 5, 1))])
        self.assertEqual(check((.5, 0, -.05)), 0)          # removed material near the edge
        (a, ra, b, rb), = cylinders
        self.assertEqual(a, (.5, 0, .25))                  # the ball's center
        self.assertAlmostEqual(b[2], 1 + self.R)           # past the top of the part
        self.assertEqual((ra, rb), (self.R, self.R))

    def test_plunge_column_counts_material_beyond_the_break(self):
        check, _ = self.column([Body()], removed=lambda cut: [(.5, 0, 2.0)])
        self.assertGreater(check((.5, 0, -.05)), .01)

    def test_plunge_column_above_the_part_or_beside_it_is_clear(self):
        check, cylinders = self.column([Body((-5, -5, -5), (5, 5, 1))])
        self.assertEqual(check((.5, 0, 2)), 0)             # the ball center is above the part
        self.assertEqual(cylinders, [])
        check, cylinders = self.column([Body((-5, -5, -5), (5, 5, 1)), Body((20, 20, -5), (21, 21, 1))])
        check((.5, 0, -.05))
        self.assertEqual(len(cylinders), 1)                # the far body is not intersected

    def test_plunge_column_stops_at_the_first_body_over_the_limit(self):
        check, cylinders = self.column([Body(), Body()], removed=lambda cut: [(.5, 0, 2.0)])
        check((.5, 0, -.05))
        self.assertEqual(len(cylinders), 1)

    def test_removed_material_is_sampled_on_edges_vertices_and_faces(self):
        cut = MagicMock()
        cut.area = .01
        cut.vertices = [NS(geometry=point((1, 2, 3)))]
        stroke = [point((0, 0, 0)), point((1, 0, 0))]
        cut.edges = [NS(evaluator=NS(getParameterExtents=lambda: (True, 0, 1),
                                     getStrokes=lambda t0, t1, tol: (True, stroke)))]
        calculator = cut.meshManager.createMeshCalculator.return_value
        calculator.calculate.return_value.nodeCoordinatesAsDouble = [4, 5, 6, 7, 8, 9]
        samples = fusion._cut_samples(cut, .04)
        self.assertEqual(samples, [(1, 2, 3), (0, 0, 0), (1, 0, 0), (4, 5, 6), (7, 8, 9)])
        self.assertAlmostEqual(calculator.maxSideLength, .04 * fusion.CUT_MESH_SIDE)
        # Review case: a large region is never meshed more coarsely; one too
        # large for the triangle budget is an error, so its chain is skipped.
        cut.area = 30.0
        with self.assertRaises(dc.dm.DeburrError):
            fusion._cut_samples(cut, .04)
        cut.area = .01
        calculator.calculate.return_value.nodeCoordinatesAsDouble = []
        with self.assertRaises(dc.dm.DeburrError):
            fusion._cut_samples(cut, .04)

    def test_gross_cut_is_not_meshed(self):
        # Review case: a ball far too big for a hole; its edge strokes
        # already fail, so the region is never meshed.
        cut = MagicMock()
        cut.vertices = [NS(geometry=point((1, 2, 3)))]
        cut.edges = []
        samples = fusion._cut_samples(cut, .04, .02, fails=lambda pts: True)
        self.assertEqual(samples, [(1, 2, 3)])
        cut.meshManager.createMeshCalculator.assert_not_called()


class TrimTests(unittest.TestCase):
    R = .3
    LOOP = [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0), (0, 0, 0)]

    @staticmethod
    def excess_for(blocked):
        # Tips where `blocked` holds cut too far; a grown ball covers its margin.
        def excess(tip, margin=0.0, enough=None):
            n = 8
            if margin == 0:
                return 1.0 if blocked(tip) else 0.0
            for k in range(-n, n + 1):
                for axis in range(2):
                    p = list(tip)
                    p[axis] += margin * k / n
                    if blocked(tuple(p)):
                        return 1.0
            return 0.0
        return excess

    def trim(self, points, blocked, closed=False, plunge_blocked=lambda p: False):
        chain = dc.Chain(pieces=[dc.Piece(points)], closed=closed)
        column = lambda p: 1.0 if plunge_blocked(p) else 0.0
        paths, removed, lost, self.notes = fusion._trim_gouges(chain, self.excess_for(blocked),
                                                               column, .05)
        return paths, removed, lost

    def test_closed_chain_rejoins_across_its_seam(self):
        paths, removed, lost = self.trim(self.LOOP, lambda p: abs(p[1] - 1) < 1e-9 and .4 < p[0] < .6,
                                         closed=True)
        self.assertEqual(len(paths), 1)       # one open path, cut only at the gouge
        self.assertEqual((len(removed), lost), (1, []))
        self.assertAlmostEqual(paths[0][0][0], .4, delta=4 * dc.TRIM_TOL)
        self.assertAlmostEqual(paths[0][-1][0], .6, delta=4 * dc.TRIM_TOL)

    def test_stretch_blocked_across_the_seam_is_counted_once(self):
        paths, removed, _ = self.trim(self.LOOP, lambda p: abs(p[1]) < 1e-9 and p[0] < .2,
                                      closed=True)
        self.assertEqual(len(paths), 1)
        self.assertEqual(len(removed), 1)

    def test_trimmed_paths_never_rejoin_across_a_trim(self):
        # Review case: a hairpin's two trim points can lie closer than the
        # join tolerance; the export must not bridge the removed stretch.
        hairpin = [(0, 0, 0), (1, 0, 0), (0, 1e-5, 0)]
        blocked = lambda p: p[0] > .9
        paths, removed, _ = self.trim(hairpin, blocked)
        self.assertEqual(len(paths), 2)
        self.assertLess(dc.dist(paths[0][-1], paths[1][0]), dc.JOIN_TOL)
        edge = NS(body='body', evaluator=NS(
            getParameterExtents=lambda: (True, 0, 1),
            getStrokes=lambda t0, t1, tol: (True, [point((0, 0, 0)), point((1, 0, 0))])))
        segment = dc.Segment(hairpin, None, None, source=edge)
        with patch.object(fusion, 'edge_segment', return_value=segment), \
                patch.object(fusion, '_cut_depth'), \
                patch.object(fusion, '_excess', return_value=self.excess_for(blocked)), \
                patch.object(fusion, '_column_excess', return_value=lambda p: 0.0), \
                patch.object(dc, 'order_chains', return_value=[([segment], False)]), \
                patch.object(dc, 'link_chain', return_value=dc.Chain(pieces=[dc.Piece(hairpin)])):
            chains, _, warnings = fusion.build_chains([edge], self.R, .05, Z)
        self.assertEqual(warnings, [])
        self.assertEqual(sum(len(dc.chain_polylines(c)) for c in chains), 2)

    def build(self, trim_result, log=None):
        edge = NS(body='body', evaluator=NS(
            getParameterExtents=lambda: (True, 0, 1),
            getStrokes=lambda t0, t1, tol: (True, [point((0, 0, 0)), point((1, 0, 0))])))
        segment = dc.Segment([(0, 0, 0), (1, 0, 0)], None, None, source=edge)
        chain = dc.Chain(pieces=[dc.Piece([(0, 0, 0), (1, 0, 0)])])
        with patch.object(fusion, 'edge_segment', return_value=segment), \
                patch.object(fusion, '_cut_depth'), patch.object(fusion, '_excess'), \
                patch.object(fusion, '_column_excess'), \
                patch.object(dc, 'order_chains', return_value=[([segment], False)]), \
                patch.object(dc, 'link_chain', return_value=chain), \
                patch.object(fusion, '_trim_gouges',
                             return_value=tuple(trim_result) + ([],) * (4 - len(trim_result))):
            return fusion.build_chains([edge], self.R, .05, Z, log)

    def test_closed_loop_is_exported_open_from_its_checked_start(self):
        # Review case, confirmed in Fusion: Trace moves the seam of a closed
        # contour, so a loop is cut a little past its start instead.
        paths, removed, lost = self.trim(self.LOOP, lambda p: False, closed=True)
        self.assertEqual((len(paths), removed, lost), (1, [], []))
        self.assertEqual(paths[0][0], self.LOOP[0])
        self.assertAlmostEqual(dc.dist(paths[0][0], paths[0][-1]), fusion.LOOP_OVERLAP)

    def test_loop_closed_again_by_plunge_trimming_is_reopened(self):
        # Review case: the plunge at the overlap end fails, trimming takes the
        # whole overlap away and the ends meet again; Trace could then move
        # the seam. The end is cut back so the export stays open.
        overlap_end = lambda p: p[1] == 0 and 0 < p[0] <= fusion.LOOP_OVERLAP + 1e-9
        paths, removed, lost = self.trim(self.LOOP, lambda p: False, closed=True,
                                         plunge_blocked=overlap_end)
        self.assertEqual((len(paths), lost), (1, []))
        self.assertGreaterEqual(dc.dist(paths[0][0], paths[0][-1]), fusion.LOOP_OVERLAP / 2)
        # The overlap's failing plunge is a gouge; the cut-back that keeps the
        # loop open is only a note.
        self.assertEqual(len(removed), 1)
        self.assertTrue(any('closed loop open' in note for note in self.notes))

    def test_end_whose_plunge_would_gouge_moves_inward(self):
        # Review case: a trimmed end beside a low boss. The ball at the end is
        # clear, but the column it plunges down is wider than the ball.
        line = [(0.0, 0, 0), (1.0, 0, 0)]
        paths, removed, lost = self.trim(line, lambda p: False, plunge_blocked=lambda p: p[0] > .8)
        self.assertEqual((len(paths), lost), (1, []))
        self.assertEqual(paths[0][0], (0.0, 0, 0))
        self.assertAlmostEqual(paths[0][-1][0], .8, delta=.01)
        self.assertEqual(len(removed), 1)
        # A closed loop whose seam cannot be plunged onto opens at the seam.
        paths, removed, _ = self.trim(self.LOOP, lambda p: False, closed=True,
                                      plunge_blocked=lambda p: p[0] < .1 and p[1] < .1)
        self.assertEqual(len(paths), 1)
        self.assertGreater(dc.dist(paths[0][0], paths[0][-1]), .1)

    def test_edge_trimmed_away_inside_a_surviving_path_is_a_warning(self):
        # Review case: edges A and B form one path; trimming removes all of A
        # but keeps B, so no path is lost, yet A is left sharp.
        a, b = NS(body='part'), NS(body='part')
        seg_a = dc.Segment([(0, 0, 0), (.5, 0, 0), (1, 0, 0)], None, None, source=a)
        seg_b = dc.Segment([(1, 0, 0), (1.5, 0, 0), (2, 0, 0)], None, None, source=b)
        chain = dc.Chain(pieces=[dc.Piece([(0, 0, 0), (1, 0, 0), (2, 0, 0)])])
        with patch.object(fusion, 'edge_segment', side_effect=[seg_a, seg_b]), \
                patch.object(fusion, '_cut_depth'), patch.object(fusion, '_excess'), \
                patch.object(fusion, '_column_excess'), \
                patch.object(fusion, '_edge_polyline', return_value=[(0, 0, 0), (2, 0, 0)]), \
                patch.object(dc, 'order_chains', return_value=[([seg_a, seg_b], False)]), \
                patch.object(dc, 'link_chain', return_value=chain), \
                patch.object(fusion, '_trim_gouges',
                             return_value=([[(1.2, 0, 0), (2, 0, 0)]], [(1.2, .1)], [], [])):
            chains, _, warnings = fusion.build_chains([a, b], self.R, .05, Z)
        self.assertEqual(len(chains), 1)
        self.assertEqual(len(warnings), 1)
        self.assertIn('Edge 1: no break left', warnings[0])

    def test_each_lost_path_gives_its_own_reason(self):
        # Review case: one path dropped for gouging, another only for being
        # too short to stay open; neither borrows the other's reason.
        _, _, warnings = self.build(([[(0, 0, 0), (1, 0, 0)]], [(.2, .1)],
                                     [.1, 'a path too short to cut as an open contour was dropped']))
        self.assertIn('2 of the paths skipped', warnings[0])
        self.assertIn('1.000 mm beyond the edge break along the whole path', warnings[0])
        self.assertIn('too short to cut as an open contour', warnings[0])

    def test_messages_never_invent_a_depth(self):
        # Review case: a center-inside rejection has no measured depth.
        _, _, warnings = self.build(([], [(1, math.inf)], [math.inf]))
        self.assertIn("center would be inside the part", warnings[0])
        self.assertNotIn('mm beyond', warnings[0])
        # A path dropped only for being too short says so.
        _, _, warnings = self.build(([], [], ['a path too short to cut as an open contour was dropped'], []))
        self.assertIn('too short', warnings[0])
        self.assertNotIn('mm beyond', warnings[0])

    def test_whole_path_lost_from_a_split_chain_is_a_warning(self):
        logged = []
        chains, _, warnings = self.build(([[(0, 0, 0), (1, 0, 0)]], [(.2, .1), (1, .1)], [.1]),
                                         logged.append)
        self.assertEqual(len(chains), 1)
        self.assertTrue(any('paths skipped' in w for w in warnings))
        self.assertTrue(logged)

    def test_trimming_is_logged_but_a_fully_blocked_chain_is_a_warning(self):
        for paths, expect_warning in (([[(.2, 0, 0), (1, 0, 0)]], False), ([], True)):
            logged = []
            chains, _, warnings = self.build((paths, [(.2, .1)], [] if paths else [.1]), logged.append)
            self.assertEqual(len(chains), 0 if expect_warning else 1)
            self.assertEqual(bool(warnings), expect_warning)
            self.assertEqual(bool(logged), not expect_warning)

    def test_skipped_gouge_checks_keep_paths_whole(self):
        edge = NS(body='body')
        segment = dc.Segment([(0, 0, 0), (1, 0, 0)], None, None, source=edge)
        chain = dc.Chain(pieces=[dc.Piece([(0, 0, 0), (1, 0, 0)])])
        with patch.object(fusion, 'edge_segment', return_value=segment), \
                patch.object(fusion, '_cut_depth') as depth, \
                patch.object(fusion, '_trim_gouges') as trim, \
                patch.object(dc, 'order_chains', return_value=[([segment], False)]), \
                patch.object(dc, 'link_chain', return_value=chain) as link:
            chains, _, _ = fusion.build_chains([edge], self.R, .05, Z, check_gouges=False)
        self.assertEqual(chains, [chain])
        trim.assert_not_called()
        depth.assert_not_called()
        self.assertIsNone(link.call_args.args[5])  # corners joined without the depth check

    def test_skip_setting_is_saved_only_when_on(self):
        edge = NS(entityToken='token', evaluator=NS(getParameterExtents=lambda: (False, 0, 1)),
                  length=1)
        for skip in (False, True):
            op = MagicMock()
            fusion.save_settings(op, [edge], .05, NS(entityToken='sketch'), skip)
            data = fusion.json.loads(op.attributes.add.call_args.args[2])
            self.assertEqual(fusion.SKIP_GOUGE_KEY in data, skip)

    def test_every_selected_body_is_checked_for_every_chain(self):
        # Review case: a ramp on one body running into a floor on another
        # body whose own selected edge forms a separate chain.
        first, second = NS(name='first'), NS(name='second')
        edges = [NS(body=first, evaluator=None), NS(body=second, evaluator=None)]
        segments = [dc.Segment([(0, 0, 0), (1, 0, 0)], None, None, source=e) for e in edges]
        seen = []
        with patch.object(fusion, 'edge_segment', side_effect=segments), \
                patch.object(fusion, '_cut_depth'), \
                patch.object(fusion, '_edge_polyline', return_value=[(0, 0, 0), (1, 0, 0)]), \
                patch.object(fusion, '_excess', side_effect=lambda bodies, *a: seen.append(bodies)
                             or (lambda p, margin=0.0, enough=None: 0.0)), \
                patch.object(fusion, '_column_excess', return_value=lambda p: 0.0), \
                patch.object(dc, 'order_chains', return_value=[([s], False) for s in segments]):
            fusion.build_chains(edges, self.R, .05, Z)
        self.assertEqual(seen, [[first, second], [first, second]])

    def test_unsampleable_edge_skips_only_itself(self):
        # Its neighbour on the same chain keeps its path, checked without the
        # skipped edge's break (stricter).
        good, bad = NS(body='part', name='good'), NS(body='part', name='bad')
        segments = [dc.Segment([(0, 0, 0), (1, 0, 0)], None, None, source=e) for e in (good, bad)]

        def polyline(edge):
            if edge is bad:
                raise dc.dm.DeburrError('could not sample a selected edge for the gouge check')
            return [(0, 0, 0), (1, 0, 0)]
        checked = []
        with patch.object(fusion, 'edge_segment', side_effect=segments), \
                patch.object(fusion, '_cut_depth'), \
                patch.object(fusion, '_edge_polyline', side_effect=polyline), \
                patch.object(fusion, '_excess', side_effect=lambda *a: checked.append(1)
                             or (lambda p, margin=0.0, enough=None: 0.0)), \
                patch.object(fusion, '_column_excess', return_value=lambda p: 0.0), \
                patch.object(dc, 'order_chains', side_effect=lambda segs: [(list(segs), False)]) as order:
            chains, _, warnings = fusion.build_chains([good, bad], self.R, .05, Z)
        self.assertEqual(order.call_args.args[0], segments[:1])
        self.assertEqual(len(chains), 1)
        self.assertEqual(len(checked), 1)
        self.assertEqual(len(warnings), 1)
        self.assertIn('Edge 2 skipped', warnings[0])

    def test_body_whose_selected_edges_were_all_skipped_is_still_checked(self):
        kept_edge = NS(body='part', name='kept')
        skipped_edge = NS(body='neighbour', name='skipped')
        segment = dc.Segment([(0, 0, 0), (1, 0, 0)], None, None, source=kept_edge)

        def edge_segment(edge, *a):
            if edge is skipped_edge:
                raise dc.dm.DeburrError('edge is concave or obstructed')
            return segment
        seen = []
        with patch.object(fusion, 'edge_segment', side_effect=edge_segment), \
                patch.object(fusion, '_cut_depth'), \
                patch.object(fusion, '_edge_polyline', return_value=[(0, 0, 0), (1, 0, 0)]), \
                patch.object(fusion, '_excess', side_effect=lambda bodies, *a: seen.append(bodies)
                             or (lambda p, margin=0.0, enough=None: 0.0)), \
                patch.object(fusion, '_column_excess', return_value=lambda p: 0.0), \
                patch.object(dc, 'order_chains', return_value=[([segment], False)]):
            _, saved, _ = fusion.build_chains([kept_edge, skipped_edge], self.R, .05, Z)
        self.assertEqual(seen, [['part', 'neighbour']])
        # Saved with the operation too, so Edit Deburr checks the same bodies.
        self.assertEqual(saved, [kept_edge, skipped_edge])

    def test_corner_depth_covers_every_body(self):
        with patch.object(fusion, '_cut_depth',
                          side_effect=lambda body, r, axis: lambda tips: body):
            depth = fusion._deepest_cut([.1, .3, .2], self.R, Z)
        self.assertEqual(depth([]), .3)


if __name__ == '__main__':
    unittest.main()
