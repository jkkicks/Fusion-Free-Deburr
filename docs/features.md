# Features and where they live

Check this list before starting new work. If something you want is here, it
already exists: use it, or improve it in place. Also check open
[issues](https://github.com/jkkicks/Fusion-Free-Deburr/issues) and
[pull requests](https://github.com/jkkicks/Fusion-Free-Deburr/pulls), which
hold work in progress.

Keep this file current: any pull request that adds, changes, removes or moves a
feature updates it.

## What the add-in does

| Feature | Where it lives |
| --- | --- |
| **Deburr** command in **Manufacture > Milling > 3D** (falls back to the Add-Ins panels) | `FusionFreeDeburr/FusionFreeDeburr.py` |
| Dialog: pick an operation to create or edit, a setup, edges, a tool and the edge-break width | `FusionFreeDeburr/ball_deburr_command.py` |
| **Edit Deburr** from the right-click menu on a Deburr operation or its sketch | `FusionFreeDeburr/FusionFreeDeburr.py`, `ball_deburr_command.py` |
| Tool picker window: document tools, local libraries and cloud libraries | `FusionFreeDeburr/tool_picker.py`, `cam_tools.py`, `resources/tool_picker/index.html` |
| **Ball endmill** edge breaks of a set width | `deburr_math.py` (`ball_center`, `tool_tip`) |
| **Lollipop mill** edge breaks, including undercuts, side pockets and other edges reached from below or the side | `deburr_math.py` (`lollipop_contact_height`), `lollipop_fusion.py` |
| Reading cutter shape (head, neck, shaft, holder) from Fusion tool data | `cutter_profile.py` |
| Linking selected edges into continuous paths, with rolls around corners | `deburr_chain.py` |
| Sampling curved edges to tolerance, drawing the 3D sketch and creating the Trace operation | `deburr_fusion.py` |
| Safety: skips edges that aren't sharp and convex, rejects gouges | `deburr_fusion.py`, `deburr_chain.py` |
| Safety: neck, shaft, holder and fixture clearance checks against setup models and other bodies; rejects cuts away from the selected edges | `lollipop_fusion.py` (`Clearance`) |
| Approach and withdrawal moves outside the part | `lollipop_fusion.py` (`add_access`) |
| Settings saved on the operation so it can be edited later; failed edits roll back | `deburr_fusion.py` (`save_settings`, `load_settings`, `update_operation`) |
| Updates run after the dialog closes, so Fusion's undo and commands stay stable | `ball_deburr_command.py` (`DeburrJob`) |
| Windows and macOS | `FusionFreeDeburr/FusionFreeDeburr.manifest` (`supportedOS`) |
| Toolbar icons | `tools/make_icons.py` generates `resources/BallDeburr/` |

Supported Fusion tool types: `ball end mill` and `lollipop mill`. Other types,
such as flat endmills, are rejected.

## Project infrastructure

| Feature | Where it lives |
| --- | --- |
| Unit tests (run outside Fusion with mocks) | `tests/` |
| CI on Windows and Linux; label check on pull requests | `.github/workflows/ci.yml` |
| Automatic release notes and verified alpha releases on merge to `main` | `.github/workflows/release-drafter.yml`, `release.yml`, `tools/release.py`, [releases.md](releases.md) |
| Installation ZIP | `tools/package.py` |
| Diagnostic post processor that exports Trace moves as CSV (not machine code) | `tools/dump_toolpath.cps` |

## In review

- **Chamfer mills** for topside edges, and **double-angle (front and back)
  chamfer cutters** defined as a single-profile thread mill, including
  back-side edges such as the bottom of a through hole. See
  [PR #7](https://github.com/jkkicks/Fusion-Free-Deburr/pull/7).
- **Lollipop lead-in and approach options** (ramped lead-in, inside or outside
  approach, clearance and lead angle inputs). Planned in
  [PR #6](https://github.com/jkkicks/Fusion-Free-Deburr/pull/6).

## Not built yet

- **In-app automatic updates.** Releases publish the files an updater would
  need, but installing an update is still manual. See [releases.md](releases.md)
  for the plan.
- **Repositioned manufacturing models** and non-solid setup selections are
  rejected.
- **Simulating remaining stock.** Checks use the finished model, so always
  simulate in Fusion with real stock and workholding.

## More detail

- [README.md](../README.md): installing and using the add-in.
- [technical-notes.md](technical-notes.md): geometry, safety limits and Fusion
  test results.
- [releases.md](releases.md): how releases are made.
