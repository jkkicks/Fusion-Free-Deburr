# Geometry, safety, and validation notes

Ball endmills, spherical lollipop mills, and conical chamfer cutters (chamfer
mills and pointed single-profile thread mills) are supported, including when
editing an existing operation. All require a positive finite diameter. Lollipop profiles also
require valid sphere radius, neck, flute, shoulder, and shaft dimensions. Trace compensation is centered, controller compensation is disabled
when exposed, and axial/radial offsets are zeroed. Missing required Trace settings stop the
operation update rather than relying on user defaults.

Convexity uses lateral body-containment probes at sampled edge points; ambiguous or
obstructed edges are skipped. Curves use Fusion's adaptive strokes with a 0.01 mm chord
tolerance. Non-planar edges also split spans longer than 0.5 mm or whose computed
tool-tip midpoint differs from the chord midpoint by more than 0.01 mm. Straight
edges between planes keep just their endpoints. Refinement stops after 12 levels;
edges that still miss tolerance, and closed edges with too few distinct samples,
are rejected. Midpoint checks are not a continuous error guarantee for surfaces
varying sharply between samples. Very small features near the probe size (0.05 mm,
retried at 0.005 mm for ambiguous containment) may be rejected.

Crossings must coincide in 3D within 0.001 mm. Failed corner construction or clearance
checks and connectors cutting more than the allowed excess depth are split into separate
Trace contours. For ball endmills, corner ball-clearance checks sample straight spans at intervals no larger
than 0.5 mm or a quarter ball radius. They inspect every body with a selected edge and do not prove
continuous clearance or check shank, holder or fixtures. Simulate generated paths and
inspect retracts before machining.

Each linked ball path is then checked along its whole length and cut back where
the ball removes too much (`checked_spans`). The rule is the one the chamfer
check uses: every bit of material the ball removes must lie within the break
width, plus a limit, of a selected edge. Material within the break of any
selected edge counts, so a ball may reach into the opposite lip of a slot as
long as it stays within that lip's own break. At each tip the ball is
intersected with every body that has a selected edge, including edges that
were skipped (a path on one body can run into another, and edges of touching
bodies can link into one chain), and
the removed material is sampled at its
vertices, its edge strokes (0.01 mm) and the nodes of a mesh with sides up to
a quarter of the width. The vertices and edge strokes are measured first, and
a cut they already fail is not meshed, so a ball far too big for a feature is
rejected quickly. A region that would still need more than 200,000 triangles
at that resolution skips its chain with a warning rather than being checked
more coarsely. The farthest sample from the selected edges, minus the width,
is the excess; a ball whose center would lie inside the part reports its
center's own distance, or no depth when that is within the limit. The limit is `excess_limit` (0.1 times the width, at
least 0.01 mm, the same as the corner check) plus the 0.01 mm path tolerance.
The path is split into moves no longer than the limit. A move passes when a
ball grown by half its length, centred at its middle, which contains every
ball along the move, removes nothing beyond the limit. A move that fails is
halved until its halves pass or both of its ends fail. A move an eighth of the
limit long that still fails is clear if both its ends pass, and removed
otherwise, so trims land within an eighth of the limit and only a failed point
check counts as a gouge; a blocked stretch in the middle splits the contour.
Each clear stretch is its own contour so trims are never bridged, and a trim
point next to a path point gives way to the path point. Points between mesh
nodes are not measured: a point inside a mesh triangle can lie up to about 0.15
times the width farther from the selected edges than the nearest node (more
for breaks under 0.04 mm, where the mesh side is held at 0.01 mm), so a face
of the removed material can pass while up to that much past the limit.
Distance to one straight edge is largest at a triangle corner, which is
sampled; the gap matters where the material lies between two selected edges,
such as a slot floor with both lips selected. The plunge check uses the same
sampling. A clear window shorter than one move inside a blocked stretch is
dropped. Like the chamfer
check, the surface samples miss a point deep inside the removed material that
is farther from the selected edges than its surface, which can happen only
when selected edges surround the material, for example a ball swallowing a
small feature whose edges are all selected. A ball whose center would lie
inside the part fails outright, and points inside the material are sampled
too: from the part's own faces (not the ball's or the plunge column's),
steps of a quarter, half, one and two widths along the inward mesh normal
that land inside the tool, in no grid cell (a quarter width across) next to
one holding a surface sample, and inside the material by a containment test
(`inward_points`). In practice the first counted point is about half a width
deep, so the core of a swallowed feature is sampled once it is about one width
thick; thinner than that, its core can be cut by up to about a quarter of a
width beyond the limit unseen. A move no longer than an eighth of the
limit whose ends pass can still differ from its end balls by at most a
sixteenth of the limit (a few micrometres for a 0.25 mm break, about
0.06 mm for a 10 mm break).

