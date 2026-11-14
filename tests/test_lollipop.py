"""Undercut linking, side access, and clearance regressions."""
import importlib
import math
import unittest
from types import SimpleNamespace as NS
from unittest.mock import MagicMock, patch

from test_safety import adsk, dc, dm, fusion

with patch.dict('sys.modules', {'adsk': adsk, 'adsk.core': adsk.core,
                               'adsk.fusion': adsk.fusion, 'adsk.cam': adsk.cam}):
    lf = importlib.import_module('safety_fusion.lollipop_fusion')


class LollipopTests(unittest.TestCase):
    def test_rotated_cylinder_bounds_use_radial_projection(self):
        component = math.sqrt(.5)
        lo, hi = lf._cylinder_bounds((0, 0, 0), (1, 0, 1), 2,
                                     (component, 0, component))
        self.assertAlmostEqual(lo[0], -math.sqrt(2))
        self.assertAlmostEqual(lo[1], -2)
        self.assertAlmostEqual(lo[2], -math.sqrt(2))
        self.assertAlmostEqual(hi[0], 1 + math.sqrt(2))
        self.assertAlmostEqual(hi[2], 1 + math.sqrt(2))

    def test_holder_above_part_skips_intersection_without_ignoring_head(self):
        outside = adsk.fusion.PointContainment.PointOutsidePointContainment
        body = NS(pointContainment=lambda p: outside,
                  boundingBox=NS(minPoint=NS(x=-5, y=-4, z=0),
                                 maxPoint=NS(x=6, y=4, z=4.7)))
        cutter = NS(radius=.4, noncutting_sections=((5, 8, 3.2),))
        check = lf.Clearance(cutter, (0, 0, 1), [body], [], .02)
        check.measure = MagicMock()
        check.measure.measureMinimumDistance.return_value.value = .4
        check.temp = MagicMock()
        self.assertIsNone(check.point((0, 0, 0), .001))
        check.temp.createCylinderOrCone.assert_not_called()
        check.measure.measureMinimumDistance.assert_called_once()

    def test_cam_proxy_resolves_to_design_body_by_identity_and_placement(self):
        def box(x=0):
            return NS(minPoint=NS(x=x, y=0, z=0), maxPoint=NS(x=x+1, y=1, z=1))
        body = NS(isSolid=True, nativeObject=None, boundingBox=box(), assemblyContext=None)
        proxy = NS(isSolid=True, nativeObject=body, boundingBox=box(), assemblyContext=None)
        design = NS(rootComponent=NS(bRepBodies=[body], allOccurrences=[]))
        setup = NS(models=[proxy], fixtures=[])
        with patch.object(adsk.fusion.BRepBody, 'cast', side_effect=lambda e: e), \
                patch.object(adsk.fusion.Occurrence, 'cast', return_value=None):
            models, fixtures = lf.setup_obstacles(setup, design)
            self.assertIs(models[0], body)
            self.assertEqual(fixtures, [])
            proxy.boundingBox = box(.1)
            with self.assertRaisesRegex(dm.DeburrError, 'repositioned manufacturing'):
                lf.setup_obstacles(setup, design)

    def test_selected_cam_edges_resolve_into_design_and_keep_native_picks(self):
        box = NS(minPoint=NS(x=0, y=0, z=0), maxPoint=NS(x=1, y=1, z=1))
        body = NS(nativeObject=None, assemblyContext=None, boundingBox=box)
        proxy = NS(nativeObject=body, assemblyContext=None, boundingBox=box)
        native = NS(body=body, nativeObject=None)
        picked = NS(body=proxy, nativeObject=native)
        resolved = lf.setup_edges([picked, native], [body])
        self.assertIs(resolved[0], native)
        self.assertIs(resolved[1], native)
        with patch.object(lf, 'Clearance'), patch.object(lf, '_directions', return_value=[]), \
                patch.object(lf.df, 'edge_segment', return_value='segment') as sample, \
                patch.object(lf.dc, 'order_chains', return_value=[]):
            cutter = NS(radius=.4)
            cutter.variants = lambda: (cutter,)
            chains, used, notes = lf.build_chains([picked], cutter, .02,
                                                 (0, 0, 1), [body])
        self.assertIs(sample.call_args.args[0], native)
        self.assertIs(used[0], native)

    def test_selected_cam_edge_retains_design_occurrence_placement(self):
        box = NS(minPoint=NS(x=2, y=0, z=0), maxPoint=NS(x=3, y=1, z=1))
        matrix = [1, 0, 0, 2, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]
        context = NS(transform2=NS(asArray=lambda: matrix))
        native_body = NS(nativeObject=None, assemblyContext=None)
        body = NS(nativeObject=native_body, assemblyContext=context, boundingBox=box)
        proxy = NS(nativeObject=native_body, assemblyContext=NS(
            transform2=NS(asArray=lambda: list(matrix))), boundingBox=box)
        mapped = NS(body=body)
        native = NS(createForAssemblyContext=MagicMock(return_value=mapped))
        self.assertIs(lf.setup_edges([NS(body=proxy, nativeObject=native)], [body])[0], mapped)
        native.createForAssemblyContext.assert_called_once_with(context)

    def test_selected_edge_rejects_wrong_model_translation_and_symmetric_rotation(self):
        box = NS(minPoint=NS(x=-1, y=-1, z=-1), maxPoint=NS(x=1, y=1, z=1))
        body = NS(nativeObject=None, assemblyContext=None, boundingBox=box)
        wrong = NS(nativeObject=None, assemblyContext=None, boundingBox=box, other=True)
        translated = [1, 0, 0, 1, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]
        rotated = [-1, 0, 0, 0, 0, -1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]
        proxies = [wrong] + [NS(nativeObject=body, boundingBox=box, assemblyContext=NS(
            transform2=NS(asArray=lambda m=m: m))) for m in (translated, rotated)]
        for proxy in proxies:
            with self.subTest(proxy=proxy), self.assertRaisesRegex(dm.DeburrError, 'must belong'):
                lf.setup_edges([NS(body=proxy)], [body])

    def test_unresolvable_edge_proxy_is_rejected(self):
        box = NS(minPoint=NS(x=0, y=0, z=0), maxPoint=NS(x=1, y=1, z=1))
        context = NS(transform2=NS(asArray=lambda: [1, 0, 0, 0, 0, 1, 0, 0,
                                                   0, 0, 1, 0, 0, 0, 0, 1]))
        native_body = NS(nativeObject=None)
        body = NS(nativeObject=native_body, boundingBox=box, assemblyContext=context)
        proxy = NS(nativeObject=native_body, boundingBox=box, assemblyContext=None)
        native = NS(createForAssemblyContext=lambda c: None)
        with self.assertRaisesRegex(dm.DeburrError, 'could not resolve'):
            lf.setup_edges([NS(body=proxy, nativeObject=native)], [body])

    def test_trace_preserves_approach_and_withdrawal_geometry(self):
        params = {name: NS(expression='unsafe') for name, _ in fusion.TRACE_EXPRESSIONS}
        op = NS(parameters=NS(itemByName=params.get))
        fusion._trace_settings(op)
        for name in ('doLeadIn', 'doLeadOut', 'smoothingFilter', 'orderByDepth'):
            self.assertEqual(params[name].expression, 'false')
        self.assertEqual(params['preserveOrder'].expression, 'true')
        self.assertEqual(params['retractionPolicy'].expression, "'full'")

    def test_upper_hemisphere_corner_roll_uses_lollipop_limit(self):
        a = dc.End((0, 0, 0), {'shared': (0, 0, -1), 'a': (1, 0, 0)})
        b = dc.End((0, 0, 0), {'shared': (0, 0, -1), 'b': (0, 1, 0)})
        with self.assertRaises(dm.DeburrError):
            dc.corner_roll(a, b, (0, 0, 1), .4, .02)
        limit = dm.lollipop_contact_height(.4, .2, .8)
        points = dc.corner_roll(a, b, (0, 0, 1), .4, .02, limit)
        self.assertGreater(len(points), 10)
        self.assertAlmostEqual(points[0][2], points[-1][2])

    def test_lollipop_does_not_fall_back_to_unbounded_vertex_crossing(self):
        cutter = NS(max_contact_height=.3)
        notes = []
        with patch.object(dc, 'corner_roll', side_effect=dm.DeburrError('neck contact')), \
                patch.object(dc, 'vertex_crossing') as crossing:
            result = dc._corner(0, None, None, (0, 0, 0), (1, 0, 0),
                                (0, 0, 1), .4, .02, None, notes, cutter)
        self.assertIsNone(result)
        crossing.assert_not_called()
        self.assertIn('paths split', notes[0])

    def test_closed_cut_gets_distinct_exterior_endpoints(self):
        box = NS(minPoint=NS(x=-1, y=-1, z=0), maxPoint=NS(x=1, y=1, z=3))
        check = NS(bodies=[NS(boundingBox=box)], fixtures=[],
                   cutter=NS(radius=.4, noncutting_sections=((.7, 2, .2),)),
                   path=MagicMock(return_value=None))
        points = [(0, -1.3, 1), (.2, -1.3, 1), (0, -1.3, 1)]
        result = lf.add_access(points, [(0, -1, 0)], check)
        self.assertEqual(result[1:-1], points)
        self.assertNotEqual(result[0], result[-1])
        self.assertLess(result[0][1], -1.4)
        self.assertLess(result[-1][1], result[0][1])

    def test_blocked_access_is_rejected(self):
        check = NS(bodies=[NS(boundingBox=NS(minPoint=NS(x=0, y=0, z=0),
                                             maxPoint=NS(x=1, y=1, z=1)))],
                   fixtures=[], cutter=NS(radius=.4, noncutting_sections=()),
                   path=MagicMock(return_value='shaft collision'))
        with self.assertRaisesRegex(dm.DeburrError, 'no clear approach'):
            lf.add_access([(0, 0, 0), (1, 0, 0)], [(0, -1, 0)], check)

    def test_clearance_samples_between_endpoints(self):
        check = lf.Clearance(NS(), (0, 0, 1), [], [], .02)
        check.point = MagicMock(side_effect=lambda p, margin: 'collision' if .49 < p[0] < .51 else None)
        self.assertEqual(check.path([(0, 0, 0), (1, 0, 0)]), 'collision')
        self.assertGreater(check.point.call_count, 40)
        for call in check.point.call_args_list:
            self.assertGreaterEqual(call.args[1], lf.CLEARANCE_MARGIN)

    def test_close_clearance_refines_instead_of_rejecting_small_breaks(self):
        check = lf.Clearance(NS(), (0, 0, 1), [], [], .001)
        check.point = MagicMock(side_effect=lambda p, margin: 'coarse envelope' if margin > .0012 else None)
        self.assertIsNone(check.path([(0, 0, 0), (.01, 0, 0)]))
        self.assertGreater(check.point.call_count, 10)

    def test_neck_collision_and_failed_intersection_are_rejected(self):
        outside = adsk.fusion.PointContainment.PointOutsidePointContainment
        body = NS(pointContainment=lambda p: outside)
        cutter = NS(radius=.4, noncutting_sections=((.75, 2, .2),))
        for success, faces, expected in ((True, 1, 'neck, shaft'), (True, 0, None),
                                          (False, 0, 'could not check')):
            check = lf.Clearance(cutter, (0, 0, 1), [body], [], .02)
            check.measure = MagicMock()
            check.measure.measureMinimumDistance.return_value.value = .4
            check.temp = MagicMock()
            check.temp.booleanOperation.return_value = success
            check.temp.createCylinderOrCone.return_value.faces.count = faces
            with patch.object(lf, '_overlap', return_value=True), \
                    patch.object(adsk.core.Point3D, 'create', side_effect=lambda *p: p):
                if not success:
                    with self.assertRaisesRegex(dm.DeburrError, expected):
                        check.point((0, 0, 0), .001)
                else:
                    result = check.point((0, 0, 0), .001)
                    if expected:
                        self.assertIn(expected, result)
                    else:
                        self.assertIsNone(result)

    def test_fixtures_have_no_edge_break_allowance(self):
        outside = adsk.fusion.PointContainment.PointOutsidePointContainment
        body = NS(pointContainment=lambda p: outside)
        for fixtures, expected in (([], None), ([body], 'touches fixture')):
            check = lf.Clearance(NS(radius=.4, noncutting_sections=()), (0, 0, 1),
                                 [] if fixtures else [body], fixtures, .02)
            check.measure = MagicMock()
            check.measure.measureMinimumDistance.return_value.value = .39
            result = check.point((0, 0, 0), .001)
            if expected:
                self.assertIn(expected, result)
            else:
                self.assertIsNone(result)

    def test_fixture_margin_does_not_spend_the_cutting_chord_tolerance(self):
        outside = adsk.fusion.PointContainment.PointOutsidePointContainment
        body = NS(pointContainment=lambda p: outside)
        check = lf.Clearance(NS(radius=.4, noncutting_sections=()), (0, 0, 1), [], [body], .02)
        check.measure = MagicMock()
        check.measure.measureMinimumDistance.return_value.value = .4005
        self.assertIn('touches fixture', check.point((0, 0, 0), .001))


