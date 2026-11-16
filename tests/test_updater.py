"""In-app updater: discovery, verification, staging, installing and rollback.

No network or Fusion: GitHub is a fake `get`, the add-in is a temporary folder.
"""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile, ZipInfo

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


updater = load('updater_under_test', ROOT / 'FusionFreeDeburr/updater.py')
release = load('release_for_updater', ROOT / 'tools/release.py')
ID = updater.ADDIN_ID
REPO = updater.REPOSITORY


def manifest(version, addin_id=ID):
    return json.dumps({'id': addin_id, 'version': version, 'supportedOS': 'windows|mac'})


def make_zip(version, files=None, addin_id=ID, extra=()):
    """A release ZIP laid out like tools/package.py builds it."""
    files = {'FusionFreeDeburr.py': f'VERSION = {version!r}\n', 'module.py': 'x = 1\n',
             'resources/page/index.html': '<html></html>', **(files or {})}
    buffer = io.BytesIO()
    with ZipFile(buffer, 'w') as archive:
        archive.writestr('FusionFreeDeburr/FusionFreeDeburr.manifest', manifest(version, addin_id))
        for name, text in files.items():
            archive.writestr(f'FusionFreeDeburr/{name}', text)
        archive.writestr('README.md', 'readme')
        archive.writestr('LICENSE', 'license')
        for entry in extra:
            archive.writestr(*entry)
    return buffer.getvalue()


def metadata(version, data, **overrides):
    name = f'Fusion-Free-Deburr-v{version}.zip'
    meta = {
        'schema_version': 1, 'version': version, 'channel': updater.default_channel(version),
        'addin_id': ID, 'supported_os': ['windows', 'mac'], 'installation_mode': 'automatic',
        'minimum_loader_version': 1, 'release_notes_url': f'https://github.com/{REPO}/releases/tag/v{version}',
        'asset': {'name': name, 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest(),
                  'url': f'https://github.com/{REPO}/releases/download/v{version}/{name}'},
    }
    meta.update(overrides)
    return meta


