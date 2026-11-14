"""Publication must never expose missing, mismatched, or replaced assets."""
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request

SPEC = importlib.util.spec_from_file_location('release', Path(__file__).resolve().parents[1] / 'tools/release.py')
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)
SHA = 'a' * 40


class FakeGitHub:
    def __init__(self, corrupt_asset=None, tag_sha=SHA, annotated=False, publish_tag=None):
        self.calls = []
        self.corrupt_asset = corrupt_asset
        self.tag_sha = tag_sha
        self.annotated = annotated
        self.publish_tag = publish_tag
        self.published = False
        self.payloads = {}

    def request(self, path, method='GET', data=None, **kwargs):
        """Keep each uploaded asset independent and optionally corrupt one download."""
        self.calls.append((path, method, data))
        if method == 'PATCH' and data.get('draft') is False:
            self.published = True
            self.tag_sha = self.tag_sha or SHA  # publishing creates the missing tag
        if path == '/releases/1' and method == 'GET':
            return {'draft': not self.published, 'tag_name': self.publish_tag or 'v0.2.0-alpha.1'}
        if path.startswith('/commits/'):
            # Real GitHub answers 422 here for unknown tag names; never use it.
            raise HTTPError(path, 422, 'Unprocessable Entity', {}, None)
        if path.startswith('/git/ref/tags/'):
            if self.tag_sha is None:
                raise HTTPError(path, 404, 'Not Found', {}, None)
            if self.annotated:
                return {'object': {'type': 'tag', 'sha': 't' * 40}}
            return {'object': {'type': 'commit', 'sha': self.tag_sha}}
        if path.startswith('/git/tags/'):
            return {'object': {'type': 'commit', 'sha': self.tag_sha}}
        if '/assets?' in path:
            return []
        if method == 'POST':
            name = parse_qs(urlparse(path).query)['name'][0]
            download = '/download/' + name
            self.payloads[download] = data
            return {'url': download}
        if path in self.payloads:
            return b'corrupt' if path == '/download/' + str(self.corrupt_asset) else self.payloads[path]
        return {}