Trace reaches and leaves every contour straight along the tool axis (full
retracts). That sweeps the ball at the end plus a cylinder of the ball's
radius from its center up past the top of the part, which can reach farther
than the ball wherever material lies entirely above the ball's center, such
as an overhang. Each contour end is checked with the same rule on that
cylinder (`_column_excess`) and moved inward (`shorten_ends`), 0.5 mm at a time and then by bisection, until it
passes. The column has the ball's radius: a shank wider than the ball is not
checked, as elsewhere for ball endmills.
Trace moves the seam of a closed contour (in Fusion it entered a hole-rim loop
about 48 degrees from the sketch's start), so a closed ball loop is cut
0.1 mm past its start (`open_loop`) and exported open; Trace then entered and
left at its two checked ends. If trimming for the plunge brings the two ends
back together, the end is cut back 0.1 mm at a time (up to three times) so
the contour stays open; a path too short for that is dropped. Each end is
cleaned up as soon as it is moved, so the end that was checked is the one
exported, and both exported ends are checked once more before export. The plunge check reads the selected edges along
the column's axis, so a long slanted column does not read every grid cell of
its box. For walls and bosses that reach the
ball center's height, clearing the ball already clears the cylinder.

Selected edges are filed in a grid (`SegmentIndex`) so each check reads only
the segments near the ball; each segment is filed under the cells it passes
through, not its whole box. An edge that cannot be sampled skips its own chain
with a warning and, because it allows no material, only makes other checks
stricter.
Fusion's `measureMinimumDistance` to a `BRepEdge` measures to the unbounded
curve (a straight edge reads as an infinite line), so distances to the
selected edges use the edges' strokes at 0.001 mm. The chamfer check's
distance to the selected edges had the same problem and now uses the same
strokes.

With **Gouge checks > Skip** on, ball paths skip the corner excess-depth check and
the trimming above. Lollipop and chamfer cutting paths skip the ball-depth and
"cut away from the selected edges" checks against setup models; fixtures and the
neck, shaft and holder sections are still checked. Their approach, withdrawal
and back-side retreat moves keep every check, so a cutting path that gouges at
its ends can still be rejected for having no clear approach. The setting is
saved as an optional `skip_gouge_checks` key, written only when it is on, so
operations saved without it load with the checks on.

Lollipop contact is limited by both the neck's intersection with the spherical head
and the defined flute length. Corner rolls use the same contact limit; the generic
vertex-crossing fallback is disabled for lollipops because it does not model that limit.
Lollipop paths additionally check all selected solid models, fixtures, and other solid
design bodies. Noncutting neck, shaft, holder, and unfluted cap sections are bounded
by cylinders; tapered sections use the larger radius and may reject otherwise possible
cuts. Clearance is evaluated along linear spans at an initial spacing of at most 0.1 mm,
with inflated envelopes and finer subdivision near a clearance limit. Fixture checks
retain the clearance margin without allowing the cutting chord tolerance.

Each lollipop contour includes explicit approach and withdrawal outside the bounding
support plane of all obstacles. Its exterior endpoints differ even for a closed cutting
rim, so Trace cannot choose an interior seam. Leads, smoothing, depth ordering, and
Trace offsets are disabled; full retracts and selection order are preserved. These
checks use the finished solid model and the tool assembly as defined in the library.
They do not simulate remaining stock or unmodeled workholding. Repositioned manufacturing
models and non-solid setup selections are rejected.

Chamfer cutters are conical flanks of revolution. A `chamfer mill` has one
flank from its tip diameter to its diameter at the taper angle (half the
included angle). Flutes above the cone at full diameter are a noncutting body.
A pointed single-profile `thread mill` has two flanks: one tooth spanning the
flute length from the tip, with the diameter at its point. Its profile angle is
the included angle between the flanks measured from the radial line, as for a
thread form, so the tooth depth is half the flute length divided by the tangent
of half that angle. Fusion renders a 90 degree thread mill this way; other
angles and the tooth's position are assumptions. Tooth count must be 1 and tip
type pointed; missing keys are rejected. The tooth root must be positive, and a
neck or shank on the tooth must not be wider than the root. Bodies above the
flanks are noncutting cylinders.

At each edge sample, the chamfer normal lies in the plane of the two face
normals, and its component along the tool axis equals the sine of the flank's
half-angle (negative for a back flank). Of the solutions at least 10 degrees
from both faces, the one nearest the bisector is used. If two solutions are
within 2 degrees of equally near, as on a ridge symmetric about the tool axis,
the edge is rejected rather than letting noise in the sampled normals pick a
side. The plane depth keeps
the larger leg equal to the edge-break width. The flank is placed tangent to
that plane with the chamfer strip centered on it, leaving 5% of the flank clear
at each end. This is exact for straight edges, because the swept cone's envelope
is then that plane. Edges steeper than the flank are rejected. Each edge uses
the first flank that can cut it, topside first. Edges cut by different flanks
are never linked into one chain. Corner rolls use the flank model, without the
vertex-crossing fallback, and are refined until chord midpoints are within
0.005 mm of the roll. Otherwise a wide cutter's chords would cut inside the arc.
Lollipop rolls also go through the cutter model now, so rolls on heads larger
than about 12 mm get extra points; positions are otherwise unchanged.

Chamfer paths use the lollipop clearance and access checks. At each sampled
position, every cutting flank, enlarged for motion between samples, is
intersected with the setup models. The enlargement offsets the slanted surface
by the motion margin and extends it along the same slope, so it covers every
point within that margin. The removed region is sampled at its vertices, along
its edges (0.01 mm chord tolerance), and at the nodes of a mesh over its faces
with sides no longer than half the edge-break width. Every sample must lie
within the edge-break width, plus that enlargement and the 0.01 mm path
tolerance, of a selected edge. Points between samples are not checked. For
edge breaks of at least 0.02 mm (below that, the 0.01 mm minimum mesh side
applies), a point on a mesh triangle is within 0.29 widths of a sample. A point
on a curved face is further off by the mesh's distance from the face, which
Fusion is asked to keep within 0.01 mm but does not guarantee. So a face point
can exceed the limit by about 0.29 widths plus that mesh deviation. Face sampling catches a thin
feature whose edges all lie near selected edges but whose face does not, such as
a shim inside the cutter. A sample needs no distance measurement when a measured
point's distance plus the gap to it stays within the limit. The check assumes no
point inside the removed solid is farther from the selected edges than its
faces, which fails only if selected edges surround that point in every
direction. Flanks may not touch fixtures. If no lateral or upward exit is
clear, a back-side endpoint steps toward the tool axis, perpendicular to the
edge in plan, until the cutter clears the face, then moves along the axis.
Steps come from the nearest segment ends; where a closed contour starts at a
trimmed concave corner, a combined step clears both walls. Trace defaults
`doChamfer` to true for chamfer mills. For chamfer and thread mills it must
exist, is set after the tool is assigned, and must read back as off, since the
path already describes the tool tip.

Chamfer verification (2026-11-13, Fusion 2706.1.17): on a scratch
80 x 60 x 12 mm plate with a 30 mm through hole and a 20 x 20 x 5 mm pocket, a
Harvey #875510 double-angle cutter (1/2 inch, 90 degrees, 1/4 inch width;
dimensions from Harvey's DXF) was defined as a thread mill. The top outer
perimeter, hole rim, and pocket rim generated three topside contours. The bottom
perimeter and hole rim generated two back-side contours; the hole exit stepped
2.54 mm toward the hole axis before rising through the hole. A mixed selection
split into one contour per flank. Fusion generated valid Trace toolpaths. Posted
feed moves were within 0.00006 mm of the computed paths. Computed vertices were
within 0.0053 mm of the posted path, which Trace thins at its 0.01 mm tolerance.
Cutter solids placed at posted positions removed exactly 0.5 mm on both faces at
straight and curved, top and bottom edges. A 1/2 inch 90 degree chamfer mill
matched its path the same way and rejected bottom edges. With Fusion's default
`doChamfer` and a 0.5 mm chamfer width, Trace shifted that path by 0.5 mm, which
the forced setting prevents. A 3 inch cutter was rejected at the 30 mm hole
for cutting away from the edge. Before roll refinement, it was also rejected at
outside corners for a 0.03 mm chord overcut. A 1.25 inch cutter could not exit
through the hole. Clearance checking took about 8 to 80 s per operation on this
plate, depending on Fusion's responsiveness. Caching Fusion bounding-box reads
reduced one 288 s case to 8 s. These checks used the add-in modules from a
Fusion script, not the dialog.

Face-interior sampling verification (2026-11-14, Fusion 2706.1.17, macOS): the
same plate and Harvey cutter were rebuilt in a new document, and the add-in
modules ran from a Fusion script. Fusion meshed every temporary cut region.
The topside, back-side, square-hole and mixed selections, the 3 inch cutter
cases and the chamfer mill cases gave the same results as before. Their
computed paths were identical to those from the boundary-only check. Run
interleaved three times each, the top perimeter plus both hole rims took
14.7 s instead of 7.4 s, with about twice as many distance measurements.
Quarter-width mesh sides took about three times as long and were not used.
Separate runs varied up to fivefold with Fusion's responsiveness. A 3 mm
diameter, 0.3 mm thick shim was added as a second setup model, entirely inside
the cutter at the middle of the outer top edge, and its top rim was selected.
At that position the boundary-only check accepted the cut; face sampling
rejected it. Along the whole edge both rejected it, because the cutter clips
the shim partially on the way in. Trace operations were not regenerated for
this change, since it only decides whether a path is accepted.

Fusion derives a thread mill's shoulder (neck) diameter from its diameter and
pitch on `Tool.createFromJson`. A tool whose pitch Fusion had normalized to 1 in
re-imported with a -0.5 in neck and was drawn as a plain cylinder. Re-imported
thread mills now get the neck from the source JSON, identical document tools
without a GUID are reused instead of re-imported, and negative necks are
rejected. Define such cutters with pitch equal to the cutter width.

The manifest lists macOS as well as Windows. Fusion on macOS refuses to add an
add-in whose manifest omits it. On macOS (2026-11-13) a `Backside` chamfer
operation on the showcase's bottom outer perimeter was created through the
dialog and generated without warnings, using a Harvey #875510 document tool with
feed presets from Harvey's chart.

After review fixes (same day), the scratch plate gained a 16 x 16 mm square
through hole. Its bottom rim, a closed back-side contour starting at a trimmed
concave corner, now exits by a combined step toward the hole center; before
the fix, such contours could not find an exit. All earlier cases regenerated;
posted paths matched within 0.0053 mm and placed cutter solids removed 0.5 mm on
both faces, including the square rim. The add-in's `update_operation` switched
a topside operation from the thread mill to a chamfer mill: `doChamfer` read
back off, the sketch was replaced, and the posted path matched. These ran as
Fusion scripts on macOS, not through the dialog. Windows has not been rechecked
for chamfer cutters.

Run regressions outside Fusion with `python -m unittest discover -s tests`.
Fusion API tests use mocks; actual Trace generation and parameter compatibility must
also be checked in Fusion.

The dialog's execute handler snapshots only the document, setup/operation, edges
(tokens and the picked edges), tool JSON, and width. It performs no Design or CAM
mutations. Once the dialog is destroyed, a delayed custom event runs the update on Fusion's main thread when the
selection command is idle. Other commands defer the job until they terminate. A
changed document or invalidated setup, operation, or edge rejects the queued job.
Cancellation queues nothing; stopping the add-in cancels queued work. Cross-product
updates are not grouped into the dialog's single undo transaction. Explicit failure
rollback below still applies. This separation avoids nesting sketch/CAM edits in a
Manufacture command's commit; live verification is required after lifecycle changes.

Edges are normally found again by their entity token. `Design.findEntityByToken`
does not resolve edges of manufacturing models (issue #14), so two fallbacks apply
only when a token fails:

- The queued job uses the picked edge itself if Fusion still reports it valid.
- For later edits, each saved edge also stores its shape: its length and nine points
  evenly spaced along it, ends included. The edge is then the single setup-model
  edge whose shape matches within 0.001 mm, in either direction, and that the saved
  token names when resolved through the design owning that edge's body (the
  manufacturing model's own design). The setup can instead give the edge from
  the document's design while the token only resolves in a manufacturing
  model's design; the edge found there is a copy, so it must be the same-numbered
  edge of a same-named body, in the same place. Shape alone never selects an
  edge, so a coincident edge of another body is refused. Three points are not enough: two
  same-radius circles in perpendicular planes share start, middle and end.

No match, or two different matches, asks for reselection. The shape list is an
optional addition to the saved settings, so older operations load unchanged.
Lollipop and chamfer paths on manufacturing models stay rejected by the clearance
setup check.

Live verification (2026-11-17, Fusion 2706.1.17, by Alex Pinson):
- Issue #14 part (flat part, 2D pockets, linked component), 2 mm DATRON ball end
  mill: edges selected, valid toolpath generated, operation edited, via the dialog.
- `xref demo CAM` (linked body in a manufacturing model, 1/4 in ball end mill):
  no setup-model edge token resolved through the design, and each edge was found
  again as itself. Two same-radius semicircular arcs in perpendicular planes each
  generated a valid toolpath, were found again as the picked arc and not the other,
  and regenerated after an edit. Coincident edges of a second body were refused.
  Saved tokens still resolved after closing and reopening the document. Editing,
  reselecting and accepting through the dialog followed the selected edge.
- The same part with the linked body used directly (no manufacturing model):
  tokens resolved through the design, so the token failure comes from the
  manufacturing model, not from the link.
- `Fusion Free Deburr - 3D Showcase` (maintainer QA, same day and version): an
  edge picked with a manufacturing model present created a valid operation, but
  editing it reported one saved edge missing. The setup gave the edge from the
  document's design and the token only resolved in the manufacturing model's
  design. With the copy check above, editing it found the edge again (edge 44
  of body `Showcase`), and a read-only script found nothing once that edge was
  removed. Editing an operation made before this change, and editing after
  save, close and reopen, also worked.
- Not yet verified: lollipop or chamfer cutters on linked edges.

A manufacturing model holds Fusion's own copy of the guide sketch, as a draggable
control-point spline. The Trace curve selection refers to the sketch in the design,
so dragging the copy does not change the toolpath. The copy cannot be fixed from
the design, so the add-in leaves it alone.

Live verification (2026-10-30, Fusion 2705.1.30): the 11 saved edges in
`Xometry_Mill_Test_Part` produced one contour and generated a valid Trace toolpath
without errors or warnings. Editing reset a deliberately changed 1 mm axial offset
to zero. A flat endmill was rejected using an actual Fusion Tool object. This is
one model check, not exhaustive coverage of geometry or machine clearance.

Failed sketch builds remove partial geometry. Failed edits restore the previous
curve selection, tool, Trace offsets, saved settings, and sketch name before removing
the replacement sketch. The old sketch is deleted only after configuration succeeds.
Rollback does not restore Fusion's cached toolpath; regenerate after a failed edit.
If Fusion itself refuses restoration, both sketches are retained with an explicit
error so potentially referenced geometry is not deleted. Toolpath generation remains
asynchronous; later generation errors are reported by Fusion.

Additional live verification: an injected settings-save failure restored the old
sketch and settings without increasing sketch count; an injected spline-build failure
also left no partial sketch. A successful replacement kept the sketch count unchanged
and regenerated a valid toolpath without errors or warnings. A newly created Trace
also generated successfully; its temporary test operation was removed afterward.

Lollipop verification (2026-11-02, Fusion 2706.1.17): the four rim edges of the
showcase side pocket generated one Trace contour using the document's 8 mm
lollipop, 4 mm neck, and 20 mm shoulder length. The top-down setup was rejected
for shaft interference; the underside setup cleared with the same tool dimensions.
A queued update to 0.25 mm generated successfully and was saved as document version 6.
A subsequent normal dialog edit to 0.2 mm completed successfully with deferred execution
and was saved as document version 7.
All seven operations remained valid, the lollipop operation had no warnings or errors,
and sketch count remained 14. The exported 82 moves, including approach and retract,
passed clearance checks at 2,155 sampled positions. Generated feed vertices were
within 0.000067 mm of the stored sketch polyline (excluding the intentional final
feed retract). The diagnostic post emits CSV, not machine code. The tool definition
had no holder, so this check does not establish clearance for an added physical holder.

Top-access verification (2026-11-02): the document's test tool was revised to
35 mm shoulder reach with the original 8 mm head and 4 mm neck. The first shaft
segment was shortened from 50 to 35 mm to retain the 75 mm overall length. Fusion
initially normalized the replacement tool's neck to 6 mm, which correctly failed
the contact check; explicitly setting the document tool's shoulder diameter to
4 mm restored the intended geometry. New-operation jobs now reuse an unchanged
document tool object rather than reimporting its JSON and losing its internal ID.
The `Lollipop Deburr - Top Access Test` operation in `Deburr New` generated all
four rim edges at 0.2 mm with a +Z tool axis, with no warnings or errors. Its
exported 82 moves passed clearance checks at 3,346 sampled positions. The revised
tool was also propagated to the underside operation, which regenerated successfully.
All nine document operations remained valid. These are modeled test-tool dimensions;
a physical cutter and holder must match the library definition for machining.

Assembly and fixture regression checks (2026-11-02): the existing document's
Maritool CAT40-ER32-2.35 holder was applied to a temporary QA lollipop assembly
with 50 mm stickout. Fusion generated a valid Trace without warnings; all 95
exported moves passed clearance checks at 4,201 sampled positions. A separate
40 mm-stickout case was rejected for assembly interference. Temporary solid
fixture checks accepted a remote clamp and rejected a clamp at the cutter path.
These exercised Fusion's BRep kernel; the fixtures were temporary solids and were
not retained in the setup. The QA operation, driving sketch, and tool were removed
afterward, leaving the original nine operations and two tools.

Noncutting-cylinder broad-phase bounds now use the radius projected perpendicular
to the tool axis. Sections entirely above the model can be excluded without
constructing and intersecting a temporary solid; rotated-axis bounds and the
above-part holder case have focused regression coverage. The UI command is named
`Deburr`, and the context-menu command is `Edit Deburr`; their internal IDs stay
unchanged so existing operation metadata and toolbar references remain compatible.

Manual new-operation QA (2026-11-04) exposed a selected-edge context mismatch:
CAM-picked lollipop edges were rejected after setup bodies had already been
resolved to design bodies. Selected edges now resolve to the matching design
body or occurrence before sampling and saving operation settings. Resolution
requires native body identity, matching placement matrices, and matching bounds;
unrelated bodies and repositioned manufacturing proxies remain unsupported.
Regression tests cover root and occurrence picks, retained native picks, rejected
translations and symmetric rotations, and failed edge proxy creation.

After reloading the fix, the user confirmed all three manual Fusion checks passed:
creating a new lollipop operation in `Deburr New` with the four front side-pocket
rim edges, the 35 mm-reach tool, and 0.2 mm width; reopening it with `Edit Deburr`
and confirming the edges, tool, and width before canceling; and editing
`Ball Deburr - Kidney Pocket` without changes and regenerating successfully.

Library tool picks (issue #9, 2026-11-15): `Tool.createFromJson` does not keep
every tool exactly as given. A cloud 3/8 in ball endmill lost its `expressions`
block and had its holder gauge length recomputed on import, and editing a
document tool (for example its number) changes its copy the same way. The
document copy then shares the library tool's GUID but not its definition, and
picking the library tool again failed with "The selected tool changed".
Deburr now reuses a document tool only when it is identical and otherwise
imports the selected definition. Through the API in Fusion 2706.1.17 (macOS), in
an unsaved scratch document holding an edited copy (#7) of that cloud tool, the
apply step created a `Ball Deburr` on four top edges at 0.020 in with the cloud
definition (#52) and a valid toolpath; the existing operation kept #7. A second
pick reused #52 instead of adding another copy. The user then checked the
dialog on the same day in `Fusion Free Deburr - 3D Showcase`: picking that
cloud tool through **Select tool...** created `Ball Deburr 4`; picking it from
the cloud again, which failed before the fix, created `Ball Deburr 5`; and
picking its `Document` entry created `Ball Deburr 6`. All three use the one
document copy (#52) and generated valid toolpaths. A Deburr with the 6 mm ball
after renumbering that document tool (#1 to #3) also worked, as did editing an
existing Deburr operation with `Edit Deburr` and switching it to a library tool.

Ball path ends gouging other faces (2026-11-20): only corners were checked, so a
path that ran down a slope into a floor kept going. On an unsaved test part in
Fusion 2706.1.17 (Windows), three linked edges (two slopes and an arch along the
top of a riser, 55.8 mm of path) with a 1/4 in ball and 0.010 in width cut up
to 1.06 mm into the floor's outer corner at both ends. With the fix, run
through the API, 4.75 mm of path came off in total, an independent check every
0.05 mm found the ball touching no face other than the selected edges' own
faces, balls at eight heights up to 15 mm straight above both ends removed
nothing beyond the break, and generation took about 9 s. A 10 mm tall boss 1 mm
behind a top edge trimmed the path around it, and balls every 0.05 mm up to
10 mm above the new ends removed nothing beyond the break; the
plunge check itself measured 8.2 mm through the boss and nothing beside or
above it. The chamfer check with stroked edge distances, through the API on a
step block with a 10 mm 90 degree chamfer mill and 0.5 mm width, accepted a top
edge ending at two outside corners and rejected one running into the step's
wall ("cutter removes material away from the selected edges"). With the inside
samples, the user's part took 11.5 to 14 s. A hole-rim loop exported open
started and ended at its checked start in the posted toolpath. A 12 mm ball on
a 4 mm hole with a 0.02 mm break was skipped with a warning in 1.9 s. In a scratch document, through the API: a 20 degree ramp into a
floor in a component moved 3 cm and turned 30 degrees lost 1.54 mm of path
(both ends); a 3.5 mm slot with both lips selected (6 mm ball, 0.5 mm break)
and a 1.4 mm hole (4 mm ball) were skipped with warnings; a 3 mm slot with
both lips (4 mm ball, 0.6 mm break) and a 10 mm hole (4 mm ball) were kept
whole. An earlier version of the fix was checked on the posted toolpath with
the diagnostic post. The user opened that operation with `Edit Deburr`,
accepted it unchanged, and it regenerated. The trim message goes to the Text
Commands log rather than a message box, because trimming is expected; skipped
edges and split corners still show one.
