"""Checked paths (lollipop and chamfer cutters) with explicit entry/exit and
sampled tool clearance.

Trace approaches only the distant ends of these open curves. Each curve
includes its own feed approach and withdrawal outside the model bounds.
"""

import itertools
import math

import adsk.core
import adsk.fusion

from . import deburr_math as dm
from . import deburr_chain as dc
from . import deburr_fusion as df

CLEARANCE_STEP = .01  # cm, 0.1 mm maximum motion spacing
CLEARANCE_MARGIN = .001  # cm, 0.01 mm
CUT_SAMPLE_TOL = .005  # cm, stroke tolerance when sampling a cone's cut region


def _same_body_placement(body, candidate):
    if (body.nativeObject or body) != (candidate.nativeObject or candidate):
        return False
    def transform(entity):
        context = entity.assemblyContext
        return (context.transform2.asArray() if context else
                [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1])
    if any(abs(a - b) > 1e-6 for a, b in zip(transform(body), transform(candidate))):
        return False
    a, b = body.boundingBox, candidate.boundingBox
    return (dc.dist(df._tup(a.minPoint), df._tup(b.minPoint)) < 1e-6 and
            dc.dist(df._tup(a.maxPoint), df._tup(b.maxPoint)) < 1e-6)


def setup_edges(edges, bodies):
    """Resolve CAM picks into the design context used for clearance and tokens."""
    result = []
    for edge in edges:
        if edge.body in bodies:
            result.append(edge)
            continue
        body = next((b for b in bodies if _same_body_placement(edge.body, b)), None)
        if body is None:
            raise dm.DeburrError('selected edges must belong to the setup models')
        native = edge.nativeObject or edge
        mapped = (native.createForAssemblyContext(body.assemblyContext)
                  if body.assemblyContext else native)
        if mapped is None or mapped.body != body:
            raise dm.DeburrError('could not resolve a selected edge in the setup model')
        result.append(mapped)
    return result


def setup_obstacles(setup, design):
    def expand(entities):
        result = []
        for entity in entities:
            body = adsk.fusion.BRepBody.cast(entity)
            occurrence = adsk.fusion.Occurrence.cast(entity)
            if body and body.isSolid:
                result.append(body)
            elif occurrence:
                result.extend(expand(occurrence.bRepBodies))
                result.extend(expand(occurrence.childOccurrences))
            else:
                raise dm.DeburrError('clearance checks require solid setup models and fixtures')
        return result
    all_bodies = list(design.rootComponent.bRepBodies)
    for occurrence in design.rootComponent.allOccurrences:
        all_bodies.extend(occurrence.bRepBodies)
    def canonical(body):
        # CAM wraps even root bodies in a manufacturing occurrence. Resolve
        # that proxy by native identity AND placement, never just by name.
        for candidate in all_bodies:
            if _same_body_placement(body, candidate):
                return candidate
        raise dm.DeburrError('repositioned manufacturing models are not supported for checked paths')
    bodies = [canonical(b) for b in expand(setup.models)]
    fixtures = [canonical(b) for b in expand(setup.fixtures)]
    if not bodies:
        raise dm.DeburrError('select solid bodies in the milling setup')
    for body in all_bodies:
        if body.isSolid and body not in bodies and body not in fixtures:
            fixtures.append(body)
    return bodies, fixtures


def _box_corners(body):
    box = body.boundingBox
    return list(itertools.product(*zip(df._tup(box.minPoint), df._tup(box.maxPoint))))


def _box(entity):
    box = entity.boundingBox
    return df._tup(box.minPoint), df._tup(box.maxPoint)


def _overlap(a0, a1, b, cache=None):
    """Whether box a0..a1 meets body b; `cache` keeps b's box by identity."""
    if cache is None:
        lo, hi = _box(b)
    else:
        if id(b) not in cache:
            cache[id(b)] = (b, _box(b))  # holding b keeps its id unique
        lo, hi = cache[id(b)][1]
    return all(a0[k] <= hi[k] and a1[k] >= lo[k] for k in range(3))


def _cylinder_bounds(a, b, radius, axis):
    # A cylinder has no radial extent along its axis. Using radius in all
    # three coordinates needlessly intersects holders high above the part.
    extent = [radius * math.sqrt(max(0, 1 - axis[k] * axis[k])) for k in range(3)]
    return (tuple(min(a[k], b[k]) - extent[k] for k in range(3)),
            tuple(max(a[k], b[k]) + extent[k] for k in range(3)))


