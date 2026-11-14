"""Cutting and noncutting envelopes from Fusion tool JSON, in cm.

Spherical cutters (ball and lollipop mills) and conical chamfer cutters
(chamfer mills for topside edges, single-profile thread mills such as
double-angle cutters for both sides). The Fusion adapter validates
shaft/holder interference and entry/retract motion before using these
profiles for CAM generation.
"""

from dataclasses import dataclass
import math

from . import deburr_math as dm

CHAMFER_TYPES = ('chamfer mill', 'thread mill')


@dataclass(frozen=True)
class SphericalCutter:
    radius: float
    max_contact_height: float
    # Conservative cylinders (bottom height, top height, radius) from tip.
    noncutting_sections: tuple = ()

    def tip(self, p, n1, n2, tool_axis, width):
        return dm.tool_tip(p, n1, n2, tool_axis, self.radius, width,
                           self.max_contact_height)

    def variants(self):
        return (self,)


@dataclass(frozen=True)
class ConeCutter:
    """One conical flank in contact; every flank still counts as cutting edge
    that must not remove material away from the selected edges."""
    flank: tuple                  # (z0, r0, z1, r1) of the contacting flank
    frustums: tuple               # all cutting flanks, same form
    noncutting_sections: tuple = ()
    name: str = 'flank'

    @property
    def radius(self):
        return max(max(f[1], f[3]) for f in self.frustums)

    def tip(self, p, n1, n2, tool_axis, width):
        return dm.cone_tip(p, n1, n2, tool_axis, width, self.flank)[0]

    def retreat(self, p, n1, n2, tool_axis, width):
        """(horizontal direction, distance) that frees the tool from the cut so
        it can move along its axis; used for back-side entry and exit."""
        tip, direction = dm.cone_tip(p, n1, n2, tool_axis, width, self.flank)
        z = dm.normalize(tool_axis)
        offset = dm.sub(tip, p)
        lateral = dm.sub(offset, dm.scale(z, dm.dot(offset, z)))
        reach = math.sqrt(dm.dot(lateral, lateral))
        return direction, max(0.0, self.radius - reach) + width + RETREAT_CLEARANCE


RETREAT_CLEARANCE = .02  # cm


@dataclass(frozen=True)
class ConeTool:
    """A chamfer cutter with one (topside) or two (both sides) flanks."""
    frustums: tuple
    noncutting_sections: tuple = ()

    @property
    def radius(self):
        return max(max(f[1], f[3]) for f in self.frustums)

    def variants(self):
        """Single-flank cutters, topside first."""
        names = ('topside', 'back side')
        return tuple(ConeCutter(f, self.frustums, self.noncutting_sections,
                                names[f[3] < f[1]]) for f in self.frustums)


def _scale(raw):
    scale = {'millimeters': .1, 'inches': 2.54}.get(raw.get('unit'))
    if scale is None:
        raise dm.DeburrError('unsupported tool units')
    return scale


