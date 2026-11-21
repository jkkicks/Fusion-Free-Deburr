"""The Deburr command: dialog, create, and edit of deburr trace operations."""

import math
import json
import traceback
import threading
from collections import deque
from dataclasses import dataclass

import adsk.core
import adsk.fusion
import adsk.cam

from . import cam_tools, deburr_fusion, tool_picker, cutter_profile, lollipop_fusion
from . import deburr_math as dm

IN_OP = 'operation'
IN_SETUP = 'setup'
IN_EDGES = 'edges'
IN_TOOL = 'tool'
IN_PICK = 'pickTool'
IN_DIAMETER = 'ballDiameter'
IN_WIDTH = 'breakWidth'
IN_SKIP_GOUGE = 'skipGougeChecks'
IN_GOUGE_GROUP = 'gougeChecks'
SKIP_GOUGE_NOTICE = ('Gouge checks are off for this operation (Checks tab): paths are not '
                     'trimmed or rejected for cutting into the part. Simulate before machining.')
IN_TAB_DEBURR = 'deburrTab'
IN_TAB_CHECKS = 'checksTab'
DIALOG_WIDTH = 420  # px; wide enough to show most tool names
SKIP_GOUGE_TOOLTIP = (
    'Turns off the checks that stop the cutter cutting into the part beyond the edge break: '
    'paths are not trimmed and gouging paths are not rejected.'
    '<br><br>Ball endmills: no part checks remain (they never had neck, shaft, holder or '
    'fixture checks). Lollipop and chamfer cutters: neck, shaft, holder and fixture checks, '
    'and approach and withdrawal moves, are still checked.'
    '<br><br>Use it to see whether a gouge check is why a toolpath fails. Simulate before machining.')


def _skip_input(inputs):
    """The Skip checkbox. The dialog's top-level inputs find it inside its
    tab and group (checked in Fusion 2706.1.17)."""
    found = inputs.itemById(IN_SKIP_GOUGE)
    return adsk.core.BoolValueCommandInput.cast(found) if found else None


def _describe(command_input, name, description, full_width=False):
    """Full name and explanation on hover; full width hides the label."""
    command_input.tooltip = name
    command_input.tooltipDescription = description
    if full_width:
        command_input.isFullWidth = True

NEW_OP_LABEL = 'New operation'
TOOL_TYPES = {cam_tools.BALL_END_MILL, *cam_tools.PROFILE_TYPES}
DEFAULT_NAME = 'Ball Deburr'
TYPE_NAMES = {cam_tools.LOLLIPOP_MILL: 'Lollipop Deburr',
              cam_tools.CHAMFER_MILL: 'Chamfer Deburr',
              cam_tools.THREAD_MILL: 'Chamfer Deburr'}
DEFAULT_WIDTH = {'in': '0.02 in'}  # anything else falls back to mm
DEFAULT_WIDTH_MM = '0.5 mm'

_app: adsk.core.Application = None
_ui: adsk.core.UserInterface = None
_log = print
_handlers = []
_session = None
_pending = deque()
_dispatch_timer = None
_job_event = None
_terminated_handler = None
_dispatch_enabled = False
JOB_EVENT_ID = 'additionalToolpaths_applyDeburr'


@dataclass(frozen=True)
class DeburrJob:
    document: object
    setup: object
    operation: object
    edge_tokens: tuple
    width: float
    tool_json: str
    keep_current_tool: bool
    # The picked edges themselves. A token cannot always be turned back into
    # its edge (linked components, manufacturing models); these still can.
    edges: tuple = ()
    skip_gouge_checks: bool = False


def init(app, ui, log, handlers):
    global _app, _ui, _log, _handlers, _job_event, _terminated_handler, _dispatch_enabled
    _app, _ui, _log, _handlers = app, ui, log, handlers
    _job_event = app.registerCustomEvent(JOB_EVENT_ID)
    _add_handler(_job_event, ApplyDeburrHandler())
    _terminated_handler = DeburrCommandTerminatedHandler()
    _ui.commandTerminated.add(_terminated_handler)
    _handlers.append(_terminated_handler)
    _dispatch_enabled = True