class ReleaseTests(unittest.TestCase):
    def test_find_draft_without_a_tag_and_paginate(self):
        """Draft discovery spans pages, and drafts do not count as publications."""
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
        """Asset redirects require HTTPS and remove credentials across hosts."""
        request = Request('https://api.github.com/repos/test/test/releases/assets/1', headers={'Authorization': 'Bearer private'})
        redirected = release.AssetRedirects().redirect_request(request, None, 302, 'Found', {}, 'https://release-assets.githubusercontent.com/file')
        self.assertIsNone(redirected.get_header('Authorization'))
        with self.assertRaises(ValueError):
            release.AssetRedirects().redirect_request(request, None, 302, 'Found', {}, 'http://example.com/file')

    def test_version_validation(self):
        """Accept supported SemVer releases and reject malformed or unsafe names."""
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
        """Corruption in any asset, including later assets, prevents publication."""
        for count in (1, 3):
            for corrupt_index in (None, *range(count)):
                with self.subTest(count=count, corrupt_index=corrupt_index), tempfile.TemporaryDirectory() as directory:
                    files = [Path(directory) / name for name in ('payload.zip', 'update.json', 'SHA256SUMS')[:count]]
                    for path in files:
                        path.write_bytes(('payload for ' + path.name).encode())
                    corrupt_name = files[corrupt_index].name if corrupt_index is not None else None
                    api = FakeGitHub(corrupt_asset=corrupt_name)
                    if corrupt_index is not None:
                        with self.assertRaisesRegex(ValueError, corrupt_name):
                            release.publish_assets(api, self.draft(), files, SHA, True)
                    else:
                        release.publish_assets(api, self.draft(), files, SHA, True)
                    published = [data for _, method, data in api.calls if method == 'PATCH' and 'draft' in data]
                    self.assertEqual(bool(published), corrupt_index is None)
                    downloads = [url for url, method, _ in api.calls if method == 'GET' and url.startswith('/download/')]
                    self.assertEqual(len(downloads), count if corrupt_index is None else corrupt_index + 1)

    def test_published_release_and_wrong_tag_rejected(self):
        """Published releases and tags for another source must remain untouched."""
        for draft, tag_sha in ((False, SHA), (True, 'b' * 40)):
            record = self.draft()
            record['draft'] = draft
            api = FakeGitHub(tag_sha=tag_sha)
            with self.assertRaises(ValueError):
                release.publish_assets(api, record, [], SHA, True)
            self.assertFalse(any(method != 'GET' for _, method, _ in api.calls))

    def test_draft_without_tag_can_publish(self):
        """Draft-first publication works before GitHub has created the tag."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'payload.zip'
            path.write_bytes(b'payload')
            api = FakeGitHub(tag_sha=None)
            release.publish_assets(api, self.draft(), [path], SHA, True)
            self.assertTrue(any(method == 'PATCH' and data.get('draft') is False for _, method, data in api.calls))

    def test_publish_keeps_the_real_tag_name(self):
        """Every PATCH resends tag_name, and a placeholder tag after publishing is fatal."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'payload.zip'
            path.write_bytes(b'payload')
            api = FakeGitHub(tag_sha=None)
            release.publish_assets(api, self.draft(), [path], SHA, True)
            patches = [data for _, method, data in api.calls if method == 'PATCH']
            self.assertEqual(len(patches), 2)
            for data in patches:
                self.assertEqual((data['tag_name'], data['target_commitish']), ('v0.2.0-alpha.1', SHA))
            api = FakeGitHub(tag_sha=None, publish_tag='untagged-abc123')
            with self.assertRaisesRegex(ValueError, 'untagged-abc123'):
                release.publish_assets(api, self.draft(), [path], SHA, True)

    def test_annotated_tags_resolve_to_their_commit(self):
        """Annotated tags are accepted only when they peel to the source commit."""
        for tag_sha, accepted in ((SHA, True), ('b' * 40, False)):
            with self.subTest(tag_sha=tag_sha), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'payload.zip'
                path.write_bytes(b'payload')
                api = FakeGitHub(tag_sha=tag_sha, annotated=True)
                if accepted:
                    release.publish_assets(api, self.draft(), [path], SHA, True)
                    self.assertTrue(any(method == 'PATCH' and data.get('draft') is False for _, method, data in api.calls))
                else:
                    with self.assertRaisesRegex(ValueError, 'another commit'):
                        release.publish_assets(api, self.draft(), [path], SHA, True)
                    self.assertFalse(any(method != 'GET' for _, method, _ in api.calls))

    def test_tag_lookup_only_treats_404_as_missing(self):
        """Other API errors, including validation failures, must stop publication."""
        class Failing:
            def __init__(self, code):
                self.code = code
            def request(self, path, **kwargs):
                raise HTTPError(path, self.code, 'error', {}, None)
        self.assertIsNone(release.tag_commit(Failing(404), 'v1.0.0'))
        for code in (401, 403, 422, 500):
            with self.subTest(code=code), self.assertRaises(HTTPError):
                release.tag_commit(Failing(code), 'v1.0.0')
        class Blob:
            def request(self, path, **kwargs):
                return {'object': {'type': 'tree', 'sha': SHA}}
        with self.assertRaisesRegex(ValueError, 'not a commit'):
            release.tag_commit(Blob(), 'v1.0.0')

    def test_existing_draft_assets_must_match_on_retry(self):
        """A retry reuses matching assets and rejects changed bytes without upload."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'payload.zip'
            path.write_bytes(b'payload')
            for remote in (b'payload', b'different'):
                api = FakeGitHub()
                original = api.request
                def request(url, method='GET', data=None, **kwargs):
                    """Serve an existing asset to exercise resumable publication."""
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
        """Version stamping must not leave source changes after packaging fails."""
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
        """Build real installation assets in isolation from checkout artifacts."""
        source = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(source / 'FusionFreeDeburr', root / 'FusionFreeDeburr')
            (root / 'tools').mkdir()
            for name in ('tools/package.py', 'README.md', 'LICENSE'):
                shutil.copyfile(source / name, root / name)
            self.check_real_build(root)

    def check_real_build(self, root):
        """Check payload identity, metadata, checksums and repeated-build bytes."""
        manifest = root / 'FusionFreeDeburr/FusionFreeDeburr.manifest'
        original = manifest.read_bytes()
        files = release.build_assets(root, '0.2.0-alpha.1', SHA, 'jkkicks/Fusion-Free-Deburr')
        first = files[0].read_bytes()
        metadata = json.loads(files[1].read_bytes())
        self.assertEqual(metadata['asset']['sha256'], release.digest(first))
        self.assertEqual(metadata['asset']['size'], len(first))
        self.assertEqual(metadata['channel'], 'preview')
        declared = json.loads(manifest.read_text(encoding='utf-8'))['supportedOS'].split('|')
        self.assertEqual(metadata['supported_os'], declared)
        self.assertEqual(files[2].read_text(encoding='utf-8').splitlines(), [
            f'{release.digest(first)}  {files[0].name}',
            f'{release.digest(files[1].read_bytes())}  {files[1].name}',
        ])
        files = release.build_assets(root, '0.2.0-alpha.1', SHA, 'jkkicks/Fusion-Free-Deburr')
        self.assertEqual(first, files[0].read_bytes())
        self.assertEqual(manifest.read_bytes(), original)
