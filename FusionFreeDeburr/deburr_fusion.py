"""Fusion-side glue: sample B-rep edges, link them into continuous tool-tip
chains, draw those in a sketch, and create a Trace operation driven by them.
All lengths are in cm."""

import json
import math
import re

import adsk.core
import adsk.fusion
import adsk.cam

from . import deburr_chain as dc
from . import deburr_math as dm

SAMPLE_STEP = 0.05          # max edge/clearance sample spacing (0.5 mm)
PATH_TOL = 0.001           # tool-tip chord tolerance (0.01 mm)
CONVEXITY_PROBE = 0.005     # lateral probe distance for convexity
VERTEX_TOL = 1e-4           # cm; vertices this close are treated as the same
PARAMETER_ROUNDOFF = 1e-4   # fraction of an edge's parameter range a lookup may be out of order
NEWTON_STEPS = 8            # Newton steps matching a sample to its parameter
SAMPLE_MATCH_TOL = PATH_TOL / 2  # cm; a parameter is a sample's own if its point is this close
CLOSED_SPLITS = (.5, 1 / 3, 2 / 3, .25, .75)  # where a closed edge may be split: shares of its range or length
CLOSED_SPLIT_GAP = 10 * VERTEX_TOL  # cm; the split point must be this far from the seam
TRACE_STRATEGY = 'trace'
TRACE_EXPRESSIONS = (
    ('compensation', "'center'"), ('compensationType', "'off'"),
    ('axialOffset', '0 mm'), ('radialOffset', '0 mm'),
    ('doLeadIn', 'false'), ('doLeadOut', 'false'),
    # Trace adds its own chamfer offset for chamfer mills unless disabled.
    ('doChamfer', 'false'),
    ('preserveOrder', 'true'), ('orderByDepth', 'false'),
    ('smoothingFilter', 'false'), ('retractionPolicy', "'full'"),
    ('tolerance', '0.01 mm'))

ATTR_GROUP = 'AdditionalToolpaths'
ATTR_NAME = 'ballDeburr'
# Optional settings key, saved only when true: the operation was made with the
# gouge checks off (no trimming or rejection for cutting into the part).
SKIP_GOUGE_KEY = 'skip_gouge_checks'


def _tup(v):
    return (v.x, v.y, v.z)


def _is_fusion_error(error):
    """Whether `error` was raised by the Fusion API ("3 : invalid argument
    parameter"), which raises instead of returning ok=False for some inputs."""
    return isinstance(error, RuntimeError) and re.match(r'\d+ : ', str(error)) is not None


def _is_line_between_planes(edge: adsk.fusion.BRepEdge) -> bool:
    if edge.geometry.curveType != adsk.core.Curve3DTypes.Line3DCurveType:
        return False
    plane = adsk.core.SurfaceTypes.PlaneSurfaceType
    return all(f.geometry.surfaceType == plane for f in edge.faces)


def _is_closed(edge: adsk.fusion.BRepEdge) -> bool:
    return edge.startVertex is None or edge.startVertex == edge.endVertex


def _vertex_key(vertex: adsk.fusion.BRepVertex):
    return tuple(round(c / VERTEX_TOL) for c in _tup(vertex.geometry))


def _sample_edge(edge: adsk.fusion.BRepEdge, straight: bool, closed=False):
    """Points along the edge in parameter order, t0 to t1."""
    ev = edge.evaluator
    _, t0, t1 = ev.getParameterExtents()
    if straight:
        return [_tup(ev.getPointAtParameter(t)[1]) for t in (t0, t1)]
    if not closed:
        return _ordered_strokes(ev, t0, t1)
    # Both ends of a closed edge are on its seam, so its strokes' ends can't
    # show which way they run (nor can point lookups: at the seam they can
    # give t0 or t1, and on a full circle Fusion returns 0 past half way).
    # Stroke it in two parts split away from the seam, each with two
    # distinct ends, and order each part by them.
    ok, seam = ev.getPointAtParameter(t0)
    if not ok:
        raise dm.DeburrError('could not evaluate edge start')
    seam = _tup(seam)
    # Split by parameter and by length: a spline's parameters can bunch up
    # near its seam, and length can't.
    candidates = [t0 + (t1 - t0) * share for share in CLOSED_SPLITS]
    try:  # best effort: the parameter shares may already be enough
        ok, total = ev.getLengthAtParameter(t0, t1)
        if ok and total > 0:
            for share in CLOSED_SPLITS:
                ok, t = ev.getParameterAtLength(t0, total * share)
                if ok and t0 < t < t1:
                    candidates.append(t)
    except RuntimeError as e:
        if not _is_fusion_error(e):
            raise
    split = None
    for t in candidates:
        ok, p = ev.getPointAtParameter(t)
        if ok and (split is None or dc.dist(_tup(p), seam) > split[0]):
            split = (dc.dist(_tup(p), seam), t)
    if split is None or split[0] <= CLOSED_SPLIT_GAP:
        raise dm.DeburrError('closed edge is too small to sample')
    first, second = _ordered_strokes(ev, t0, split[1]), _ordered_strokes(ev, split[1], t1)
    return first + second[1:]


def _ordered_strokes(ev, a, b):
    """The edge's strokes from parameter a to b, P(a) first. getStrokes can
    return them from b back to a (seen on edges whose parameters run against
    their curve), and _refine_samples pairs them with parameters in order."""
    ok, points = ev.getStrokes(a, b, 0.001)  # 0.01 mm chord tolerance
    if not ok or len(points) < 2:
        raise dm.DeburrError('could not sample edge')
    points = [_tup(p) for p in points]
    ok, start = ev.getPointAtParameter(a)
    if not ok:
        raise dm.DeburrError('could not evaluate edge start')
    start = _tup(start)
    return points[::-1] if dc.dist(points[-1], start) < dc.dist(points[0], start) else points