class Clearance:
    def __init__(self, cutter, axis, bodies, fixtures, width, edges=()):
        self.cutter, self.axis = cutter, dm.normalize(axis)
        self.bodies, self.fixtures, self.width = list(bodies), list(fixtures), width
        self.edges = list(edges)  # selected edges: cones may cut only near them
        self.measure = adsk.core.Application.get().measureManager
        self.temp = adsk.fusion.TemporaryBRepManager.get()
        self.cache = {}
        # Fusion bounding-box reads cost about a millisecond; read each once.
        self._boxes = {}
        self._edge_boxes = None
        self._last_edge = 0

    def _ball(self, tip, margin):
        center = dm.add(tip, dm.scale(self.axis, self.cutter.radius))
        point = adsk.core.Point3D.create(*center)
        outside = adsk.fusion.PointContainment.PointOutsidePointContainment
        for body in self.bodies + self.fixtures:
            fixture = body in self.fixtures
            allowance = 0 if fixture else self.width
            if body.pointContainment(point) != outside:
                return 'ball center enters model or fixture'
            distance = self.measure.measureMinimumDistance(point, body).value
            tolerance = 0 if fixture else df.PATH_TOL
            if distance < self.cutter.radius + margin - allowance - tolerance:
                return 'ball exceeds edge-break depth or touches fixture'
        return None

    def _near_edges(self, point, limit):
        """True if `point` is within `limit` of a selected edge."""
        if self._edge_boxes is None:
            self._edge_boxes = [_box(edge) for edge in self.edges]
        p = adsk.core.Point3D.create(*point)
        count = len(self.edges)
        # Consecutive samples usually lie near the same edge: try it first.
        for i in [(self._last_edge + j) % count for j in range(count)]:
            lo, hi = self._edge_boxes[i]
            if any(point[k] < lo[k] - limit or point[k] > hi[k] + limit for k in range(3)):
                continue
            if self.measure.measureMinimumDistance(p, self.edges[i]).value <= limit:
                self._last_edge = i
                return True
        return False

    def _cone(self, tip, margin):
        """Every cutting flank may remove material only within the edge break
        of a selected edge, and must not touch fixtures at all."""
        obstacles = self.bodies + self.fixtures
        for z0, r0, z1, r1 in self.cutter.frustums:
            # Radial growth that offsets the slanted surface by at least margin.
            grow = margin * math.hypot(1, (r1 - r0) / (z1 - z0))
            a = dm.add(tip, dm.scale(self.axis, z0 - margin))
            b = dm.add(tip, dm.scale(self.axis, z1 + margin))
            lo, hi = _cylinder_bounds(a, b, max(r0, r1) + grow, self.axis)
            limit = self.width + grow + df.PATH_TOL
            for body in obstacles:
                if not _overlap(lo, hi, body, self._boxes):
                    continue
                cone = self.temp.createCylinderOrCone(adsk.core.Point3D.create(*a), r0 + grow,
                                                      adsk.core.Point3D.create(*b), r1 + grow)
                if not cone or not self.temp.booleanOperation(
                        cone, body, adsk.fusion.BooleanTypes.IntersectionBooleanType):
                    raise dm.DeburrError('could not check chamfer cutter clearance')
                if not cone.faces.count:
                    continue
                if body in self.fixtures:
                    return 'cutter touches fixture'
                # The removed region is bounded by its edges: a cone and a
                # locally convex wedge meet in a convex region, whose farthest
                # points from an edge line lie on its boundary curves.
                samples = [df._tup(v.geometry) for v in cone.vertices]
                for edge in cone.edges:
                    ev = edge.evaluator
                    ok, t0, t1 = ev.getParameterExtents()
                    ok, points = ev.getStrokes(t0, t1, CUT_SAMPLE_TOL) if ok else (False, [])
                    if not ok:
                        raise dm.DeburrError('could not check chamfer cutter clearance')
                    samples.extend(df._tup(p) for p in points)
                if any(not self._near_edges(p, limit) for p in samples):
                    return 'cutter removes material away from the selected edges'
        return None

    def point(self, tip, margin):
        key = (tip, margin)
        if key in self.cache:
            return self.cache[key]
        cutting = self._cone if hasattr(self.cutter, 'frustums') else self._ball
        self.cache[key] = cutting(tip, margin) or self._noncutting(tip, margin)
        return self.cache[key]

    def _noncutting(self, tip, margin):
        obstacles = self.bodies + self.fixtures
        for bottom, top, radius in self.cutter.noncutting_sections:
            # Cylinders bound tapered sections and their motion between samples.
            a = dm.add(tip, dm.scale(self.axis, bottom - margin))
            b = dm.add(tip, dm.scale(self.axis, top + margin))
            r = radius + margin
            lo, hi = _cylinder_bounds(a, b, r, self.axis)
            for body in obstacles:
                if not _overlap(lo, hi, body, self._boxes):
                    continue
                cylinder = self.temp.createCylinderOrCone(adsk.core.Point3D.create(*a), r,
                                                          adsk.core.Point3D.create(*b), r)
                if not cylinder or not self.temp.booleanOperation(
                        cylinder, body, adsk.fusion.BooleanTypes.IntersectionBooleanType):
                    raise dm.DeburrError('could not check shaft clearance')
                if cylinder.faces.count:
                    return 'neck, shaft, or holder intersects model or fixture'
        return None

    def path(self, points):
        def span(a, b, depth=0):
            margin = dc.dist(a, b) / 2 + CLEARANCE_MARGIN
            for p in (a, b):
                error = self.point(p, margin)
                if not error:
                    continue
                # A close but valid cut can fail the coarse swept envelope.
                # Refine it rather than rejecting small edge-break widths.
                if self.point(p, CLEARANCE_MARGIN) or depth == 10:
                    return error
                midpoint = dm.scale(dm.add(a, b), .5)
                return span(a, midpoint, depth + 1) or span(midpoint, b, depth + 1)
            return None
        for a, b in zip(points, points[1:]):
            n = max(1, math.ceil(dc.dist(a, b) / CLEARANCE_STEP))
            for k in range(n):
                error = span(dm.add(a, dm.scale(dm.sub(b, a), k / n)),
                             dm.add(a, dm.scale(dm.sub(b, a), (k + 1) / n)))
                if error:
                    return error
        return None


