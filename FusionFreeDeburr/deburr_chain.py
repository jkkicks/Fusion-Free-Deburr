"""Linking per-edge tool-tip paths into continuous chains. Pure Python (no
Fusion imports) so it can be tested outside Fusion.

Each selected edge becomes a Segment: its tool-tip points plus, at each end,
the edge vertex and the outward normals of its two faces (keyed by a face id,
so we can tell which face two neighbouring edges share).

Where two segments meet:
  - tangent joint: the paths already touch, just connect them
  - convex corner: the paths stop short of each other; roll the ball around
    the vertex by sweeping the non-shared face normal from one edge to the next
  - concave corner: the paths cross; trim both back to the crossing
"""

import math
from dataclasses import dataclass, field, replace

try:
    from . import deburr_math as dm
except ImportError:  # imported directly by the unit tests
    import deburr_math as dm

JOIN_TOL = 1e-4              # cm; endpoints closer than this are the same point
ROLL_STEP_DEG = 5.0          # angular step when rolling around a convex corner
ROLL_CHORD_TOL = 5e-4        # cm; cutter-model roll chords stay this close to the arc
ROLL_MAX_STEPS = 4096
CROSSING_SEARCH = 200        # segments searched back/forward for a crossing


@dataclass
class End:
    vertex: tuple            # edge vertex position
    normals: dict            # face id -> outward unit normal at the vertex
    tangent: tuple = None    # unit edge direction at the vertex, pointing into the edge


@dataclass
class Segment:
    tips: list               # tool-tip points along the edge, start -> end
    start: End
    end: End
    start_key: object = None  # vertex identity, for chaining
    end_key: object = None
    closed: bool = False      # a single closed edge (e.g. a full circle)
    source: object = None     # caller's data (e.g. the BRepEdge)

    def reversed(self):
        return replace(self, tips=self.tips[::-1], start=self.end, end=self.start,
                       start_key=self.end_key, end_key=self.start_key)


@dataclass
class Piece:
    points: list


@dataclass
class Chain:
    pieces: list = field(default_factory=list)
    closed: bool = False
    notes: list = field(default_factory=list)


# --- Ordering edges into chains -------------------------------------------

def order_chains(segments):
    """Groups segments into connected chains by vertex key and orients them.

    Returns [(list_of_oriented_segments, closed)]. Open chains are started from
    a dangling end so they come out whole; at branch vertices (3+ selected
    edges) the extra branches become separate chains.
    """
    chains = []
    used = [False] * len(segments)
    at_vertex = {}
    for i, seg in enumerate(segments):
        if seg.closed:
            continue
        at_vertex.setdefault(seg.start_key, []).append(i)
        at_vertex.setdefault(seg.end_key, []).append(i)

    for i, seg in enumerate(segments):
        if seg.closed:
            used[i] = True
            chains.append(([seg], True))

    def start_order():
        # Dangling ends first so open chains aren't split in the middle.
        dangling = [i for i, s in enumerate(segments) if not s.closed and
                    (len(at_vertex[s.start_key]) == 1 or len(at_vertex[s.end_key]) == 1)]
        return dangling + list(range(len(segments)))

    for i in start_order():
        if used[i]:
            continue
        used[i] = True
        first = segments[i]
        if len(at_vertex[first.start_key]) != 1 and len(at_vertex[first.end_key]) == 1:
            first = first.reversed()
        chain = [first]
        while True:
            key = chain[-1].end_key
            nxt = next((j for j in at_vertex.get(key, []) if not used[j]), None)
            if nxt is None:
                break
            used[nxt] = True
            seg = segments[nxt]
            chain.append(seg if seg.start_key == key else seg.reversed())
        closed = len(chain) > 1 and chain[-1].end_key == chain[0].start_key
        chains.append((chain, closed))
    return chains


# --- Geometry helpers ------------------------------------------------------

def dist(a, b):
    d = dm.sub(a, b)
    return math.sqrt(dm.dot(d, d))