def _refine_samples(edge, samples, radius, width, tool_axis, cutter=None):
    """Split long spans or spans whose tool-tip midpoint misses the chord.
    `samples` must run from parameter t0 to t1 (see _sample_edge)."""
    ev = edge.evaluator
    f1, f2 = edge.faces.item(0), edge.faces.item(1)

    def tip(p):
        if cutter is not None:
            return cutter.tip(p, _normal(f1, p), _normal(f2, p), tool_axis, width)
        return dm.tool_tip(p, _normal(f1, p), _normal(f2, p), tool_axis, radius, width)

    def split(a, b, pa, pb, ta, tb, depth):
        ok, length = ev.getLengthAtParameter(min(pa, pb), max(pa, pb))
        ok_p, p = ev.getPointAtParameter((pa + pb) / 2)
        if not ok or not ok_p:
            raise dm.DeburrError('could not refine edge samples')
        mid = _tup(p)
        tm = tip(mid)
        if length > SAMPLE_STEP or dc.dist(tm, dm.scale(dm.add(ta, tb), 0.5)) > PATH_TOL:
            if depth == 12:
                raise dm.DeburrError('tool-tip sampling tolerance could not be reached')
            return (split(a, mid, pa, (pa + pb) / 2, ta, tm, depth + 1) +
                    split(mid, b, (pa + pb) / 2, pb, tm, tb, depth + 1))
        return [a]

    ok, t0, t1 = ev.getParameterExtents()
    if not ok:
        raise dm.DeburrError('could not evaluate edge parameters')
    parameters = _lookup_parameters(ev, samples, t0, t1)
    if parameters is None:
        # Fusion's point lookup on a full circle returns 0 for every point
        # past half way (Fusion 2706.1.17), so go by length along the edge.
        samples, parameters = _length_parameters(ev, samples, t0, t1)
    points = []
    for a, b, pa, pb in zip(samples, samples[1:], parameters, parameters[1:]):
        points.extend(split(a, b, pa, pb, tip(a), tip(b), 0))
    return points + samples[-1:]


def _lookup_parameters(ev, samples, t0, t1):
    """Each sample's parameter by point lookup, or None if any is out of
    order or is not the sample's point. Paired with the wrong parameters,
    refinement would check the wrong part of the edge; outside [t0, t1],
    Fusion raises."""
    parameters = [t0]
    # A lookup can land a hair outside the order by rounding: before the
    # previous sample or past t1. It is clamped into order (this mostly
    # matters on tiny parameter ranges); anything more is a wrong pairing.
    roundoff = (t1 - t0) * PARAMETER_ROUNDOFF + 4 * math.ulp(max(abs(t0), abs(t1)))
    for p in samples[1:-1]:
        try:
            ok, t = ev.getParameterAtPoint(adsk.core.Point3D.create(*p))
        except RuntimeError as e:
            if not _is_fusion_error(e):
                raise
            ok = False
        if not ok:
            return None
        if not parameters[-1] <= t <= t1:
            clamped = min(max(t, parameters[-1]), t1)
            if abs(t - clamped) > roundoff:
                # A lookup that gives back its own sample shows the samples
                # really are out of order: reject the edge. One that doesn't
                # (Fusion's full-circle lookup) leaves it to the fallback.
                ok, q = ev.getPointAtParameter(t) if t0 <= t <= t1 else (False, None)
                if ok and dc.dist(_tup(q), p) <= SAMPLE_MATCH_TOL:
                    raise dm.DeburrError('edge samples are out of parameter order')
                return None
            t = clamped
        # In order is not enough: the parameter must be the sample's own
        # point (a lookup that returns 0 for every point is in order).
        ok, q = ev.getPointAtParameter(t)
        if not ok or dc.dist(_tup(q), p) > SAMPLE_MATCH_TOL:
            return None
        parameters.append(t)
    return parameters + [t1]  # at a closed seam, point lookup could return t0 instead


def _length_parameters(ev, samples, t0, t1):
    """(samples, parameters) without point lookups: each interior sample's
    share of the strokes' length, as the same share of the edge's length,
    gives a first guess that Newton steps along the edge move onto the
    sample. Each must land within SAMPLE_MATCH_TOL of its sample, in order, or
    the edge is rejected. Samples move onto the edge at their parameter."""
    ok, total = ev.getLengthAtParameter(t0, t1)
    chords = [dc.dist(a, b) for a, b in zip(samples, samples[1:])]
    run = sum(chords)
    if not ok or total <= 0 or run <= 0:
        raise dm.DeburrError('edge samples are out of parameter order')
    points, parameters, along = [samples[0]], [t0], 0.0
    for i, p in enumerate(samples[1:-1]):
        along += chords[i]
        ok, t = ev.getParameterAtLength(t0, total * along / run)
        if not ok:
            raise dm.DeburrError('edge samples are out of parameter order')
        t, q = _nearest_parameter(ev, p, min(max(t, t0), t1), t0, t1)
        if q is None or t < parameters[-1] or dc.dist(q, p) > SAMPLE_MATCH_TOL:
            raise dm.DeburrError('edge samples are out of parameter order')
        points.append(q)
        parameters.append(t)
    return points + samples[-1:], parameters + [t1]


def _nearest_parameter(ev, p, t, t0, t1, steps=NEWTON_STEPS):
    """(t, point) after Newton steps from `t` toward the point of the edge
    nearest `p`, kept inside [t0, t1]; point is None if Fusion can't say.
    The caller judges the point: the steps stop early on the sample itself,
    or where the derivative can't give a step."""
    for _ in range(steps):
        ok, point = ev.getPointAtParameter(t)
        if not ok:
            return t, None
        q = _tup(point)
        if dc.dist(q, p) == 0:
            break
        ok, d = ev.getFirstDerivative(t)
        d = _tup(d) if ok else (0, 0, 0)
        speed = dm.dot(d, d)
        if speed <= 0:
            break
        step = dm.dot(dm.sub(p, q), d) / speed
        nxt = min(max(t + step, t0), t1)
        if nxt == t:
            break
        t = nxt
    ok, point = ev.getPointAtParameter(t)
    return t, _tup(point) if ok else None


def _normal(face: adsk.fusion.BRepFace, p):
    ok, n = face.evaluator.getNormalAtPoint(adsk.core.Point3D.create(*p))
    if not ok:
        raise dm.DeburrError('could not evaluate face normal')
    return dm.normalize(_tup(n))


class _FaceIds:
    """Small integer ids for faces, so neighbouring edges can find their shared face."""

    def __init__(self):
        self._faces = []

    def __call__(self, face):
        for i, f in enumerate(self._faces):
            if f == face:
                return i
        self._faces.append(face)
        return len(self._faces) - 1