def _positive(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v > 0


def _shaft_and_holder(raw, g, scale, start, sections):
    """Append shaft and holder cylinders; returns nothing, mutates `sections`."""
    def add_segments(profile, start, section_scale=scale):
        for segment in profile.get('segments', []):
            vals = [segment.get(k) for k in ('height', 'lower-diameter', 'upper-diameter')]
            if not all(_positive(v) for v in vals):
                raise dm.DeburrError('invalid shaft or holder segment')
            height, lower, upper = [v * section_scale for v in vals]
            sections.append((start, start + height, max(lower, upper) / 2))
            start += height

    add_segments(raw.get('shaft', {}), start)
    holder = raw.get('holder', {})
    if holder.get('segments'):
        holder_scale = {'millimeters': .1, 'inches': 2.54}.get(holder.get('unit'))
        if holder_scale is None:
            raise dm.DeburrError('unsupported holder units')
        gauge = holder.get('gaugeLength')
        assembly = g.get('assemblyGaugeLength')
        if not all(_positive(v) for v in (gauge, assembly)):
            raise dm.DeburrError('missing holder assembly gauge length')
        stickout = assembly * scale - gauge * holder_scale
        if stickout <= start:
            raise dm.DeburrError('holder begins below the tool shoulder')
        add_segments(holder, stickout, holder_scale)


def from_tool_json(raw):
    """Read only supported tool types; never infer a tool from its name."""
    scale = _scale(raw)
    g = raw.get('geometry', {})

    def length(key):
        v = g.get(key)
        if not _positive(v):
            raise dm.DeburrError('missing or invalid tool dimension: ' + key)
        return v * scale

    kind = raw.get('type')
    if kind in CHAMFER_TYPES:
        return _cone_tool(raw, g, scale, length)
    if kind not in ('ball end mill', 'lollipop mill'):
        raise dm.DeburrError('select a ball, lollipop, chamfer, or single-profile thread mill')
    radius = length('DC') / 2
    if kind == 'ball end mill':
        return SphericalCutter(radius, 0.0)
    if not math.isclose(length('RE'), radius, rel_tol=1e-6, abs_tol=1e-8):
        raise dm.DeburrError('lollipop must have a spherical head')
    neck = length('shoulder-diameter') / 2
    limit = dm.lollipop_contact_height(radius, neck,
                                      length('LCF'))
    shoulder, overall = length('shoulder-length'), length('OAL')
    neck_start = radius + math.sqrt(radius * radius - neck * neck)
    if not neck_start < shoulder < overall:
        raise dm.DeburrError('invalid lollipop shoulder or overall length')
    sections = [(neck_start, shoulder, neck)]
    if radius + limit < neck_start:
        sections.append((radius + limit, neck_start,
                         math.sqrt(radius * radius - limit * limit)))
    sections.append((shoulder, overall, length('SFDM') / 2))
    _shaft_and_holder(raw, g, scale, shoulder, sections)
    return SphericalCutter(radius, limit, tuple(sections))


def _cone_tool(raw, g, scale, length):
    """Chamfer mill: one flank widening from the tip diameter to DC at the
    taper angle TA (half the included angle). Thread mill: one pointed tooth
    of the profile angle, spanning the flute length LCF from the tip, with DC
    at its point (a double-angle cutter). Bodies above the flanks are
    noncutting; nothing narrower than the profile is assumed."""
    radius, overall = length('DC') / 2, length('OAL')
    if raw['type'] == 'chamfer mill':
        half = g.get('TA')
        if not _positive(half) or half >= 90:
            raise dm.DeburrError('chamfer mill needs a taper angle between 0 and 90 degrees')
        tip = g.get('tip-diameter', 0)
        if not (isinstance(tip, (int, float)) and math.isfinite(tip) and 0 <= tip * scale / 2 < radius):
            raise dm.DeburrError('chamfer mill tip diameter must be smaller than its diameter')
        tip *= scale / 2
        top = (radius - tip) / math.tan(math.radians(half))
        frustums = ((0.0, tip, top, radius),)
        body = max(radius, (g.get('shoulder-diameter') or 0) * scale / 2)
    else:
        angle = g.get('thread-profile-angle')
        if not _positive(angle) or angle >= 180:
            raise dm.DeburrError('thread mill needs a profile angle between 0 and 180 degrees')
        if g.get('thread-tip-type', 'point') != 'point' or g.get('NT', 1) != 1:
            raise dm.DeburrError('only pointed single-profile (one tooth) thread mills are supported')
        neck = g.get('shoulder-diameter')
        if isinstance(neck, (int, float)) and neck < 0:
            # Fusion derives the neck from the pitch on import; a pitch larger
            # than the tooth leaves a negative neck and a wrongly drawn tool.
            raise dm.DeburrError('thread mill neck diameter is negative; set its pitch '
                                 'to the cutter width and its shoulder diameter to the neck')
        top = length('LCF')
        root = radius - top / 2 * math.tan(math.radians(angle / 2))
        if root <= 0:
            raise dm.DeburrError('thread mill flute length is too long for its diameter and angle')
        frustums = ((0.0, root, top / 2, radius), (top / 2, radius, top, root))
        body = (g.get('shoulder-diameter') or 0) * scale / 2
    shoulder = max(top, (g.get('shoulder-length') or 0) * scale)
    if raw['type'] == 'thread mill':
        # Whatever sits directly on the tooth must not overhang its root.
        above = body if shoulder > top and body > 0 else length('SFDM') / 2
        if shoulder > top and body <= 0:
            body = above
        if above > root * (1 + 1e-6):
            raise dm.DeburrError('thread mill neck or shank must not be wider than the tooth root')
    if not shoulder < overall:
        raise dm.DeburrError('invalid chamfer cutter shoulder or overall length')
    sections = []
    if shoulder > top:
        sections.append((top, shoulder, body))
    sections.append((shoulder, overall, length('SFDM') / 2))
    _shaft_and_holder(raw, g, scale, shoulder, sections)
    return ConeTool(frustums, tuple(sections))
