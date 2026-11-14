"""Prevent dialog transactions from containing cross-product Design/CAM edits."""
from contextlib import ExitStack
import importlib
import json
import sys
import unittest
from types import SimpleNamespace as NS
from unittest.mock import MagicMock, patch

from test_safety import adsk, dm

with ExitStack() as stack:
    stack.enter_context(patch.dict(sys.modules, {'adsk': adsk, 'adsk.core': adsk.core,
                                                'adsk.fusion': adsk.fusion, 'adsk.cam': adsk.cam}))
    for name in ('ApplicationCommandEventHandler', 'CustomEventHandler', 'CommandEventHandler',
                 'CommandCreatedEventHandler', 'InputChangedEventHandler', 'HTMLEventHandler'):
        stack.enter_context(patch.object(adsk.core, name, object))
    cmd = importlib.import_module('safety_fusion.ball_deburr_command')


class CommandJobTests(unittest.TestCase):
    def setUp(self):
        self.session = NS(job=None)
        self.ui = MagicMock(activeCommand='SelectCommand')
        self.app = MagicMock()
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        for name, value in (('_app', self.app), ('_ui', self.ui), ('_session', self.session),
                            ('_dispatch_enabled', True), ('_dispatch_timer', None),
                            ('_log', MagicMock())):
            self.patches.enter_context(patch.object(cmd, name, value))
        cmd._pending.clear()
        self.addCleanup(cmd._pending.clear)

    def test_execute_only_captures_inputs_and_destroy_queues_once(self):
        job = object()
        with patch.object(cmd, '_capture_job', return_value=job), \
                patch.object(cmd, '_apply_job') as apply, \
                patch.object(cmd, '_schedule_dispatch') as schedule:
            cmd.CommandExecuteHandler(self.session).notify(NS(command=NS(commandInputs=object())))
            self.assertIs(self.session.job, job)
            self.assertEqual(len(cmd._pending), 0)
            apply.assert_not_called()
            schedule.assert_not_called()
            destroy = cmd.CommandDestroyHandler(self.session)
            destroy.notify(None)
            destroy.notify(None)
            self.assertEqual(list(cmd._pending), [job])
            schedule.assert_called_once()
            apply.assert_not_called()
            cmd.ApplyDeburrHandler().notify(None)
            apply.assert_called_once_with(job)
            self.assertEqual(len(cmd._pending), 0)

    def test_cancel_and_failed_capture_do_not_apply_work(self):
        with patch.object(cmd, '_schedule_dispatch') as schedule:
            cmd.CommandDestroyHandler(self.session).notify(None)
            schedule.assert_not_called()
        self.session.job = object()  # A failed retry must not retain an earlier capture.
        args = NS(command=NS(commandInputs=object()))
        with patch.object(cmd, '_capture_job', side_effect=ValueError('invalid width')):
            cmd.CommandExecuteHandler(self.session).notify(args)
        self.assertIsNone(self.session.job)
        self.assertTrue(args.executeFailed)
        self.assertEqual(args.executeFailedMessage, 'invalid width')

    def test_capture_keeps_tokens_and_values_after_inputs_are_destroyed(self):
        setup, doc = object(), object()
        op = NS(parentSetup=setup)
        session = NS(document=doc, edit_op=op)
        edges = [NS(entityToken='edge-a'), NS(entityToken='edge-b')]
        selection = NS(selectionCount=2, selection=lambda i: NS(entity=edges[i]))
        width = NS(value=.025)
        values = {cmd.IN_EDGES: selection, cmd.IN_WIDTH: width}
        inputs = NS(itemById=values.get)
        tool = NS(toJson=lambda: '{"type":"lollipop mill"}')
        with patch.object(cmd, '_selected_tool', return_value=('tool', tool, True, None)), \
                patch.object(adsk.core.SelectionCommandInput, 'cast', side_effect=lambda v: v), \
                patch.object(adsk.core.ValueCommandInput, 'cast', side_effect=lambda v: v), \
                patch.object(adsk.fusion.BRepEdge, 'cast', side_effect=lambda v: v):
            job = cmd._capture_job(inputs, session)
        values.clear()
        edges.clear()
        width.value = 10
        self.assertIs(job.document, doc)
        self.assertIs(job.operation, op)
        self.assertEqual(job.edge_tokens, ('edge-a', 'edge-b'))
        self.assertEqual(job.width, .025)
        self.assertTrue(job.keep_current_tool)

    def test_old_dialog_destroy_does_not_clear_new_session(self):
        older = NS(job=object())
        with patch.object(cmd, '_schedule_dispatch'), patch.object(cmd.tool_picker, 'close_picker') as close:
            cmd.CommandDestroyHandler(older).notify(None)
        self.assertIs(cmd._session, self.session)
        close.assert_not_called()

    def test_other_commands_keep_jobs_queued_until_termination(self):
        job = object()
        cmd._pending.append(job)
        self.ui.activeCommand = 'SketchLine'
        with patch.object(cmd, '_apply_job') as apply, \
                patch.object(cmd, '_schedule_dispatch') as schedule:
            cmd.ApplyDeburrHandler().notify(None)
            self.assertEqual(list(cmd._pending), [job])
            apply.assert_not_called()
            cmd.DeburrCommandTerminatedHandler().notify(None)
            schedule.assert_called_once()

    def test_worker_only_fires_event_and_never_changes_design(self):
        cmd._pending.append(object())
        with patch.object(cmd.threading, 'Timer') as timer, patch.object(cmd, '_apply_job') as apply:
            cmd._schedule_dispatch()
            worker_callback = timer.call_args.args[1]
            worker_callback()
            self.app.fireCustomEvent.assert_called_once_with(cmd.JOB_EVENT_ID)
            apply.assert_not_called()

    def test_stopping_cancels_timer_pending_work_and_event_handlers(self):
        cmd._pending.append(object())
        timer, event, terminated = MagicMock(), MagicMock(), MagicMock()
        with patch.object(cmd, '_dispatch_timer', timer), patch.object(cmd, '_job_event', event), \
                patch.object(cmd, '_terminated_handler', terminated), \
                patch.object(cmd, '_apply_job') as apply:
            cmd.stop()
            timer.cancel.assert_called_once()
            self.assertFalse(cmd._dispatch_enabled)
            self.assertEqual(len(cmd._pending), 0)
            self.ui.commandTerminated.remove.assert_called_once_with(terminated)
            self.app.unregisterCustomEvent.assert_called_once_with(cmd.JOB_EVENT_ID)
            cmd.ApplyDeburrHandler().notify(None)
            apply.assert_not_called()

    def test_changed_document_is_rejected_before_mutation(self):
        doc = NS(isValid=True)
        job = cmd.DeburrJob(doc, NS(isValid=True), None, ('edge',), .02, '{}', False)
        self.app.activeDocument = object()
        with patch.object(cmd.deburr_fusion, 'build_sketch') as build:
            with self.assertRaisesRegex(dm.DeburrError, 'active document changed'):
                cmd._apply_job(job)
            build.assert_not_called()

    def test_invalidated_operation_is_rejected_before_mutation(self):
        doc = NS(isValid=True)
        self.app.activeDocument = doc
        job = cmd.DeburrJob(doc, NS(isValid=True), NS(isValid=False), (), .02, '{}', True)
        with self.assertRaisesRegex(dm.DeburrError, 'no longer exists'):
            cmd._apply_job(job)

    def test_new_operation_reuses_the_existing_document_tool_object(self):
        raw = {'guid': 'tool-id', 'geometry': {'DC': 8}}
        tool = NS(toJson=lambda: json.dumps(raw))
        job = NS(keep_current_tool=False, tool_json=json.dumps(raw))
        with patch.object(adsk.cam.Tool, 'createFromJson') as create:
            self.assertIs(cmd._job_tool(NS(documentToolLibrary=[tool]), job), tool)
            create.assert_not_called()

    def test_external_library_tool_is_imported_only_when_not_in_document(self):
        raw = json.dumps({'guid': 'external-tool'})
        job = NS(keep_current_tool=False, tool_json=raw)
        with patch.object(adsk.cam.Tool, 'createFromJson') as create:
            self.assertIs(cmd._job_tool(NS(documentToolLibrary=[]), job), create.return_value)
            create.assert_called_once_with(raw)

    def test_document_tool_without_guid_is_reused_when_identical(self):
        raw = {'guid': '', 'type': 'thread mill', 'geometry': {'DC': .5}}
        other = NS(toJson=lambda: json.dumps({'guid': '', 'geometry': {'DC': .25}}))
        tool = NS(toJson=lambda: json.dumps(raw))
        job = NS(keep_current_tool=False, tool_json=json.dumps(raw))
        with patch.object(adsk.cam.Tool, 'createFromJson') as create:
            self.assertIs(cmd._job_tool(NS(documentToolLibrary=[other, tool]), job), tool)
            create.assert_not_called()

    def test_reimported_thread_mill_keeps_its_neck(self):
        raw = {'type': 'thread mill', 'unit': 'inches', 'geometry': {'shoulder-diameter': .25}}
        for got, repaired in ((-.5, True), (.25, False)):
            state = {'neck': got}

            class Param:
                expression = 'unchanged'

                def __setattr__(self, name, value):
                    object.__setattr__(self, name, value)
                    state['neck'] = float(value.split()[0])
            param = Param()
            imported = MagicMock()
            imported.toJson.side_effect = lambda: json.dumps({'geometry': {'shoulder-diameter': state['neck']}})
            imported.parameters.itemByName.return_value = param
            with self.subTest(got=got), patch.object(adsk.cam.Tool, 'createFromJson',
                                                     return_value=imported):
                self.assertIs(cmd.cam_tools.tool_from_json(json.dumps(raw)), imported)
                self.assertEqual(param.expression, '0.25 in' if repaired else 'unchanged')
        # A neck Fusion refuses to restore is an error, not a silently different tool.
        imported = MagicMock()
        imported.toJson.return_value = json.dumps({'geometry': {'shoulder-diameter': -.5}})
        with patch.object(adsk.cam.Tool, 'createFromJson', return_value=imported), \
                self.assertRaisesRegex(ValueError, 'neck diameter'):
            cmd.cam_tools.tool_from_json(json.dumps(raw))

    def test_changed_document_tool_is_not_silently_reimported(self):
        tool = NS(toJson=lambda: json.dumps({'guid': 'tool-id', 'geometry': {'DC': 10}}))
        job = NS(keep_current_tool=False,
                 tool_json=json.dumps({'guid': 'tool-id', 'geometry': {'DC': 8}}))
        with patch.object(adsk.cam.Tool, 'createFromJson') as create:
            with self.assertRaisesRegex(dm.DeburrError, 'selected tool changed'):
                cmd._job_tool(NS(documentToolLibrary=[tool]), job)
            create.assert_not_called()