def _check_convexity(edge, f1, f2, checks):
    """Across the wedge, both probes are outside only at a convex edge."""
    for p in checks:
        direction = dm.normalize(dm.sub(_normal(f1, p), _normal(f2, p)))
        for step in (CONVEXITY_PROBE, CONVEXITY_PROBE / 10):
            states = [edge.body.pointContainment(adsk.core.Point3D.create(
                *dm.add(p, dm.scale(direction, sign * step)))) for sign in (-1, 1)]
            if all(v == adsk.fusion.PointContainment.PointOutsidePointContainment for v in states):
                break
            if any(v == adsk.fusion.PointContainment.PointInsidePointContainment for v in states):
                raise dm.DeburrError('edge is concave or obstructed')
        else:
            raise dm.DeburrError('could not establish edge convexity')


def edge_segment(edge: adsk.fusion.BRepEdge, radius, width, tool_axis, face_ids,
                 cutter=None) -> dc.Segment:
    """Tool-tip path for one edge, oriented start vertex -> end vertex.
    Raises DeburrError if the edge can't be done."""
    try:
        return _edge_segment(edge, radius, width, tool_axis, face_ids, cutter)
    except RuntimeError as e:
        # A Fusion evaluator failing on this edge fails the edge, not the
        # operation. Other errors are bugs and keep their traceback.
        if not _is_fusion_error(e):
            raise
        raise dm.DeburrError(f'Fusion could not evaluate the edge ({e})') from e


def _edge_segment(edge, radius, width, tool_axis, face_ids, cutter):
    if edge.faces.count != 2:
        raise dm.DeburrError('edge does not have exactly two faces')
    f1, f2 = edge.faces.item(0), edge.faces.item(1)
    id1, id2 = face_ids(f1), face_ids(f2)

    straight = _is_line_between_planes(edge)
    closed = _is_closed(edge)
    samples = _sample_edge(edge, straight, closed)
    if closed:
        # Drop near-duplicate points but keep both ends: they are P(t0) and
        # P(t1), which _refine_samples pairs with t0 and t1.
        samples = dc._clean(samples[:-1]) + samples[-1:]
        if len(samples) > 2 and dc.dist(samples[-2], samples[-1]) <= dc.JOIN_TOL:
            del samples[-2]
        if len(samples) < 4 or len(set(samples[:-1])) < 3:
            raise dm.DeburrError('closed edge has too few sample points')
    if not straight:
        if cutter is None:
            samples = _refine_samples(edge, samples, radius, width, tool_axis)
        else:
            samples = _refine_samples(edge, samples, radius, width, tool_axis, cutter)
    if closed:
        samples = samples[:-1]
        if len(dc._clean(samples)) < 3:
            raise dm.DeburrError('closed edge has too few distinct sample points')
    else:
        start = _tup(edge.startVertex.geometry)
        if dc.dist(samples[0], start) > dc.dist(samples[-1], start):
            samples.reverse()

    checks = samples[1:-1] or [dm.scale(dm.add(samples[0], samples[-1]), 0.5)]
    _check_convexity(edge, f1, f2, checks)

    # Edge direction at each end, pointing into the edge (for corner rolls).
    tangents = {0: dm.normalize(dm.sub(samples[1], samples[0])),
                len(samples) - 1: dm.normalize(dm.sub(samples[-2], samples[-1]))}
    tips, ends = [], []
    for k, p in enumerate(samples):
        n1, n2 = _normal(f1, p), _normal(f2, p)
        tips.append(cutter.tip(p, n1, n2, tool_axis, width) if cutter is not None
                    else dm.tool_tip(p, n1, n2, tool_axis, radius, width))
        if k in tangents:
            ends.append(dc.End(p, {id1: n1, id2: n2}, tangents[k]))

    if closed:
        tips.append(tips[0])

    return dc.Segment(
        tips=tips, start=ends[0], end=ends[-1],
        start_key=None if closed else _vertex_key(edge.startVertex),
        end_key=None if closed else _vertex_key(edge.endVertex),
        closed=closed, source=edge)


def _cut_depth(body: adsk.fusion.BRepBody, radius, tool_axis):
    """Function(tool tips) -> deepest cut of the ball into `body` along them
    (radius minus the ball center's distance to the body)."""
    measure = adsk.core.Application.get().measureManager
    z = dm.normalize(tool_axis)

    def depth(tips):
        worst = -radius
        samples = []
        for a, b in zip(tips, tips[1:]):
            n = max(1, math.ceil(dc.dist(a, b) / min(SAMPLE_STEP, radius / 4)))
            samples.extend(dm.add(a, dm.scale(dm.sub(b, a), k / n)) for k in range(n))
        samples.extend(tips[-1:])
        for tip in samples:
            center = adsk.core.Point3D.create(*dm.add(tip, dm.scale(z, radius)))
            if body.pointContainment(center) == adsk.fusion.PointContainment.PointInsidePointContainment:
                return 2 * radius
            worst = max(worst, radius - measure.measureMinimumDistance(center, body).value)
        return worst
    return depth


EDGE_DISTANCE_TOL = 1e-4   # cm; chord tolerance of the edge shapes measured against
CUT_SAMPLE_TOL = PATH_TOL  # cm; stroke tolerance when sampling the material a ball removes
CUT_MESH_SIDE = .25        # longest mesh side over the removed material, as a fraction of the width
LOOP_OVERLAP = 0.01        # cm; a closed ball loop is cut this far past its start, so its ends differ
BREAK_KEPT_TOL = 0.001     # cm; a kept path this close to an edge's tool tips still breaks it
CUT_MESH_BUDGET = 200000   # most triangles meshed over one ball's (or plunge's) removed material
INSIDE_STEP = .25          # steps into the removed material, as a fraction of the width


def _edge_polyline(edge):
    """The edge as points on it, in either direction, within EDGE_DISTANCE_TOL.

    measureMinimumDistance to a BRepEdge measures to the unbounded curve (a
    straight edge reads as an infinite line), so distances to an edge are
    taken from this polyline instead."""
    try:
        ev = edge.evaluator
        ok, t0, t1 = ev.getParameterExtents()
        ok, points = ev.getStrokes(t0, t1, EDGE_DISTANCE_TOL) if ok else (False, [])
    except RuntimeError as e:
        if not _is_fusion_error(e):
            raise
        ok = False
    if not ok or len(points) < 2:
        raise dm.DeburrError('could not sample a selected edge for the gouge check')
    return [_tup(p) for p in points]