def stop():
    global _dispatch_enabled, _dispatch_timer, _job_event, _terminated_handler
    _dispatch_enabled = False
    if _dispatch_timer:
        _dispatch_timer.cancel()
        _dispatch_timer = None
    _pending.clear()
    if _terminated_handler:
        _ui.commandTerminated.remove(_terminated_handler)
        _terminated_handler = None
    if _job_event:
        _app.unregisterCustomEvent(JOB_EVENT_ID)
        _job_event = None


def _schedule_dispatch():
    """Queue work after the command stack unwinds; workers never edit Fusion."""
    global _dispatch_timer
    if not _dispatch_enabled or not _pending:
        return
    if _dispatch_timer:
        _dispatch_timer.cancel()
    def fire():
        # fireCustomEvent is the documented thread-safe bridge to the UI thread.
        if _dispatch_enabled:
            try:
                _app.fireCustomEvent(JOB_EVENT_ID)
            except RuntimeError:
                pass  # Fusion may have shut down since the timer was queued.
    _dispatch_timer = threading.Timer(.1, fire)
    _dispatch_timer.daemon = True
    _dispatch_timer.start()


class DeburrCommandTerminatedHandler(adsk.core.ApplicationCommandEventHandler):
    def notify(self, args):
        _schedule_dispatch()


class ApplyDeburrHandler(adsk.core.CustomEventHandler):
    def notify(self, args):
        if not _dispatch_enabled or not _pending:
            return
        # Do not change a design while another command owns a transaction.
        # commandTerminated schedules another attempt when that command ends.
        if _ui.activeCommand not in ('', 'SelectCommand'):
            return
        job = _pending.popleft()
        try:
            _apply_job(job)
        except Exception as error:
            _log('Deburr job failed:\n' + traceback.format_exc())
            _ui.messageBox('Could not apply deburr operation:\n' + str(error))
        finally:
            _schedule_dispatch()


def _add_handler(event, handler):
    event.add(handler)
    _handlers.append(handler)


class _Session:
    """State for one run of the command dialog."""

    def __init__(self):
        doc = _app.activeDocument
        self.document = doc
        self.design = adsk.fusion.Design.cast(doc.products.itemByProductType('DesignProductType'))
        self.cam = adsk.cam.CAM.cast(doc.products.itemByProductType('CAMProductType'))
        self.units = self.design.unitsManager
        self.setups = deburr_fusion.milling_setups(self.cam)
        self.ops = deburr_fusion.deburr_operations(self.setups)
        self.catalog = cam_tools.Catalog(_log)
        if self.cam:
            self.catalog.load_document(self.cam)
        self.local_loaded = False
        self.picked = []        # catalog entry ids chosen in the tool picker
        self.tool_choices = []  # [(label, tool, is_current_op_tool, catalog_id)]
        self.inputs = None
        self.edit_op = None
        self.edges_loaded = False
        self.job = None

    def fmt(self, cm):
        return self.units.formatValue(cm)

    def settings(self):
        return deburr_fusion.load_settings(self.edit_op) if self.edit_op else None


def _set_mode(inputs: adsk.core.CommandInputs, op):
    """Switch the dialog between creating a new operation and editing `op`."""
    s = _session
    s.edit_op = op
    settings = s.settings()

    inputs.itemById(IN_SETUP).isVisible = op is None
    _fill_tools(inputs)

    if settings:
        adsk.core.ValueCommandInput.cast(inputs.itemById(IN_WIDTH)).value = settings['width']
    # A new operation always starts with the checks on.
    skip = _skip_input(inputs)
    if skip:
        # Only a saved true: a hand-edited "false" or 1 must not turn checks off.
        skip.value = bool(settings) and settings.get(deburr_fusion.SKIP_GOUGE_KEY) is True