class Workspace(unittest.TestCase):
    """A temporary installed add-in folder and updater store."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.addin = self.tmp / 'AddIns' / 'FusionFreeDeburr'
        (self.addin / 'resources' / 'page').mkdir(parents=True)
        (self.addin / 'FusionFreeDeburr.manifest').write_text(manifest('0.1.1'))
        (self.addin / 'FusionFreeDeburr.py').write_text("VERSION = '0.1.1'\n")
        (self.addin / 'old_only.py').write_text('old = True\n')
        (self.addin / 'resources' / 'page' / 'index.html').write_text('<html>old</html>')
        (self.addin / '__pycache__').mkdir()
        (self.addin / '__pycache__' / 'module.cpython-312.pyc').write_bytes(b'stale')
        self.store = updater.Store(self.tmp / 'data')

    def stage(self, version, **kwargs):
        data = make_zip(version, **kwargs)
        updater.download_and_stage(self.store, metadata(version, data), get=lambda url, limit: data)

    def text(self, relative):
        return (self.addin / relative).read_text()


class VersionTests(unittest.TestCase):
    def test_version_order_matches_release_tool(self):
        versions = ['0.1.0-alpha', '0.1.0-alpha.2', '0.1.0-alpha.10', '0.1.0-nightly.0', '0.1.0-nightly.1',
                    '0.1.0', '0.1.1-nightly.0', '0.1.1', '0.2.0', '1.0.0']
        for a in versions:
            for b in versions:
                self.assertEqual(updater.version_key(a) < updater.version_key(b),
                                 release.version_key(a) < release.version_key(b), (a, b))
        self.assertEqual(sorted(versions, key=updater.version_key), versions)
        self.assertTrue(updater.is_newer('0.1.1', '0.1.1-nightly.4'))
        self.assertFalse(updater.is_newer('not-a-version', '0.1.0'))

    def test_release_metadata_is_installable_by_this_updater(self):
        """The release tool must emit what the updater accepts, or nobody updates."""
        source = (ROOT / 'tools/release.py').read_text()
        self.assertIn("'installation_mode': 'automatic'", source)
        self.assertIn(f"'minimum_loader_version': {updater.UPDATER_VERSION}", source)


class SettingsTests(Workspace):
    def test_defaults_and_channel_from_first_install(self):
        self.assertEqual(self.store.settings('0.1.1')['mode'], 'notify')
        self.assertEqual(self.store.settings('0.1.1')['channel'], 'stable')
        for version in ('0.1.0-alpha', '0.1.0-alpha.3', '0.1.0-test.1', 'garbage'):
            self.assertEqual(updater.default_channel(version), 'stable', version)
        nightly = updater.Store(self.tmp / 'other')
        self.assertEqual(nightly.settings('0.1.2-nightly.3')['channel'], 'nightly')
        # Once saved, a nightly user who updates to a stable release stays on nightly.
        self.assertEqual(nightly.settings('0.1.2')['channel'], 'nightly')

    def test_invalid_settings_are_corrected_and_rejected(self):
        self.store.settings_path.parent.mkdir(parents=True)
        self.store.settings_path.write_text('{"mode": "sometimes", "channel": 3, "last_check": "x"}')
        self.assertEqual(self.store.settings('0.1.1'), {'mode': 'notify', 'channel': 'stable',
                                                        'skipped_version': None, 'last_check': 0})
        self.store.settings_path.write_text('not json')
        self.assertEqual(self.store.settings('0.1.1')['mode'], 'notify')
        with self.assertRaises(ValueError):
            self.store.update_settings('0.1.1', mode='always')
        self.assertEqual(self.store.update_settings('0.1.1', mode='auto')['mode'], 'auto')


class DiscoveryTests(unittest.TestCase):
    def test_stable_uses_latest_redirect_only(self):
        data = make_zip('0.2.0')
        urls = []

        def get(url, limit):
            urls.append(url)
            return json.dumps(metadata('0.2.0', data)).encode()

        meta, notes = updater.latest_release('stable', get)
        self.assertEqual(meta['version'], '0.2.0')
        self.assertIsNone(notes)
        self.assertEqual(urls, [updater.STABLE_METADATA_URL])

    def test_nightly_takes_highest_published_release_across_pages(self):
        def record(tag, draft=False, assets=True):
            return {'tag_name': tag, 'draft': draft, 'body': f'notes {tag}',
                    'assets': [{'name': 'update.json', 'browser_download_url': f'https://github.com/meta/{tag}'}]
                    if assets else []}

        page1 = [record(f'v0.1.0-alpha.{n}') for n in range(100)]
        page2 = [record('v0.1.1'), record('v0.1.2-nightly.10'), record('v0.1.2-nightly.2'),
                 record('v9.0.0', draft=True), record('v8.0.0', assets=False), record('latest-build')]

        def get(url, limit):
            if url.startswith(updater.RELEASES_API):
                return json.dumps(page1 if url.endswith('page=1') else page2).encode()
            self.assertEqual(url, 'https://github.com/meta/v0.1.2-nightly.10')
            return json.dumps(metadata('0.1.2-nightly.10', b'zip')).encode()

        meta, notes = updater.latest_release('nightly', get)
        self.assertEqual((meta['version'], notes), ('0.1.2-nightly.10', 'notes v0.1.2-nightly.10'))

    def test_metadata_must_belong_to_this_project(self):
        good = metadata('0.2.0', b'zip')
        bad = [
            dict(good, addin_id='someone-else'),
            dict(good, schema_version=2),
            dict(good, version='v0.2.0'),
            dict(good, asset=dict(good['asset'], url='https://evil.example/Fusion-Free-Deburr-v0.2.0.zip')),
            dict(good, asset=dict(good['asset'], url='http://github.com' + good['asset']['url'][18:])),
            dict(good, asset=dict(good['asset'], url=good['asset']['url'].replace(REPO, 'fork/Fusion-Free-Deburr'))),
            dict(good, asset=dict(good['asset'], url=good['asset']['url'].replace('v0.2.0/', 'v0.1.0/'))),
            dict(good, asset=dict(good['asset'], sha256='abc')),
            dict(good, asset=dict(good['asset'], size=updater.MAX_ARCHIVE + 1)),
            {'version': '0.2.0'},
        ]
        updater.check_metadata(good)
        for meta in bad:
            with self.subTest(meta=meta), self.assertRaises(updater.UpdateError):
                updater.check_metadata(meta)

    def test_downloads_only_follow_https_github_redirects(self):
        handler = updater._Redirects()
        for url in ('http://github.com/x', 'https://example.com/x', 'file:///etc/passwd'):
            with self.subTest(url=url), self.assertRaises(updater.UpdateError):
                handler.redirect_request(None, None, 302, '', {}, url)
        with self.assertRaises(updater.UpdateError):
            updater.http_get('https://example.com/update.json', 10)

    def test_blockers(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        addin = tmp / 'FusionFreeDeburr'
        addin.mkdir()
        (addin / 'FusionFreeDeburr.manifest').write_text(manifest('0.1.1'))
        good = metadata('0.2.0', b'zip')
        self.assertIsNone(updater.install_blocker(good, addin))
        self.assertIn('by hand', updater.install_blocker(dict(good, installation_mode='manual'), addin))
        self.assertIn('newer updater', updater.install_blocker(dict(good, minimum_loader_version=2), addin))
        with patch.object(updater, 'current_os', return_value='mac'):
            self.assertIn('macOS', updater.install_blocker(dict(good, supported_os=['windows']), addin))
        (tmp / '.git').mkdir()
        self.assertIn('git', updater.install_blocker(good, addin))


class StagingTests(Workspace):
    def test_checksum_mismatch_is_discarded(self):
        data = make_zip('0.2.0')
        tampered = make_zip('0.2.0', files={'module.py': 'evil = 1\n'})
        with self.assertRaises(updater.UpdateError):
            updater.download_and_stage(self.store, metadata('0.2.0', data), get=lambda url, limit: tampered)
        self.assertIsNone(self.store.staged_version())

    def test_unsafe_or_wrong_archives_are_refused(self):
        link = ZipInfo('FusionFreeDeburr/link')
        link.external_attr = 0o120777 << 16
        cases = {
            'traversal': dict(extra=[('FusionFreeDeburr/../../escape.py', 'x')]),
            'absolute': dict(extra=[('/abs.py', 'x')]),
            'backslash': dict(extra=[('FusionFreeDeburr\\..\\x.py', 'x')]),
            'reserved': dict(extra=[('FusionFreeDeburr/CON.py', 'x')]),
            'other root': dict(extra=[('Other/thing.py', 'x')]),
            'link': dict(extra=[(link, 'target')]),
            'other add-in': dict(addin_id='someone-else'),
        }
        for name, kwargs in cases.items():
            with self.subTest(name), self.assertRaises(updater.UpdateError):
                self.stage('0.2.0', **kwargs)
        data = make_zip('0.3.0')
        with self.assertRaises(updater.UpdateError):  # manifest says 0.3.0, release says 0.2.0
            self.store.stage(data, '0.2.0')
        self.assertIsNone(self.store.staged_version())
        self.assertFalse((self.tmp / 'escape.py').exists())

    def test_restaging_replaces_previous_download(self):
        self.stage('0.2.0')
        self.stage('0.3.0')
        self.assertEqual(self.store.staged_version(), '0.3.0')
        self.store.discard_staged()
        self.assertIsNone(self.store.staged_version())
        self.assertIsNone(updater.apply_staged(self.store, self.addin))


class InstallTests(Workspace):
    def test_install_replaces_files_and_keeps_backup(self):
        (self.addin / 'my_notes.txt').write_text('mine')
        self.stage('0.2.0')
        self.assertEqual(updater.apply_staged(self.store, self.addin), '0.2.0')
        self.assertEqual(json.loads(self.text('FusionFreeDeburr.manifest'))['version'], '0.2.0')
        self.assertEqual(self.text('FusionFreeDeburr.py'), "VERSION = '0.2.0'\n")
        self.assertEqual(self.text('resources/page/index.html'), '<html></html>')
        self.assertTrue((self.addin / 'module.py').exists())
        # The first update didn't install these, so they are left alone.
        self.assertTrue((self.addin / 'old_only.py').exists())
        self.assertEqual(self.text('my_notes.txt'), 'mine')
        self.assertFalse((self.addin / '__pycache__').exists())
        self.assertFalse(list(self.addin.rglob('*.updating')))
        self.assertEqual(updater.read_manifest(self.store.backup_dir)['version'], '0.1.1')
        self.assertIsNone(self.store.staged_version())
        self.assertFalse(self.store.staged_dir.exists())

    def test_files_dropped_by_a_later_update_are_removed(self):
        self.stage('0.2.0')
        updater.apply_staged(self.store, self.addin)
        self.store.mark_started()
        data = make_zip('0.3.0')
        with ZipFile(io.BytesIO(data)) as source:
            names = [n for n in source.namelist() if not n.endswith('module.py')]
            buffer = io.BytesIO()
            with ZipFile(buffer, 'w') as target:
                for n in names:
                    target.writestr(n, source.read(n))
        data = buffer.getvalue()
        updater.download_and_stage(self.store, metadata('0.3.0', data), get=lambda url, limit: data)
        updater.apply_staged(self.store, self.addin)
        self.assertFalse((self.addin / 'module.py').exists())
        self.assertTrue((self.addin / 'old_only.py').exists())

    def test_failed_install_puts_old_files_back(self):
        self.stage('0.2.0')
        real_replace = os.replace
        calls = []

        def flaky(src, dst):
            calls.append(dst)
            if str(dst).endswith('module.py'):
                raise PermissionError('file in use')
            return real_replace(src, dst)

        with patch.object(updater.os, 'replace', side_effect=flaky):
            with self.assertRaises(updater.UpdateError):
                updater.apply_staged(self.store, self.addin)
        self.assertEqual(json.loads(self.text('FusionFreeDeburr.manifest'))['version'], '0.1.1')
        self.assertEqual(self.text('FusionFreeDeburr.py'), "VERSION = '0.1.1'\n")
        self.assertEqual(self.store.staged_version(), '0.2.0')  # retried at the next stop

    def test_git_checkout_is_never_overwritten(self):
        (self.addin.parent / '.git').mkdir()
        self.stage('0.2.0')
        self.assertIsNone(updater.apply_staged(self.store, self.addin))
        self.assertEqual(self.text('FusionFreeDeburr.py'), "VERSION = '0.1.1'\n")

    def test_damaged_staging_is_discarded(self):
        self.stage('0.2.0')
        (self.store.staged_dir / 'FusionFreeDeburr.manifest').write_text(manifest('0.9.0'))
        with self.assertRaises(updater.UpdateError):
            updater.apply_staged(self.store, self.addin)
        self.assertIsNone(self.store.staged_version())
        self.assertEqual(self.text('FusionFreeDeburr.py'), "VERSION = '0.1.1'\n")


class RollbackTests(Workspace):
    def test_failed_start_can_restore_previous_version(self):
        self.stage('0.2.0')
        updater.apply_staged(self.store, self.addin)
        self.assertEqual(self.store.rollback_candidate(), ('0.2.0', '0.1.1'))
        self.assertEqual(self.store.stage_rollback(), ('0.2.0', '0.1.1'))
        self.assertEqual(self.store.staged_version(), '0.1.1')
        self.assertEqual(updater.apply_staged(self.store, self.addin), '0.1.1')
        self.assertEqual(self.text('FusionFreeDeburr.py'), "VERSION = '0.1.1'\n")
        self.assertEqual(self.text('resources/page/index.html'), '<html>old</html>')
        self.assertFalse((self.addin / 'module.py').exists())  # only the broken update had it

    def test_no_rollback_once_started(self):
        self.stage('0.2.0')
        updater.apply_staged(self.store, self.addin)
        self.assertEqual(self.store.mark_started(), '0.2.0')
        self.assertIsNone(self.store.mark_started())
        self.assertIsNone(self.store.rollback_candidate())
        with self.assertRaises(updater.UpdateError):
            self.store.stage_rollback()



class ReviewRegressionTests(Workspace):
    def test_never_downgrades_a_newer_hand_install(self):
        self.stage('0.2.0')
        (self.addin / 'FusionFreeDeburr.manifest').write_text(manifest('0.3.0'))  # installed by hand meanwhile
        self.assertIsNone(updater.apply_staged(self.store, self.addin))
        self.assertIsNone(self.store.staged_version())
        self.assertEqual(json.loads(self.text('FusionFreeDeburr.manifest'))['version'], '0.3.0')

    def test_rollback_only_undoes_the_failed_version(self):
        self.stage('0.2.0')
        updater.apply_staged(self.store, self.addin)
        self.store.stage_rollback()
        (self.addin / 'FusionFreeDeburr.manifest').write_text(manifest('0.3.0'))
        self.assertIsNone(updater.apply_staged(self.store, self.addin))
        self.assertEqual(json.loads(self.text('FusionFreeDeburr.manifest'))['version'], '0.3.0')

    def test_rollback_does_not_adopt_user_files(self):
        (self.addin / 'my_notes.txt').write_text('mine')
        self.stage('0.2.0')
        updater.apply_staged(self.store, self.addin)
        self.store.stage_rollback()
        updater.apply_staged(self.store, self.addin)
        self.store.mark_started()
        self.stage('0.3.0')
        updater.apply_staged(self.store, self.addin)
        self.assertEqual(self.text('my_notes.txt'), 'mine')

    def test_interrupted_install_is_finished_at_next_start(self):
        self.stage('0.2.0')
        real_replace = os.replace

        def killed(src, dst):
            if str(dst).endswith('module.py'):
                raise SystemExit('Fusion killed')  # not an Exception: no restore runs
            return real_replace(src, dst)

        with patch.object(updater.os, 'replace', side_effect=killed), self.assertRaises(SystemExit):
            updater.apply_staged(self.store, self.addin)
        self.assertTrue(self.store.state().get('installing'))
        self.assertEqual(updater.resume_interrupted(self.store, self.addin), '0.2.0')
        self.assertEqual(self.text('FusionFreeDeburr.py'), "VERSION = '0.2.0'\n")
        self.assertTrue((self.addin / 'module.py').exists())
        self.assertFalse(self.store.state().get('installing'))
        self.assertEqual(updater.read_manifest(self.store.backup_dir)['version'], '0.1.1')
        self.assertIsNone(updater.resume_interrupted(self.store, self.addin))

    def test_failed_install_keeps_last_good_backup_and_gives_up_eventually(self):
        self.stage('0.2.0')
        updater.apply_staged(self.store, self.addin)
        self.store.mark_started()
        self.stage('0.3.0')
        real_replace = os.replace

        def flaky(src, dst):
            if str(dst).endswith('module.py') and 'AddIns' in str(dst):
                raise PermissionError('file in use')
            return real_replace(src, dst)

        for attempt in range(updater.MAX_INSTALL_FAILURES):
            with patch.object(updater.os, 'replace', side_effect=flaky), self.assertRaises(updater.UpdateError):
                updater.apply_staged(self.store, self.addin)
            self.assertEqual(updater.read_manifest(self.store.backup_dir)['version'], '0.1.1')
            self.assertEqual(json.loads(self.text('FusionFreeDeburr.manifest'))['version'], '0.2.0')
            self.assertFalse(list(self.addin.rglob('*.updating')))
        self.assertIsNone(self.store.staged_version())
        self.assertIn('discarded', self.store.state()['install_error'])

    def test_asset_name_must_be_the_release_zip(self):
        good = metadata('0.2.0', b'zip')
        name = '../../evil.zip'
        bad = dict(good, asset=dict(good['asset'], name=name,
                                    url=f'https://github.com/{REPO}/releases/download/v0.2.0/{name}'))
        with self.assertRaises(updater.UpdateError):
            updater.check_metadata(bad)


if __name__ == '__main__':
    unittest.main()
