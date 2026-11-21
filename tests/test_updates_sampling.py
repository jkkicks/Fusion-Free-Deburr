"""Regressions for sketch replacement and normal-sensitive sampling."""
import math
import unittest
from types import SimpleNamespace as NS
from unittest.mock import MagicMock, patch

from test_safety import fusion, dm, adsk


class UpdateTests(unittest.TestCase):
    def test_failed_updates_restore_operation_and_remove_replacement(self):
        for failure in ('curves', 'settings', 'delete', None):
            with self.subTest(failure=failure):
                new = MagicMock()

                class OldSketch:
                    def __init__(self):
                        self._name = 'Deburr'
                        self.deleteMe = MagicMock()

                    @property
                    def name(self):
                        return self._name

                    @name.setter
                    def name(self, name):
                        # Fusion adds a suffix if the replacement still owns this name.
                        occupied = new.name == name and not new.deleteMe.called
                        self._name = name + ' (1)' if occupied else name

                old = OldSketch()
                old.deleteMe.return_value = failure != 'delete'
                params = {n: NS(expression='original') for n in
                          ('compensation', 'compensationType', 'axialOffset', 'radialOffset')}
                op = MagicMock(name='operation')
                op.name, op.tool = 'Deburr', 'old tool'
                op.parameters.itemByName.side_effect = params.get
                attr = NS(value='original settings')
                op.attributes.itemByName.return_value = attr
                op.attributes.add.side_effect = lambda group, name, data: setattr(attr, 'value', data)
                value = MagicMock()
                value.getCurveSelections.return_value = ['old curves']
                selected = ['old curves']
                value.applyCurveSelections.side_effect = lambda curves: selected.__setitem__(slice(None), curves)

                def update(op, curves):
                    params['axialOffset'].expression = '0 mm'
                    selected[:] = ['new curves']
                    if failure == 'curves':
                        raise RuntimeError('curve application failed')

                def save(*args):
                    attr.value = 'new settings'
                    if failure == 'settings':
                        raise RuntimeError('settings save failed')

                with patch.object(fusion, 'load_settings', return_value={'sketch': 'old token'}), \
                     patch.object(fusion, 'find_by_token', return_value=old), \
                     patch.object(fusion, '_curves_parameter', return_value=value), \
                     patch.object(fusion, 'build_sketch', return_value=(new, [['new curves']])), \
                     patch.object(fusion, 'update_trace_curves', side_effect=update), \
                     patch.object(fusion, 'save_settings', side_effect=save):
                    if failure:
                        with self.assertRaises(Exception):
                            fusion.update_operation(None, op, [], [], .1, 'new tool')
                        self.assertEqual(op.tool, 'old tool')
                        self.assertEqual(selected, ['old curves'])
                        self.assertEqual(attr.value, 'original settings')
                        self.assertEqual(params['axialOffset'].expression, 'original')
                        self.assertEqual(old.name, 'Deburr')
                        new.deleteMe.assert_called_once()
                        if failure != 'delete':
                            old.deleteMe.assert_not_called()
                    else:
                        self.assertIs(fusion.update_operation(None, op, [], [], .1, 'new tool'), new)
                        self.assertEqual(op.tool, 'new tool')
                        old.deleteMe.assert_called_once()
                        new.deleteMe.assert_not_called()

    def test_partial_sketch_is_removed_when_building_fails(self):
        design = MagicMock()
        sketch = design.rootComponent.sketches.add.return_value
        sketch.sketchCurves.sketchFixedSplines.addByNurbsCurve.side_effect = RuntimeError('spline failure')
        with patch.object(fusion.dc, 'chain_polylines', return_value=[[(0, 0, 0), (1, 0, 0)]]):
            with self.assertRaisesRegex(RuntimeError, 'spline failure'):
                fusion.build_sketch(design, [object()], 'Deburr')
        sketch.deleteMe.assert_called_once()

    def test_failed_trace_creation_removes_operation(self):
        setup = MagicMock()
        op = setup.operations.add.return_value
        with patch.object(fusion, '_trace_settings', side_effect=RuntimeError('settings failure')):
            with self.assertRaises(RuntimeError):
                fusion.create_trace(setup, [], 'Deburr')
        op.deleteMe.assert_called_once()

    def test_rollback_failure_keeps_geometry_that_may_be_referenced(self):
        op, old, new, value = MagicMock(), MagicMock(), MagicMock(), MagicMock()
        old.name = 'Deburr'
        value.applyCurveSelections.side_effect = RuntimeError('restore failure')
        with patch.object(fusion, 'load_settings', return_value={'sketch': 'old'}), \
             patch.object(fusion, 'find_by_token', return_value=old), \
             patch.object(fusion, '_curves_parameter', return_value=value), \
             patch.object(fusion, 'build_sketch', return_value=(new, [])), \
             patch.object(fusion, 'update_trace_curves', side_effect=RuntimeError('update failure')):
            with self.assertRaisesRegex(dm.DeburrError, 'both sketches retained'):
                fusion.update_operation(None, op, [], [], .1)
        old.deleteMe.assert_not_called()
        new.deleteMe.assert_not_called()


