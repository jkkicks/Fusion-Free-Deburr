"""Build canonical update assets and publish only a fully verified draft."""

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.error import HTTPError
from urllib.parse import quote, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
SEMVER = re.compile(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?')


def validate_version(version):
    """Reject unsupported SemVer forms before using a version in paths or URLs."""
    match = SEMVER.fullmatch(version)
    if not match or (match[4] and any(p.isdigit() and len(p) > 1 and p[0] == '0' for p in match[4].split('.'))):
        raise ValueError('Expected SemVer without v or build metadata')
    return version


def digest(data):
    return hashlib.sha256(data).hexdigest()


def build_assets(root, version, source_sha, repository):
    """Stamp the packaged version, restore source, and emit verified update assets."""
    validate_version(version)
    if not re.fullmatch(r'[0-9a-f]{40}', source_sha):
        raise ValueError('Source SHA must be a full commit ID')
    manifest_path = root / 'FusionFreeDeburr/FusionFreeDeburr.manifest'
    original = manifest_path.read_bytes()
    manifest = json.loads(original)
    manifest['version'] = version
    try:
        manifest_path.write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
        subprocess.run([sys.executable, str(root / 'tools/package.py')], check=True, cwd=root)
    finally:
        manifest_path.write_bytes(original)
    output = root / 'dist'
    payload = output / f'Fusion-Free-Deburr-v{version}.zip'
    with ZipFile(payload) as archive:
        packed = json.loads(archive.read('FusionFreeDeburr/FusionFreeDeburr.manifest'))
        if packed['version'] != version or archive.testzip() is not None:
            raise ValueError('Packaged manifest/version or archive integrity check failed')
        for theme in ('', '-dark'):
            for size in (16, 32, 64):
                name = f'FusionFreeDeburr/resources/BallDeburr/{size}x{size}{theme}.png'
                from PIL import Image
                with archive.open(name) as icon, Image.open(icon) as image:
                    if image.size != (size, size):
                        raise ValueError(f'Unexpected icon dimensions: {name}')
    data = payload.read_bytes()
    release_url = f'https://github.com/{repository}/releases/tag/v{version}'
    metadata = {
        'schema_version': 1, 'version': version,
        'channel': 'preview' if '-' in version else 'stable',
        'source_sha': source_sha, 'addin_id': manifest['id'],
        'supported_os': ['windows'],
        # No loader exists in legacy installations. They must install manually once.
        'installation_mode': 'manual', 'minimum_loader_version': 1,
        'release_notes_url': release_url,
        'asset': {'name': payload.name, 'size': len(data), 'sha256': digest(data),
                  'url': f'https://github.com/{repository}/releases/download/v{version}/{payload.name}'},
    }
    update = output / 'update.json'
    update.write_text(json.dumps(metadata, indent=2) + '\n', encoding='utf-8')
    sums = output / 'SHA256SUMS'
    sums.write_text(''.join(f'{digest(p.read_bytes())}  {p.name}\n' for p in (payload, update)), encoding='utf-8')
    return [payload, update, sums]


class AssetRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        """Allow HTTPS redirects without forwarding credentials to asset hosts."""
        if urlparse(newurl).scheme != 'https':
            raise ValueError('Refusing an insecure asset redirect')
        redirected = super().redirect_request(request, fp, code, msg, headers, newurl)
        if urlparse(request.full_url).netloc != urlparse(newurl).netloc:
            redirected.remove_header('Authorization')
        return redirected


class GitHub:
    def __init__(self, repository, token):
        self.base = f'https://api.github.com/repos/{repository}'
        self.token = token
        self.opener = build_opener(AssetRedirects())

    def releases(self):
        """Yield all visible releases, including authenticated drafts, across pages."""
        page = 1
        while True:
            batch = self.request(f'/releases?per_page=100&page={page}')
            yield from batch
            if len(batch) < 100:
                break
            page += 1

    def find_release(self, tag):
        """Resolve one draft or published release even when its tag is absent."""
        # The get-by-tag API is for published releases. Authenticated listing
        # also includes drafts whose tags may not exist yet.
        matches = [record for record in self.releases() if record['tag_name'] == tag]
        if len(matches) != 1:
            raise ValueError(f'Expected exactly one release/draft for {tag}, found {len(matches)}')
        return matches[0]

    def already_published(self, sha):
        """Prevent CI reruns from releasing an already published source commit."""
        return any(not record['draft'] and record['target_commitish'] == sha for record in self.releases())

    def request(self, path, method='GET', data=None, raw=False, content_type='application/json'):
        """Send authenticated GitHub API requests or download raw asset bytes."""
        url = path if path.startswith('https://') else self.base + path
        if urlparse(url).netloc not in ('api.github.com', 'uploads.github.com'):
            raise ValueError('Unexpected GitHub API or upload host')
        headers = {'Authorization': f'Bearer {self.token}', 'Accept': 'application/octet-stream' if raw else 'application/vnd.github+json',
                   'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'Fusion-Free-Deburr-release'}
        if data is not None:
            headers['Content-Type'] = content_type
            if not isinstance(data, bytes):
                data = json.dumps(data).encode()
        with self.opener.open(Request(url, data=data, method=method, headers=headers), timeout=120) as response:
            body = response.read()
            return body if raw else (json.loads(body) if body else None)


def publish_assets(api, release, files, sha, publish):
    """Bind a draft to source, verify every remote asset, then optionally publish."""
    if not release['draft']:
        raise ValueError('Refusing to modify a published release')
    target = release['target_commitish']
    if target != sha and re.fullmatch(r'[0-9a-f]{40}', target):
        raise ValueError('Draft already belongs to a different source commit')
    # A pre-existing tag must point to this exact source, including annotated tags.
    tag = quote(release['tag_name'], safe='')
    try:
        commit = api.request(f'/commits/{tag}')
    except HTTPError as error:
        if error.code != 404:
            raise
    else:
        if commit['sha'] != sha:
            raise ValueError('Existing tag points to another commit')
    rid = release['id']
    api.request(f'/releases/{rid}', 'PATCH', {'target_commitish': sha})
    assets = []
    page = 1
    while True:
        batch = api.request(f'/releases/{rid}/assets?per_page=100&page={page}')
        assets.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    expected = {p.name: p for p in files}
    if set(a['name'] for a in assets) - expected.keys():
        raise ValueError('Unexpected draft assets; review/remove them before retrying')
    for path in files:
        data = path.read_bytes()
        existing = next((a for a in assets if a['name'] == path.name), None)
        if existing:
            uploaded = existing
        else:
            upload_url = release['upload_url'].split('{')[0] + '?name=' + quote(path.name)
            uploaded = api.request(upload_url, 'POST', data, content_type='application/octet-stream')
        received = api.request(uploaded['url'], raw=True, content_type='application/octet-stream')
        if received != data:
            raise ValueError(f'Uploaded asset mismatch: {path.name}; draft left unpublished')
    if publish:
        api.request(f'/releases/{rid}', 'PATCH', {'draft': False, 'make_latest': 'false' if release['prerelease'] else 'true'})


def main():
    """Check source eligibility or build and publish the selected draft release."""
    sha = os.environ['SOURCE_SHA']
    actual = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    if actual != sha:
        raise ValueError('Checkout does not match requested source SHA')
    repository = os.environ['GITHUB_REPOSITORY']
    api = GitHub(repository, os.environ['GH_TOKEN'])
    if sys.argv[1:] == ['--check-source']:
        proceed = not api.already_published(sha)
        with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as output:
            output.write(f'proceed={str(proceed).lower()}\n')
        print('Source is eligible for drafting' if proceed else 'Source is already published; skipping')
        return
    version = validate_version(os.environ['RELEASE_VERSION'].removeprefix('v'))
    # Require drafted notes; this script never invents a release or version.
    release = api.find_release('v' + version)
    if release['prerelease'] != ('-' in version):
        raise ValueError('Draft prerelease flag disagrees with version')
    if not release['draft']:
        raise ValueError('Version is already published; choose a new version')
    files = build_assets(ROOT, version, sha, repository)
    publish_assets(api, release, files, sha, os.environ.get('PUBLISH_RELEASE') == 'true')


if __name__ == '__main__':
    main()
