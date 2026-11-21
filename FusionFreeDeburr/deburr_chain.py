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
TRIM_TOL = 1e-4              # cm; trimmed path ends lie within this of the clear limit


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


# Points sampled along an edge for its saved shape, both ends included. Start,
# middle and end alone cannot tell apart two same-radius circles in
# perpendicular planes that share those points; the points between do.
EDGE_SHAPE_POINTS = 9


def same_edge_shape(a, b, tol):
    """Whether two edge shapes [length, point, ...] describe the same edge, in
    either direction. A shape with any other number of points never matches."""
    size = 1 + EDGE_SHAPE_POINTS
    if not a or not b or len(a) != size or len(b) != size:
        return False
    if abs(a[0] - b[0]) > tol:
        return False
    return any(all(dist(p, q) <= tol for p, q in zip(a[1:], points))
               for points in (b[1:], b[:0:-1]))


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
    if excess > excess_limit(width):
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


# --- Trimming where the ball cuts too far ------------------------------------

def polyline_distance(point, polyline):
    """Distance from `point` to the nearest point of `polyline`, ends included."""
    best = math.inf
    for a, b in zip(polyline, polyline[1:]):
        best = min(best, _segment_distance(point, a, b))
    return best if len(polyline) > 1 else dist(point, polyline[0])


def _segment_distance(point, a, b):
    ab = dm.sub(b, a)
    length2 = dm.dot(ab, ab)
    t = 0.0 if length2 < 1e-24 else max(0.0, min(1.0, dm.dot(dm.sub(point, a), ab) / length2))
    return dist(point, dm.add(a, dm.scale(ab, t)))


def box_gap(point, lo, hi):
    return math.sqrt(sum(max(lo[k] - point[k], 0, point[k] - hi[k]) ** 2 for k in range(3)))


def polyline_segments(polyline):
    """[(a, b, lo, hi)] for each segment of `polyline`, with its bounding box."""
    return [(a, b, tuple(min(a[k], b[k]) for k in range(3)), tuple(max(a[k], b[k]) for k in range(3)))
            for a, b in zip(polyline, polyline[1:])]


def segments_within(segments, center, reach):
    """The segments that come within `reach` of `center`."""
    return [s for s in segments if box_gap(center, s[2], s[3]) <= reach]


def segments_near_box(segments, lo, hi, reach):
    """The segments whose boxes come within `reach` of the box lo..hi."""
    return [s for s in segments if all(s[2][k] <= hi[k] + reach and s[3][k] >= lo[k] - reach
                                       for k in range(3))]


class SegmentIndex:
    """Segments filed in a grid of `cell`-sized cubes by their boxes, so a
    search near one point reads only the cells around it."""

    def __init__(self, segments, cell):
        self.cell, self.cells = cell, {}
        for s in segments:
            # File the segment under the cells it passes through: the boxes of
            # pieces no longer than a cell. Its own box would fill a solid
            # block of cells for a long diagonal edge.
            a, b = s[0], s[1]
            n = max(1, math.ceil(dist(a, b) / cell))
            keys = set()
            for k in range(n):
                p = dm.add(a, dm.scale(dm.sub(b, a), k / n))
                q = dm.add(a, dm.scale(dm.sub(b, a), (k + 1) / n))
                keys.update(self._keys([min(p[i], q[i]) for i in range(3)],
                                       [max(p[i], q[i]) for i in range(3)]))
            for key in keys:
                self.cells.setdefault(key, []).append(s)

    def _keys(self, lo, hi):
        first = [math.floor(c / self.cell) for c in lo]
        last = [math.floor(c / self.cell) for c in hi]
        return [(i, j, k) for i in range(first[0], last[0] + 1)
                for j in range(first[1], last[1] + 1) for k in range(first[2], last[2] + 1)]

    def near_box(self, lo, hi, reach):
        """The segments filed in cells within `reach` of the box lo..hi whose
        boxes come within `reach` of it. A segment is filed only under the
        cells it passes through, so every segment that truly comes within
        `reach` is found; one whose box does but whose line does not may
        be left out, which only makes a check stricter."""
        found = {}
        for key in self._keys([c - reach for c in lo], [c + reach for c in hi]):
            for s in self.cells.get(key, ()):
                found[id(s)] = s
        return segments_near_box(found.values(), lo, hi, reach)

    def within(self, center, reach):
        """The segments that come within `reach` of `center`."""
        return segments_within(self.near_box(center, center, reach), center, reach)

    def near_path(self, a, b, reach):
        """The segments whose boxes come within `reach` of the straight path
        a-b, read along the path: a long slanted path's box holds far more
        cells than the path comes near."""
        n = max(1, math.ceil(dist(a, b) / self.cell))
        keys = set()
        for k in range(n):
            p = dm.add(a, dm.scale(dm.sub(b, a), k / n))
            q = dm.add(a, dm.scale(dm.sub(b, a), (k + 1) / n))
            keys.update(self._keys([min(p[i], q[i]) - reach for i in range(3)],
                                   [max(p[i], q[i]) + reach for i in range(3)]))
        found = {}
        for key in keys:
            for s in self.cells.get(key, ()):
                found[id(s)] = s
        lo = [min(a[i], b[i]) for i in range(3)]
        hi = [max(a[i], b[i]) for i in range(3)]
        return segments_near_box(found.values(), lo, hi, reach)


