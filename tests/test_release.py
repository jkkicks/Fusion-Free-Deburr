"""Publication must never expose missing, mismatched, or replaced assets."""
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request

SPEC = importlib.util.spec_from_file_location('release', Path(__file__).resolve().parents[1] / 'tools/release.py')
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)
SHA = 'a' * 40


class FakeGitHub:
    def __init__(self, corrupt=False, tag_sha=SHA):
        self.calls = []
        self.corrupt = corrupt
        self.tag_sha = tag_sha

    def request(self, path, method='GET', data=None, **kwargs):
        self.calls.append((path, method, data))
        if path.startswith('/commits/'):
            if self.tag_sha is None:
                raise HTTPError(path, 404, 'Tag not created yet', {}, None)
            return {'sha': self.tag_sha}
        if '/assets?' in path:
            return []
        if method == 'POST':
            self.data = data
            return {'url': '/download'}
        if path == '/download':
            return b'corrupt' if self.corrupt else self.data
        return {}


class ReleaseTests(unittest.TestCase):
    def test_find_draft_without_a_tag_and_paginate(self):
        api = release.GitHub('test/test', 'unused')
        first = [{'draft': False, 'tag_name': f'v1.0.{i}', 'target_commitish': 'b' * 40} for i in range(100)]
        draft = {'draft': True, 'tag_name': 'v2.0.0-alpha.1', 'target_commitish': SHA}
        with patch.object(api, 'request', side_effect=[first, [draft]]) as request:
            self.assertEqual(api.find_release(draft['tag_name']), draft)
            self.assertIn('page=2', request.call_args.args[0])
        with patch.object(api, 'releases', return_value=iter([draft])):
            self.assertFalse(api.already_published(SHA))
        with patch.object(api, 'releases', return_value=iter([dict(draft, draft=False)])):
            self.assertTrue(api.already_published(SHA))

    def test_redirect_does_not_forward_token_to_asset_host(self):
        request = Request('https://api.github.com/repos/test/test/releases/assets/1', headers={'Authorization': 'Bearer private'})
        redirected = release.AssetRedirects().redirect_request(request, None, 302, 'Found', {}, 'https://release-assets.githubusercontent.com/file')
        self.assertIsNone(redirected.get_header('Authorization'))
        with self.assertRaises(ValueError):
            release.AssetRedirects().redirect_request(request, None, 302, 'Found', {}, 'http://example.com/file')

    def test_version_validation(self):
        for version in ('0.1.0-alpha', '0.2.0-alpha.10', '1.0.0'):
            self.assertEqual(release.validate_version(version), version)
        for version in ('v1.0.0', '../evil', '1.0', '01.0.0', '1.0.0-alpha.01', '1.0.0+local'):
            with self.subTest(version=version), self.assertRaises(ValueError):
                release.validate_version(version)

    def draft(self):
        return {'draft': True, 'id': 1, 'target_commitish': SHA,
                'tag_name': 'v0.2.0-alpha.1', 'prerelease': True,
                'upload_url': 'https://uploads.github.com/test{?name}'}

    def test_publish_only_after_asset_verification(self):
        for corrupt in (False, True):
            with self.subTest(corrupt=corrupt), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'test.zip'
                path.write_bytes(b'payload')
                api = FakeGitHub(corrupt=corrupt)
                if corrupt:
                    with self.assertRaises(ValueError):
                        release.publish_assets(api, self.draft(), [path], SHA, True)
                else:
                    release.publish_assets(api, self.draft(), [path], SHA, True)
                published = [data for _, method, data in api.calls if method == 'PATCH' and 'draft' in data]
                self.assertEqual(bool(published), not corrupt)

    def test_published_release_and_wrong_tag_rejected(self):
        for draft, tag_sha in ((False, SHA), (True, 'b' * 40)):
            record = self.draft()
            record['draft'] = draft
            api = FakeGitHub(tag_sha=tag_sha)
            with self.assertRaises(ValueError):
                release.publish_assets(api, record, [], SHA, True)
            self.assertFalse(any(method != 'GET' for _, method, _ in api.calls))

    def test_draft_without_tag_can_publish(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'payload.zip'
            path.write_bytes(b'payload')
            api = FakeGitHub(tag_sha=None)
            release.publish_assets(api, self.draft(), [path], SHA, True)
            self.assertEqual(api.calls[-1][2]['draft'], False)

    def test_existing_draft_assets_must_match_on_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'payload.zip'
            path.write_bytes(b'payload')
            for remote in (b'payload', b'different'):
                api = FakeGitHub()
                original = api.request
                def request(url, method='GET', data=None, **kwargs):
                    if '/assets?' in url:
                        return [{'name': path.name, 'url': '/download'}]
                    if url == '/download':
                        return remote
                    return original(url, method, data, **kwargs)
                api.request = request
                if remote == b'payload':
                    release.publish_assets(api, self.draft(), [path], SHA, True)
                else:
                    with self.assertRaisesRegex(ValueError, 'mismatch'):
                        release.publish_assets(api, self.draft(), [path], SHA, True)
                self.assertFalse(any(method == 'POST' for _, method, _ in api.calls))

    def test_manifest_restored_after_failed_build(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / 'FusionFreeDeburr/FusionFreeDeburr.manifest'
            manifest.parent.mkdir()
            original = b'{"version":"0.1.0-alpha", "id":"test"}\n'
            manifest.write_bytes(original)
            with patch.object(release.subprocess, 'run', side_effect=RuntimeError('build failed')):
                with self.assertRaises(RuntimeError):
                    release.build_assets(root, '0.2.0-alpha.1', SHA, 'test/test')
            self.assertEqual(manifest.read_bytes(), original)

    def test_real_build_metadata_and_repeatability(self):
        source = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(source / 'FusionFreeDeburr', root / 'FusionFreeDeburr')
            (root / 'tools').mkdir()
            for name in ('tools/package.py', 'README.md', 'LICENSE'):
                shutil.copyfile(source / name, root / name)
            self.check_real_build(root)

    def check_real_build(self, root):
        manifest = root / 'FusionFreeDeburr/FusionFreeDeburr.manifest'
        original = manifest.read_bytes()
        files = release.build_assets(root, '0.2.0-alpha.1', SHA, 'jkkicks/Fusion-Free-Deburr')
        first = files[0].read_bytes()
        metadata = json.loads(files[1].read_bytes())
        self.assertEqual(metadata['asset']['sha256'], release.digest(first))
        self.assertEqual(metadata['asset']['size'], len(first))
        self.assertEqual(metadata['channel'], 'preview')
        files = release.build_assets(root, '0.2.0-alpha.1', SHA, 'jkkicks/Fusion-Free-Deburr')
        self.assertEqual(first, files[0].read_bytes())
        self.assertEqual(manifest.read_bytes(), original)
