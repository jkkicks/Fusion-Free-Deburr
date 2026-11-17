"""Fusion Free Deburr - Fusion add-in entry point.

Registers the Deburr button; the command itself lives in ball_deburr_command.py.
In-app updates live in updater.py and updater_fusion.py.
"""

import importlib
import os
import traceback

import adsk.core
import adsk.fusion
import adsk.cam

# The updater loads first and on its own, so a broken update can still be
# reported, rolled back or replaced by a newer one.
from . import updater, updater_fusion

# Stop/Run in Fusion doesn't re-import submodules; reload so edits take effect.
# Order matters: dependencies before the modules that import them.
for _module in (updater, updater_fusion):
    importlib.reload(_module)

# If Fusion was killed part-way through installing an update, finish it now,
# before any toolpath module is imported, so old and new code never mix.
_import_error = None
_blocked = None  # set when Deburr must stay off this session
try:
    _resumed = updater.resume_interrupted(updater.Store(updater.data_dir()), os.path.dirname(os.path.abspath(__file__)))
    if _resumed:
        _blocked = (f'Finished installing v{_resumed}, which was interrupted. '
                         'Restart Fusion to use it.')
except Exception:
    _blocked = ('An interrupted update could not be finished, so Deburr is turned off to avoid mixing '
                     'versions. Reinstall the add-in from the release page.\n\n' + traceback.format_exc())

try:
    if _blocked:
        raise ImportError(_blocked)
    from . import deburr_math, deburr_chain, deburr_fusion, cutter_profile, lollipop_fusion, cam_tools, tool_picker, ball_deburr_command
    for _module in (deburr_math, deburr_chain, deburr_fusion, cutter_profile, lollipop_fusion, cam_tools, tool_picker, ball_deburr_command):
        importlib.reload(_module)
except Exception:
    if not _blocked:
        _import_error = traceback.format_exc()

CMD_ID = 'additionalToolpaths_ballDeburr'
CMD_NAME = 'Deburr'
CMD_TOOLTIP = ('Generate edge-break geometry and a trace toolpath for a ball, lollipop, or chamfer cutter.\n'
               'Select an existing Deburr operation first to edit it.')
# Right-click menu entry on Deburr operations; runs the same command,
# which opens in edit mode for the selected operation.
EDIT_CMD_ID = 'additionalToolpaths_editBallDeburr'
EDIT_CMD_NAME = 'Edit Deburr'
EDIT_CMD_TOOLTIP = 'Edit the edges, tool, or edge break width of this Deburr operation.'
# Icons are required for the button to be pinnable to the toolbar.
CMD_RESOURCES = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'resources', 'BallDeburr')

# Tried in order; the first panel found gets the button.
PANEL_CANDIDATES = [
    ('CAMEnvironment', 'CAM3DPanel'),             # Manufacture > Milling > 3D
    ('CAMEnvironment', 'CAMScriptsAddinsPanel'),  # Manufacture > Utilities > Add-Ins
    ('FusionSolidEnvironment', 'SolidScriptsAddinsPanel'),
]

# Promote (pin to the ribbon face) only in the add-ins panels; in the 3D panel
# the button lives in the dropdown alongside the other 3D toolpaths.
PROMOTE_IN_PANELS = {'CAMScriptsAddinsPanel', 'SolidScriptsAddinsPanel'}

# Log all Manufacture workspace tab/panel IDs on startup (to find panel IDs).
DUMP_UI_IDS = False

_app: adsk.core.Application = None
_ui: adsk.core.UserInterface = None

# Event handlers must be kept referenced or Python will garbage-collect them.
_handlers = []
_marking_menu_handler = None  # kept separately so stop() can unregister it


def log(msg: str):
    """Write to the Text Commands window (View > Show Text Commands)."""
    _app.log(f'[FusionFreeDeburr] {msg}')


def _dump_ui_ids(ws_id: str):
    ws = _ui.workspaces.itemById(ws_id)
    if not ws:
        log(f'Workspace {ws_id} not found')
        return
    for tab in ws.toolbarTabs:
        log(f'TAB {tab.id} ({tab.name})')
        for panel in tab.toolbarPanels:
            log(f'    PANEL {panel.id} ({panel.name})')


def _find_panel():
    for ws_id, panel_id in PANEL_CANDIDATES:
        ws = _ui.workspaces.itemById(ws_id)
        panel = ws.toolbarPanels.itemById(panel_id) if ws else None
        if panel:
            return panel
    return None


