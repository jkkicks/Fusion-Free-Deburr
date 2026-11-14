"""Chamfer-cutter geometry: flank contact, tool profiles, and corner rolls."""
import copy
import math
import unittest

from FusionFreeDeburr import cutter_profile as cp
from FusionFreeDeburr import deburr_chain as dc
from FusionFreeDeburr import deburr_math as dm

Z = (0.0, 0.0, 1.0)
LOWER = (0.0, 1.0, 1.0, 2.0)    # 90 degree included, widening upward
UPPER = (1.0, 2.0, 2.0, 1.0)    # back flank of the same double-angle cutter
# Harvey #875510 double-angle shank cutter (inches): 90 deg, 1/2 point, 1/4 width.
HARVEY = {'type': 'thread mill', 'unit': 'inches',
          'geometry': {'DC': .5, 'thread-profile-angle': 90, 'LCF': .25, 'SFDM': .25,
                       'shoulder-diameter': .25, 'shoulder-length': .25, 'OAL': 3.25,
                       'thread-tip-type': 'point'}}


def surface_gap(x, tip, flank, axis=Z):
    """Horizontal distance of x outside (+) or inside (-) the flank cone,
    or None if x is beyond the flank's height range."""
    z0, r0, z1, r1 = flank
    rel = dm.sub(x, tip)
    h = dm.dot(rel, axis)
    if not z0 - 1e-9 <= h <= z1 + 1e-9:
        return None
    lateral = dm.sub(rel, dm.scale(axis, h))
    return math.sqrt(dm.dot(lateral, lateral)) - (r0 + (r1 - r0) * (h - z0) / (z1 - z0))


def rotate_z(v, deg):
    a = math.radians(deg)
    return (v[0] * math.cos(a) - v[1] * math.sin(a), v[0] * math.sin(a) + v[1] * math.cos(a), v[2])


class ConeTipTests(unittest.TestCase):
    def test_square_top_edge_is_cut_back_width_on_both_faces(self):
        w = .05
        tip, retreat = dm.cone_tip((0, 0, 0), Z, (1, 0, 0), Z, w, LOWER)
        for q in ((-w, 0, 0), (0, 0, -w)):
            self.assertAlmostEqual(surface_gap(q, tip, LOWER), 0)
        self.assertEqual(retreat, (1.0, 0.0, 0.0))

    def test_contact_strip_is_centered_on_flank(self):
        tip, _ = dm.cone_tip((0, 0, 0), Z, (1, 0, 0), Z, .05, LOWER)
        # The chamfer midpoint (-w/2, 0, -w/2) sits halfway up the flank.
        self.assertAlmostEqual(-.025 - tip[2], .5)

    def test_back_edge_uses_upper_flank_and_rejects_lower(self):
        w = .05
        bottom, wall = (0, 0, -1), (1, 0, 0)
        tip, retreat = dm.cone_tip((0, 0, 0), bottom, wall, Z, w, UPPER)
        for q in ((-w, 0, 0), (0, 0, w)):
            self.assertAlmostEqual(surface_gap(q, tip, UPPER), 0)
        self.assertEqual(retreat, (1.0, 0.0, 0.0))
        with self.assertRaisesRegex(dm.DeburrError, 'cannot reach'):
            dm.cone_tip((0, 0, 0), bottom, wall, Z, w, LOWER)

    def test_largest_leg_equals_width_on_drafted_wall(self):
        w = .05
        for draft in (5, 15, 30):
            a = math.radians(draft)
            wall = (math.cos(a), 0, math.sin(a))     # outward-leaning top edge
            tip, _ = dm.cone_tip((0, 0, 0), Z, wall, Z, w, LOWER)
            k, *_ = dm.chamfer_plane(Z, wall, Z, math.sqrt(.5))
            down_wall = (-math.sin(a), 0, math.cos(a))
            legs = []
            for direction in ((-1, 0, 0), dm.scale(down_wall, -1)):
                # The flank meets the face where the face crosses the chamfer plane.
                lo, hi = 0.0, 1.0
                for _ in range(60):
                    mid = (lo + hi) / 2
                    gap = surface_gap(dm.scale(direction, mid), tip, LOWER)
                    lo, hi = (mid, hi) if gap is not None and gap < 0 else (lo, mid)
                legs.append(lo)
            with self.subTest(draft=draft):
                self.assertAlmostEqual(max(legs), w, places=6)
                self.assertLess(min(legs), w)

    def test_cone_stays_on_tool_side_of_chamfer_for_sloped_rotated_edges(self):
        w = .04
        for heading in (0, 37, 90, 200):
            for slope in (0, 10, 30):
                s = math.radians(slope)
                tangent = rotate_z((math.cos(s), 0, math.sin(s)), heading)
                top = rotate_z((-math.sin(s), 0, math.cos(s)), heading)
                side = dm.normalize(dm.cross(tangent, top))
                if dm.dot(side, rotate_z((0, -1, 0), heading)) < 0:
                    side = dm.scale(side, -1)
                p = (1, 2, 3)
                tip, _ = dm.cone_tip(p, top, side, Z, w, LOWER)
                k, phi, psi, *_ = dm.chamfer_plane(top, side, Z, math.sqrt(.5))
                depth = w * min(math.sin(phi), math.sin(psi - phi))
                lowest = float('inf')
                for i in range(41):
                    h = i / 40
                    r = 1 + h
                    for j in range(72):
                        a = math.radians(j * 5)
                        x = dm.add(tip, (r * math.cos(a), r * math.sin(a), h))
                        lowest = min(lowest, dm.dot(dm.sub(x, p), k) + depth)
                with self.subTest(heading=heading, slope=slope):
                    self.assertGreater(lowest, -1e-3)   # never past the chamfer plane
                    self.assertLess(lowest, 2e-3)       # and tangent to it

    def test_steep_or_oversized_cuts_are_rejected(self):
        # Edge rising at 60 degrees: steeper than a 45 degree flank allows.
        steep = (math.cos(math.radians(60)), 0, math.sin(math.radians(60)))
        side = (0, 1, 0)
        with self.assertRaisesRegex(dm.DeburrError, 'too steep'):
            dm.cone_tip((0, 0, 0), side, dm.cross(steep, side), Z, .05, LOWER)
        gentle = (math.cos(math.radians(30)), 0, math.sin(math.radians(30)))
        dm.cone_tip((0, 0, 0), side, dm.cross(gentle, side), Z, .05, LOWER)
        with self.assertRaisesRegex(dm.DeburrError, 'too large'):
            dm.cone_tip((0, 0, 0), Z, (1, 0, 0), Z, 1.0, LOWER)