_NEIGHBOURS = [(i, j, k) for i in (-1, 0, 1) for j in (-1, 0, 1) for k in (-1, 0, 1)]


def inward_points(nodes, normals, samples, depth, steps=(1, 2, 3, 4, 6, 8)):
    """Candidate points deep inside a solid sampled on its surface: from one
    surface node per `depth`-sized cell, step `depth` times each of `steps`
    against its outward normal, and keep the points whose cell and its 26
    neighbours hold none of `samples` (so they lie more than `depth`, and up
    to about twice that, from every sample).

    A point deep inside can be farther from something (selected edges) than
    every surface sample. In a solid thinner than about twice `depth`, every
    step lands near the opposite surface or outside, so nothing is kept. The
    caller must still test that a kept point is inside the solid."""
    def cell(p):
        return (math.floor(p[0] / depth), math.floor(p[1] / depth), math.floor(p[2] / depth))
    occupied = {cell(p) for p in samples}
    seen, found = set(), []
    for node, normal in zip(nodes, normals):
        key = cell(node)
        if key in seen:
            continue
        seen.add(key)
        for step in steps:
            p = (node[0] - normal[0] * depth * step, node[1] - normal[1] * depth * step,
                 node[2] - normal[2] * depth * step)
            c = cell(p)
            if not any((c[0] + i, c[1] + j, c[2] + k) in occupied for i, j, k in _NEIGHBOURS):
                found.append(p)
    return found


def cut_back(points, length):
    """`points` with the last `length` of the path removed, or None if the
    path is not longer than that."""
    remaining = length
    pts = list(points)
    while len(pts) >= 2:
        step = dist(pts[-2], pts[-1])
        if step > remaining:
            a, b = pts[-2], pts[-1]
            pts[-1] = dm.add(b, dm.scale(dm.sub(a, b), remaining / step))
            return pts
        remaining -= step
        pts.pop()
    return None


def open_loop(points, overlap):
    """A closed polyline continued past its start by `overlap`, so its two
    ends differ. Trace may move the seam of a closed contour to any point;
    an open one starts and ends where it is drawn."""
    if len(points) < 3 or dist(points[0], points[-1]) > JOIN_TOL:
        return points
    # A loop shorter than twice the overlap still gets two distinct ends.
    overlap = min(overlap, sum(dist(a, b) for a, b in zip(points, points[1:])) / 2)
    extra, travelled = [], 0.0
    for a, b in zip(points, points[1:]):
        step = dist(a, b)
        if travelled + step >= overlap:
            extra.append(dm.add(a, dm.scale(dm.sub(b, a), (overlap - travelled) / step)))
            break
        extra.append(b)
        travelled += step
    return points + extra


def farthest(samples, segments, cap, enough=None):
    """The largest distance from any of `samples` to its nearest segment,
    with each distance capped at `cap` (and `cap` when there are no segments).

    With `enough`, only whether the result exceeds `enough` matters: the
    search stops at the first sample beyond it (returning that sample's
    distance) and may overstate results that stay within it.

    Samples are visited cell by cell, so neighbours are usually nearest the
    same segment. The distance to that segment bounds the sample's nearest
    distance; only a sample it does not settle is measured to every segment."""
    if not samples:
        return 0.0
    if not segments:
        return cap
    size = max(cap / 8, 1e-4)
    samples = sorted(samples, key=lambda p: tuple(math.floor(c / size) for c in p))
    best, last = -math.inf, 0
    for point in samples:
        bound = best if enough is None else max(best, enough)
        a, b, _, _ = segments[last]
        d = _segment_distance(point, a, b)
        if d <= bound:
            if enough is not None:
                best = max(best, d)
            continue
        for i, (a, b, _, _) in enumerate(segments):
            other = _segment_distance(point, a, b)
            if other < d:
                d, last = other, i
        best = max(best, min(cap, d))
        if enough is not None and best > enough:
            return best
    return best


def excess_limit(width):
    """How much farther than the edge break the ball may cut (cm)."""
    return max(0.1 * width, 1e-3)