def _fill_tools(inputs: adsk.core.CommandInputs, select_id=None):
    """Dropdown: current op tool, supported document tools, then picked tools."""
    s = _session
    op = s.edit_op
    s.tool_choices = []
    if op and op.tool:
        s.tool_choices.append((f'Current: {op.tool.description}', op.tool, True, None))
    ids = [e['id'] for e in s.catalog.entries
           if e['source'] == 'document' and e['type'] in TOOL_TYPES] + s.picked
    for id_ in ids:
        e = s.catalog.entry(id_)
        s.tool_choices.append((f'{e["library"]}: {e["description"]}', s.catalog.tool(id_), False, id_))
    tool_dd = adsk.core.DropDownCommandInput.cast(inputs.itemById(IN_TOOL))
    ids = [c[3] for c in s.tool_choices]
    select_index = ids.index(select_id) if select_id in ids else 0
    tool_dd.listItems.clear()
    for i, (label, *_) in enumerate(s.tool_choices):
        tool_dd.listItems.add(label, i == select_index)
    _update_diameter(inputs)


def _open_picker():
    s = _session
    if not s.local_loaded:
        s.catalog.load_local()
        s.local_loaded = True
    tool_picker.open_picker(s.catalog, s.units, _on_tool_picked, TOOL_TYPES)


def _on_tool_picked(entry_id):
    """Called by the tool picker palette with the chosen catalog entry."""
    s = _session
    if not s:
        return
    if entry_id not in [c[3] for c in s.tool_choices]:
        s.picked.append(entry_id)
    _fill_tools(s.inputs, entry_id)


def _load_edges(inputs: adsk.core.CommandInputs):
    """Fill the edge selection from the edited operation. Not valid in commandCreated."""
    s = _session
    sel = adsk.core.SelectionCommandInput.cast(inputs.itemById(IN_EDGES))
    sel.clearSelection()
    settings = s.settings()
    if not settings:
        return
    missing = 0
    candidates = []

    def model_edges():
        # Read once, and only if a token fails to resolve.
        if not candidates:
            candidates.extend(deburr_fusion.setup_model_edges(s.edit_op.parentSetup))
        return candidates
    tokens = settings['edges']
    shapes = settings.get('edge_shapes') or []
    for i, token in enumerate(tokens):
        shape = shapes[i] if i < len(shapes) else None
        edge = deburr_fusion.find_edge(
            s.design, token, shape, model_edges,
            lambda: deburr_fusion.manufacturing_model_designs(s.design))
        if not (edge and sel.addSelection(edge)):
            missing += 1
    if missing:
        _ui.messageBox(f'{missing} saved edge(s) could not be found (the model may have changed). '
                       f'Reselect them before pressing OK.')


def _selected_tool(inputs, session=None):
    item = adsk.core.DropDownCommandInput.cast(inputs.itemById(IN_TOOL)).selectedItem
    return (session or _session).tool_choices[item.index] if item else None


def _update_diameter(inputs):
    text = adsk.core.TextBoxCommandInput.cast(inputs.itemById(IN_DIAMETER))
    choice = _selected_tool(inputs)
    if not choice:
        text.text = 'No tool selected'
        return
    tool = choice[1]
    text.text = _session.fmt(cam_tools.diameter_cm(tool))


