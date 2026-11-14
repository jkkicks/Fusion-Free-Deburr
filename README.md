<div align="center">

# Fusion Free Deburr

[![Version](https://img.shields.io/badge/version-0.1.0--alpha-blue)](https://github.com/jkkicks/Fusion-Free-Deburr/releases) [![License](https://img.shields.io/badge/license-MIT-green)](LICENSE) ![Status: alpha](https://img.shields.io/badge/status-alpha-orange)

**Free 3-axis deburring toolpaths for Autodesk Fusion, using ball, lollipop, and chamfer cutters.**

[Download the latest release](https://github.com/jkkicks/Fusion-Free-Deburr/releases)

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

1. Download the ZIP from the [latest release](https://github.com/jkkicks/Fusion-Free-Deburr/releases) and extract it.
2. In Fusion, press **Shift+S** to open **Scripts and Add-Ins**, or find it under **Utilities > Add-Ins**.
3. Click **+** beside **My Add-Ins**, select the extracted `FusionFreeDeburr` folder, and run the add-in.

## Use

In **Manufacture > Milling > 3D > Deburr**, select the edges, choose a ball, lollipop, or chamfer cutter, set the edge-break width, and generate the toolpath. Right-click the operation and choose **Edit Deburr** to edit it later.

Lollipop mills support undercut contact on accessible side-pocket rims and other convex edges. The cutter must be defined as a **lollipop mill**, with its spherical head, neck, shoulder length, shaft, and holder dimensions matching the physical tool. The add-in checks sampled cutting motion against solid setup models, fixtures, and other solid bodies in the design. It includes explicit approach and withdrawal moves outside the part before Fusion's Trace linking moves.

Chamfer cutters cut a flat chamfer at the angle of their flank, centered on the flank. The edge-break width is the larger of the two chamfer legs, so neither face is cut back further than the width. Define a topside-only tool as a **chamfer mill** (diameter, taper angle as half the included angle, tip diameter, shank diameter, overall length). Define a front-and-back cutter, such as a double-angle shank cutter, as a single-profile **thread mill** (one tooth, pointed tip): diameter at the point, profile angle equal to the included angle, flute length and pitch equal to the cutter width, and a neck or shank no wider than the tooth root. Fusion derives a thread mill's neck from its pitch, so a mismatched pitch draws the wrong tool. Each edge uses the top flank when it can and the back flank otherwise, for example the bottom edge of a through hole. Each cut enters and leaves from outside the part, sideways or from above; when neither is clear, a back-side cut instead steps toward the tool axis and then moves along it. Ridges that could be chamfered equally well from either side are rejected. Chamfer paths get the same clearance checks as lollipop paths, and any cut away from the selected edges is rejected.

A rim can be reachable by the head while the neck or shaft hits the part. In that case generation is rejected: use a tool with suitable neck reach or a setup that provides access. Repositioned manufacturing models and non-solid setup selections are currently unsupported. These checks use the finished model, not a simulation of remaining stock; simulate with the actual stock and workholding before machining.

![Creating a Ball Deburr toolpath in Fusion](docs/images/readme/ball-deburr-walkthrough.gif)

*Select edges, choose a tool and set the edge-break width.*

## How it works

Fusion Free Deburr generates custom 3D sketch geometry and uses Fusion's native Trace strategy to create the toolpath.

> **Alpha software:** Simulate and inspect the toolpath before machining.