class ChamferCheckTests(unittest.TestCase):
    """Cone cutters reuse the checked path: gouge limits, flanks, back-side access."""
    CUTTER = NS(radius=.6, noncutting_sections=(), frustums=((0, .3, .3, .6),))

    def cone_check(self, vertices, near, fixture=False):
        body = NS()
        check = lf.Clearance(self.CUTTER, (0, 0, 1), [] if fixture else [body],
                             [body] if fixture else [], .05, edges=['edge'])
        check.temp = MagicMock()
        cut = check.temp.createCylinderOrCone.return_value
        cut.faces.count = 1
        cut.vertices = [NS(geometry=NS(x=x, y=y, z=z)) for x, y, z in vertices]
        cut.edges = []
        check._near_edges = MagicMock(side_effect=lambda p, limit: near(p, limit))
        with patch.object(lf, '_overlap', return_value=True), \
                patch.object(adsk.core.Point3D, 'create', side_effect=lambda *p: p):
            return check.point((0, 0, 0), .001), check

    def test_cone_cut_must_stay_near_selected_edges(self):
        near = lambda p, limit: abs(p[0]) <= limit
        result, check = self.cone_check([(.04, 0, 0), (-.03, 0, 0)], near)
        self.assertIsNone(result)
        # The allowance covers the break width plus the inflated motion envelope.
        limit = check._near_edges.call_args.args[1]
        self.assertGreater(limit, .05)
        self.assertLess(limit, .05 + .003)
        result, _ = self.cone_check([(.04, 0, 0), (.3, 0, 0)], near)
        self.assertIn('away from the selected edges', result)

    def test_cone_may_not_touch_fixtures(self):
        result, _ = self.cone_check([(0, 0, 0)], lambda p, limit: True, fixture=True)
        self.assertIn('touches fixture', result)

    def test_edges_use_first_flank_that_can_cut_them(self):
        top = NS(name='topside', radius=.6, frustums=(), noncutting_sections=())
        back = NS(name='back side', radius=.6, frustums=(), noncutting_sections=())
        tool = NS(radius=.6, noncutting_sections=(), variants=lambda: (top, back))

        def segment(edge, radius, width, axis, ids, variant):
            if edge.flank != variant.name:
                raise dm.DeburrError('cutter flank cannot reach this edge')
            return variant.name
        edges = [NS(flank='topside'), NS(flank='back side')]
        with patch.object(lf, 'setup_edges', side_effect=lambda e, b: e), \
                patch.object(lf, 'Clearance'), patch.object(lf, '_directions', return_value=[]), \
                patch.object(lf.df, 'edge_segment', side_effect=segment), \
                patch.object(lf.dc, 'order_chains', return_value=[]) as order:
            lf.build_chains(edges, tool, .05, (0, 0, 1), [])
            self.assertEqual([c.args[0] for c in order.call_args_list],
                             [['topside'], ['back side']])
            edges.append(NS(flank='neither'))
            with self.assertRaisesRegex(dm.DeburrError, 'edge 3 cannot be cut.*back side'):
                lf.build_chains(edges, tool, .05, (0, 0, 1), [])

    def test_back_side_cut_steps_toward_axis_before_rising(self):
        box = NS(minPoint=NS(x=-1, y=-1, z=0), maxPoint=NS(x=1, y=1, z=1))
        start, end = (0, 0, .2), (.1, 0, .2)
        lateral_or_vertical = lambda pts: 'gouge' if len(pts) == 2 else None
        check = NS(bodies=[NS(boundingBox=box)], fixtures=[], axis=(0, 0, 1),
                   cutter=NS(radius=.6, noncutting_sections=()),
                   path=MagicMock(side_effect=lateral_or_vertical))
        retreats = {start: [((1, 0, 0), .3)], end: [((-1, 0, 0), .2)]}
        result = lf.add_access([start, end], [(0, -1, 0), (0, 0, 1)], check, retreats.get)
        self.assertEqual(result[2:4], [start, end])
        self.assertEqual(result[1], (.3, 0, .2))
        self.assertEqual(result[4], (-.1, 0, .2))
        self.assertGreater(result[0][2], 1)
        self.assertGreater(result[5][2], result[0][2])


