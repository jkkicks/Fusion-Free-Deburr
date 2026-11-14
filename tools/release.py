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


def version_key(version):
    """Sort key giving SemVer 2.0.0 precedence, so updaters and releases agree on "newer"."""
    major, minor, patch, pre = SEMVER.fullmatch(validate_version(version)).groups()
    # A release outranks its prereleases. Numeric identifiers sort numerically
    # and below alphanumeric ones; a longer identifier list wins a shared prefix.
    identifiers = tuple((0, int(p), '') if p.isdigit() else (1, 0, p) for p in pre.split('.')) if pre else ()
    return int(major), int(minor), int(patch), not pre, identifiers


def channel_for(version):
    """Stable versions have no prerelease part; every prerelease is a nightly build."""
    return 'nightly' if SEMVER.fullmatch(validate_version(version))[4] else 'stable'


def require_newer(version, published_tags):
    """Every release must outrank all earlier ones, or updaters could miss it."""
    for tag in published_tags:
        try:
            older = version_key(tag.removeprefix('v'))
        except ValueError:
            continue  # tags that are not v-prefixed SemVer are not update candidates
        if version_key(version) <= older:
            raise ValueError(f'v{version} is not newer than published {tag}; choose a higher version')


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
        'channel': channel_for(version),
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

    def already_published(self, sha, stable_only=False):
        """Detect a source commit that already has a published (optionally stable) release."""
        return any(not record['draft'] and record['target_commitish'] == sha
                   and not (stable_only and record['prerelease']) for record in self.releases())

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


def tag_commit(api, name):
    """Return the commit a tag resolves to, or None when the tag does not exist.

    Only the exact-ref endpoint's 404 means "missing"; GitHub answers 422 to
    /commits/<unknown>, and any other error must stay visible.
    """
    try:
        obj = api.request(f'/git/ref/tags/{quote(name, safe="")}')['object']
    except HTTPError as error:
        if error.code != 404:
            raise
        return None
    for _ in range(10):  # annotated tags may nest; bound the walk
        if obj['type'] == 'commit':
            return obj['sha']
        if obj['type'] != 'tag':
            raise ValueError(f"Tag {name} points to a {obj['type']}, not a commit")
        obj = api.request(f"/git/tags/{obj['sha']}")['object']
    raise ValueError(f'Tag {name} has too many nested annotations')


def verify_published(api, rid, name, sha):
    """Fail loudly if the published release is not bound to its real tag and source."""
    published = api.request(f'/releases/{rid}')
    if published['draft'] or published['tag_name'] != name:
        raise ValueError(f"Published release has tag {published['tag_name']!r}, expected {name!r}; repair it before use")
    if tag_commit(api, name) != sha:
        raise ValueError(f'Tag {name} does not point at the release source commit')


def publish_assets(api, release, files, sha, publish):
    """Bind a draft to source, verify every remote asset, then optionally publish."""
    if not release['draft']:
        raise ValueError('Refusing to modify a published release')
    target = release['target_commitish']
    if target != sha and re.fullmatch(r'[0-9a-f]{40}', target):
        raise ValueError('Draft already belongs to a different source commit')
    # A pre-existing tag must point to this exact source, including annotated tags.
    existing = tag_commit(api, release['tag_name'])
    if existing is not None and existing != sha:
        raise ValueError('Existing tag points to another commit')
    rid = release['id']
    name = release['tag_name']
    # Retargeting a draft whose tag does not exist yet makes GitHub swap its
    # tag_name for an "untagged-..." placeholder, so always resend the name.
    api.request(f'/releases/{rid}', 'PATCH', {'tag_name': name, 'target_commitish': sha})
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
        api.request(f'/releases/{rid}', 'PATCH', {'tag_name': name, 'target_commitish': sha, 'draft': False,
                                                  'make_latest': 'false' if release['prerelease'] else 'true'})
        verify_published(api, rid, name, sha)


def main():
    """Check source eligibility or build and publish the selected draft release."""
    sha = os.environ['SOURCE_SHA']
    actual = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    if actual != sha:
        raise ValueError('Checkout does not match requested source SHA')
    repository = os.environ['GITHUB_REPOSITORY']
    api = GitHub(repository, os.environ['GH_TOKEN'])
    if sys.argv[1:] == ['--check-source']:
        # Nightly: nothing new since the last build of any kind, so skip quietly.
        # Stable: someone asked for it, so a commit that is already stable fails.
        if os.environ.get('RELEASE_CHANNEL') == 'stable':
            if api.already_published(sha, stable_only=True):
                raise ValueError('This commit is already a stable release')
            proceed = True
        else:
            proceed = not api.already_published(sha)
        with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as output:
            output.write(f'proceed={str(proceed).lower()}\n')
        print('Source is eligible for release' if proceed else 'Source is already published; skipping')
        return
    version = validate_version(os.environ['RELEASE_VERSION'].removeprefix('v'))
    # Require drafted notes; this script never invents a release or version.
    release = api.find_release('v' + version)
    if release['prerelease'] != ('-' in version):
        raise ValueError('Draft prerelease flag disagrees with version')
    if not release['draft']:
        raise ValueError('Version is already published; choose a new version')
    require_newer(version, [r['tag_name'] for r in api.releases() if not r['draft']])
    files = build_assets(ROOT, version, sha, repository)
    publish_assets(api, release, files, sha, os.environ.get('PUBLISH_RELEASE') == 'true')


if __name__ == '__main__':
    main()