def _same(entity, entities):
    return any(entity == other for other in entities)


def _unique(entities):
    result = []
    for entity in entities:
        if not _same(entity, result):
            result.append(entity)
    return result


def _cut_samples(cut, width, inside=None, on_tool=None, in_tool=None, fails=None):
    """Points on the material a ball removes: its vertices, edge strokes,
    and the nodes of a mesh over its faces, so face interiors are covered.

    With `inside` (a depth), points inside the material are added too:
    stepping in from the surface along the mesh normals, wherever a point
    lies more than `inside` from every surface sample (dc.inward_points). A
    point deep inside can be farther from the selected edges than any point
    of the surface when selected edges surround the material, for example a
    ball swallowing a small feature. Steps start only from the part's own
    faces: `on_tool(point)` marks nodes on the tool's surface, whose steps
    head into the tool, not the part. The removed material lies inside the
    tool, so steps outside it (`in_tool(point)` false) are dropped before
    the containment test.

    With `fails(samples)`, the vertices and edge strokes are returned alone
    when they already fail: a gross cut is not meshed. A region too large to
    mesh at the full resolution within CUT_MESH_BUDGET triangles is an error
    (its chain is skipped with a warning), never a coarser check."""
    samples = [_tup(v.geometry) for v in cut.vertices]
    for edge in cut.edges:
        ev = edge.evaluator
        ok, t0, t1 = ev.getParameterExtents()
        ok, points = ev.getStrokes(t0, t1, CUT_SAMPLE_TOL) if ok else (False, [])
        if not ok:
            raise dm.DeburrError('could not sample the material the ball removes')
        samples.extend(_tup(p) for p in points)
    if fails and fails(samples):
        return samples
    side = max(width * CUT_MESH_SIDE, CUT_SAMPLE_TOL)
    if 2 * cut.area / (side * side) > CUT_MESH_BUDGET:
        raise dm.DeburrError('the material the ball would remove is too large to check '
                             'at full resolution')
    calculator = cut.meshManager.createMeshCalculator()
    mesh = None
    if calculator:
        calculator.surfaceTolerance = CUT_SAMPLE_TOL
        calculator.maxSideLength = side
        mesh = calculator.calculate()
    coords = list(mesh.nodeCoordinatesAsDouble) if mesh else []
    if not coords or len(coords) % 3:
        raise dm.DeburrError('could not mesh the material the ball removes')
    nodes = [tuple(coords[i:i + 3]) for i in range(0, len(coords), 3)]
    samples.extend(nodes)
    if inside:
        normals = list(mesh.normalVectorsAsDouble)
        if len(normals) != len(coords):
            raise dm.DeburrError('could not read the mesh normals of the material the ball removes')
        normals = [tuple(normals[i:i + 3]) for i in range(0, len(normals), 3)]
        part = [(n, v) for n, v in zip(nodes, normals) if not (on_tool and on_tool(n))]
        within = adsk.fusion.PointContainment.PointInsidePointContainment
        samples.extend(p for p in dc.inward_points([n for n, _ in part], [v for _, v in part],
                                                   samples, inside)
                       if (not in_tool or in_tool(p))
                       and cut.pointContainment(adsk.core.Point3D.create(*p)) == within)
    return samples


def _excess(bodies, segments, radius, width, tool_axis, limit):
    """Function(tip, margin=0, enough=None) -> how far beyond the edge break
    the ball cuts: the largest distance from any material it removes from
    `bodies` to the nearest selected edge (`segments`, from
    dc.polyline_segments), minus the break width. `margin` grows the ball,
    so one check covers every ball within `margin` of the tip. With
    `enough`, the result only needs to say whether it exceeds `enough`.

    The removed material is the ball intersected with each body, sampled at
    its vertices, edge strokes and mesh nodes; points between samples are
    not measured. Results above `limit` are capped a little above it, since
    only the selected edges within reach of a passing cut are searched."""
    temp = adsk.fusion.TemporaryBRepManager.get()
    z = dm.normalize(tool_axis)
    intersect = adsk.fusion.BooleanTypes.IntersectionBooleanType
    outside = adsk.fusion.PointContainment.PointOutsidePointContainment
    boxes = [(_tup(b.boundingBox.minPoint), _tup(b.boundingBox.maxPoint)) for b in bodies]

    def excess(tip, margin=0.0, enough=None):
        center = dm.add(tip, dm.scale(z, radius))
        size = radius + margin
        # A removed point within width + limit of an edge lies within this of
        # the center, so farther segments cannot make any sample pass; the
        # distances are capped here, a little above the limit.
        reach = size + width + 2 * limit
        near = segments.within(center, reach)
        worst = 0.0
        for body, (lo, hi) in zip(bodies, boxes):
            if dc.box_gap(center, lo, hi) >= size:
                continue
            if body.pointContainment(adsk.core.Point3D.create(*center)) != outside:
                # Breaking an edge keeps the center outside the part. Inside,
                # the ball can swallow a feature whose middle is farther from
                # the edges than its surface. The center is removed material,
                # so its own distance is an honest lower bound.
                own = dc.farthest([center], near, reach) - width
                return own if own > limit else math.inf   # fails; no depth measured
            cut = temp.createSphere(adsk.core.Point3D.create(*center), size)
            if not cut or not temp.booleanOperation(cut, body, intersect):
                raise dm.DeburrError('could not intersect the ball with the part')
            if not cut.faces.count:
                continue
            on_ball = lambda p: abs(dc.dist(p, center) - size) <= CUT_SAMPLE_TOL
            in_ball = lambda p: dc.dist(p, center) < size
            threshold = width + (limit if enough is None else enough)
            fails = lambda pts: dc.farthest(pts, near, reach, threshold) > threshold
            far = dc.farthest(_cut_samples(cut, width, width * INSIDE_STEP, on_ball, in_ball, fails), near, reach,
                              None if enough is None else width + enough)
            worst = max(worst, far - width)
            if enough is not None and worst > enough:
                break
        return worst
    return excess


