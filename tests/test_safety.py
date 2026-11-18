"""Focused safety regressions; Fusion API boundaries are mocked."""
import importlib
import math
import os
import sys
import unittest
from types import SimpleNamespace as NS, ModuleType
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
package = ModuleType('safety_fusion')
package.__path__ = [os.path.join(os.path.dirname(__file__), '..', 'FusionFreeDeburr')]
sys.modules['safety_fusion'] = package
dc = importlib.import_module('safety_fusion.deburr_chain')
dm = importlib.import_module('safety_fusion.deburr_math')

adsk = MagicMock()
with patch.dict(sys.modules, {'adsk': adsk, 'adsk.core': adsk.core,
                             'adsk.fusion': adsk.fusion, 'adsk.cam': adsk.cam}):
    fusion = importlib.import_module('safety_fusion.deburr_fusion')
    tools = importlib.import_module('safety_fusion.cam_tools')


class SafetyTests(unittest.TestCase):
    def test_projected_crossing_requires_same_height(self):
        a = [(0, 0, 0), (2, 2, 0)]
        self.assertIsNone(dc.find_crossing(a, [(0, 2, 1), (2, 0, 1)], (0, 0, 1)))
        self.assertIsNotNone(dc.find_crossing(a, [(0, 2, 0), (2, 0, 0)], (0, 0, 1)))

    def test_failed_corner_stays_split_in_export(self):
        end = dc.End((0, 0, 0), {})
        a = dc.Segment([(-2, 0, 0), (-1, 0, 0)], end, end)
        b = dc.Segment([(1, 0, 0), (2, 0, 0)], end, end)
        with patch.object(dc, 'vertex_crossing', side_effect=dm.DeburrError('undefined corner')):
            chain = dc.link_chain([a, b], True, (0, 0, 1), 1, .1)
        self.assertEqual(len(dc.chain_polylines(chain)), 2)
        self.assertTrue(chain.notes)
        with self.assertRaises(dm.DeburrError):
            dc.chain_points(chain)

    def test_deep_or_unmeasurable_corner_is_rejected(self):
        end = dc.End((0, 0, 0), {})
        a, b = (-1, 0, 0), (1, 0, 0)
        for depth in (lambda pts: 0 if len(pts) == 1 else 1,
                      MagicMock(side_effect=RuntimeError('measurement failed'))):
            notes = []
            self.assertIsNone(dc._corner(0, end, end, a, b, (0, 0, 1), 1, .1, depth, notes))
            self.assertIn('split', notes[0])

    def test_convexity_uses_lateral_probes(self):
        outside = adsk.fusion.PointContainment.PointOutsidePointContainment
        inside = adsk.fusion.PointContainment.PointInsidePointContainment
        with patch.object(fusion, '_normal', side_effect=lambda f, p: f), patch.object(
                adsk.core.Point3D, 'create', side_effect=lambda *p: p):
            for convex in (True, False):
                def contains(p):
                    # Positive quadrant solid: convex; its complement: concave.
                    solid = p[0] < 0 and p[1] < 0
                    if not convex:
                        solid = p[0] < 0 or p[1] < 0
                    return inside if solid else outside
                edge = NS(body=NS(pointContainment=contains))
                if convex:
                    fusion._check_convexity(edge, (1, 0, 0), (0, 1, 0), [(0, 0, 0)])
                else:
                    with self.assertRaises(dm.DeburrError):
                        fusion._check_convexity(edge, (1, 0, 0), (0, 1, 0), [(0, 0, 0)])

    def test_adaptive_strokes_requested_and_failures_rejected(self):
        ev = MagicMock()
        ev.getParameterExtents.return_value = (True, 0, 1)
        ev.getStrokes.return_value = (True, [NS(x=0, y=0, z=0), NS(x=1, y=0, z=0)])
        self.assertEqual(len(fusion._sample_edge(NS(evaluator=ev), False)), 2)
        ev.getStrokes.assert_called_once_with(0, 1, .001)
        ev.getStrokes.return_value = (False, [])
        with self.assertRaises(dm.DeburrError):
            fusion._sample_edge(NS(evaluator=ev), False)

    def test_clearance_checks_between_vertices_and_inside_centers(self):
        body = MagicMock()
        inside = adsk.fusion.PointContainment.PointInsidePointContainment
        outside = adsk.fusion.PointContainment.PointOutsidePointContainment
        body.pointContainment.side_effect = lambda p: inside if .45 < p[0] < .55 else outside
        measure = adsk.core.Application.get().measureManager
        measure.measureMinimumDistance.return_value.value = 1
        with patch.object(adsk.core.Point3D, 'create', side_effect=lambda *p: p):
            depth = fusion._cut_depth(body, .3, (0, 0, 1))
            self.assertEqual(depth([(0, 0, 0), (1, 0, 0)]), .6)
            self.assertLess(depth([(0, 0, 0)]), 0)

    def test_tool_validation_including_existing_operation_tool(self):
        for kind, diameter in [('flat end mill', 1), ('ball end mill', 0),
                                ('ball end mill', float('nan')), ('ball end mill', None)]:
            tool = MagicMock()
            with patch.object(tools, 'tool_type', return_value=kind), patch.object(
                    tools, 'diameter_cm', return_value=diameter):
                with self.assertRaises(ValueError):
                    tools.validate_ball_tool(tool)
        with patch.object(tools, 'tool_type', return_value='ball end mill'), patch.object(
                tools, 'diameter_cm', return_value=1):
            self.assertEqual(tools.validate_ball_tool(MagicMock()), .5)

    @staticmethod
    def _curve_edge(point_at, length):
        """A mock edge whose point at parameter t (0 to 1) is point_at(t)."""
        evaluator = NS(getParameterExtents=lambda: (True, 0, 1),
                       getPointAtParameter=lambda t: (True, NS(**dict(zip('xyz', point_at(t))))))
        nowhere = NS(findEntityByToken=lambda token: [])
        return NS(evaluator=evaluator, entityToken='token', length=length,
                  nativeObject=None, assemblyContext=None,
                  body=NS(parentComponent=NS(parentDesign=nowhere)))

    class _Native:
        """An edge as its own design resolves it from a token."""
        nativeObject = None

        def __init__(self, path=''):
            self.assemblyContext = NS(fullPathName=path) if path else None

    @staticmethod
    def _owned(edge, saved, path='', native=None):
        """Make `edge` a setup-model edge at occurrence `path` whose body's own
        design resolves the token to `saved`."""
        edge.nativeObject = native or saved
        edge.assemblyContext = NS(fullPathName=path) if path else None
        owner = NS(findEntityByToken=lambda token: [saved])
        edge.body = NS(parentComponent=NS(parentDesign=owner))
        return edge

    @classmethod
    def _edge(cls, start, end, length=None):
        return cls._curve_edge(
            lambda t: tuple(a + (b - a) * t for a, b in zip(start, end)),
            dc.dist(start, end) if length is None else length)

    @classmethod
    def _circle(cls, u, v, radius=1, reverse=False):
        """A full circle about the origin in the plane of unit vectors u, v,
        starting and ending at radius * u."""
        def point_at(t):
            a = 2 * math.pi * (1 - t if reverse else t)
            return tuple(radius * (math.cos(a) * p + math.sin(a) * q) for p, q in zip(u, v))
        return cls._curve_edge(point_at, 2 * math.pi * radius)

    def test_saved_edge_is_found_by_shape_when_its_token_does_not_resolve(self):
        # Issue #14: linked components and manufacturing models.
        saved = self._edge((0, 0, 0), (1, 0, 0))
        op = MagicMock()
        fusion.save_settings(op, [saved], .05, NS(entityToken='sketch'))
        data = fusion.json.loads(op.attributes.add.call_args.args[2])
        self.assertEqual(data['version'], 1)
        self.assertEqual(data['edges'], ['token'])
        shape = data['edge_shapes'][0]
        self.assertEqual(shape, [1] + [[i / 8, 0, 0] for i in range(9)])

        design = NS(findEntityByToken=lambda token: [])
        native = self._Native()
        same = self._owned(self._edge((1, 0, 0), (0, 0, 0)), native)  # direction may differ
        arc = self._edge((0, 0, 0), (1, 0, 0), length=1.2)  # same ends, longer
        other = self._edge((0, 1, 0), (1, 1, 0))
        with patch.object(adsk.fusion.BRepEdge, 'cast', side_effect=lambda v: v):
            self.assertIs(fusion.find_edge(design, 'token', shape, lambda: [other, arc, same]), same)
            self.assertIs(fusion.find_edge(design, 'token', shape, lambda: [same, same]), same)
            # Moved: reselect.
            self.assertIsNone(fusion.find_edge(design, 'token', shape, lambda: [other, arc]))
            # A different edge in the same place does not hide the saved one.
            twin = self._edge((0, 0, 0), (1, 0, 0))
            self.assertIs(fusion.find_edge(design, 'token', shape, lambda: [twin, same]), same)
            # Two placements of the saved edge in the same place: reselect.
            copy = self._owned(self._edge((0, 0, 0), (1, 0, 0)), native)
            self.assertIsNone(fusion.find_edge(design, 'token', shape, lambda: [same, copy]))
            # Settings saved by earlier versions have no shapes.
            self.assertIsNone(fusion.find_edge(design, 'token', None, lambda: [same]))

    def test_coincident_edge_of_another_body_is_not_the_saved_edge(self):
        # Review of the issue #14 fix: the saved edge's body leaves the setup
        # and an edge with the same length and points remains on another body.
        shape = fusion.edge_shape(self._edge((0, 0, 0), (1, 0, 0)))
        design = NS(findEntityByToken=lambda token: [])
        native = self._Native()
        with patch.object(adsk.fusion.BRepEdge, 'cast', side_effect=lambda v: v):
            # Its body's design does not know the token at all.
            stranger = self._edge((0, 0, 0), (1, 0, 0))
            self.assertIsNone(fusion.find_edge(design, 'token', shape, lambda: [stranger]))
            # Its body's design resolves the token, but to a different edge.
            neighbour = self._owned(self._edge((0, 0, 0), (1, 0, 0)), native,
                                    native=self._Native())
            self.assertIsNone(fusion.find_edge(design, 'token', shape, lambda: [neighbour]))
            # The saved edge is still there but no longer has the saved shape.
            moved = self._owned(self._edge((0, 1, 0), (1, 1, 0)), native)
            self.assertIsNone(fusion.find_edge(design, 'token', shape, lambda: [moved, stranger]))

    def test_saved_edge_is_matched_by_the_end_of_its_occurrence_path(self):
        # A manufacturing model's design gives the edge from its own root; the
        # setup gives the same edge from the document's root.
        shape = fusion.edge_shape(self._edge((0, 0, 0), (1, 0, 0)))
        design = NS(findEntityByToken=lambda token: [])
        native = self._Native()
        saved = self._Native('Model:1+Part:1')
        saved.nativeObject = native
        with patch.object(adsk.fusion.BRepEdge, 'cast', side_effect=lambda v: v):
            for path, found in (('Models:1+Mfg:1+Model:1+Part:1', True),
                                ('Model:1+Part:1', True),
                                ('Models:1+Mfg:1+Model:1+Part:2', False),
                                ('Models:1+Mfg:1+OtherModel:1+Part:1', False),
                                ('', False)):
                with self.subTest(path=path):
                    edge = self._owned(self._edge((0, 0, 0), (1, 0, 0)), saved, path, native)
                    self.assertIs(fusion.find_edge(design, 'token', shape, lambda: [edge]),
                                  edge if found else None)

    def _in_body(self, edge, name, index):
        """Make `edge` edge number `index` of a body called `name`."""
        edges = [object() for _ in range(index + 2)]
        edges[index] = edge
        edge.body = NS(name=name, edges=edges, parentComponent=edge.body.parentComponent)
        return edge

    def test_saved_edge_is_found_through_a_manufacturing_models_copy(self):
        # Seen in Fusion 2706.1.17: the edge was picked in the manufacturing
        # model, whose design resolves its token, but the setup gives the same
        # edge from the document's design, which does not. The two are
        # different edges of copies of one body.
        shape = fusion.edge_shape(self._edge((0, 0, 0), (1, 0, 0)))
        design = NS(findEntityByToken=lambda token: [])
        candidate = self._in_body(self._edge((0, 0, 0), (1, 0, 0)), 'Part', 3)
        with patch.object(adsk.fusion.BRepEdge, 'cast', side_effect=lambda v: v):
            for copy, found in ((self._in_body(self._edge((1, 0, 0), (0, 0, 0)), 'Part', 3), True),
                                (self._in_body(self._edge((0, 0, 0), (1, 0, 0)), 'Other', 3), False),
                                (self._in_body(self._edge((0, 0, 0), (1, 0, 0)), 'Part', 2), False),
                                (self._in_body(self._edge((0, 1, 0), (1, 1, 0)), 'Part', 3), False)):
                model = NS(findEntityByToken=lambda token, copy=copy: [copy])
                with self.subTest(body=copy.body.name, place=fusion.edge_shape(copy)[1]):
                    self.assertIs(fusion.find_edge(design, 'token', shape, lambda: [candidate],
                                                   lambda: [model]),
                                  candidate if found else None)
            # Without the manufacturing models' designs the token is unknown.
            self.assertIsNone(fusion.find_edge(design, 'token', shape, lambda: [candidate]))

    def test_saved_circle_is_not_matched_to_a_perpendicular_circle(self):
        # Review of the issue #14 fix: two same-radius circles in perpendicular planes have
        # the same length and the same start, middle and end points.
        x, y, z = (1, 0, 0), (0, 1, 0), (0, 0, 1)
        saved, other = self._circle(x, y), self._circle(x, z)
        shape, other_shape = fusion.edge_shape(saved), fusion.edge_shape(other)
        self.assertEqual(len(shape), 1 + dc.EDGE_SHAPE_POINTS)
        self.assertAlmostEqual(shape[0], other_shape[0])
        for i in (1, 5, 9):  # start, middle, end
            self.assertLess(dc.dist(shape[i], other_shape[i]), 1e-9)
        self.assertFalse(dc.same_edge_shape(shape, other_shape, fusion.VERTEX_TOL))

        design = NS(findEntityByToken=lambda token: [])
        native = self._Native()
        again = self._owned(self._circle(x, y), native)
        backwards = self._owned(self._circle(x, y, reverse=True), native)
        other = self._owned(other, native)  # even when the token names it
        with patch.object(adsk.fusion.BRepEdge, 'cast', side_effect=lambda v: v):
            # Only the other circle is left: reselect, never the wrong edge.
            self.assertIsNone(fusion.find_edge(design, 'token', shape, lambda: [other]))
            self.assertIs(fusion.find_edge(design, 'token', shape, lambda: [other, again]), again)
            self.assertIs(fusion.find_edge(design, 'token', shape, lambda: [other, backwards]),
                          backwards)

    def test_edge_shape_with_fewer_points_never_matches(self):
        # Start, middle and end only, as first proposed for issue #14.
        sparse = [1, [0, 0, 0], [.5, 0, 0], [1, 0, 0]]
        full = fusion.edge_shape(self._edge((0, 0, 0), (1, 0, 0)))
        self.assertTrue(dc.same_edge_shape(full, full, fusion.VERTEX_TOL))
        self.assertFalse(dc.same_edge_shape(sparse, sparse, fusion.VERTEX_TOL))
        self.assertFalse(dc.same_edge_shape(sparse, full, fusion.VERTEX_TOL))

    def test_saved_edge_token_is_preferred_over_shape(self):
        by_token = object()
        design = NS(findEntityByToken=lambda token: [by_token])
        candidates = MagicMock()
        with patch.object(adsk.fusion.BRepEdge, 'cast', side_effect=lambda v: v):
            self.assertIs(fusion.find_edge(design, 'token', [1] + [[0] * 3] * 9,
                                           candidates), by_token)
        candidates.assert_not_called()

    def test_setup_model_edges_include_bodies_inside_occurrences(self):
        body, nested = NS(edges=['a', 'b']), NS(edges=['c'])
        child = NS(bRepBodies=[nested], childOccurrences=[])
        occurrence = NS(bRepBodies=[], childOccurrences=[child])
        with patch.object(adsk.fusion.BRepBody, 'cast',
                          side_effect=lambda e: e if hasattr(e, 'edges') else None), \
                patch.object(adsk.fusion.Occurrence, 'cast',
                             side_effect=lambda e: e if hasattr(e, 'childOccurrences') else None):
            self.assertEqual(fusion.setup_model_edges(NS(models=[body, occurrence])),
                             ['a', 'b', 'c'])

    def test_trace_settings_reset_on_create_and_edit(self):
        op = MagicMock()
        params = {name: NS(expression='unsafe') for name in
                  ('compensation', 'compensationType', 'axialOffset', 'radialOffset')}
        op.parameters.itemByName.side_effect = params.get
        fusion._trace_settings(op)
        self.assertEqual([p.expression for p in params.values()], ["'center'", "'off'", '0 mm', '0 mm'])
        with patch.object(fusion, '_curves_parameter'), patch.object(fusion, '_apply_curves'):
            fusion.update_trace_curves(op, [])
        self.assertEqual(params['axialOffset'].expression, '0 mm')