def _plane_basis(axis):
    """Two unit vectors spanning the plane perpendicular to axis."""
    z = dm.normalize(axis)
    helper = (1.0, 0.0, 0.0) if abs(z[0]) < 0.9 else (0.0, 1.0, 0.0)
    u = dm.normalize(dm.sub(helper, dm.scale(z, dm.dot(helper, z))))
    return u, dm.cross(z, u)


def _in_plane(v, n):
    """Component of v perpendicular to unit vector n."""
    return dm.sub(v, dm.scale(n, dm.dot(v, n)))


def _rotate(h, n, angle):
    """Rotates h (perpendicular to unit axis n) by angle about n."""
    return dm.add(dm.scale(h, math.cos(angle)), dm.scale(dm.cross(n, h), math.sin(angle)))


def _seg_intersection_2d(p1, p2, q1, q2):
    """Parameters (s, t) where 2D segments p1p2 and q1q2 cross, or None."""
    r = (p2[0] - p1[0], p2[1] - p1[1])
    s_ = (q2[0] - q1[0], q2[1] - q1[1])
    denom = r[0] * s_[1] - r[1] * s_[0]
    if abs(denom) < 1e-12:
        return None
    qp = (q1[0] - p1[0], q1[1] - p1[1])
    s = (qp[0] * s_[1] - qp[1] * s_[0]) / denom
    t = (qp[0] * r[1] - qp[1] * r[0]) / denom
    if -1e-9 <= s <= 1 + 1e-9 and -1e-9 <= t <= 1 + 1e-9:
        return s, t
    return None


def find_crossing(a, b, axis):
    """Where polyline a's tail crosses polyline b's head, viewed along the tool axis.

    Returns (i, j, point): a[i]..a[i+1] crosses b[j]..b[j+1] at point (3D,
    interpolated on a). Both interpolated 3D positions must agree within JOIN_TOL.
    """
    u, v = _plane_basis(axis)
    to2d = lambda p: (dm.dot(p, u), dm.dot(p, v))
    a2, b2 = [to2d(p) for p in a], [to2d(p) for p in b]
    for i in range(len(a) - 2, max(-1, len(a) - 2 - CROSSING_SEARCH), -1):
        for j in range(0, min(len(b) - 1, CROSSING_SEARCH)):
            hit = _seg_intersection_2d(a2[i], a2[i + 1], b2[j], b2[j + 1])
            if hit:
                s, t = hit
                point = dm.add(a[i], dm.scale(dm.sub(a[i + 1], a[i]), s))
                other = dm.add(b[j], dm.scale(dm.sub(b[j + 1], b[j]), t))
                if dist(point, other) <= JOIN_TOL:
                    return i, j, point
    return None