class CommandCreatedHandler(adsk.core.CommandCreatedEventHandler):
    def notify(self, args: adsk.core.CommandCreatedEventArgs):
        global _session
        try:
            _session = s = _Session()
            if not s.setups:
                # No inputs created, so the command auto-executes (no-op) and closes.
                _ui.messageBox('Create a milling setup first.')
                return

            cmd = args.command
            _add_handler(cmd.execute, CommandExecuteHandler(s))
            _add_handler(cmd.activate, CommandActivateHandler())
            _add_handler(cmd.inputChanged, InputChangedHandler())
            _add_handler(cmd.destroy, CommandDestroyHandler(s))

            inputs = s.inputs = cmd.commandInputs
            dd_style = adsk.core.DropDownStyles.TextListDropDownStyle

            # Edit the deburr op that's selected (in the browser, or its path
            # sketch in the canvas), if any.
            preselected = 0
            for i in range(_ui.activeSelections.count):
                op = deburr_fusion.operation_for_entity(s.design, _ui.activeSelections.item(i).entity, s.ops)
                if op:
                    preselected = s.ops.index(op) + 1

            # Fusion's label column has a fixed width that doesn't grow with
            # the dialog, so labels are kept short enough to fit and tooltips
            # give the full name. Every row stays in the label/control grid
            # except the operation picker across the top.
            try:
                cmd.setDialogSize(DIALOG_WIDTH, 0)  # height 0: fit the inputs
            except RuntimeError:
                _log(f'Could not size the dialog:\n{traceback.format_exc()}')

            main = inputs.addTabCommandInput(IN_TAB_DEBURR, 'Deburr').children
            op_dd = main.addDropDownCommandInput(IN_OP, 'Operation', dd_style)
            op_dd.listItems.add(NEW_OP_LABEL, preselected == 0)
            for j, op in enumerate(s.ops):
                op_dd.listItems.add(f'Edit: {op.name} ({op.parentSetup.name})', preselected == j + 1)
            _describe(op_dd, 'Operation', 'Create a new Deburr operation, or edit an existing one.', True)

            setup_dd = main.addDropDownCommandInput(IN_SETUP, 'Setup', dd_style)
            for i, setup in enumerate(s.setups):
                setup_dd.listItems.add(setup.name, i == 0)
            _describe(setup_dd, 'Setup', 'The milling setup the new operation goes in.')

            edges = main.addSelectionInput(IN_EDGES, 'Edges', 'Select sharp convex edges to deburr')
            edges.addSelectionFilter('Edges')
            edges.setSelectionLimits(1, 0)
            _describe(edges, 'Edges', 'The sharp convex edges to break.')

            tool_dd = main.addDropDownCommandInput(IN_TOOL, 'Tool', dd_style)
            _describe(tool_dd, 'Deburr tool',
                      'A ball endmill, lollipop mill, chamfer mill, or single-profile thread mill.')
            pick = main.addBoolValueInput(IN_PICK, 'Library', False)
            pick.text = 'Select tool...'
            _describe(pick, 'Tool library', 'Pick a tool from a local or cloud tool library.')
            diameter = main.addTextBoxCommandInput(IN_DIAMETER, 'Dia.', '', 1, True)
            _describe(diameter, 'Tool diameter', 'The diameter of the selected tool.')

            units = s.units.defaultLengthUnits
            default_width = adsk.core.ValueInput.createByString(DEFAULT_WIDTH.get(units, DEFAULT_WIDTH_MM))
            width = main.addValueInput(IN_WIDTH, 'Width', units, default_width)
            _describe(width, 'Edge break width', 'How far the break cuts back each face from the edge.')

            # Like Fusion's own toolpaths, settings rarely changed go on a
            # separate tab. Inputs in tabs are still found by ID from `inputs`.
            checks = inputs.addTabCommandInput(IN_TAB_CHECKS, 'Checks').children
            group = checks.addGroupCommandInput(IN_GOUGE_GROUP, 'Gouge checks')
            group.isExpanded = True
            skip = group.children.addBoolValueInput(IN_SKIP_GOUGE, 'Skip', True, '', False)
            _describe(skip, 'Skip gouge checks', SKIP_GOUGE_TOOLTIP)

            _set_mode(inputs, s.ops[preselected - 1] if preselected else None)
        except:
            _ui.messageBox(f'CommandCreated failed:\n{traceback.format_exc()}')


class CommandActivateHandler(adsk.core.CommandEventHandler):
    def notify(self, args: adsk.core.CommandEventArgs):
        try:
            if _session.edit_op and not _session.edges_loaded:
                _session.edges_loaded = True
                _load_edges(args.command.commandInputs)
        except:
            _ui.messageBox(f'Activate failed:\n{traceback.format_exc()}')


