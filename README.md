<div align="center">

# Fusion Free Deburr

[![Version](https://img.shields.io/github/v/release/jkkicks/Fusion-Free-Deburr?label=version)](https://github.com/jkkicks/Fusion-Free-Deburr/releases/latest) [![License](https://img.shields.io/badge/license-MIT-green)](LICENSE) ![Status: beta](https://img.shields.io/badge/status-beta-yellow)

**Free 3-axis deburring toolpaths for Autodesk Fusion, using ball, lollipop, and chamfer cutters.**

[Download the latest release](https://github.com/jkkicks/Fusion-Free-Deburr/releases/latest)

</div>

![Fusion simulation of ball deburring on the showcase part](docs/images/readme/simulation.gif)

*Fusion simulation of the showcase part.*

![Selected edges and resulting toolpaths](docs/images/readme/workflow-select-edges-to-toolpath.gif)

*Selected edges and their resulting toolpaths.*

## Examples

| Curved edges and varying Z | Freeform pocket |
| :---: | :---: |
| ![Perimeter toolpath](docs/images/readme/gallery-varying-z-perimeter-side.png) | ![Pocket toolpath](docs/images/readme/gallery-freeform-pocket.png) |
| **Saddle holes** | **Holes on a slope** |
| ![Ridge hole toolpaths](docs/images/readme/gallery-ridge-saddle-holes.png) | ![Slope hole toolpaths](docs/images/readme/gallery-holes-normal-to-25deg-slope.png) |

## Install

1. Download the ZIP from the [latest release](https://github.com/jkkicks/Fusion-Free-Deburr/releases/latest) and extract it.
2. In Fusion, press **Shift+S** to open **Scripts and Add-Ins**, or find it under **Utilities > Add-Ins**.
3. Click **+** beside **My Add-Ins**, select the extracted `FusionFreeDeburr` folder, and run the add-in.

### Updates

When Fusion starts, the add-in checks GitHub for a new version (at most once a day) and shows a window when one is out. In that window you can install it, skip it, or choose how updates work: **install automatically**, **ask me first** (the default) or **don't check automatically**, and pick the **stable** or **nightly** track. Updates install the next time you restart Fusion. Check by hand any time from **Utilities > Add-Ins > Check for Deburr Updates**. If an update fails to start, you're offered the previous version back. Versions up to v0.1.1 don't have the updater, so install the first version that has it by hand.

Nightly builds of the latest changes are on the [Releases page](https://github.com/jkkicks/Fusion-Free-Deburr/releases), marked **Pre-release**. They are for testing; use the latest release for real work.

## Use

In **Manufacture > Milling > 3D > Deburr**, select the edges, choose a ball, lollipop, or chamfer cutter, set the edge-break width, and generate the toolpath. Right-click the operation and choose **Edit Deburr** to edit it later.

Ball endmill paths stop short wherever the ball would remove material farther than the edge-break width from the selected edges, for example where an edge runs down into a floor or wall, or where a ball is too big for a hole or slot. The edge break fades out over about a millimetre there, and the rest of the edge up to the obstacle is left sharp, so deburr it by hand. An edge the ball can't break anywhere is skipped with a warning. Skipped edges stay selected in the operation, so their bodies are still checked, and the warning repeats, when you edit it. Each contour end is also moved inward until the plunge straight down onto it, and the retract from it, are clear. A closed loop, such as a hole rim, is cut 0.1 mm past its start so Fusion enters it where that plunge was checked. Removed material may lie up to 0.1 times the edge-break width (at least 0.01 mm) plus 0.01 mm beyond the break, measured at sampled points. Material between the sampled points can lie farther than that, so always check the stock simulation. Material within the break of another selected edge, such as the opposite lip of a slot, is allowed. How much path was removed is written to the Text Commands window (**View > Show Text Commands**). The check intersects the ball with the part along the path, so long selections take longer to generate (about 15 s for 51 mm of path in testing, longer on edges between faces that meet at a shallow angle, such as a chamfer's edges), and Fusion is busy until it finishes. Only the bodies the selected edges belong to are checked; the shank, holder, fixtures, other bodies and Fusion's linking moves are not.

On the **Checks** tab, **Gouge checks > Skip** turns off the checks that stop any cutter cutting into the part beyond the edge break: ball paths are not trimmed, and lollipop and chamfer paths are not rejected for gouging. For ball endmills that leaves no checks against the part at all. For lollipop and chamfer cutters, neck, shaft, holder and fixture checks, and approach and withdrawal moves, are still checked. Use it to find out whether a gouge check is why a toolpath fails. It is saved with the operation and always starts off for a new one; while it is on, a message after every generate says so. Simulate before machining.

Lollipop mills support undercut contact on accessible side-pocket rims and other convex edges. The cutter must be defined as a **lollipop mill**, with its spherical head, neck, shoulder length, shaft, and holder dimensions matching the physical tool. The add-in checks sampled cutting motion against solid setup models, fixtures, and other solid bodies in the design. It includes explicit approach and withdrawal moves outside the part before Fusion's Trace linking moves.

Chamfer cutters cut a flat chamfer at the angle of their flank, centered on the flank. The edge-break width is the larger of the two chamfer legs, so neither face is cut back further than the width. Define a topside-only tool as a **chamfer mill** (diameter, taper angle as half the included angle, tip diameter, shank diameter, overall length). Define a front-and-back cutter, such as a double-angle shank cutter, as a single-profile **thread mill** (one tooth, pointed tip): diameter at the point, profile angle equal to the included angle, flute length and pitch equal to the cutter width, and a neck or shank no wider than the tooth root. Fusion derives a thread mill's neck from its pitch, so a mismatched pitch draws the wrong tool. Each edge uses the top flank when it can and the back flank otherwise, for example the bottom edge of a through hole. Each cut enters and leaves from outside the part, sideways or from above; when neither is clear, a back-side cut instead steps toward the tool axis and then moves along it. Ridges that could be chamfered equally well from either side are rejected. Chamfer paths get the same clearance checks as lollipop paths, and cuts away from the selected edges are rejected. The check samples the removed material, so a cut between samples can go up to about 0.3 times the edge-break width beyond the limit; simulate before machining.

A rim can be reachable by the head while the neck or shaft hits the part. In that case generation is rejected: use a tool with suitable neck reach or a setup that provides access. Repositioned manufacturing models and non-solid setup selections are currently unsupported. These checks use the finished model, not a simulation of remaining stock; simulate with the actual stock and workholding before machining.

![Creating a Ball Deburr toolpath in Fusion](docs/images/readme/ball-deburr-walkthrough.gif)

*Select edges, choose a tool and set the edge-break width.*

## Contributing

Bug reports, ideas and pull requests are welcome, including from people using
AI assistants. See [CONTRIBUTING.md](CONTRIBUTING.md) and check
[what already exists](docs/features.md) first.

## How it works

Fusion Free Deburr generates custom 3D sketch geometry and uses Fusion's native Trace strategy to create the toolpath.

In a manufacturing model, Fusion's copy of the deburr sketch can be dragged, but the toolpath follows the sketch in the design, so dragging the copy changes nothing. To change a deburr path, use **Edit Deburr**.

> **Beta software:** Simulate and inspect the toolpath before machining.
