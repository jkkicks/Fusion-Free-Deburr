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
than 0.5 mm or a quarter ball radius. They inspect the selected body only and do not prove
continuous clearance or check shank, holder, fixtures, other bodies, or Fusion's linking
moves. Simulate generated paths and inspect retracts before machining.

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

The dialog's execute handler snapshots only the document, setup/operation, edge tokens,
tool JSON, and width. It performs no Design or CAM mutations. Once the dialog is
destroyed, a delayed custom event runs the update on Fusion's main thread when the
selection command is idle. Other commands defer the job until they terminate. A
changed document or invalidated setup, operation, or edge rejects the queued job.
Cancellation queues nothing; stopping the add-in cancels queued work. Cross-product
updates are not grouped into the dialog's single undo transaction. Explicit failure
rollback below still applies. This separation avoids nesting sketch/CAM edits in a
Manufacture command's commit; live verification is required after lifecycle changes.

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