class InputChangedHandler(adsk.core.InputChangedEventHandler):
    def notify(self, args: adsk.core.InputChangedEventArgs):
        try:
            # args.inputs holds only the changed input's tab; use the whole dialog.
            inputs = _session.inputs or args.inputs
            if args.input.id == IN_OP:
                index = adsk.core.DropDownCommandInput.cast(args.input).selectedItem.index
                _set_mode(inputs, _session.ops[index - 1] if index else None)
                _load_edges(inputs)
            elif args.input.id == IN_TOOL:
                _update_diameter(inputs)
            elif args.input.id == IN_PICK:
                _open_picker()
        except:
            _ui.messageBox(f'InputChanged failed:\n{traceback.format_exc()}')


class CommandExecuteHandler(adsk.core.CommandEventHandler):
    def __init__(self, session):
        super().__init__()
        self.session = session

    def notify(self, args: adsk.core.CommandEventArgs):
        self.session.job = None
        try:
            # Only copy inputs here. Mixing Design and CAM edits inside this
            # dialog's transaction can corrupt Fusion's command commit.
            self.session.job = _capture_job(args.command.commandInputs, self.session)
        except Exception as error:
            args.executeFailed = True
            args.executeFailedMessage = str(error)


class CommandDestroyHandler(adsk.core.CommandEventHandler):
    def __init__(self, session):
        super().__init__()
        self.session = session

    def notify(self, args: adsk.core.CommandEventArgs):
        global _session
        session = self.session
        if _session is session:
            tool_picker.close_picker()
            _session = None
        if session and session.job:
            _pending.append(session.job)
            session.job = None
            _schedule_dispatch()


def _unique_operation_name(base, cam):
    """Picks the operation name up front so the sketch can share it without a
    rename later (Fusion would otherwise suffix duplicates itself)."""
    names = {item.name for item in cam.allOperations}
    if base not in names:
        return base
    n = 2
    while f'{base} {n}' in names:
        n += 1
    return f'{base} {n}'


def _capture_job(inputs: adsk.core.CommandInputs, session):
    s = session
    choice = _selected_tool(inputs, s)
    if not choice:
        raise ValueError('No deburr tool selected. Use "Select tool..." to pick one.')
    _, tool, is_current_tool, _ = choice

    sel = adsk.core.SelectionCommandInput.cast(inputs.itemById(IN_EDGES))
    edges = [adsk.fusion.BRepEdge.cast(sel.selection(i).entity) for i in range(sel.selectionCount)]
    width = adsk.core.ValueCommandInput.cast(inputs.itemById(IN_WIDTH)).value  # cm
    if not math.isfinite(width) or width <= 0:
        raise ValueError('Edge break width must be positive and finite.')
    if not edges or any(edge is None for edge in edges):
        raise ValueError('Select sharp convex edges to deburr.')

    op = s.edit_op
    if op:
        setup = op.parentSetup
    else:
        setup_index = adsk.core.DropDownCommandInput.cast(inputs.itemById(IN_SETUP)).selectedItem.index
        setup = s.setups[setup_index]

    skip = _skip_input(inputs)
    # Only an explicit tick turns the checks off.
    skip = bool(skip) and skip.value is True
    return DeburrJob(s.document, setup, op,
                     tuple(edge.entityToken for edge in edges), width,
                     tool.toJson(), bool(op and is_current_tool), tuple(edges), bool(skip))


def _job_tool(cam, job):
    if job.keep_current_tool:
        return job.operation.tool
    snapshot = json.loads(job.tool_json)
    for tool in cam.documentToolLibrary:
        if json.loads(tool.toJson()) == snapshot:
            # Preserve Fusion's internal document-tool ID. Reimporting JSON
            # does not preserve that ID, even if its public GUID is unchanged.
            return tool
    # A library tool, or a library version of a tool whose document copy has
    # since been edited (same GUID, different definition). Import exactly what
    # was selected; Fusion keeps it as a separate document tool and leaves the
    # other operations on their own copy.
    return cam_tools.tool_from_json(job.tool_json)