def _selected_deburr_op():
    """True if the selection includes an operation made by this add-in, or a
    curve of its path sketch (what a click on the toolpath in the canvas picks)."""
    selections = _ui.activeSelections
    if selections.count == 0:
        return False
    doc = _app.activeDocument
    cam = adsk.cam.CAM.cast(doc.products.itemByProductType('CAMProductType')) if doc else None
    design = adsk.fusion.Design.cast(doc.products.itemByProductType('DesignProductType')) if doc else None
    if not (cam and design):
        return False
    ops = deburr_fusion.deburr_operations(deburr_fusion.milling_setups(cam))
    return any(deburr_fusion.operation_for_entity(design, selections.item(i).entity, ops)
               for i in range(selections.count))


class MarkingMenuHandler(adsk.core.MarkingMenuEventHandler):
    def notify(self, args: adsk.core.MarkingMenuEventArgs):
        try:
            controls = args.linearMarkingMenu.controls
            existing = controls.itemById(EDIT_CMD_ID)
            show = _selected_deburr_op()
            if existing:
                existing.isVisible = show
            elif show:
                controls.addCommand(_ui.commandDefinitions.itemById(EDIT_CMD_ID))
        except:
            log(f'Context menu failed:\n{traceback.format_exc()}')


def run(context):
    global _app, _ui
    try:
        _app = adsk.core.Application.get()
        _ui = _app.userInterface
        try:
            updater_fusion.start(_app, _ui, log, _handlers)
        except Exception:
            log(f'Updater failed to start:\n{traceback.format_exc()}')
        if _blocked:
            _ui.messageBox(_blocked, 'Fusion Free Deburr')
            return
        if _import_error:
            raise ImportError(_import_error)
        ball_deburr_command.init(_app, _ui, log, _handlers)
        tool_picker.start(_app, _ui, log, _handlers)

        cmd_def = _ui.commandDefinitions.itemById(CMD_ID)
        if not cmd_def:
            cmd_def = _ui.commandDefinitions.addButtonDefinition(CMD_ID, CMD_NAME, CMD_TOOLTIP, CMD_RESOURCES)

        on_created = ball_deburr_command.CommandCreatedHandler()
        cmd_def.commandCreated.add(on_created)
        _handlers.append(on_created)

        edit_def = _ui.commandDefinitions.itemById(EDIT_CMD_ID)
        if not edit_def:
            edit_def = _ui.commandDefinitions.addButtonDefinition(
                EDIT_CMD_ID, EDIT_CMD_NAME, EDIT_CMD_TOOLTIP, CMD_RESOURCES)
        on_edit_created = ball_deburr_command.CommandCreatedHandler()
        edit_def.commandCreated.add(on_edit_created)
        _handlers.append(on_edit_created)

        global _marking_menu_handler
        _marking_menu_handler = MarkingMenuHandler()
        _ui.markingMenuDisplaying.add(_marking_menu_handler)

        if DUMP_UI_IDS:
            _dump_ui_ids('CAMEnvironment')

        panel = _find_panel()
        if panel is None:
            _ui.messageBox('FusionFreeDeburr: could not find a toolbar panel to add the button to.')
            return
        if not panel.controls.itemById(CMD_ID):
            control = panel.controls.addCommand(cmd_def)
            control.isPromoted = panel.id in PROMOTE_IN_PANELS

        # Only now is a freshly installed update known to work; until then a
        # failed start offers to restore the previous version.
        updater_fusion.started()
        log(f'Started; button added to panel "{panel.id}"')
    except:
        error = traceback.format_exc()
        if _ui and not updater_fusion.offer_rollback(error):
            _ui.messageBox(f'FusionFreeDeburr failed to start:\n{error}')


def stop(context):
    try:
        if _import_error or _blocked:
            return
        ball_deburr_command.stop()
        for ws_id, panel_id in PANEL_CANDIDATES:
            ws = _ui.workspaces.itemById(ws_id)
            panel = ws.toolbarPanels.itemById(panel_id) if ws else None
            control = panel.controls.itemById(CMD_ID) if panel else None
            if control:
                control.deleteMe()

        global _marking_menu_handler
        if _marking_menu_handler:
            _ui.markingMenuDisplaying.remove(_marking_menu_handler)
            _marking_menu_handler = None

        for cmd_id in (CMD_ID, EDIT_CMD_ID):
            cmd_def = _ui.commandDefinitions.itemById(cmd_id)
            if cmd_def:
                cmd_def.deleteMe()

        tool_picker.stop()
        _handlers.clear()
        log('Stopped')
    except:
        if _ui:
            _ui.messageBox(f'FusionFreeDeburr failed to stop:\n{traceback.format_exc()}')
    finally:
        # Last, once nothing of the old version is running: installs a
        # downloaded update. Runs on Stop and when Fusion closes.
        updater_fusion.stop()
        _handlers.clear()
