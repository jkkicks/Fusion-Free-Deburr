"""Pure geometry for ball-endmill edge breaks. No Fusion imports, so it can be
tested outside Fusion. Vectors are (x, y, z) tuples; units are whatever the
caller uses consistently (Fusion internal units are cm).

Cross-section through a sharp convex edge at point P, with outward face
normals n1, n2 and half-angle alpha between them (cos(alpha) = |n1 + n2| / 2):
the ball center sits on the outward bisector u at distance s from P. The ball
then cuts each face back a distance w from the edge, where

    w = sqrt(R^2 - s^2 cos^2(alpha)) - s sin(alpha)

Solving for s:

    s = -w sin(alpha) + sqrt(R^2 - w^2 cos^2(alpha))
"""

import math

MIN_EDGE_ANGLE_DEG = 10.0
_EPS = 1e-9


class DeburrError(Exception):
    pass


def add(a, b): return (a[0] + b[0], a[1] + b[1], a[2] + b[2])
def sub(a, b): return (a[0] - b[0], a[1] - b[1], a[2] - b[2])
def scale(a, k): return (a[0] * k, a[1] * k, a[2] * k)
def dot(a, b): return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]
def cross(a, b): return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def normalize(a):
    length = math.sqrt(dot(a, a))
    if length < _EPS:
        raise DeburrError('zero-length vector')
    return scale(a, 1.0 / length)


def center_distance(radius, width, cos_a, sin_a):
    """Distance s from the edge to the ball center along the bisector."""
    disc = radius * radius - (width * cos_a) ** 2
    if disc < 0:
        raise DeburrError('edge break is too large for this ball')
    s = -width * sin_a + math.sqrt(disc)
    if s <= 0:
        raise DeburrError('edge break is too large for this ball')
    return s


def ball_center(p, n1, n2, radius, width):
    """Ball center that cuts an edge break of `width` at edge point p.

    Returns (center, u) where u is the unit outward bisector.
    """
    n1, n2 = normalize(n1), normalize(n2)
    c = max(-1.0, min(1.0, dot(n1, n2)))
    if math.degrees(math.acos(c)) < MIN_EDGE_ANGLE_DEG:
        raise DeburrError('faces are nearly tangent (not a sharp edge)')

    u = normalize(add(n1, n2))
    cos_a = math.sqrt((1 + c) / 2)
    sin_a = math.sqrt((1 - c) / 2)
    s = center_distance(radius, width, cos_a, sin_a)
    return add(p, scale(u, s)), u


def contact_points(p, n1, n2, u, width):
    """Where the cut meets each face: `width` back from the edge, in-face."""
    points = []
    for n in (normalize(n1), normalize(n2)):
        m = normalize(sub(u, scale(n, dot(u, n))))  # in-face, pointing off the edge
        points.append(sub(p, scale(m, width)))
    return points


def lollipop_contact_height(radius, neck_radius, flute_length):
    """Highest usable spherical contact above the center, in length units.

    The neck intersects the sphere at sqrt(R² - neck²). Flute length is
    measured from the tool tip, so it can further restrict that height.
    This is a cutting-envelope limit, not a shaft/holder clearance test.
    """
    values = (radius, neck_radius, flute_length)
    if (not all(isinstance(v, (int, float)) and math.isfinite(v) for v in values)
            or radius <= 0 or not 0 < neck_radius < radius or flute_length <= radius):
        raise DeburrError('lollipop requires a smaller positive neck and flutes above the ball center')
    return min(math.sqrt(radius * radius - neck_radius * neck_radius),
               flute_length - radius)


FLANK_MARGIN = 0.05   # fraction of the flank kept clear of the chamfer at each end