def checked_spans(points, excess, swept, limit, step, shortest=TRIM_TOL):
    """The parts of tool-tip polyline `points` the ball can follow.

    excess(tip): how far beyond the edge break the ball cuts with its tip at
    that point. swept(a, b): True only if every tip on the straight move a-b
    cuts within `limit` (a conservative check of the whole move). The path is
    split into moves at most `step` long; each move that `swept` rejects is
    halved until its halves pass or both its ends are blocked. A move no
    longer than `shortest` that `swept` still rejects is clear if both its
    ends pass and blocked otherwise, so trims land within `shortest` of the
    limit and only a failed point check counts as a gouge.

    Returns (spans, removed): the clear polylines in path order, keeping the
    path's own points and adding trim points, and one (length, deepest
    excess) per removed stretch. A trim point within JOIN_TOL of a path point
    gives way to the path point, which lies on the checked side."""
    samples = []                        # (point, distance along the path, is a path point)
    position = 0.0
    for a, b in zip(points, points[1:]):
        n = max(1, math.ceil(dist(a, b) / step))
        for k in range(n):
            samples.append((dm.add(a, dm.scale(dm.sub(b, a), k / n)),
                            position + dist(a, b) * k / n, k == 0))
        position += dist(a, b)
    samples.append((points[-1], position, True))

    known = {}

    def at(p):
        if p not in known:
            known[p] = excess(p)
        return known[p]

    pieces = []                         # (a, b, length, clear, deepest)

    def classify(a, b, length):
        if a in known and known[a] > limit and at(b) > limit:
            # Inside a blocked stretch: two blocked ends settle the move.
            pieces.append((a, b, length, False, max(known[a], known[b])))
            return
        if swept(a, b):
            pieces.append((a, b, length, True, 0.0))
            return
        ea, eb = at(a), at(b)
        if (ea > limit and eb > limit) or length <= shortest * (1 + 1e-9):
            clear = ea <= limit and eb <= limit
            pieces.append((a, b, length, clear, 0.0 if clear else max(ea, eb)))
            return
        middle = dm.scale(dm.add(a, b), .5)
        classify(a, middle, length / 2)
        classify(middle, b, length / 2)

    spans, removed, span, gap = [], [], None, None
    for (a, pa, _), (b, pb, vertex) in zip(samples, samples[1:]):
        first = len(pieces)
        classify(a, b, pb - pa)
        for k in range(first, len(pieces)):
            p, q, length, clear, deepest = pieces[k]
            last = k == len(pieces) - 1
            if clear:
                if gap is not None:
                    removed.append(gap)
                    gap = None
                if span is None:
                    span = [p]
                if last and vertex:     # moves between path points are straight
                    span.append(q)
            else:
                if span is not None:
                    if span[-1] != p:
                        span.append(p)
                    spans.append(span)
                    span = None
                gap = (gap[0] + length, max(gap[1], deepest)) if gap else (length, deepest)
    if span is not None:
        if span[-1] != points[-1]:
            span.append(points[-1])
        spans.append(span)
    if gap is not None:
        removed.append(gap)
    for span in spans:
        # A trim point next to a path point: keep the path point, so clean-up
        # never swaps two checked moves for an unchecked shortcut.
        if len(span) > 2 and dist(span[0], span[1]) <= JOIN_TOL:
            del span[0]
        if len(span) > 2 and dist(span[-1], span[-2]) <= JOIN_TOL:
            del span[-1]
    spans = [s for s in (_clean(s) for s in spans) if len(s) >= 2]
    return spans, removed


def shorten_ends(points, excess, limit, step, shortest=TRIM_TOL):
    """Move the ends of polyline `points` inward until excess(end) <= limit.

    excess(point) judges an end on its own (for example the plunge straight
    down onto it). From each end, points every `step` along the path are
    tried until one passes; the boundary is then bisected to within
    `shortest`. Points between the new end and the far end are not
    rechecked: only the ends matter.

    Returns (points or None, removed): the shortened polyline (None when no
    point passes, or nothing usable is left) and one (length, deepest excess)
    per shortened end."""
    removed = []

    def from_start(pts):
        first = excess(pts[0])
        if first <= limit:
            return pts
        deepest, travelled = first, 0.0
        for i, (a, b) in enumerate(zip(pts, pts[1:])):
            length = dist(a, b)
            n = max(1, math.ceil(length / step))
            bad = a
            for k in range(1, n + 1):
                p = b if k == n else dm.add(a, dm.scale(dm.sub(b, a), k / n))
                value = excess(p)
                if value > limit:
                    deepest, bad = max(deepest, value), p
                    continue
                good = p
                while dist(bad, good) > shortest:
                    middle = dm.scale(dm.add(bad, good), .5)
                    value = excess(middle)
                    if value > limit:
                        deepest, bad = max(deepest, value), middle
                    else:
                        good = middle
                rest = pts[i + 1:]              # starts with b
                if rest[0] != good and dist(good, rest[0]) <= JOIN_TOL and excess(rest[0]) <= limit:
                    # Like checked_spans: a new end next to a path point
                    # gives way to the path point, so clean-up keeps it.
                    good = rest[0]
                removed.append((travelled + dist(a, good), deepest))
                return [good] + (rest[1:] if rest[0] == good else rest)
            travelled += length
        removed.append((travelled, deepest))
        return None

    def settled(pts):
        # Clean up right away, while the checked end comes first: clean-up
        # keeps the first of two points closer than JOIN_TOL, so the end
        # that was checked is the one exported.
        if pts is None:
            return None
        pts = _clean(pts)
        return pts if len(pts) >= 2 else None

    pts = settled(from_start(list(points)))
    if pts is not None:
        pts = settled(from_start(pts[::-1]))
        pts = pts[::-1] if pts is not None else None
    return pts, removed


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