def _column_excess(bodies, segments, radius, width, tool_axis, limit):
    """Function(tip) -> how far beyond the edge break the ball cuts while
    plunging straight down the tool axis onto `tip` (or retracting from it),
    as _excess measures it. Trace reaches and leaves every contour this way.

    The plunge sweeps the ball at `tip` plus a cylinder of the ball's radius
    from its center up past the top of every body; the cylinder is checked
    here, and the ball at the tip by _excess."""
    temp = adsk.fusion.TemporaryBRepManager.get()
    z = dm.normalize(tool_axis)
    intersect = adsk.fusion.BooleanTypes.IntersectionBooleanType
    boxes = [(_tup(b.boundingBox.minPoint), _tup(b.boundingBox.maxPoint)) for b in bodies]
    corners = [(x, y, w) for lo, hi in boxes for x in (lo[0], hi[0])
               for y in (lo[1], hi[1]) for w in (lo[2], hi[2])]
    top = max(dm.dot(c, z) for c in corners) + radius

    def column(tip):
        bottom = dm.add(tip, dm.scale(z, radius))
        height = top - dm.dot(bottom, z)
        if height <= 0:
            return 0.0
        upper = dm.add(bottom, dm.scale(z, height))
        lo = tuple(min(bottom[k], upper[k]) - radius for k in range(3))
        hi = tuple(max(bottom[k], upper[k]) + radius for k in range(3))
        reach = width + 2 * limit
        near = segments.near_path(bottom, upper, radius + reach)
        worst = 0.0
        for body, (blo, bhi) in zip(bodies, boxes):
            if any(blo[k] > hi[k] or bhi[k] < lo[k] for k in range(3)):
                continue
            cut = temp.createCylinderOrCone(adsk.core.Point3D.create(*bottom), radius,
                                            adsk.core.Point3D.create(*upper), radius)
            if not cut or not temp.booleanOperation(cut, body, intersect):
                raise dm.DeburrError('could not intersect the plunge with the part')
            if not cut.faces.count:
                continue
            # Capped like _excess: only whether it passes matters, and the
            # depth it reports is a lower bound.
            def place(p):
                along = dm.dot(dm.sub(p, bottom), z)
                return along, dc.dist(p, dm.add(bottom, dm.scale(z, along)))

            def on_column(p):
                along, off = place(p)
                return abs(off - radius) <= CUT_SAMPLE_TOL or abs(along) <= CUT_SAMPLE_TOL

            def in_column(p):
                along, off = place(p)
                return 0 < along < height and off < radius
            fails = lambda pts: dc.farthest(pts, near, radius + reach, width + limit) > width + limit
            far = dc.farthest(_cut_samples(cut, width, width * INSIDE_STEP, on_column, in_column, fails), near,
                              radius + reach, width + limit)
            worst = max(worst, far - width)
            if worst > limit:
                break
        return worst
    return column


def gouge_limit(width):
    """How far beyond the break width removed material may lie (cm): the
    corner check's limit plus the path's chord tolerance."""
    return dc.excess_limit(width) + PATH_TOL


def _trim_gouges(chain, excess, column, width):
    """Cut `chain` back wherever the ball would cut too far (see _excess),
    then move each contour end inward until the plunge onto it and the
    retract from it are clear too (see _column_excess).

    Returns (paths, removed, lost, notes): the clear polylines, which must
    stay separate open contours (a closed loop is continued LOOP_OVERLAP
    past its start), one (length, deepest excess) per stretch removed for
    cutting too far (math.inf when no depth was measured), one reason per
    polyline removed whole (its own deepest excess, or a note when it was
    dropped for another reason), and notes on path removed for other
    reasons."""
    limit = gouge_limit(width)

    def swept(a, b):
        # A ball grown by half the move covers every ball along it.
        middle = dm.scale(dm.add(a, b), .5)
        return excess(middle, dc.dist(a, b) / 2, limit) <= limit
    paths = dc.chain_polylines(chain)
    kept, removed, lost, notes, opened = [], [], [], [], 0.0
    for points in paths:
        # Only whether a point is over the limit matters, so its check stops
        # at the first sample beyond it: removed depths are lower bounds.
        spans, cut = dc.checked_spans(points, lambda p: excess(p, 0.0, limit), swept, limit, limit,
                                      limit / 8)
        loop = chain.closed and len(paths) == 1 and dc.dist(points[0], points[-1]) <= dc.JOIN_TOL
        if (cut and loop and len(spans) > 1 and spans[0][0] == points[0]
                and spans[-1][-1] == points[-1]):
            # The loop's seam lies inside a clear stretch: rejoin it there.
            spans = [spans[-1] + spans[0][1:]] + spans[1:-1]
        elif (cut and loop and len(cut) > 1 and (not spans or spans[0][0] != points[0])
                and (not spans or spans[-1][-1] != points[-1])):
            # One blocked stretch across the seam, reported in two parts.
            (a, da), (b, db) = cut[0], cut[-1]
            cut = [(a + b, max(da, db))] + cut[1:-1]
        ends, why = [], 'no clear stretch'
        for span in spans:
            # Trace may start a closed contour anywhere; an open one starts
            # where it is drawn, so only its two ends need a clear plunge.
            span = dc.open_loop(span, LOOP_OVERLAP)
            # Coarse steps, then bisection: a step too far can only skip a
            # narrow clear window, which shortens the path more (safe).
            shortened, moved = dc.shorten_ends(span, column, limit, SAMPLE_STEP, limit / 8)
            cut = cut + moved
            closed = lambda pts: pts is not None and dc.dist(pts[0], pts[-1]) < LOOP_OVERLAP / 2
            for _ in range(3):
                # Trimming the plunge can take the overlap away and close
                # the loop again; cut its end back so Trace cannot move it.
                if not closed(shortened):
                    break
                back = dc.cut_back(shortened, LOOP_OVERLAP)
                opened += LOOP_OVERLAP
                shortened, moved = (None, []) if back is None else dc.shorten_ends(
                    back, column, limit, SAMPLE_STEP, limit / 8)
                cut = cut + moved
            if closed(shortened):
                why = 'a path too short to cut as an open contour was dropped'
                notes.append(why)
                shortened = None
            if shortened is not None and (column(shortened[0]) > limit or column(shortened[-1]) > limit):
                # The exported ends are the ones Trace plunges to: check them as they are.
                why = 'a path whose exported ends could not be plunged to was dropped'
                notes.append(why)
                shortened = None
            if shortened is not None:
                ends.append(shortened)
        if not ends:
            # This path's own reason: its deepest cut, or why it was dropped.
            depths = [d for _, d in cut]
            lost.append(max(depths) if depths else why)
        kept.extend(ends)
        removed.extend(cut)
    if opened:
        notes.append(f'{opened * 10:.2f} mm of path removed to keep a closed loop open, so '
                     f'Fusion starts it where its plunge was checked')
    return kept, removed, lost, notes