class ChamferWiringTests(unittest.TestCase):
    """The real build_chains loop, cut-region sampling, and Trace safety settings."""

    def test_inflated_flank_covers_margin_everywhere(self):
        for flank in ((0, .3, .3, .6), (.3, .6, .6, .3), (0, .1, .05, .6)):
            calls = []
            temp = MagicMock()
            def cone(a, ra, b, rb):
                calls.append((a, ra, b, rb))
                result = MagicMock(); result.faces.count = 0
                return result
            temp.createCylinderOrCone.side_effect = cone
            check = lf.Clearance(NS(radius=.6, noncutting_sections=(), frustums=(flank,)),
                                 (0, 0, 1), [NS()], [], .05, edges=['e'])
            check.temp = temp
            margin = .01
            with patch.object(lf, '_overlap', return_value=True), \
                    patch.object(adsk.core.Point3D, 'create', side_effect=lambda *p: p):
                self.assertIsNone(check.point((0, 0, 0), margin))
            (a, ra, b, rb), = calls
            z0, r0, z1, r1 = flank
            def inflated(z):
                return ra + (rb - ra) * (z - a[2]) / (b[2] - a[2])
            # Every point within `margin` of the flank surface or its rims is inside.
            length = math.hypot(z1 - z0, r1 - r0)
            normal = ((z1 - z0) / length, -(r1 - r0) / length)       # (radial, axial)
            for t in [i / 20 for i in range(21)]:
                z, r = z0 + (z1 - z0) * t, r0 + (r1 - r0) * t
                for ang in range(0, 360, 10):
                    dr = margin * math.cos(math.radians(ang)); dz = margin * math.sin(math.radians(ang))
                    if dr * normal[0] + dz * normal[1] < 0 and 0 < t < 1:
                        continue   # inward side of the surface is inside the tool already
                    pz, pr = z + dz, r + dr
                    with self.subTest(flank=flank, t=t, ang=ang):
                        self.assertGreaterEqual(pz, a[2] - 1e-12)
                        self.assertLessEqual(pz, b[2] + 1e-12)
                        self.assertLessEqual(pr, inflated(pz) + 1e-9)

    def test_cut_region_edges_are_sampled_and_prefiltered(self):
        # One selected edge along x at the origin; the removed region has an
        # edge whose stroke points run 0..0.04 cm away from it, then one at 0.3.
        edge = NS(boundingBox=NS(minPoint=NS(x=-1, y=0, z=0), maxPoint=NS(x=1, y=0, z=0)))
        far = NS(boundingBox=NS(minPoint=NS(x=5, y=5, z=5), maxPoint=NS(x=6, y=6, z=6)))
        measure = MagicMock()
        measure.measureMinimumDistance.side_effect = lambda p, e: NS(value=math.hypot(p[1], p[2]))
        for reach, expected in ((.04, None), (.3, 'away from the selected edges')):
            stroke = [NS(x=0, y=0, z=0), NS(x=.1, y=reach, z=0)]
            cut_edge = NS(evaluator=NS(getParameterExtents=lambda: (True, 0, 1),
                                       getStrokes=lambda t0, t1, tol, s=stroke: (True, s)))
            cut = MagicMock(); cut.faces.count = 1; cut.vertices = []; cut.edges = [cut_edge]
            check = lf.Clearance(NS(radius=.6, noncutting_sections=(), frustums=((0, .3, .3, .6),)),
                                 (0, 0, 1), [NS()], [], .05, edges=[far, edge])
            check.measure = measure
            check.temp = MagicMock(); check.temp.createCylinderOrCone.return_value = cut
            with self.subTest(reach=reach), patch.object(lf, '_overlap', return_value=True), \
                    patch.object(adsk.core.Point3D, 'create', side_effect=lambda *p: p):
                result = check.point((0, 0, 0), .001)
                if expected:
                    self.assertIn(expected, result)
                else:
                    self.assertIsNone(result)
            # The far edge's box never reaches a distance measurement.
            self.assertTrue(all(call.args[1] is edge for call in measure.measureMinimumDistance.call_args_list))

    def test_build_chains_runs_cones_without_ball_depth_and_offers_retreats(self):
        from FusionFreeDeburr import cutter_profile as cp
        harvey = {'type': 'thread mill', 'unit': 'inches',
                  'geometry': {'DC': .5, 'thread-profile-angle': 90, 'LCF': .25, 'SFDM': .25,
                               'shoulder-diameter': .25, 'shoulder-length': .25, 'OAL': 3.25,
                               'thread-tip-type': 'point', 'NT': 1}}
        tool = cp.from_tool_json(harvey)
        back = tool.variants()[1]
        # Bottom rim of a square through-hole (material outside the square,
        # z > 0): four back-side edges meeting at concave corners.
        down = (0, 0, -1)
        corners = [(-1, -1, 0), (1, -1, 0), (1, 1, 0), (-1, 1, 0)]
        walls = [(0, 1, 0), (-1, 0, 0), (0, -1, 0), (1, 0, 0)]   # wall normals point into the hole
        segments = []
        for k in range(4):
            a, b, n = corners[k], corners[(k + 1) % 4], walls[k]
            ids = {'bottom': down, f'wall{k}': n}
            tips = [back.tip(p, down, n, (0, 0, 1), .05) for p in (a, b)]
            t = dm.normalize(dm.sub(b, a))
            segments.append(lf.dc.Segment(tips, lf.dc.End(a, ids, t), lf.dc.End(b, ids, dm.scale(t, -1)),
                                          start_key=k, end_key=(k + 1) % 4))
        edges = [NS(i=k) for k in range(4)]
        seg_of = {id(e): s for e, s in zip(edges, segments)}

        def edge_segment(edge, radius, width, axis, ids, variant):
            if variant.name != 'back side':
                raise dm.DeburrError('cutter flank cannot reach this edge')
            return seg_of[id(edge)]
        captured = []
        def add_access(points, directions, clearance, retreats=None):
            captured.append((points, retreats))
            return points
        with patch.object(lf, 'setup_edges', side_effect=lambda e, b: e), \
                patch.object(lf, 'Clearance') as clearance, \
                patch.object(lf, '_directions', return_value=[]), \
                patch.object(lf.df, 'edge_segment', side_effect=edge_segment), \
                patch.object(lf.df, '_cut_depth', side_effect=AssertionError('ball depth used for a cone')), \
                patch.object(lf, 'add_access', side_effect=add_access):
            clearance.return_value.path.return_value = None
            chains, used, notes = lf.build_chains(edges, tool, .05, (0, 0, 1), [])
        self.assertEqual(len(chains), 1)
        (points, retreats), = captured
        self.assertTrue(callable(retreats))
        # The closed loop starts at a trimmed concave corner; a combined step
        # toward the hole center is offered there, clearing both walls.
        options = retreats(points[0])
        directions = [d for d, _ in options]
        self.assertTrue(any(d[0] * d[1] != 0 for d in directions), options)
        for d, dist in options:
            self.assertGreater(dist, 0)

    def test_chamfer_tool_requires_disabled_trace_chamfer(self):
        def operation(do_chamfer_present, reads_back):
            params = {name: NS(expression=None) for name, _ in fusion.TRACE_EXPRESSIONS}
            params['tool_type'] = NS(value=NS(value='chamfer mill'))
            if not do_chamfer_present:
                del params['doChamfer']
            else:
                params['doChamfer'].value = NS(value=not reads_back)
            return NS(parameters=NS(itemByName=params.get))
        fusion._trace_settings(operation(True, True))
        with self.assertRaisesRegex(dm.DeburrError, 'doChamfer'):
            fusion._trace_settings(operation(False, True))
        with self.assertRaisesRegex(dm.DeburrError, 'could not be disabled'):
            fusion._trace_settings(operation(True, False))

    def test_edit_assigns_tool_before_trace_settings(self):
        order = []
        op = MagicMock()
        type(op).tool = property(lambda self: 'old', lambda self, v: order.append('tool'))
        with patch.object(fusion, 'load_settings', return_value={}), \
                patch.object(fusion, 'find_by_token', return_value=None), \
                patch.object(fusion, '_curves_parameter'), \
                patch.object(fusion, 'build_sketch', return_value=(MagicMock(), [])), \
                patch.object(fusion, 'update_trace_curves', side_effect=lambda *a: order.append('settings')), \
                patch.object(fusion, 'save_settings'):
            fusion.update_operation(MagicMock(), op, [], [], .05, tool='new')
        self.assertEqual(order[:2], ['tool', 'settings'])
