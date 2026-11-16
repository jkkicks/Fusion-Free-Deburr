"""Update flow in Fusion: which setting shows the window, downloads, or installs.

Fusion is mocked; background threads run inline and custom events are
delivered straight to the handler.
"""
import importlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from types import ModuleType, SimpleNamespace as NS
from unittest.mock import MagicMock, patch

from test_updater import make_zip, manifest, metadata

ROOT = Path(__file__).resolve().parents[1]
package = ModuleType('updater_fusion_pkg')
package.__path__ = [str(ROOT / 'FusionFreeDeburr')]
sys.modules['updater_fusion_pkg'] = package
adsk = MagicMock()
# Handlers subclass these; a MagicMock instance can't be a base class.
for _base in ('CustomEventHandler', 'CommandCreatedEventHandler', 'CommandEventHandler', 'HTMLEventHandler'):
    setattr(adsk.core, _base, object)
with patch.dict(sys.modules, {'adsk': adsk, 'adsk.core': adsk.core}):
    uf = importlib.import_module('updater_fusion_pkg.updater_fusion')
updater = uf.updater


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.addin = self.tmp / 'AddIns' / 'FusionFreeDeburr'
        self.addin.mkdir(parents=True)
        (self.addin / 'FusionFreeDeburr.manifest').write_text(manifest('0.1.1'))
        (self.addin / 'FusionFreeDeburr.py').write_text("VERSION = '0.1.1'\n")
        self.data = make_zip('0.2.0')
        self.meta = metadata('0.2.0', self.data)
        self.fetched = []

        def get(url, limit):
            self.fetched.append(url)
            if url == updater.STABLE_METADATA_URL:
                return json.dumps(self.meta).encode()
            if url.startswith(updater.RELEASES_API + '/tags/'):
                return json.dumps({'body': '## Fixes\n- fix: something'}).encode()
            if url == self.meta['asset']['url']:
                return self.data
            raise AssertionError(url)

        self.app, self.ui = MagicMock(), MagicMock()
        self.ui.palettes.itemById.return_value = None
        self.app.fireCustomEvent.side_effect = lambda event, info: uf._ResultHandler().notify(NS(additionalInfo=info))
        patches = [patch.object(uf, 'ADDIN_DIR', self.addin),
                   patch.object(updater, 'data_dir', lambda: self.tmp / 'data'),
                   patch.object(uf, '_spawn', lambda f, *a, delay=0: f(*a))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        # latest_release/release_notes take get as a default argument bound at import.
        for name in ('latest_release', 'release_notes', 'download_and_stage'):
            original = getattr(updater, name)
            p = patch.object(updater, name, lambda *a, _f=original, **k: _f(*a, get=get, **k))
            p.start()
            self.addCleanup(p.stop)
        self.store = updater.Store(self.tmp / 'data')
        self.addCleanup(uf.stop)  # as Fusion does on exit

    def start(self, mode):
        self.store.update_settings('0.1.1', mode=mode)
        uf.start(self.app, self.ui, lambda msg: None, [])

    def window_shown(self):
        return self.ui.palettes.add.called

    def test_notify_mode_shows_window_with_notes(self):
        self.start('notify')
        self.assertTrue(self.window_shown())
        state = json.loads(uf._payload())
        self.assertEqual((state['installed'], state['latest']['version']), ('0.1.1', '0.2.0'))
        self.assertIn('Fixes', state['latest']['notes'])
        self.assertIsNone(state['blocker'])
        self.assertIsNone(self.store.staged_version())

    def test_install_button_downloads_then_stop_installs(self):
        self.start('notify')
        handler = uf._IncomingHandler()
        args = NS(action='install', data='{}', returnData=None)
        handler.notify(args)
        self.assertEqual(json.loads(args.returnData)['staged'], '0.2.0')
        self.assertEqual(json.loads((self.addin / 'FusionFreeDeburr.manifest').read_text())['version'], '0.1.1')
        uf.stop()
        self.assertEqual(json.loads((self.addin / 'FusionFreeDeburr.manifest').read_text())['version'], '0.2.0')

    def test_auto_mode_downloads_without_asking(self):
        self.start('auto')
        self.assertFalse(self.window_shown())
        self.assertEqual(self.store.staged_version(), '0.2.0')

    def test_off_mode_never_checks(self):
        self.start('off')
        self.assertEqual(self.fetched, [])
        self.assertFalse(self.window_shown())

    def test_skipped_version_stays_quiet_until_a_newer_one(self):
        self.store.update_settings('0.1.1', skipped_version='0.2.0')
        self.start('notify')
        self.assertFalse(self.window_shown())
        self.store.update_settings('0.1.1', last_check=0)
        self.data = make_zip('0.3.0')
        self.meta = metadata('0.3.0', self.data)
        uf.start(self.app, self.ui, lambda msg: None, [])
        self.assertTrue(self.window_shown())

    def test_checks_at_most_daily(self):
        self.start('notify')
        count = len(self.fetched)
        uf.start(self.app, self.ui, lambda msg: None, [])
        self.assertEqual(len(self.fetched), count)

    def test_git_checkout_is_told_to_use_git(self):
        (self.addin.parent / '.git').mkdir()
        self.start('auto')
        self.assertFalse(self.window_shown())  # automatic checks don't interrupt developers
        self.assertIsNone(self.store.staged_version())
        uf._ExecuteHandler().notify(None)  # a manual check explains why it can't install
        self.assertTrue(self.window_shown())
        self.assertIn('git', json.loads(uf._payload())['blocker'])

    def test_changing_channel_rechecks(self):
        self.start('notify')
        handler = uf._IncomingHandler()
        with patch.object(updater, 'latest_release', return_value=(metadata('0.2.1-nightly.0', b'z'), 'n')) as latest:
            handler.notify(NS(action='settings', data='{"channel": "nightly"}', returnData=None))
        latest.assert_called_once_with('nightly')
        self.assertEqual(json.loads(uf._payload())['latest']['version'], '0.2.1-nightly.0')

    def test_offline_check_is_quiet_unless_asked(self):
        with patch.object(updater, 'latest_release', side_effect=updater.UpdateError('offline')):
            self.start('notify')
            self.assertFalse(self.window_shown())
            uf._ExecuteHandler().notify(None)
        self.assertEqual(json.loads(uf._payload())['error'], 'offline')
        self.assertTrue(self.window_shown())


    def test_results_from_before_a_stop_are_dropped(self):
        self.start('notify')
        self.ui.palettes.add.reset_mock()
        stale = uf._generation
        uf.stop()
        uf.start(self.app, self.ui, lambda msg: None, [])
        uf._post({'kind': 'checked', 'manual': True, 'channel': 'stable', 'meta': self.meta, 'notes': None}, stale)
        self.assertFalse(self.window_shown())

    def test_channel_change_during_a_check_rechecks(self):
        self.start('notify')
        self.store.update_settings('0.1.1', channel='nightly')
        with patch.object(updater, 'latest_release', return_value=(metadata('0.2.1-nightly.0', b'z'), 'n')) as latest:
            uf._ResultHandler().notify(NS(additionalInfo=json.dumps({
                'kind': 'checked', 'manual': True, 'channel': 'stable', 'meta': self.meta, 'notes': None,
                'generation': uf._generation})))
        latest.assert_called_once_with('nightly')
        self.assertEqual(json.loads(uf._payload())['latest']['version'], '0.2.1-nightly.0')

    def test_window_keeps_its_position_once_shown(self):
        self.start('notify')
        palette = self.ui.palettes.add.return_value
        self.ui.palettes.itemById.return_value = palette
        palette.setPosition.reset_mock()
        uf._ExecuteHandler().notify(None)
        palette.setPosition.assert_not_called()

    def test_rejected_setting_still_returns_saved_state(self):
        self.start('notify')
        args = NS(action='settings', data='{"mode": "sometimes"}', returnData=None)
        uf._IncomingHandler().notify(args)
        self.assertEqual(json.loads(args.returnData)['settings']['mode'], 'notify')

    def test_install_error_is_shown(self):
        self.start('notify')
        state = self.store.state()
        state['install_error'] = 'Installing v0.2.0 failed'
        self.store.save_state(state)
        self.assertEqual(json.loads(uf._payload())['error'], 'Installing v0.2.0 failed')


if __name__ == '__main__':
    unittest.main()