def corner_roll(a_end: End, b_start: End, tool_axis, radius, width, max_contact_height=0.0,
                tip=None):
    """Tool-tip points rolling the ball around a convex vertex shared by two edges.

    The ball stays on the shared face while the other face's normal turns
    about the shared normal, on the side the corner points to. (Taking the
    shortest turn instead fails at a spike, where the two edges leave the
    vertex in the same direction: the shortest turn goes over the shared face.)
    `tip(p, n1, n2)` overrides the ball tool-tip model (e.g. a chamfer flank).
    """
    shared = set(a_end.normals) & set(b_start.normals)
    if len(shared) != 1:
        raise dm.DeburrError('edges at corner do not share exactly one face')
    face = shared.pop()
    n_shared = dm.normalize(a_end.normals[face])
    a_other = next(n for k, n in a_end.normals.items() if k != face)
    b_other = next(n for k, n in b_start.normals.items() if k != face)

    # Each other-face normal as: tilt from the shared normal + direction around it.
    tilt_a = math.acos(max(-1.0, min(1.0, dm.dot(a_other, n_shared))))
    tilt_b = math.acos(max(-1.0, min(1.0, dm.dot(b_other, n_shared))))
    h_a = dm.normalize(_in_plane(a_other, n_shared))
    h_b = dm.normalize(_in_plane(b_other, n_shared))
    turn = math.atan2(dm.dot(dm.cross(h_a, h_b), n_shared), dm.dot(h_a, h_b))

    if a_end.tangent and b_start.tangent:
        outward = _in_plane(dm.scale(dm.add(a_end.tangent, b_start.tangent), -1.0), n_shared)
        if dm.dot(outward, outward) > 1e-12 and dm.dot(_rotate(h_a, n_shared, turn / 2), outward) < 0:
            turn -= math.copysign(2 * math.pi, turn)

    def at(t):
        tilt = tilt_a + (tilt_b - tilt_a) * t
        h = _rotate(h_a, n_shared, turn * t)
        n_other = dm.add(dm.scale(n_shared, math.cos(tilt)), dm.scale(h, math.sin(tilt)))
        if tip is not None:
            return tip(a_end.vertex, n_shared, n_other)
        return dm.tool_tip(a_end.vertex, n_shared, n_other, tool_axis, radius, width,
                           max_contact_height)

    steps = max(2, math.ceil(math.degrees(abs(turn)) / ROLL_STEP_DEG))
    points = [at(k / steps) for k in range(steps + 1)]
    # A large cutter rolls on a wide arc whose chords can cut inside it. Cutter
    # models refine until every chord midpoint is on the arc within tolerance.
    while tip is not None:
        mids = [at((k + .5) / steps) for k in range(steps)]
        if all(dist(m, dm.scale(dm.add(a, b), .5)) <= ROLL_CHORD_TOL
               for m, a, b in zip(mids, points, points[1:])):
            break
        if steps >= ROLL_MAX_STEPS:
            raise dm.DeburrError('corner roll tolerance could not be reached')
        points = [p for pair in zip(points, mids) for p in pair] + points[-1:]
        steps *= 2
    return points


def vertex_crossing(a_tip, b_tip, vertex, tool_axis, radius):
    """Tool-tip points carrying the ball over the vertex from one edge's end
    position to the next edge's start, keeping the ball center at a steady
    distance from the vertex (the short way round). Used where rolling around
    the outside isn't possible or would cut into material beyond the corner."""
    z = dm.normalize(tool_axis)
    c_a = dm.add(a_tip, dm.scale(z, radius))
    c_b = dm.add(b_tip, dm.scale(z, radius))
    d_a, d_b = dist(c_a, vertex), dist(c_b, vertex)
    u_a = dm.normalize(dm.sub(c_a, vertex))
    u_b = dm.normalize(dm.sub(c_b, vertex))
    angle = math.acos(max(-1.0, min(1.0, dm.dot(u_a, u_b))))
    if angle > math.pi - 1e-6:
        raise dm.DeburrError('no defined way over the vertex')
    steps = max(2, math.ceil(math.degrees(angle) / ROLL_STEP_DEG))
    points = []
    for k in range(steps + 1):
        t = k / steps
        if angle < 1e-9:
            u = u_a
        else:
            u = dm.normalize(dm.add(dm.scale(u_a, math.sin((1 - t) * angle)), dm.scale(u_b, math.sin(t * angle))))
        center = dm.add(vertex, dm.scale(u, d_a + (d_b - d_a) * t))
        points.append(dm.sub(center, dm.scale(z, radius)))
    return points


# --- Linking -----------------------------------------------------------------

def _clean(points):
    out = []
    for p in points:
        if not out or dist(p, out[-1]) > JOIN_TOL:
            out.append(p)
    return out