def _job_edges(design, job):
    """The job's edges: by token, else the picked edge if it is still valid.

    Design.findEntityByToken does not resolve edges of linked (external
    reference) components or manufacturing models."""
    edges = []
    for i, token in enumerate(job.edge_tokens):
        edge = deburr_fusion.find_by_token(design, token, adsk.fusion.BRepEdge.cast)
        if edge is None and i < len(job.edges) and job.edges[i].isValid:
            edge = job.edges[i]
        if edge is None:
            raise dm.DeburrError('A selected edge no longer exists; reselect the edges.')
        edges.append(edge)
    return edges


def _apply_job(job):
    """Apply accepted input after the dialog's transaction has committed."""
    if not job.document.isValid or _app.activeDocument != job.document:
        raise dm.DeburrError('The active document changed; run Deburr again in the original document.')
    if not job.setup.isValid or (job.operation and not job.operation.isValid):
        raise dm.DeburrError('The selected setup or operation no longer exists.')
    design = adsk.fusion.Design.cast(job.document.products.itemByProductType('DesignProductType'))
    cam = adsk.cam.CAM.cast(job.document.products.itemByProductType('CAMProductType'))
    edges = _job_edges(design, job)
    op, setup, width = job.operation, job.setup, job.width
    if op and op.parentSetup != setup:
        raise dm.DeburrError('The operation moved to another setup; run Deburr again.')
    tool = _job_tool(cam, job)
    cutter = None
    kind = cam_tools.tool_type(tool)
    if kind in cam_tools.PROFILE_TYPES:
        cutter = cutter_profile.from_tool_json(json.loads(tool.toJson()))
        radius = cutter.radius
    else:
        radius = cam_tools.validate_ball_tool(tool)

    tool_axis = deburr_fusion.setup_tool_axis(setup)
    _log(f'Setup "{setup.name}", tool axis {tool_axis}, tool diameter '
         f'{cam_tools.diameter_expression(tool)} -> R={radius} cm, w={width} cm')

    check = not job.skip_gouge_checks
    if cutter is not None:
        bodies, fixtures = lollipop_fusion.setup_obstacles(setup, design)
        chains, used_edges, warnings = lollipop_fusion.build_chains(
            edges, cutter, width, tool_axis, bodies, fixtures, check)
    else:
        chains, used_edges, warnings = deburr_fusion.build_chains(
            edges, radius, width, tool_axis, _log, check)
    if not check:
        # Shown every time, so an edit can't regenerate unchecked unnoticed:
        # the setting is saved with the operation and sits on another tab.
        warnings = [SKIP_GOUGE_NOTICE] + warnings
    for msg in warnings:
        _log(msg)
    if not chains:
        raise dm.DeburrError('No usable edges:\n' + '\n'.join(warnings))

    # Make every design/operation change first and generate last; any change
    # after generation (even renaming the sketch) marks the toolpath outdated.
    if op:
        sketch = deburr_fusion.update_operation(
            design, op, chains, used_edges, width,
            None if job.keep_current_tool else tool, job.skip_gouge_checks)
        cam.generateToolpath(op)
        verb = 'Updated'
    else:
        name = _unique_operation_name(TYPE_NAMES.get(kind, DEFAULT_NAME), cam)
        sketch, curve_chains = deburr_fusion.build_sketch(design, chains, name)
        try:
            op = deburr_fusion.create_trace(setup, curve_chains, name, tool)
            deburr_fusion.save_settings(op, used_edges, width, sketch, job.skip_gouge_checks)
            cam.generateToolpath(op)
        except Exception:
            if op:
                op.deleteMe()
            sketch.deleteMe()
            raise
        verb = 'Created'

    summary = ', '.join(f'{len(c.pieces)} pieces{" (closed)" if c.closed else ""}' for c in chains)
    _log(f'{verb} "{op.name}": {len(chains)} chain(s) [{summary}], sketch "{sketch.name}"')
    if warnings:
        _ui.messageBox(f'{verb} "{op.name}" with {len(chains)} chain(s).\n\n' + '\n'.join(warnings))