def _directions(edges, axis):
    candidates = []
    for edge in edges:
        p = df._tup(edge.pointOnEdge)
        for face in edge.faces:
            n = df._normal(face, p)
            horizontal = dm.sub(n, dm.scale(axis, dm.dot(n, axis)))
            if dm.dot(horizontal, horizontal) > 1e-8:
                candidates.append(dm.normalize(horizontal))
    # Also allow access from above, for top faces and open pockets.
    candidates.append(axis)
    unique = []
    for n in candidates:
        if not any(dm.dot(n, other) > .999 for other in unique):
            unique.append(n)
    return unique


def add_access(points, directions, clearance, retreats=None):
    """Move horizontally (or upward) outside every obstacle before Trace links.

    A back-side cut cannot leave sideways or straight up, so an endpoint with a
    known retreat also tries stepping toward the tool axis, then rising.
    Return an open curve even when the cutting contour is closed. Trace must
    start and finish at the exterior endpoints, rather than choose a seam.
    """
    corners = [p for body in clearance.bodies + clearance.fixtures for p in _box_corners(body)]
    envelope = max([clearance.cutter.radius] +
                   [r for _, _, r in clearance.cutter.noncutting_sections])

    def outward(point, direction, extra):
        reach = max(dm.dot(dm.sub(p, point), direction) for p in corners)
        return dm.add(point, dm.scale(direction, max(0, reach) + envelope + .2 + extra))

    def access(point, extra=0):
        for direction in directions:
            outer = outward(point, direction, extra)
            # Any shaft point above this endpoint stays outside the support
            # plane for a lateral direction or rises farther for an upward one.
            if clearance.path([outer, point]) is None:
                return [outer]
        retreat = (retreats or {}).get(point)
        if retreat:
            direction, distance = retreat
            side = dm.add(point, dm.scale(direction, distance))
            outer = outward(side, clearance.axis, extra)
            if clearance.path([outer, side, point]) is None:
                return [outer, side]
        raise dm.DeburrError('no clear approach/withdrawal; select an accessible edge')
    # Different exterior endpoints prevent Trace from treating the entire
    # approach/cut/withdrawal curve as a closed contour with a movable seam.
    return access(points[0]) + points + access(points[-1], .1)[::-1]


def build_chains(edges, cutter, width, tool_axis, bodies, fixtures=()):
    """Build and validate all selected edges; reject the operation on any failure.

    A cutter with several flanks (a double-angle chamfer cutter) uses the
    first flank that can cut each edge, topside first. Edges cut by
    different flanks are never linked into one chain."""
    if not edges or not math.isfinite(width) or width <= 0:
        raise dm.DeburrError('select edges and a positive finite edge break')
    axis = dm.normalize(tool_axis)
    edges = setup_edges(edges, bodies)
    clearance = Clearance(cutter, axis, bodies, fixtures, width, edges)
    ids = df._FaceIds()
    variants = cutter.variants() if hasattr(cutter, 'variants') else (cutter,)
    groups = [[] for _ in variants]
    for i, edge in enumerate(edges):
        errors = []
        for group, variant in zip(groups, variants):
            try:
                group.append(df.edge_segment(edge, variant.radius, width, axis, ids, variant))
                break
            except dm.DeburrError as error:
                if len(variants) == 1:
                    raise
                errors.append(f'{variant.name}: {error}')
        else:
            raise dm.DeburrError(f'edge {i + 1} cannot be cut ({"; ".join(errors)})')
    chains, notes = [], []
    directions = _directions(edges, axis)
    for segments, variant in zip(groups, variants):
        retreats = {}
        if hasattr(variant, 'retreat'):
            for seg in segments:
                for end, tip in ((seg.start, seg.tips[0]), (seg.end, seg.tips[-1])):
                    retreats[tip] = variant.retreat(end.vertex, *end.normals.values(), axis, width)
        for ordered, closed in dc.order_chains(segments):
            depth = None
            if not hasattr(variant, 'frustums'):
                depth = df._cut_depth(ordered[0].source.body, variant.radius, axis)
            chain = dc.link_chain(ordered, closed, axis, variant.radius, width, depth, variant)
            notes.extend(chain.notes)
            for points in dc.chain_polylines(chain):
                error = clearance.path(points)
                if error:
                    raise dm.DeburrError('cutting path: ' + error)
                points = add_access(points, directions, clearance, retreats)
                chains.append(dc.Chain(pieces=[dc.Piece(points)], closed=False))
    return chains, edges, notes