def _edge_list(numbers):
    return ('Edge ' if len(numbers) == 1 else 'Edges ') + ', '.join(str(n) for n in numbers)


def _deepest_cut(bodies, radius, tool_axis):
    depths = [_cut_depth(body, radius, tool_axis) for body in bodies]
    return lambda tips: max(depth(tips) for depth in depths)


def build_chains(edges, radius, width, tool_axis, log=None, check_gouges=True):
    """Returns (chains, edges to save with the operation, warnings). Connected edges are linked into
    continuous chains (see deburr_chain). Paths trimmed where the ball would
    cut other faces are expected, so they go to `log`, not the warnings.

    check_gouges=False keeps every path whole: no trimming, and corners are
    joined without the excess-depth check."""
    face_ids = _FaceIds()
    segments, warnings, numbers = [], [], {}
    for i, edge in enumerate(edges):
        try:
            segments.append(edge_segment(edge, radius, width, tool_axis, face_ids))
            numbers[id(segments[-1].source)] = i + 1
        except dm.DeburrError as e:
            warnings.append(f'Edge {i + 1} skipped: {e}')

    chains = []
    # Every body with a selected edge, even one whose edges were all skipped:
    # a path on one can run into another, and edges of touching bodies can
    # share vertices and link into one chain.
    bodies = _unique([e.body for e in edges])
    depth = _deepest_cut(bodies, radius, tool_axis) if check_gouges else None
    shapes, unsampled = None, set()
    if check_gouges:
        # Material within the break of any selected edge may be cut,
        # including edges of other chains (both lips of a slot). An edge that
        # cannot be sampled allows nothing, which only makes checks stricter.
        found = []
        for s in segments:
            try:
                found.extend(dc.polyline_segments(_edge_polyline(s.source)))
            except dm.DeburrError:
                unsampled.add(id(s.source))
                warnings.append(f'Edge {numbers[id(s.source)]} skipped: it could not be sampled '
                                'for the gouge check')
        shapes = dc.SegmentIndex(found, radius)
        # Only the unsampled edge is lost: its neighbours still chain and are
        # checked, without its break, which only makes the check stricter.
        segments = [s for s in segments if id(s.source) not in unsampled]
    for ordered, closed in dc.order_chains(segments):
        sources = [s.source for s in ordered]
        chain = dc.link_chain(ordered, closed, tool_axis, radius, width, depth)
        warnings += chain.notes
        if not check_gouges:
            if chain.pieces:
                chains.append(chain)
            continue
        label = _edge_list(sorted(numbers[id(s)] for s in sources))
        try:
            limit = gouge_limit(width)
            paths, removed, lost, notes = _trim_gouges(
                chain, _excess(bodies, shapes, radius, width, tool_axis, limit),
                _column_excess(bodies, shapes, radius, width, tool_axis, limit), width)
        except Exception as e:
            warnings.append(f'{label} skipped: the gouge check failed ({e})')
            continue
        # One open chain per clear path, so nothing rejoins them across a
        # trim and Trace starts each where its plunge was checked.
        chains.extend(dc.Chain(pieces=[dc.Piece(p)]) for p in paths)
        def reason(depths):
            measured = [d for d in depths if not math.isinf(d)]
            if measured:
                return (f'the ball, or the plunge onto it, would cut at least '
                        f'{max(measured) * 10:.3f} mm beyond the edge break')
            return "the ball's center would be inside the part"
        why = reason([d for _, d in removed])
        whole = [reason([r]) + ' along the whole path' if not isinstance(r, str) else r
                 for r in lost]
        if not paths:
            warnings.append(f'{label} skipped: ' + '; '.join(dict.fromkeys(whole)))
            continue
        if lost:
            warnings.append(f'{label}: {len(lost)} of the paths skipped: ' +
                            '; '.join(dict.fromkeys(whole)))
        # An edge inside a longer path can lose its whole break while the
        # path survives on its neighbours: that edge is left sharp.
        for s in ordered:
            middle = s.tips[len(s.tips) // 4:len(s.tips) - len(s.tips) // 4] or s.tips
            if not any(dc.polyline_distance(t, p) <= BREAK_KEPT_TOL for t in middle for p in paths):
                warnings.append(f'Edge {numbers[id(s.source)]}: no break left; {why} along it')
        if log:
            if removed:
                length = sum(l for l, _ in removed) * 10
                log(f'{label}: {length:.2f} mm of path removed in {len(removed)} place(s) where {why}')
            for note in notes:
                log(f'{label}: {note}')
    # Every selected edge is kept with the operation, skipped ones too, so
    # Edit Deburr restores the same selection and checks the same bodies.
    return chains, list(edges), warnings


def setup_tool_axis(setup: adsk.cam.Setup):
    _, _, _, z = setup.workCoordinateSystem.getAsCoordinateSystem()
    return _tup(z)


def build_sketch(design: adsk.fusion.Design, chains, name):
    """Draws each chain as a single curve: a degree-1 NURBS (exact polyline,
    corners stay sharp). Returns (sketch, [[curve] per chain]).

    One curve per chain, not one per piece: Trace's chain resolver mishandles
    multi-curve chains that are edge-on to the tool axis (e.g. a profile in a
    vertical plane) - it fails with "No contours have been specified" or, on
    regeneration, swaps the chain for another chain's curves."""
    root = design.rootComponent
    sketch = root.sketches.add(root.xYConstructionPlane)
    try:
        sketch.name = name
        sketch.isComputeDeferred = True
        splines = sketch.sketchCurves.sketchFixedSplines
        curve_chains = []
        for points in [p for chain in chains for p in dc.chain_polylines(chain)]:
            ctrl = [sketch.modelToSketchSpace(adsk.core.Point3D.create(*p)) for p in points]
            nurbs = adsk.core.NurbsCurve3D.createNonRational(ctrl, 1, dc.polyline_knots(points), False)
            curve_chains.append([splines.addByNurbsCurve(nurbs)])
        sketch.isComputeDeferred = False
    except Exception:
        sketch.deleteMe()
        raise
    return sketch, curve_chains


def _curves_parameter(op_or_input):
    """The curve-selection parameter of a trace Operation or OperationInput."""
    params = op_or_input.parameters
    param = params.itemByName('curves')
    if param:
        value = adsk.cam.CadContours2dParameterValue.cast(param.value)
        if value:
            return value
    for param in params:
        value = adsk.cam.CadContours2dParameterValue.cast(param.value)
        if value:
            return value
    names = ', '.join(p.name for p in params)
    raise RuntimeError(f'No curve parameter on trace input. Parameters: {names}')


def _apply_curves(value: adsk.cam.CadContours2dParameterValue, curve_chains):
    """One chain selection per connected chain, so Trace runs it without retracts."""
    selections = value.getCurveSelections()
    selections.clear()
    for curves in curve_chains:
        chain = selections.createNewChainSelection()
        chain.inputGeometry = curves
    value.applyCurveSelections(selections)


CHAMFER_TOOL_TYPES = ('chamfer mill', 'thread mill')


def _trace_settings(op):
    """Paths already describe the tool tip: disable Trace displacement.

    Call after the operation's tool is assigned. For chamfer tools Fusion
    turns Trace's own chamfer offset on by default, so it must exist and
    must read back as off."""
    kind = op.parameters.itemByName('tool_type')
    chamfer_tool = bool(kind) and kind.value.value in CHAMFER_TOOL_TYPES
    required = ('compensation', 'axialOffset') + (('doChamfer',) if chamfer_tool else ())
    for name, expression in TRACE_EXPRESSIONS:
        param = op.parameters.itemByName(name)
        if not param and name in required:
            raise dm.DeburrError(f'Trace is missing required safety parameter: {name}')
        if param:
            param.expression = expression
    if chamfer_tool and op.parameters.itemByName('doChamfer').value.value:
        raise dm.DeburrError('Trace chamfer offset could not be disabled')


def create_trace(setup: adsk.cam.Setup, curve_chains, name, tool=None):
    op_input = setup.operations.createInput(TRACE_STRATEGY)
    op_input.displayName = name
    if tool:
        op_input.tool = tool
    # Not generated here: the caller generates after its last change to the op.
    op_input.generationMode = adsk.cam.AutomaticGenerationModes.SkipGeneration
    op = setup.operations.add(op_input)
    # Curves go on the added operation (same path as editing), so create and
    # edit can't diverge in how the selection is stored.
    try:
        _trace_settings(op)
        _apply_curves(_curves_parameter(op), curve_chains)
    except Exception:
        op.deleteMe()
        raise
    return op


def update_trace_curves(op: adsk.cam.Operation, curve_chains):
    _trace_settings(op)
    _apply_curves(_curves_parameter(op), curve_chains)


def update_operation(design, op, chains, edges, width, tool=None, skip_gouge_checks=False):
    """Replace the sketch only after all operation settings have been applied."""
    settings = load_settings(op)
    old_sketch = find_by_token(design, settings.get('sketch'), adsk.fusion.Sketch.cast)
    old_name = old_sketch.name if old_sketch else None
    old_tool = op.tool
    value = _curves_parameter(op)
    old_curves = value.getCurveSelections()
    params = [op.parameters.itemByName(n) for n, _ in TRACE_EXPRESSIONS]
    expressions = [(p, p.expression) for p in params if p]
    old_attr = op.attributes.itemByName(ATTR_GROUP, ATTR_NAME).value
    sketch, curves = build_sketch(design, chains, op.name + ' (new)')
    try:
        # Assign the tool first: Fusion may re-default tool-dependent
        # parameters such as doChamfer when the tool changes.
        if tool is not None:
            op.tool = tool
        update_trace_curves(op, curves)
        if old_sketch:
            old_sketch.name = old_name + ' (old)'
        sketch.name = op.name
        save_settings(op, edges, width, sketch, skip_gouge_checks)
        if old_sketch and not old_sketch.deleteMe():
            raise dm.DeburrError('could not remove the replaced sketch')
    except Exception:
        # If restoration fails, keep both sketches: either may still be referenced.
        try:
            if tool is not None:
                op.tool = old_tool
            for p, expression in expressions:
                p.expression = expression
            value.applyCurveSelections(old_curves)
            op.attributes.add(ATTR_GROUP, ATTR_NAME, old_attr)
        except Exception as rollback_error:
            raise dm.DeburrError('Update rollback failed; both sketches retained: ' + str(rollback_error))
        sketch.deleteMe()
        if old_sketch:
            old_sketch.name = old_name
        raise
    return sketch


# --- Settings stored on the operation, so it can be edited later ---------

def save_settings(op: adsk.cam.Operation, edges, width, sketch, skip_gouge_checks=False):
    data = {
        'version': 1,
        'edges': [e.entityToken for e in edges],
        # Optional, one per edge: finds the edge again when its token cannot
        # be resolved (linked components, manufacturing models).
        'edge_shapes': [edge_shape(e) for e in edges],
        'width': width,
        'sketch': sketch.entityToken,
    }
    if skip_gouge_checks:
        data[SKIP_GOUGE_KEY] = True
    op.attributes.add(ATTR_GROUP, ATTR_NAME, json.dumps(data))


def load_settings(op: adsk.cam.Operation):
    attr = op.attributes.itemByName(ATTR_GROUP, ATTR_NAME)
    return json.loads(attr.value) if attr else None


def find_by_token(design: adsk.fusion.Design, token, cast):
    for entity in design.findEntityByToken(token) if token else []:
        typed = cast(entity)
        if typed:
            return typed
    return None


def edge_shape(edge: adsk.fusion.BRepEdge):
    """[length, point, ...] of an edge in model space, or None: its length and
    dc.EDGE_SHAPE_POINTS points evenly spaced in parameter, start to end.

    The shape is optional (None only means the edge cannot be found again by
    shape), so a Fusion error here never fails the operation."""
    try:
        ev = edge.evaluator
        ok, t0, t1 = ev.getParameterExtents()
        if not ok:
            return None
        points = []
        last = dc.EDGE_SHAPE_POINTS - 1
        for i in range(last + 1):
            # The end is t1 itself: t0 + (t1 - t0) can round past t1, and
            # Fusion raises for a parameter outside the edge.
            ok, p = ev.getPointAtParameter(t1 if i == last else t0 + (t1 - t0) * i / last)
            if not ok:
                return None
            points.append(list(_tup(p)))
        return [edge.length] + points
    except RuntimeError as e:
        if not _is_fusion_error(e):
            raise
        return None


def setup_model_edges(setup: adsk.cam.Setup):
    """Every edge of the setup's model bodies, as CAM presents them."""
    def bodies(entities):
        result = []
        for entity in entities:
            body = adsk.fusion.BRepBody.cast(entity)
            occurrence = adsk.fusion.Occurrence.cast(entity)
            if body:
                result.append(body)
            elif occurrence:
                result.extend(bodies(occurrence.bRepBodies))
                result.extend(bodies(occurrence.childOccurrences))
        return result
    return [edge for body in bodies(setup.models) for edge in body.edges]


def _occurrence_path(entity):
    context = entity.assemblyContext
    return context.fullPathName if context else ''


def manufacturing_model_designs(design: adsk.fusion.Design):
    """The own design of each manufacturing model in the design's document."""
    cam = adsk.cam.CAM.cast(
        design.parentDocument.products.itemByProductType('CAMProductType'))
    models = getattr(cam, 'manufacturingModels', None) if cam else None
    designs = []
    for model in models or []:
        occurrence = model.occurrence
        if occurrence:
            designs.append(occurrence.component.parentDesign)
    return designs


def _edge_index(edge: adsk.fusion.BRepEdge):
    native = edge.nativeObject or edge
    for i, other in enumerate(native.body.edges):
        if other == native:
            return i
    return None


def _is_copy_of(saved: adsk.fusion.BRepEdge, candidate: adsk.fusion.BRepEdge):
    """Whether `candidate` is the same edge of a copy of `saved`'s body: the
    same body name, the same place in the body's edge list, and the same
    shape. A manufacturing model's design holds such a copy."""
    index = _edge_index(saved)
    return (index is not None and index == _edge_index(candidate)
            and (saved.nativeObject or saved).body.name
            == (candidate.nativeObject or candidate).body.name
            and dc.same_edge_shape(edge_shape(saved), edge_shape(candidate),
                                   VERTEX_TOL))


def _is_token_edge(candidate: adsk.fusion.BRepEdge, token, designs=()):
    """Whether `token` names this setup-model edge.

    The token is resolved through the design that owns the candidate's body.
    The edge it gives is seen from that design's root, so it is compared with
    the candidate by native edge and by the end of the occurrence path. If it
    does not resolve there, it is tried in `designs` (the manufacturing
    models' own designs), whose edges are copies: the edge found must be the
    candidate's copy (see `_is_copy_of`)."""
    owner = candidate.body.parentComponent.parentDesign
    saved = find_by_token(owner, token, adsk.fusion.BRepEdge.cast) if owner else None
    if saved is not None:
        if saved == candidate:
            return True
        if (saved.nativeObject or saved) != (candidate.nativeObject or candidate):
            return False
        path, saved_path = _occurrence_path(candidate), _occurrence_path(saved)
        return not saved_path or path == saved_path or path.endswith('+' + saved_path)
    for other in designs:
        if other == owner:
            continue
        saved = find_by_token(other, token, adsk.fusion.BRepEdge.cast)
        if saved is not None:
            return _is_copy_of(saved, candidate)
    return False


def find_edge(design: adsk.fusion.Design, token, shape, candidates, designs=lambda: ()):
    """The saved edge: by token, else the one edge from `candidates()` that
    the token names and that still has the saved shape.

    Design.findEntityByToken does not resolve edges of manufacturing models.
    Those are looked up among the setup's model edges instead, with the token
    resolved in the candidate's own design or in one of `designs()`. A
    coincident edge of another body has the saved shape but is not the saved
    edge, so shape alone never selects an edge. No match, or more than one
    distinct match, returns None so the user reselects."""
    edge = find_by_token(design, token, adsk.fusion.BRepEdge.cast)
    if edge or not shape:
        return edge
    matches = []
    other_designs = None
    for candidate in candidates():
        if abs(candidate.length - shape[0]) > VERTEX_TOL:
            continue
        if not dc.same_edge_shape(edge_shape(candidate), shape, VERTEX_TOL):
            continue
        if other_designs is None:
            other_designs = list(designs())
        if _is_token_edge(candidate, token, other_designs) and candidate not in matches:
            matches.append(candidate)
    return matches[0] if len(matches) == 1 else None


def operation_for_entity(design: adsk.fusion.Design, entity, ops):
    """The deburr op in `ops` that `entity` refers to: the op itself, or its
    sketch or one of its sketch curves (what a click on the path in the canvas
    picks, since the sketch lies on the toolpath). None if neither."""
    op = adsk.cam.Operation.cast(entity)
    if op:
        return next((o for o in ops if o == op), None)
    sketch = adsk.fusion.Sketch.cast(entity)
    if not sketch:
        curve = adsk.fusion.SketchCurve.cast(entity)
        sketch = curve.parentSketch if curve else None
    if not sketch:
        return None
    # A canvas pick in Manufacture is a proxy through the CAM model occurrence.
    while sketch.assemblyContext:
        sketch = sketch.nativeObject
    for o in ops:
        settings = load_settings(o)
        if settings and find_by_token(design, settings.get('sketch'), adsk.fusion.Sketch.cast) == sketch:
            return o
    return None


def milling_setups(cam: adsk.cam.CAM):
    return [s for s in cam.setups if s.operationType == adsk.cam.OperationTypes.MillingOperation] if cam else []


def deburr_operations(setups):
    """All operations in the given setups that were created by this add-in."""
    ops = []
    for setup in setups:
        for item in setup.allOperations:
            op = adsk.cam.Operation.cast(item)
            if op and op.attributes.itemByName(ATTR_GROUP, ATTR_NAME):
                ops.append(op)
    return ops