def _corner(k, a_end, b_start, a_tip, b_tip, tool_axis, radius, width, depth, notes,
            cutter=None):
    """Connector points for a convex corner. Candidates: rolling around the
    outside, and crossing over the vertex. With `depth` (max cut depth of a
    tool-tip path into the real part), the shallower one wins; otherwise the
    roll is preferred. Adds a note if even the best one cuts deeper than the
    edges themselves do at this corner; unsafe candidates split the paths."""
    candidates, errors = [], []
    makers = ((lambda: corner_roll(a_end, b_start, tool_axis, radius, width)),
              (lambda: vertex_crossing(a_tip, b_tip, a_end.vertex, tool_axis, radius)))
    if cutter is not None:
        # The vertex crossing models neither a lollipop's finite cutting cap
        # nor a conical flank; roll with the cutter's own contact model.
        tip = lambda p, n1, n2: cutter.tip(p, n1, n2, tool_axis, width)
        makers = (lambda: corner_roll(a_end, b_start, tool_axis, radius, width, tip=tip),)
    for make in makers:
        try:
            points = make()
            points[0], points[-1] = a_tip, b_tip
            candidates.append(points)
        except dm.DeburrError as e:
            errors.append(str(e))
        if candidates and depth is None:
            break
    if not candidates:
        notes.append(f'corner {k + 1}: {errors[0]}; paths split')
        return None
    if depth is None:
        return candidates[0]
    try:
        scored = sorted((depth(c), i, c) for i, c in enumerate(candidates))
        best_depth, _, best = scored[0]
        edge_depth = max(depth([a_tip]), depth([b_tip]))
    except Exception:
        notes.append(f'corner {k + 1}: clearance check failed; paths split')
        return None
    excess = best_depth - edge_depth
    if excess > max(0.1 * width, 1e-3):
        notes.append(f'corner {k + 1}: the ball cuts about {excess * 10:.3f} mm deeper than '
                     f'the edge break here; paths split')
        return None
    return best


def link_chain(segments, closed, tool_axis, radius, width, depth=None, cutter=None):
    """Turns an ordered chain of segments into connected pieces (end of each
    piece == start of the next; for closed chains the last connects to the first).

    depth: optional function(tool_tips) -> deepest cut into the actual part,
    used to pick the safest way around each convex corner."""
    tips = [list(s.tips) for s in segments]
    chain = Chain(closed=closed)
    connectors = [None] * len(segments)   # connector[k]: points between seg k and k+1

    joints = range(len(segments)) if closed else range(len(segments) - 1)
    for k in joints:
        n = (k + 1) % len(segments)
        if len(segments) == 1:          # single closed edge: nothing to join
            break
        a, b = tips[k], tips[n]
        if dist(a[-1], b[0]) <= JOIN_TOL:
            b[0] = a[-1]
            continue
        crossing = find_crossing(a, b, tool_axis)
        if crossing:
            i, j, point = crossing
            tips[k] = a[:i + 1] + [point]
            tips[n] = [point] + b[j + 1:]
            continue
        connectors[k] = _corner(k, segments[k].end, segments[n].start, a[-1], b[0],
                                tool_axis, radius, width, depth, chain.notes, cutter)

    for k, seg in enumerate(segments):
        points = _clean(tips[k])
        if len(points) >= 2:
            chain.pieces.append(Piece(points))
        if connectors[k]:
            points = _clean(connectors[k])
            if len(points) >= 2:
                chain.pieces.append(Piece(points))
    return chain


# --- Output as one polyline per chain ----------------------------------------

def chain_polylines(chain):
    """Separate contours at every unconnected joint; never bridge a failed corner."""
    paths = []
    for piece in chain.pieces:
        points = _clean(piece.points)
        if len(points) < 2:
            continue
        if paths and dist(paths[-1][-1], points[0]) <= JOIN_TOL:
            paths[-1].extend(points[1:])
        else:
            paths.append(points)
    if chain.closed and len(paths) > 1 and dist(paths[-1][-1], paths[0][0]) <= JOIN_TOL:
        paths[0] = paths.pop() + paths[0][1:]
    return paths


def chain_points(chain):
    """Return one connected polyline; reject disconnected chains."""
    paths = chain_polylines(chain)
    if len(paths) > 1:
        raise dm.DeburrError('chain contains split paths')
    return paths[0] if paths else []


def polyline_knots(points):
    """Clamped knot vector for a degree-1 NURBS through `points`, parameterised
    by arc length (so the curve is exactly the polyline)."""
    knots = [0.0, 0.0]
    t = 0.0
    for a, b in zip(points, points[1:]):
        t += dist(a, b)
        knots.append(t)
    knots.append(t)
    return knots
