"""Fusion-side glue: sample B-rep edges, link them into continuous tool-tip
chains, draw those in a sketch, and create a Trace operation driven by them.
All lengths are in cm."""

import json
import math

import adsk.core
import adsk.fusion
import adsk.cam

from . import deburr_chain as dc
from . import deburr_math as dm

SAMPLE_STEP = 0.05          # max edge/clearance sample spacing (0.5 mm)
PATH_TOL = 0.001           # tool-tip chord tolerance (0.01 mm)
CONVEXITY_PROBE = 0.005     # lateral probe distance for convexity
VERTEX_TOL = 1e-4           # cm; vertices this close are treated as the same
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


def _tup(v):
    return (v.x, v.y, v.z)


def _is_line_between_planes(edge: adsk.fusion.BRepEdge) -> bool:
    if edge.geometry.curveType != adsk.core.Curve3DTypes.Line3DCurveType:
        return False
    plane = adsk.core.SurfaceTypes.PlaneSurfaceType
    return all(f.geometry.surfaceType == plane for f in edge.faces)


def _is_closed(edge: adsk.fusion.BRepEdge) -> bool:
    return edge.startVertex is None or edge.startVertex == edge.endVertex


def _vertex_key(vertex: adsk.fusion.BRepVertex):
    return tuple(round(c / VERTEX_TOL) for c in _tup(vertex.geometry))


def _sample_edge(edge: adsk.fusion.BRepEdge, straight: bool):
    ev = edge.evaluator
    _, t0, t1 = ev.getParameterExtents()
    if straight:
        return [_tup(ev.getPointAtParameter(t)[1]) for t in (t0, t1)]
    ok, points = ev.getStrokes(t0, t1, 0.001)  # 0.01 mm chord tolerance
    if not ok or len(points) < 2:
        raise dm.DeburrError('could not sample edge')
    return [_tup(p) for p in points]


def _refine_samples(edge, samples, radius, width, tool_axis, cutter=None):
    """Split long spans or spans whose tool-tip midpoint misses the chord."""
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
    parameters = [t0]
    for p in samples[1:-1]:
        ok, t = ev.getParameterAtPoint(adsk.core.Point3D.create(*p))
        if not ok:
            raise dm.DeburrError('could not evaluate sample parameters')
        parameters.append(t)
    parameters.append(t1)  # at a closed seam, point lookup could return t0 instead
    points = []
    for a, b, pa, pb in zip(samples, samples[1:], parameters, parameters[1:]):
        points.extend(split(a, b, pa, pb, tip(a), tip(b), 0))
    return points + samples[-1:]


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
    if edge.faces.count != 2:
        raise dm.DeburrError('edge does not have exactly two faces')
    f1, f2 = edge.faces.item(0), edge.faces.item(1)
    id1, id2 = face_ids(f1), face_ids(f2)

    straight = _is_line_between_planes(edge)
    closed = _is_closed(edge)
    samples = _sample_edge(edge, straight)
    if closed:
        samples = dc._clean(samples)
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


def build_chains(edges, radius, width, tool_axis):
    """Returns (chains, used_edges, warnings). Connected edges are linked into
    continuous chains (see deburr_chain)."""
    face_ids = _FaceIds()
    segments, warnings = [], []
    for i, edge in enumerate(edges):
        try:
            segments.append(edge_segment(edge, radius, width, tool_axis, face_ids))
        except dm.DeburrError as e:
            warnings.append(f'Edge {i + 1} skipped: {e}')

    chains = []
    for ordered, closed in dc.order_chains(segments):
        depth = _cut_depth(ordered[0].source.body, radius, tool_axis)
        chain = dc.link_chain(ordered, closed, tool_axis, radius, width, depth)
        warnings += chain.notes
        if chain.pieces:
            chains.append(chain)
    return chains, [s.source for s in segments], warnings


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


def _trace_settings(op):
    """Paths already describe the tool tip: disable Trace displacement."""
    for name, expression in TRACE_EXPRESSIONS:
        param = op.parameters.itemByName(name)
        if not param and name in ('compensation', 'axialOffset'):
            raise dm.DeburrError(f'Trace is missing required safety parameter: {name}')
        if param:
            param.expression = expression


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


def update_operation(design, op, chains, edges, width, tool=None):
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
        update_trace_curves(op, curves)
        if tool is not None:
            op.tool = tool
        if old_sketch:
            old_sketch.name = old_name + ' (old)'
        sketch.name = op.name
        save_settings(op, edges, width, sketch)
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

def save_settings(op: adsk.cam.Operation, edges, width, sketch):
    data = {
        'version': 1,
        'edges': [e.entityToken for e in edges],
        'width': width,
        'sketch': sketch.entityToken,
    }
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