def chamfer_plane(n1, n2, tool_axis, sin_flank):
    """Outward normal k of the planar chamfer a conical flank cuts at a convex edge.

    The edge tangent is n1 x n2, so the chamfer normal lies in the plane of the
    face normals. A cone with axis z is tangent to that plane when k.z equals
    `sin_flank`: positive for a flank widening upward (topside), negative for
    one narrowing upward (back side). Returns (k, phi, psi, e1, e2): k is at
    angle phi from n1 toward n2, and psi is the angle between n1 and n2.
    """
    n1, n2 = normalize(n1), normalize(n2)
    c = max(-1.0, min(1.0, dot(n1, n2)))
    psi = math.acos(c)
    if math.degrees(psi) < MIN_EDGE_ANGLE_DEG:
        raise DeburrError('faces are nearly tangent (not a sharp edge)')
    e1, e2 = n1, normalize(sub(n2, scale(n1, c)))
    z = normalize(tool_axis)
    z1, z2 = dot(z, e1), dot(z, e2)
    rho = math.hypot(z1, z2)
    if rho <= abs(sin_flank) + 1e-9:
        raise DeburrError('edge is too steep for the cutter flank')
    base, spread = math.atan2(z2, z1), math.acos(sin_flank / rho)
    # Keep the chamfer at least MIN_EDGE_ANGLE_DEG away from both faces;
    # otherwise the flank would graze one face along a long sliver.
    margin = math.radians(MIN_EDGE_ANGLE_DEG)
    options = [a % (2 * math.pi) for a in (base - spread, base + spread)]
    options = [a for a in options if margin <= a <= psi - margin]
    if not options:
        raise DeburrError('cutter flank cannot reach this edge')
    phi = min(options, key=lambda a: abs(a - psi / 2))
    k = add(scale(e1, math.cos(phi)), scale(e2, math.sin(phi)))
    return k, phi, psi, e1, e2


def cone_tip(p, n1, n2, tool_axis, width, flank):
    """Tool tip and retreat direction for a conical flank chamfering edge point p.

    `flank` is (z0, r0, z1, r1): heights above the tool tip and radii of the
    flank's lower and upper circles. The chamfer plane is chosen so that
    neither face is cut back more than `width`; the cut is centered on the
    flank. Exact for straight edges between planes (the swept cone's envelope
    is then that plane). Returns (tip, retreat): retreat is the horizontal unit
    direction from the contact toward the tool axis, away from the material.
    """
    z0, r0, z1, r1 = flank
    length = math.hypot(z1 - z0, r1 - r0)
    if not (z1 > z0 and length > _EPS and min(r0, r1) >= 0):
        raise DeburrError('invalid cutter flank')
    z = normalize(tool_axis)
    k, phi, psi, e1, e2 = chamfer_plane(n1, n2, z, (r1 - r0) / length)
    s1, s2 = math.sin(phi), math.sin(psi - phi)
    offset = width * min(s1, s2)       # chamfer plane depth below the edge
    d1 = scale(e2, -1.0)               # along face 1, away from the edge
    d2 = add(scale(e1, -math.sin(psi)), scale(e2, math.cos(psi)))
    q1, q2 = add(p, scale(d1, offset / s1)), add(p, scale(d2, offset / s2))
    mid = scale(add(q1, q2), 0.5)

    # Flank generator in the chamfer plane, pointing up the tool.
    g = normalize(sub(z, scale(k, dot(z, k))))
    t = normalize(cross(n1, n2))
    denom = dot(cross(g, t), k)
    if abs(denom) < 1e-6:
        raise DeburrError('edge is too steep for the cutter flank')
    # Where the generator through `mid` crosses the chamfer's two boundary lines.
    lam = [dot(cross(sub(q, mid), t), k) / denom for q in (q1, q2)]
    if abs(lam[0] - lam[1]) > length * (1 - 2 * FLANK_MARGIN):
        raise DeburrError('edge break is too large for the cutter flank')
    bottom = sub(mid, scale(g, length / 2 - (lam[0] + lam[1]) / 2))
    retreat = normalize(sub(k, scale(z, dot(k, z))))
    axis_point = add(bottom, scale(retreat, r0))
    return sub(axis_point, scale(z, z0)), retreat


def tool_tip(p, n1, n2, tool_axis, radius, width, max_contact_height=0.0):
    """Tool tip position (center - R * axis) for a vertical-axis ball endmill.

    By default only the lower hemisphere cuts. A lollipop may supply its
    validated upper contact height. Clearance must be checked separately.
    """
    z = normalize(tool_axis)
    center, u = ball_center(p, n1, n2, radius, width)
    for q in contact_points(p, n1, n2, u, width):
        if dot(sub(q, center), z) > max_contact_height + 1e-6:
            raise DeburrError('edge is not reachable along the tool axis (undercut or neck contact)')
    return sub(center, scale(z, radius))