class ChamferProfileTests(unittest.TestCase):
    def test_harvey_double_angle_matches_vendor_drawing(self):
        tool = cp.from_tool_json(HARVEY)
        inch = 2.54
        expected = ((0, .125 * inch, .125 * inch, .25 * inch),
                    (.125 * inch, .25 * inch, .25 * inch, .125 * inch))
        for got, want in zip(tool.frustums, expected):
            for a, b in zip(got, want):
                self.assertAlmostEqual(a, b)
        self.assertAlmostEqual(tool.radius, .25 * inch)
        self.assertEqual(tool.noncutting_sections, ((.25 * inch, 3.25 * inch, .125 * inch),))
        names = [v.name for v in tool.variants()]
        self.assertEqual(names, ['topside', 'back side'])

    def test_chamfer_mill_is_topside_only(self):
        raw = {'type': 'chamfer mill', 'unit': 'millimeters',
               'geometry': {'DC': 10, 'TA': 45, 'tip-diameter': 2, 'SFDM': 10,
                            'shoulder-length': 20, 'OAL': 60}}
        tool = cp.from_tool_json(raw)
        self.assertEqual(len(tool.variants()), 1)
        z0, r0, z1, r1 = tool.frustums[0]
        self.assertAlmostEqual(r0, .1)
        self.assertAlmostEqual(r1, .5)
        self.assertAlmostEqual(z1, .4)
        self.assertEqual(tool.noncutting_sections[0][:2], (z1, 2.0))

    def test_inch_and_mm_definitions_match(self):
        mm = copy.deepcopy(HARVEY)
        mm['unit'] = 'millimeters'
        mm['geometry'] = {k: v * 25.4 if isinstance(v, (int, float)) and k != 'thread-profile-angle'
                          else v for k, v in mm['geometry'].items()}
        for a, b in zip(cp.from_tool_json(HARVEY).frustums, cp.from_tool_json(mm).frustums):
            for x, y in zip(a, b):
                self.assertAlmostEqual(x, y)

    def test_bad_chamfer_profiles_are_rejected(self):
        cases = (('thread-profile-angle', 180), ('thread-profile-angle', 0),
                 ('thread-tip-type', 'flat'), ('NT', 3), ('SFDM', .3), ('LCF', 1.0),
                 ('shoulder-diameter', -.5))
        for key, value in cases:
            raw = copy.deepcopy(HARVEY)
            raw['geometry'][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(dm.DeburrError):
                cp.from_tool_json(raw)
        mill = {'type': 'chamfer mill', 'unit': 'millimeters',
                'geometry': {'DC': 10, 'TA': 90, 'SFDM': 10, 'OAL': 60}}
        with self.assertRaises(dm.DeburrError):
            cp.from_tool_json(mill)

    def test_retreat_frees_back_flank_from_the_wall(self):
        cutter = cp.from_tool_json(HARVEY).variants()[1]
        direction, distance = cutter.retreat((0, 0, 0), (0, 0, -1), (1, 0, 0), Z, .05)
        tip = cutter.tip((0, 0, 0), (0, 0, -1), (1, 0, 0), Z, .05)
        moved = dm.add(tip, dm.scale(direction, distance))
        # Material is at x < 0. After retreating, the widest point of the tool
        # clears the wall plane x = 0 by the edge break plus clearance.
        self.assertAlmostEqual(moved[0] - cutter.radius, .05 + cp.RETREAT_CLEARANCE)
        self.assertGreater(direction[0], .99)


class ConeCornerTests(unittest.TestCase):
    def test_outside_corner_rolls_around_vertex_with_cone(self):
        cutter = cp.from_tool_json(HARVEY).variants()[0]
        w = .05
        a_end = dc.End((0, 0, 0), {'top': Z, 'x': (1, 0, 0)}, (-1, 0, 0))
        b_start = dc.End((0, 0, 0), {'top': Z, 'y': (0, 1, 0)}, (0, -1, 0))
        tip = lambda p, n1, n2: cutter.tip(p, n1, n2, Z, w)
        points = dc.corner_roll(a_end, b_start, Z, cutter.radius, w, tip=tip)
        reach = [math.hypot(p[0], p[1]) for p in points]
        heights = [p[2] for p in points]
        self.assertGreater(len(points), 10)
        self.assertAlmostEqual(max(reach), min(reach))
        self.assertAlmostEqual(max(heights), min(heights))
        # The roll goes around the outside (+x, +y quadrant).
        mid = points[len(points) // 2]
        self.assertGreater(mid[0], 0)
        self.assertGreater(mid[1], 0)

    def test_wide_corner_roll_chords_stay_on_the_arc(self):
        big = copy.deepcopy(HARVEY)
        big['geometry'].update({'DC': 3.0, 'LCF': .5, 'shoulder-length': .5})
        cutter = cp.from_tool_json(big).variants()[0]
        w = .05
        a_end = dc.End((0, 0, 0), {'top': Z, 'x': (1, 0, 0)}, (-1, 0, 0))
        b_start = dc.End((0, 0, 0), {'top': Z, 'y': (0, 1, 0)}, (0, -1, 0))
        tip = lambda p, n1, n2: cutter.tip(p, n1, n2, Z, w)
        points = dc.corner_roll(a_end, b_start, Z, cutter.radius, w, tip=tip)
        reach = math.hypot(points[0][0], points[0][1])
        for a, b in zip(points, points[1:]):
            mid = dm.scale(dm.add(a, b), .5)
            self.assertLess(reach - math.hypot(mid[0], mid[1]), dc.ROLL_CHORD_TOL + 1e-9)

    def test_corner_with_cutter_uses_its_contact_model(self):
        cutter = cp.from_tool_json(HARVEY).variants()[0]
        w = .05
        a_end = dc.End((0, 0, 0), {'top': Z, 'x': (1, 0, 0)}, (-1, 0, 0))
        b_start = dc.End((0, 0, 0), {'top': Z, 'y': (0, 1, 0)}, (0, -1, 0))
        a_tip = cutter.tip((0, 0, 0), Z, (1, 0, 0), Z, w)
        b_tip = cutter.tip((0, 0, 0), Z, (0, 1, 0), Z, w)
        notes = []
        points = dc._corner(0, a_end, b_start, a_tip, b_tip, Z, cutter.radius, w, None,
                            notes, cutter)
        self.assertEqual(notes, [])
        self.assertEqual(points[0], a_tip)
        self.assertEqual(points[-1], b_tip)


if __name__ == '__main__':
    unittest.main()