class SamplingTests(unittest.TestCase):
    def edge(self):
        ev = MagicMock()
        ev.getParameterExtents.return_value = (True, 0, .01)
        ev.getLengthAtParameter.side_effect = lambda a, b: (True, b - a)
        ev.getPointAtParameter.side_effect = lambda t: (True, NS(x=t, y=0, z=0))
        ev.getParameterAtPoint.side_effect = lambda p: (True, p[0])
        ev.getParameterAtLength.side_effect = lambda t, length: (True, t + length)
        ev.getFirstDerivative.side_effect = lambda t: (True, NS(x=1, y=0, z=0))
        edge = NS(evaluator=ev, faces=MagicMock())
        edge.faces.item.side_effect = lambda i: i
        return edge

    def normal(self, face, p):
        angle = math.pi * p[0] / .02
        return (0, 0, 1) if face == 0 else (math.cos(angle), math.sin(angle), 0)

    def test_straight_edge_with_changing_normals_refines_tool_tip(self):
        edge = self.edge()
        points = [(0, 0, 0), (.01, 0, 0)]
        with patch.object(fusion, '_normal', side_effect=self.normal):
            samples = fusion._refine_samples(edge, points, .3, .025, (0, 0, 1))
        self.assertGreater(len(samples), 2)
        def tip(p):
            return dm.tool_tip(p, self.normal(0, p), self.normal(1, p), (0, 0, 1), .3, .025)
        for a, b in zip(samples, samples[1:]):
            for k in range(11):
                t = k / 10
                p = dm.add(a, dm.scale(dm.sub(b, a), t))
                chord = dm.add(tip(a), dm.scale(dm.sub(tip(b), tip(a)), t))
                self.assertLessEqual(fusion.dc.dist(tip(p), chord), fusion.PATH_TOL)

    def test_constant_normals_on_short_straight_edge_need_only_two_points(self):
        edge = self.edge()
        with patch.object(fusion, '_normal', side_effect=lambda face, p: (0, 0, 1) if face == 0 else (1, 0, 0)):
            samples = fusion._refine_samples(edge, [(0, 0, 0), (.01, 0, 0)], .3, .025, (0, 0, 1))
        self.assertEqual(len(samples), 2)

    def test_unresolved_normal_change_is_rejected_at_refinement_limit(self):
        edge = self.edge()
        def normal(face, p):
            if face == 0:
                return (0, 0, 1)
            return (1, 0, 0) if p[0] == 0 else (0, 1, 0)
        with patch.object(fusion, '_normal', side_effect=normal):
            with self.assertRaisesRegex(dm.DeburrError, 'sampling tolerance'):
                fusion._refine_samples(edge, [(0, 0, 0), (.01, 0, 0)], .3, .025, (0, 0, 1))
        self.assertLessEqual(edge.evaluator.getPointAtParameter.call_count, 13)

    def test_closed_seam_uses_parameter_extent_instead_of_ambiguous_point_lookup(self):
        edge = self.edge()
        ev = edge.evaluator
        radius = .002
        ev.getParameterExtents.return_value = (True, 0, 2 * math.pi)
        ev.getLengthAtParameter.side_effect = lambda a, b: (True, radius * (b - a))
        ev.getPointAtParameter.side_effect = lambda t: (True, NS(x=radius * math.cos(t), y=radius * math.sin(t), z=0))
        ev.getParameterAtPoint.side_effect = lambda p: (True, math.atan2(p[1], p[0]) % (2 * math.pi))
        points = [(radius, 0, 0), (0, radius, 0), (-radius, 0, 0), (0, -radius, 0), (radius, 0, 0)]
        with patch.object(fusion, '_normal', side_effect=lambda face, p: (0, 0, 1) if face == 0 else (1, 0, 0)), \
             patch.object(adsk.core.Point3D, 'create', side_effect=lambda *p: p):
            fusion._refine_samples(edge, points, .3, .025, (0, 0, 1))
        self.assertAlmostEqual(ev.getPointAtParameter.call_args.args[0], 7 * math.pi / 4)

    @staticmethod
    def strokes_edge(t0, t1, point_at, strokes, param_at=None, speed=1, derivative=None):
        """A mock edge that returns `strokes` (or `strokes(a, b)`) from
        getStrokes and, like Fusion, raises for a parameter outside its extents. Its length per
        unit of parameter is `speed`; `derivative(t)` defaults to a central
        difference of point_at."""
        def point(t):
            if not t0 <= t <= t1:
                raise RuntimeError('3 : invalid argument parameter')
            return True, NS(**dict(zip('xyz', point_at(t))))

        def first_derivative(t):
            if not t0 <= t <= t1:
                raise RuntimeError('3 : invalid argument parameter')
            if derivative:
                return True, NS(**dict(zip('xyz', derivative(t))))
            h = (t1 - t0) * 1e-6
            a, b = point_at(t - h), point_at(t + h)
            return True, NS(**dict(zip('xyz', ((q - p) / (2 * h) for p, q in zip(a, b)))))
        ev = NS(getParameterExtents=lambda: (True, t0, t1), getPointAtParameter=point,
                getStrokes=lambda a, b, tol: (True, [NS(**dict(zip('xyz', p))) for p in
                                                     (strokes(a, b) if callable(strokes) else strokes)]),
                getParameterAtPoint=lambda p: (True, param_at(p)),
                getLengthAtParameter=lambda a, b: (True, abs(b - a) * speed),
                getParameterAtLength=lambda t, length: (True, t + length / speed),
                getFirstDerivative=first_derivative)
        return NS(evaluator=ev, length=1)

    def test_strokes_from_end_to_start_are_put_in_parameter_order(self):
        # Seen in Fusion on a straight edge between a plane and a curved face,
        # and on an arc: strokes ran t1 -> t0, so refinement paired each point
        # with the other end's parameter and never converged.
        edge = self.strokes_edge(-.015, -.011, lambda t: (0, 0, t),
                                 [(0, 0, -.011), (0, 0, -.013), (0, 0, -.015)])
        self.assertEqual(fusion._sample_edge(edge, False),
                         [(0, 0, -.015), (0, 0, -.013), (0, 0, -.011)])
        forward = self.strokes_edge(0, 1, lambda t: (t, 0, 0), [(0, 0, 0), (1, 0, 0)])
        self.assertEqual(fusion._sample_edge(forward, False), [(0, 0, 0), (1, 0, 0)])

    @staticmethod
    def ranged(point_at, params, backwards=False, repeat_start=False):
        """getStrokes for a range, as Fusion strokes it: P(a), the stroke
        points strictly inside (a, b), then P(b); from b back to a if
        `backwards`, and with the start repeated if `repeat_start`."""
        def strokes(a, b):
            points = [point_at(a)] * (2 if repeat_start else 1) \
                + [point_at(t) for t in params if a < t < b] + [point_at(b)]
            return points[::-1] if backwards else points
        return strokes

    def test_closed_strokes_running_backwards_are_reversed(self):
        def point_at(t):
            return (math.cos(t), math.sin(t), 0)
        quarters = [0, math.pi / 2, math.pi, 3 * math.pi / 2, 2 * math.pi]
        for backwards in (False, True):
            with self.subTest(backwards=backwards):
                edge = self.strokes_edge(0, 2 * math.pi, point_at, self.ranged(point_at, quarters, backwards))
                self.assertEqual(fusion._sample_edge(edge, False, closed=True),
                                 [point_at(t) for t in quarters])

    def test_repeated_seam_point_does_not_reverse_closed_strokes(self):
        # A stroke run that starts with its first point twice must keep its
        # direction, and a failed evaluation is an error, not a guess.
        def point_at(t):
            return (math.cos(t), math.sin(t), 0)
        quarters = [0, math.pi / 2, math.pi, 3 * math.pi / 2, 2 * math.pi]
        for backwards in (False, True):
            with self.subTest(backwards=backwards):
                edge = self.strokes_edge(0, 2 * math.pi, point_at,
                                         self.ranged(point_at, quarters, backwards, repeat_start=True))
                samples = fusion._sample_edge(edge, False, closed=True)
                self.assertEqual((samples[0], samples[-1]), (point_at(0), point_at(2 * math.pi)))
                angles = [math.atan2(p[1], p[0]) % (2 * math.pi) for p in samples[:-1]]
                self.assertEqual(angles, sorted(angles))
        edge.evaluator.getPointAtParameter = lambda t: (False, None)
        with self.assertRaisesRegex(dm.DeburrError, 'could not'):
            fusion._sample_edge(edge, False, closed=True)

    def test_reversed_open_edge_with_ends_close_together_is_still_reordered(self):
        # Open and closed come from the edge's topology, not the end distance.
        edge = self.strokes_edge(0, 1, lambda t: (t * 8e-5, 0, 0), [(8e-5, 0, 0), (0, 0, 0)])
        self.assertEqual(fusion._sample_edge(edge, False), [(0, 0, 0), (8e-5, 0, 0)])

    def test_reversed_strokes_refine_within_tolerance(self):
        # The live failure: a reversed two-point stroke along changing normals
        # never converged, and the edge was skipped.
        edge = self.edge()
        point = edge.evaluator.getPointAtParameter.side_effect
        edge.evaluator.getStrokes = lambda a, b, tol: (True, [point(.01)[1], point(0)[1]])
        with patch.object(fusion, '_normal', side_effect=self.normal):
            samples = fusion._refine_samples(edge, fusion._sample_edge(edge, False), .3, .025, (0, 0, 1))
        self.assertEqual((samples[0], samples[-1]), ((0, 0, 0), (.01, 0, 0)))
        def tip(p):
            return dm.tool_tip(p, self.normal(0, p), self.normal(1, p), (0, 0, 1), .3, .025)
        for a, b in zip(samples, samples[1:]):
            mid = dm.scale(dm.add(a, b), .5)
            self.assertLessEqual(fusion.dc.dist(tip(mid), dm.scale(dm.add(tip(a), tip(b)), .5)),
                                 fusion.PATH_TOL)

    def test_samples_out_of_parameter_order_are_rejected(self):
        edge = self.edge()
        points = [(0, 0, 0), (.008, 0, 0), (.003, 0, 0), (.01, 0, 0)]
        with patch.object(fusion, '_normal', side_effect=self.normal), \
             patch.object(adsk.core.Point3D, 'create', side_effect=lambda *p: p):
            with self.assertRaisesRegex(dm.DeburrError, 'parameter order'):
                fusion._refine_samples(edge, points, .3, .025, (0, 0, 1))
            with self.assertRaisesRegex(dm.DeburrError, 'parameter order'):
                fusion._refine_samples(edge, [(0, 0, 0), (.02, 0, 0), (.01, 0, 0)], .3, .025, (0, 0, 1))

    def tiny_edge(self, t0, t1, scale, samples, lookups):
        """An edge whose point at t is ((t - t0) * scale, 0, 0); each sample
        looks up the parameter in `lookups` (by x), like a rounded inverse."""
        def point_at(t):
            return ((t - t0) * scale, 0, 0)
        edge = self.strokes_edge(t0, t1, point_at, [p for p in samples],
                                 lambda p: lookups[p[0]])
        edge.evaluator.getLengthAtParameter = lambda a, b: (True, abs(b - a) * scale)
        edge.evaluator.getParameterAtLength = lambda t, length: (True, t + length / scale)
        edge.evaluator.getFirstDerivative = lambda t: (True, NS(x=scale, y=0, z=0))
        edge.faces = MagicMock()
        edge.faces.item.side_effect = lambda i: i
        edge.faces.count = 2
        return edge

    def constant_normals(self):
        return patch.object(fusion, '_normal',
                            side_effect=lambda face, p: (0, 0, 1) if face == 0 else (1, 0, 0))

    def test_rounded_lookups_out_of_order_are_kept(self):
        # On a tiny parameter range, a lookup a few ULPs before the
        # previous sample, or past t1, is rounding. The mock raises for any
        # parameter outside [t0, t1], as Fusion does.
        t0, t1 = 1.0, 1.0 + 1e-8
        u = math.ulp(1.0)
        a, b, c = t0 + .25e-8, t0 + .25e-8 + 2 * u, t1 - u
        point = lambda t: ((t - t0) * 1e4, 0, 0)
        samples = [point(t) for t in (t0, a, b, c, t1)]
        lookups = {point(a)[0]: b, point(b)[0]: a, point(c)[0]: t1 + u}
        edge = self.tiny_edge(t0, t1, 1e4, samples, lookups)
        with self.constant_normals(), \
             patch.object(adsk.core.Point3D, 'create', side_effect=lambda *p: p):
            # Clamped by the lookup itself, not left to the length fallback.
            self.assertEqual(fusion._lookup_parameters(edge.evaluator, samples, t0, t1),
                             [t0, b, b, t1, t1])
            refined = fusion._refine_samples(edge, samples, .3, .025, (0, 0, 1))
        self.assertEqual((refined[0], refined[-1]), (samples[0], samples[-1]))

    def test_rounded_lookup_passes_through_edge_segment(self):
        t0, t1 = 1.0, 1.0 + 1e-8
        u = math.ulp(1.0)
        a, b = t0 + .25e-8, t0 + .25e-8 + 2 * u
        point = lambda t: ((t - t0) * 1e4, 0, 0)
        samples = [point(t) for t in (t0, a, b, t1)]
        edge = self.tiny_edge(t0, t1, 1e4, samples, {point(a)[0]: b, point(b)[0]: a})
        edge.startVertex = NS(geometry=NS(x=0, y=0, z=0))
        edge.endVertex = NS(geometry=NS(**dict(zip('xyz', point(t1)))))
        with self.constant_normals(), \
             patch.object(adsk.core.Point3D, 'create', side_effect=lambda *p: p), \
             patch.object(fusion, '_is_line_between_planes', return_value=False), \
             patch.object(fusion, '_is_closed', return_value=False), \
             patch.object(fusion, '_check_convexity'), \
             patch.object(fusion, '_vertex_key', side_effect=lambda v: (v.geometry.x,)):
            self.assertIsNotNone(fusion._lookup_parameters(edge.evaluator, samples, t0, t1))
            segment = fusion.edge_segment(edge, .3, .025, (0, 0, 1), lambda f: f)
        self.assertEqual(len(segment.tips), 4)

    def test_reversal_on_an_edge_shorter_than_the_vertex_tolerance_is_rejected(self):
        # Every point of a 0.8 um edge is within VERTEX_TOL of every
        # other, so only the parameter error can show a real reversal.
        t0, t1 = 0.0, 1.0
        point = lambda t: (t * 8e-5, 0, 0)
        samples = [point(t) for t in (0, .75, .25, 1)]
        edge = self.tiny_edge(t0, t1, 8e-5, samples, {point(.75)[0]: .75, point(.25)[0]: .25})
        with self.constant_normals(), \
             patch.object(adsk.core.Point3D, 'create', side_effect=lambda *p: p):
            with self.assertRaisesRegex(dm.DeburrError, 'parameter order'):
                fusion._refine_samples(edge, samples, .3, .025, (0, 0, 1))
            # The lookups give back their own samples, so the reversal is
            # real: it is rejected, never left to the fallback, which here
            # can't step (no derivative) and would accept its first guesses.
            edge.evaluator.getFirstDerivative = lambda t: (False, None)
            with patch.object(fusion, '_length_parameters', side_effect=AssertionError('fallback used')):
                with self.assertRaisesRegex(dm.DeburrError, 'parameter order'):
                    fusion._refine_samples(edge, samples, .3, .025, (0, 0, 1))

    def test_full_circle_whose_point_lookup_fails_past_half_way_is_placed_by_length(self):
        # Seen on every full circle (hole rims) of a part in Fusion 2706.1.17:
        # getParameterAtPoint returned 0 (ok) for every point past half way,
        # so refinement never converged ("sampling tolerance"); edges now go
        # by length along the edge instead.
        radius, n = .6, 55
        def point_at(t):
            return (radius * math.cos(t), radius * math.sin(t), 0)
        def clamped_lookup(p):
            return max(0.0, math.atan2(p[1], p[0]))
        strokes = self.ranged(point_at, [2 * math.pi * k / n for k in range(n + 1)])
        edge = self.strokes_edge(0, 2 * math.pi, point_at, strokes, clamped_lookup)
        edge.evaluator.getLengthAtParameter = lambda a, b: (True, radius * abs(b - a))
        edge.evaluator.getParameterAtLength = lambda t, length: (True, t + length / radius)
        edge.faces = MagicMock()
        edge.faces.item.side_effect = lambda i: i
        def normal(face, p):
            return (0, 0, 1) if face == 0 else dm.normalize((p[0], p[1], 0))
        with patch.object(fusion, '_normal', side_effect=normal), \
             patch.object(adsk.core.Point3D, 'create', side_effect=lambda *p: p):
            samples = fusion._sample_edge(edge, False, closed=True)
            self.assertIsNone(fusion._lookup_parameters(edge.evaluator, samples, 0, 2 * math.pi))
            refined = fusion._refine_samples(edge, samples, .3175, .0508, (0, 0, 1))
        self.assertEqual((refined[0], refined[-1]), (point_at(0), point_at(2 * math.pi)))
        angles = [math.atan2(p[1], p[0]) % (2 * math.pi) for p in refined[1:-1]]
        self.assertEqual(angles, sorted(angles))  # one pass, in order
        for p in refined:
            self.assertAlmostEqual(math.hypot(p[0], p[1]), radius)

    def circle_edge(self, t0, strokes_at, lookup, radius=.6, backwards=False):
        """A full circle with extents [t0, t0 + 2 pi] whose strokes are at
        the angles `strokes_at` and whose point lookup is `lookup`."""
        def point_at(t):
            return (radius * math.cos(t), radius * math.sin(t), 0)
        edge = self.strokes_edge(t0, t0 + 2 * math.pi, point_at, self.ranged(point_at, strokes_at, backwards),
                                 lookup, speed=radius,
                                 derivative=lambda t: (-radius * math.sin(t), radius * math.cos(t), 0))
        edge.faces = MagicMock()
        edge.faces.item.side_effect = lambda i: i
        return edge, point_at

    def refine_rim(self, edge, radius=.6):
        def normal(face, p):
            return (0, 0, 1) if face == 0 else dm.normalize((p[0], p[1], 0))
        with patch.object(fusion, '_normal', side_effect=normal), \
             patch.object(adsk.core.Point3D, 'create', side_effect=lambda *p: p):
            samples = fusion._sample_edge(edge, False, closed=True)
            return samples, fusion._refine_samples(edge, samples, .3175, .0508, (0, 0, 1))

    def assert_one_pass(self, refined, t0=0.0, radius=.6):
        angles = [(math.atan2(p[1], p[0]) - t0) % (2 * math.pi) for p in refined[1:-1]]
        self.assertEqual(angles, sorted(angles))
        for p in refined:
            self.assertAlmostEqual(math.hypot(p[0], p[1]), radius)

    def test_uneven_strokes_on_a_circle_keep_their_direction_and_place_exactly(self):
        # Chords of 0.006 mm and 0.6 mm either side of the seam, and uneven
        # chords elsewhere, must neither flip the direction nor make placement
        # by length drift from the strokes.
        radius = .6
        small, big = .0006 / radius, .06 / radius
        forward = [0, small] + [small + k * (2 * math.pi - small - big) / 40 for k in range(1, 40)] \
            + [2 * math.pi - big, 2 * math.pi]
        # And the mirror: a long step out of the seam, a short one back in.
        mirrored = [0] + [2 * math.pi - a for a in forward[-2:0:-1]] + [2 * math.pi]
        broken = lambda p: max(0.0, math.atan2(p[1], p[0]))  # Fusion's lookup on a full circle
        for layout in (forward, mirrored):
            for backwards in (False, True):
                with self.subTest(mirrored=layout is mirrored, backwards=backwards):
                    edge, point_at = self.circle_edge(0, layout, broken, backwards=backwards)
                    samples, refined = self.refine_rim(edge)
                    self.assertEqual(samples[1], point_at(layout[1]))
                    self.assert_one_pass(refined)
                    # Every stroke point was matched, none dropped or moved.
                    placed, parameters = fusion._length_parameters(edge.evaluator, samples, 0, 2 * math.pi)
                    self.assertEqual(len(placed), len(samples))
                    for p, q in zip(placed, samples):
                        self.assertLessEqual(fusion.dc.dist(p, q), fusion.VERTEX_TOL)
                    self.assertEqual(parameters, sorted(parameters))

    @staticmethod
    def polygon_edge(corners, backwards=False):
        """A closed mock edge running straight from corner to corner, one unit
        of parameter per side, with exact lengths; Fusion-like range errors.
        Its strokes are its corners within the range asked for."""
        sides = len(corners) - 1
        lengths = [fusion.dc.dist(a, b) for a, b in zip(corners, corners[1:])]

        def side(t):
            if not 0 <= t <= sides:
                raise RuntimeError('3 : invalid argument parameter')
            return min(int(t), sides - 1)

        def at(t):
            k = side(t)
            a, b = corners[k], corners[k + 1]
            return tuple(p + (q - p) * (t - k) for p, q in zip(a, b))

        def point(t):
            return True, NS(**dict(zip('xyz', at(t))))

        def strokes(a, b):
            points = [at(a)] + [corners[k] for k in range(1, sides) if a < k < b] + [at(b)]
            return points[::-1] if backwards else points

        def derivative(t):
            k = side(t)
            return True, NS(**dict(zip('xyz', (q - p for p, q in zip(corners[k], corners[k + 1])))))

        def length_at(a, b):
            def along(t):
                k = side(t)
                return sum(lengths[:k]) + lengths[k] * (t - k)
            return True, abs(along(b) - along(a))

        def parameter_at_length(t, length):
            k = side(t)
            target = sum(lengths[:k]) + lengths[k] * (t - k) + length
            for k, l in enumerate(lengths):
                if target <= l or k == sides - 1:
                    return True, k + min(target / l, 1.0)
                target -= l

        ev = NS(getParameterExtents=lambda: (True, 0, sides), getPointAtParameter=point,
                getFirstDerivative=derivative, getLengthAtParameter=length_at,
                getParameterAtLength=parameter_at_length,
                getStrokes=lambda a, b, tol: (True, [NS(**dict(zip('xyz', p))) for p in strokes(a, b)]))
        return NS(evaluator=ev, length=sum(lengths))

    def assert_polygon_in_order(self, corners):
        results = []
        for backwards in (False, True):
            with self.subTest(backwards=backwards):
                samples = fusion._sample_edge(self.polygon_edge(corners, backwards), False, closed=True)
                self.assertEqual((samples[0], samples[-1]), (corners[0], corners[-1]))
                where = [samples.index(c) for c in corners[1:-1]]
                self.assertEqual(where, sorted(where))
                results.append(samples)
        self.assertEqual(results[0], results[1])

    def test_closed_edge_with_a_corner_at_its_seam_keeps_its_direction(self):
        # The seam is a corner: the edge leaves along +X for 0.2 cm and
        # arrives from (0.25, -0.433), farther along +X.
        self.assert_polygon_in_order([(0, 0, 0), (.2, 0, 0), (.6, -.3, 0), (.25, -.433, 0), (0, 0, 0)])

    def test_sharp_bend_after_a_tiny_first_step_keeps_its_direction(self):
        # The first stroke point is within VERTEX_TOL of the seam, the next
        # comes after a sharp bend, and the arriving point lies near the
        # tangent at the seam.
        self.assert_polygon_in_order([(0, 0, 0), (5e-5, 0, 0), (5e-5, .3, 0), (.3, .3, 0),
                                      (.2, -.1, 0), (0, 0, 0)])

    def test_narrow_closed_edge_keeps_its_direction(self):
        # Both sides of a narrow loop run 4 um apart near the seam, so stroke
        # points either side of it both lie close to the outgoing side.
        self.assert_polygon_in_order([(0, 0, 0), (.02, 0, 0), (.3, 0, 0), (.3, -.0004, 0),
                                      (.02, -.0004, 0), (0, 0, 0)])

    def test_closed_spline_whose_parameters_bunch_at_the_seam_is_split_by_length(self):
        # Every fixed share of the parameter range lands within 0.01 mm of the
        # seam (angle = 2 pi t^30); a share of the length does not.
        radius = .6
        def angle(t):
            return 2 * math.pi * t ** 30
        def point_at(t):
            return (radius * math.cos(angle(t)), radius * math.sin(angle(t)), 0)
        params = [(k / 64) ** (1 / 30) for k in range(65)]
        for backwards in (False, True):
            with self.subTest(backwards=backwards):
                edge = self.strokes_edge(0, 1, point_at, self.ranged(point_at, params, backwards))
                edge.evaluator.getLengthAtParameter = lambda a, b: (True, radius * (angle(b) - angle(a)))
                edge.evaluator.getParameterAtLength = \
                    lambda t, length: (True, (t ** 30 + length / (2 * math.pi * radius)) ** (1 / 30))
                for share in fusion.CLOSED_SPLITS:
                    self.assertLess(fusion.dc.dist(point_at(share), point_at(0)), fusion.CLOSED_SPLIT_GAP)
                samples = fusion._sample_edge(edge, False, closed=True)
                self.assertEqual((samples[0], samples[-1]), (point_at(0), point_at(1)))
                angles = [math.atan2(p[1], p[0]) % (2 * math.pi) for p in samples[:-1]]
                self.assertEqual(angles, sorted(angles))

    def test_closed_edge_too_small_to_split_is_rejected(self):
        def point_at(t):
            return (5e-4 * math.cos(t), 5e-4 * math.sin(t), 0)  # 0.005 mm radius
        edge = self.strokes_edge(0, 2 * math.pi, point_at, self.ranged(point_at, []), speed=5e-4)
        with self.assertRaisesRegex(dm.DeburrError, 'too small'):
            fusion._sample_edge(edge, False, closed=True)

    def test_closed_edge_keeps_both_seam_ends_through_cleanup(self):
        # A stroke point 0.5 um before P(t1) is dropped, not P(t1) itself,
        # which refinement pairs with t1.
        seam = (0, 0, 0)
        samples = [seam, (.1, 0, 0), (.1, .1, 0), (0, .1, 0), (0, 5e-5, 0), seam]
        edge = MagicMock()
        edge.faces.count = 2
        with patch.object(fusion, '_is_line_between_planes', return_value=False), \
             patch.object(fusion, '_is_closed', return_value=True), \
             patch.object(fusion, '_sample_edge', return_value=list(samples)), \
             patch.object(fusion, '_refine_samples', side_effect=dm.DeburrError('stop')) as refine:
            with self.assertRaises(dm.DeburrError):
                fusion.edge_segment(edge, .3, .025, (0, 0, 1), lambda f: 0)
        self.assertEqual(refine.call_args.args[1], samples[:4] + [seam])

    def test_newton_accepts_an_exact_match_where_the_derivative_vanishes(self):
        # A sample at a point of zero speed was rejected even
        # though the first guess was the sample itself.
        ev = NS(getPointAtParameter=lambda t: (True, NS(x=t * t, y=0, z=0)),
                getFirstDerivative=lambda t: (True, NS(x=2 * t, y=0, z=0)))
        t, q = fusion._nearest_parameter(ev, (0, 0, 0), 0.0, 0.0, 1.0)
        self.assertEqual((t, q), (0.0, (0, 0, 0)))
        # A failed derivative stops the steps; the caller judges the point.
        stuck = NS(getPointAtParameter=ev.getPointAtParameter,
                   getFirstDerivative=lambda t: (False, None))
        self.assertEqual(fusion._nearest_parameter(stuck, (.25, 0, 0), .5, 0.0, 1.0), (.5, (.25, 0, 0)))

    def test_lookup_a_micron_off_stays_on_the_lookup(self):
        # A lookup 1.1 um off its sample worked on main and
        # must not be sent to the fallback for it.
        edge = self.edge()
        samples = [(0, 0, 0), (.005, 0, 0), (.01, 0, 0)]
        edge.evaluator.getParameterAtPoint.side_effect = lambda p: (True, p[0] + 1.1e-4)
        with patch.object(adsk.core.Point3D, 'create', side_effect=lambda *p: p):
            self.assertEqual(fusion._lookup_parameters(edge.evaluator, samples, 0, .01),
                             [0, .005 + 1.1e-4, .01])

    def test_circle_whose_extents_do_not_start_at_zero(self):
        # Extents [pi, 3 pi]; a lookup into [0, 2 pi) is out
        # of order, so the samples are matched along the edge instead.
        t0, n = math.pi, 30
        strokes = [t0 + 2 * math.pi * k / n for k in range(n + 1)]
        edge, _ = self.circle_edge(t0, strokes, lambda p: math.atan2(p[1], p[0]) % (2 * math.pi))
        _, refined = self.refine_rim(edge)
        self.assert_one_pass(refined, t0)

    def test_open_arc_with_a_broken_lookup_is_matched_along_the_edge(self):
        # An open edge can take the fallback too.
        radius = .6
        def point_at(t):
            return (radius * math.cos(t), radius * math.sin(t), 0)
        strokes = [point_at(math.pi * k / 20) for k in range(21)]
        edge = self.strokes_edge(0, math.pi, point_at, strokes, lambda p: 0.0, speed=radius,
                                 derivative=lambda t: (-radius * math.sin(t), radius * math.cos(t), 0))
        edge.faces = MagicMock()
        edge.faces.item.side_effect = lambda i: i
        with self.constant_normals(), \
             patch.object(adsk.core.Point3D, 'create', side_effect=lambda *p: p):
            refined = fusion._refine_samples(edge, fusion._sample_edge(edge, False), .3, .025, (0, 0, 1))
        self.assertEqual((refined[0], refined[-1]), (strokes[0], strokes[-1]))
        self.assert_one_pass(refined)

    def test_fusion_errors_while_sampling_become_edge_errors(self):
        # Fusion's evaluators raise RuntimeError for some inputs: that edge
        # fails like any other (DeburrError), while bugs keep their traceback.
        fusion_error = RuntimeError('3 : invalid argument parameter')
        edge = MagicMock()
        edge.faces.count = 2
        for error, expected in ((fusion_error, dm.DeburrError),
                                (RuntimeError('dictionary changed size during iteration'), RuntimeError)):
            with self.subTest(error=str(error)), \
                 patch.object(fusion, '_is_line_between_planes', return_value=False), \
                 patch.object(fusion, '_is_closed', return_value=False), \
                 patch.object(fusion, '_sample_edge', side_effect=error):
                with self.assertRaises(expected) as caught:
                    fusion.edge_segment(edge, .3, .025, (0, 0, 1), lambda f: 0)
                self.assertIn(str(error), str(caught.exception))

        class Unevaluable:
            length = 1

            @property
            def evaluator(self):
                raise fusion_error
        broken = NS(getParameterExtents=MagicMock(side_effect=fusion_error))
        for edge in (NS(evaluator=broken, length=1), Unevaluable()):
            with self.subTest(edge=edge):
                with self.assertRaises(dm.DeburrError):
                    fusion._edge_polyline(edge)
                self.assertIsNone(fusion.edge_shape(edge))
        bug = NS(getParameterExtents=MagicMock(side_effect=RuntimeError('not from Fusion')))
        with self.assertRaisesRegex(RuntimeError, 'not from Fusion'):
            fusion.edge_shape(NS(evaluator=bug, length=1))

    def test_fusion_error_on_one_edge_leaves_the_other_chains(self):
        Z = (0, 0, 1)
        good, bad = NS(body='part', name='good'), NS(body='part', name='bad')
        segment = fusion.dc.Segment([(0, 0, 0), (1, 0, 0)], None, None, source=good)

        def edge_segment(edge, *a):
            if edge is bad:
                raise RuntimeError('3 : invalid argument parameter')
            return segment
        with patch.object(fusion, '_edge_segment', side_effect=edge_segment), \
             patch.object(fusion, '_cut_depth'), \
             patch.object(fusion, '_edge_polyline', return_value=[(0, 0, 0), (1, 0, 0)]), \
             patch.object(fusion, '_excess', return_value=lambda p, margin=0.0, enough=None: 0.0), \
             patch.object(fusion, '_column_excess', return_value=lambda p: 0.0), \
             patch.object(fusion.dc, 'order_chains', return_value=[([segment], False)]) as order:
            chains, _, warnings = fusion.build_chains([good, bad], .3, .05, Z)
        self.assertEqual(order.call_args.args[0], [segment])  # the good edge, and only it
        self.assertEqual(len(chains), 1)
        self.assertEqual(len(warnings), 1)
        self.assertIn('Edge 2 skipped', warnings[0])
        self.assertIn('invalid argument parameter', warnings[0])

    def test_edge_segment_samples_by_the_edge_topology(self):
        edge = MagicMock()
        edge.faces.count = 2
        for closed in (False, True):
            with self.subTest(closed=closed), \
                 patch.object(fusion, '_is_line_between_planes', return_value=False), \
                 patch.object(fusion, '_is_closed', return_value=closed), \
                 patch.object(fusion, '_sample_edge', side_effect=dm.DeburrError('stop')) as sample:
                with self.assertRaises(dm.DeburrError):
                    fusion.edge_segment(edge, .3, .025, (0, 0, 1), lambda f: 0)
                self.assertEqual(sample.call_args.args[2], closed)

    def test_edge_shape_ends_exactly_at_the_last_parameter(self):
        # Extents of an arc in Fusion: t0 + (t1 - t0) rounds past t1, and
        # Fusion raised "3 : invalid argument parameter" when saving settings.
        t0, t1 = -2.2286068368116654, -0.9129858167781278
        self.assertGreater(t0 + (t1 - t0), t1)
        edge = self.strokes_edge(t0, t1, lambda t: (t, 0, 0), [])
        shape = fusion.edge_shape(edge)
        self.assertEqual(shape[1][0], t0)
        self.assertEqual(shape[-1][0], t1)

    def test_small_closed_edges_fail_gracefully_before_indexing(self):
        edge = MagicMock()
        edge.faces.count = 2
        for points in ([], [(0, 0, 0)], [(0, 0, 0)] * 2,
                       [(0, 0, 0), (1, 0, 0), (0, 0, 0)], [(0, 0, 0)] * 4):
            with self.subTest(points=points), \
                 patch.object(fusion, '_is_closed', return_value=True), \
                 patch.object(fusion, '_sample_edge', return_value=points):
                with self.assertRaisesRegex(dm.DeburrError, 'too few'):
                    fusion.edge_segment(edge, .3, .025, (0, 0, 1), lambda f: 0)
