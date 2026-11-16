"""In-app updates: settings, release discovery, download, staging and installing.

No Fusion imports, so it can be unit tested. Fusion wiring lives in
updater_fusion.py.

Updates install in place. A release ZIP is downloaded, checked against the
size and SHA-256 in its update.json, and extracted to a staging folder outside
the add-in. Its files replace the add-in's files only when the add-in stops
(Fusion closing, or Stop in Scripts and Add-Ins), never while a command runs.
The replaced files are kept, so a version that fails to start can be restored.
"""

import filecmp
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import tempfile
from contextlib import nullcontext
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zipfile import BadZipFile, ZipFile

REPOSITORY = 'jkkicks/Fusion-Free-Deburr'
ADDIN_ID = '969d0d30-1695-489b-9fd9-aaa5c91226dc'
ADDIN_FOLDER = 'FusionFreeDeburr'
MANIFEST = 'FusionFreeDeburr.manifest'
# Releases whose update.json asks for a newer minimum_loader_version than this
# are announced but must be installed by hand.
UPDATER_VERSION = 1
STABLE_METADATA_URL = f'https://github.com/{REPOSITORY}/releases/latest/download/update.json'
RELEASES_API = f'https://api.github.com/repos/{REPOSITORY}/releases'
RELEASES_PAGE = f'https://github.com/{REPOSITORY}/releases'
# GitHub redirects release downloads to its asset hosts.
ALLOWED_HOSTS = {'github.com', 'api.github.com', 'objects.githubusercontent.com',
                 'release-assets.githubusercontent.com'}

MODES = ('auto', 'notify', 'off')
CHANNELS = ('stable', 'nightly')
CHECK_INTERVAL = 20 * 3600  # automatic checks at most about once a day
TIMEOUT = 20
MAX_METADATA = 256 * 1024
MAX_API = 4 * 1024 * 1024
MAX_ARCHIVE = 50 * 1024 * 1024
MAX_EXPANDED = 150 * 1024 * 1024
MAX_ENTRIES = 2000
MAX_INSTALL_FAILURES = 3   # then the staged update is dropped
PENDING_BACKUP = '.backup-pending'

SEMVER = re.compile(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?')


class UpdateError(Exception):
    """A check, download or install failed; the message is shown to the user."""


# ---- Versions --------------------------------------------------------------

def validate_version(version):
    """Same rules as tools/release.py: SemVer without v or build metadata."""
    match = SEMVER.fullmatch(version) if isinstance(version, str) else None
    if not match or (match[4] and any(p.isdigit() and len(p) > 1 and p[0] == '0' for p in match[4].split('.'))):
        raise ValueError(f'Not a release version: {version!r}')
    return version


def version_key(version):
    """SemVer precedence; must agree with version_key() in tools/release.py."""
    major, minor, patch, pre = SEMVER.fullmatch(validate_version(version)).groups()
    identifiers = tuple((0, int(p), '') if p.isdigit() else (1, 0, p) for p in pre.split('.')) if pre else ()
    return int(major), int(minor), int(patch), not pre, identifiers


def is_newer(candidate, current):
    try:
        return version_key(candidate) > version_key(current)
    except ValueError:
        return False


def default_channel(version):
    """Stable, unless the first build installed was a nightly (e.g. 0.1.2-nightly.3).

    Other prereleases, such as the source manifest's 0.1.0-alpha in a
    development checkout, also get stable.
    """
    try:
        pre = SEMVER.fullmatch(validate_version(version))[4]
    except ValueError:
        return 'stable'
    return 'nightly' if pre and pre.split('.')[0] == 'nightly' else 'stable'


def current_os():
    return 'mac' if sys.platform == 'darwin' else 'windows'


def read_manifest(folder):
    try:
        return json.loads((Path(folder) / MANIFEST).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


# ---- Storage ---------------------------------------------------------------

def data_dir():
    """Per-user folder outside the add-in, so settings survive updates."""
    if sys.platform == 'darwin':
        base = Path.home() / 'Library' / 'Application Support'
    else:
        base = Path(os.environ.get('LOCALAPPDATA') or os.environ.get('APPDATA') or Path.home())
    return base / 'FusionFreeDeburr' / 'updater'


def write_json(path, value):
    """Write atomically so a crash never leaves half a settings/state file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.tmp-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _read_json(path):
    try:
        value = json.loads(Path(path).read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


class Store:
    """Settings, update state, the staged update and the backup of replaced files."""

    def __init__(self, root):
        self.root = Path(root)
        self.settings_path = self.root / 'settings.json'
        self.state_path = self.root / 'state.json'
        self.staged_dir = self.root / 'staged'
        self.backup_dir = self.root / 'backup'

    # Settings: mode, channel, skipped_version, last_check.
    def settings(self, installed_version):
        saved = _read_json(self.settings_path)
        settings = {
            'mode': saved.get('mode') if saved.get('mode') in MODES else 'notify',
            # Set from the build first installed, then only by the user.
            'channel': saved.get('channel') if saved.get('channel') in CHANNELS else default_channel(installed_version),
            'skipped_version': saved.get('skipped_version') if isinstance(saved.get('skipped_version'), str) else None,
            'last_check': saved.get('last_check') if isinstance(saved.get('last_check'), (int, float)) else 0,
        }
        if settings != saved:
            write_json(self.settings_path, settings)
        return settings

    def update_settings(self, installed_version, **changes):
        settings = self.settings(installed_version)
        if 'mode' in changes and changes['mode'] not in MODES:
            raise ValueError('Unknown update mode')
        if 'channel' in changes and changes['channel'] not in CHANNELS:
            raise ValueError('Unknown update channel')
        settings.update(changes)
        write_json(self.settings_path, settings)
        return settings

    # State (state.json):
    #   staged, staged_kind ('update' or 'rollback'), staged_replaces (the
    #   version a rollback undoes), staged_tracked (files a rollback restores
    #   as updater-installed); installing (journal of an install in progress);
    #   installed_files and backup_installed_files (files the updater put in the
    #   add-in folder and in the backup, the only files it may delete);
    #   applied (last install, and whether it has started); install_error.
    def state(self):
        state = _read_json(self.state_path)
        staged = state.get('staged')
        if staged is not None and not (isinstance(staged, str) and SEMVER.fullmatch(staged)
                                       and (self.staged_dir / MANIFEST).is_file()):
            state['staged'] = None
        return state

    def save_state(self, state):
        write_json(self.state_path, state)

    def staged_version(self):
        return self.state().get('staged')

    def staged_is_rollback(self):
        state = self.state()
        return bool(state.get('staged')) and state.get('staged_kind') == 'rollback'

    def cleanup(self):
        """Remove temporary folders left by a crash (not one an install still needs)."""
        if not self.root.is_dir():
            return
        for path in self.root.glob('.staging-*'):
            shutil.rmtree(path, ignore_errors=True)
        if not self.state().get('installing'):
            shutil.rmtree(self.root / PENDING_BACKUP, ignore_errors=True)

    def stage(self, data, version):
        """Validate a release ZIP and extract its add-in folder as the pending update."""
        validate_version(version)
        temporary = Path(tempfile.mkdtemp(prefix='.staging-', dir=_mkdir(self.root)))
        try:
            extract_addin(data, temporary)
            manifest = read_manifest(temporary)
            if not manifest or manifest.get('id') != ADDIN_ID or manifest.get('version') != version:
                raise UpdateError('The downloaded add-in does not match the release it came from')
            if not (temporary / 'FusionFreeDeburr.py').is_file():
                raise UpdateError('The downloaded add-in is incomplete')
            self._replace_staged(temporary, version)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)

    def _replace_staged(self, folder, version, kind='update', replaces=None, tracked=None):
        state = self.state()
        if state.get('installing'):
            raise UpdateError('An interrupted install must finish first')
        state['staged'] = None
        self.save_state(state)
        if self.staged_dir.exists():
            shutil.rmtree(self.staged_dir)
        os.replace(folder, self.staged_dir)
        state.update(staged=version, staged_kind=kind, staged_replaces=replaces, staged_tracked=tracked,
                     install_error=None, install_failures=0)
        self.save_state(state)

    def discard_staged(self):
        state = self.state()
        if state.get('installing'):
            return  # its files are still needed to finish the install
        state.update(staged=None, staged_kind=None, staged_replaces=None, staged_tracked=None)
        self.save_state(state)
        if self.staged_dir.exists():
            shutil.rmtree(self.staged_dir, ignore_errors=True)

    def mark_started(self):
        """Record that the installed version started; returns it if it was just installed."""
        state = self.state()
        applied = state.get('applied')
        if isinstance(applied, dict) and not applied.get('started'):
            applied['started'] = True
            self.save_state(state)
            return applied.get('version')
        return None

    def rollback_candidate(self):
        """(failed version, version to restore) when the last install never started."""
        applied = self.state().get('applied')
        backup = read_manifest(self.backup_dir)
        if (isinstance(applied, dict) and not applied.get('started') and backup
                and backup.get('id') == ADDIN_ID and isinstance(backup.get('version'), str)):
            return applied.get('version'), backup['version']
        return None

    def stage_rollback(self):
        """Stage the backed-up files so the next stop puts them back."""
        candidate = self.rollback_candidate()
        if not candidate:
            raise UpdateError('There is no earlier version to restore')
        temporary = Path(tempfile.mkdtemp(prefix='.staging-', dir=self.root))
        try:
            shutil.rmtree(temporary)
            shutil.copytree(self.backup_dir, temporary)
            # The backup holds every file that was in the folder, including the
            # user's own; only the ones an update installed stay tracked.
            self._replace_staged(temporary, candidate[1], 'rollback', replaces=candidate[0],
                                 tracked=self.state().get('backup_installed_files') or [])
        finally:
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)
        return candidate


def _mkdir(path):
    Path(path).mkdir(parents=True, exist_ok=True)
    return path


# ---- Network ---------------------------------------------------------------

def check_url(url):
    parsed = urlparse(url)
    if parsed.scheme != 'https' or parsed.hostname not in ALLOWED_HOSTS:
        raise UpdateError(f'Refusing to download from {parsed.scheme}://{parsed.hostname}')
    return url


class _Redirects(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        check_url(newurl)
        return super().redirect_request(request, fp, code, msg, headers, newurl)


def http_get(url, limit):
    """GET over HTTPS from GitHub only, with a timeout and a size limit."""
    opener = build_opener(_Redirects())
    request = Request(check_url(url), headers={
        'User-Agent': 'Fusion-Free-Deburr-updater',
        'Accept': 'application/vnd.github+json' if url.startswith('https://api.github.com/') else '*/*',
    })
    try:
        with opener.open(request, timeout=TIMEOUT) as response:
            data = response.read(limit + 1)
    except UpdateError:
        raise
    except Exception as error:
        raise UpdateError(f'Could not reach GitHub: {error}') from error
    if len(data) > limit:
        raise UpdateError('The download is larger than expected')
    return data


def _json(data):
    try:
        return json.loads(data)
    except ValueError as error:
        raise UpdateError('GitHub returned unreadable update information') from error


# ---- Release discovery -----------------------------------------------------

def check_metadata(meta):
    """Validate update.json (schema 1) before trusting anything in it."""
    try:
        if meta['schema_version'] != 1 or meta['addin_id'] != ADDIN_ID:
            raise UpdateError('The update information is for a different add-in or format')
        version = validate_version(meta['version'])
        asset = meta['asset']
        if not (isinstance(asset['size'], int) and 0 < asset['size'] <= MAX_ARCHIVE):
            raise UpdateError('The update has an unexpected size')
        if not re.fullmatch(r'[0-9a-f]{64}', asset['sha256']):
            raise UpdateError('The update has no valid checksum')
        url = urlparse(asset['url'])
        if (asset['name'] != f'Fusion-Free-Deburr-v{version}.zip' or url.scheme != 'https'
                or url.hostname != 'github.com' or url.query or url.fragment
                or url.path != f'/{REPOSITORY}/releases/download/v{version}/{asset["name"]}'):
            raise UpdateError('The update points outside this project\'s releases')
        if not isinstance(meta.get('supported_os', []), list):
            raise UpdateError('The update information is malformed')
    except (KeyError, TypeError, ValueError) as error:
        raise UpdateError('The update information is malformed') from error
    return meta


def latest_release(channel, get=http_get):
    """Newest release for a channel: (update.json, release notes or None).

    Stable uses GitHub's Latest redirect, which never points at a nightly.
    Nightly takes the highest version across both tracks, so testers also get
    each stable release.
    """
    if channel == 'stable':
        return check_metadata(_json(get(STABLE_METADATA_URL, MAX_METADATA))), None
    best = None
    for page in range(1, 11):
        batch = _json(get(f'{RELEASES_API}?per_page=100&page={page}', MAX_API))
        if not isinstance(batch, list):
            raise UpdateError('GitHub returned unreadable release information')
        for release in batch:
            tag = release.get('tag_name') or ''
            asset = next((a for a in release.get('assets') or [] if a.get('name') == 'update.json'), None)
            if release.get('draft') or not tag.startswith('v') or not asset:
                continue
            try:
                key = version_key(tag[1:])
            except ValueError:
                continue
            if best is None or key > best[0]:
                best = (key, release, asset)
        if len(batch) < 100:
            break
    if best is None:
        raise UpdateError('No releases with update information were found')
    _, release, asset = best
    meta = check_metadata(_json(get(asset['browser_download_url'], MAX_METADATA)))
    if meta['version'] != release['tag_name'][1:]:
        raise UpdateError('The release and its update information disagree')
    return meta, release.get('body') or ''


def release_notes(version, get=http_get):
    """Notes for one release; best effort, as the API has a low anonymous rate limit."""
    try:
        return _json(get(f'{RELEASES_API}/tags/v{validate_version(version)}', MAX_API)).get('body') or ''
    except Exception:
        return None


def install_blocker(meta, addin_dir):
    """Why this release can't be installed here automatically, or None."""
    os_name = current_os()
    if os_name not in meta.get('supported_os', [os_name]):
        return f'This version does not support {"macOS" if os_name == "mac" else "Windows"}.'
    if meta.get('installation_mode') != 'automatic':
        return 'This version has to be installed by hand.'
    if not isinstance(meta.get('minimum_loader_version'), int) or meta['minimum_loader_version'] > UPDATER_VERSION:
        return 'This version needs a newer updater, so it has to be installed by hand once.'
    return folder_blocker(addin_dir)


def is_checkout(addin_dir):
    addin_dir = Path(addin_dir)
    return (addin_dir / '.git').exists() or (addin_dir.parent / '.git').exists()


def folder_blocker(addin_dir):
    addin_dir = Path(addin_dir)
    if is_checkout(addin_dir):
        return 'This copy runs from a git checkout. Update it with git instead.'
    manifest = read_manifest(addin_dir)
    if not manifest or manifest.get('id') != ADDIN_ID:
        return 'The add-in folder could not be identified.'
    if not os.access(addin_dir, os.W_OK):
        return 'The add-in folder is read-only.'
    return None


def download_and_stage(store, meta, get=http_get, lock=None):
    """Download the release ZIP, verify it, and stage it for the next stop.

    `lock` is held only while staging, not during the download.
    """
    asset = check_metadata(meta)['asset']
    data = get(asset['url'], MAX_ARCHIVE)
    if len(data) != asset['size'] or hashlib.sha256(data).hexdigest() != asset['sha256']:
        raise UpdateError('The download did not match the published checksum, so it was discarded')
    with lock or nullcontext():
        store.stage(data, meta['version'])


# ---- Archives --------------------------------------------------------------

_RESERVED = re.compile(r'(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', re.I)


def _safe_parts(name):
    path = PurePosixPath(name)
    parts = name.rstrip('/').split('/')
    if (not name or '\\' in name or '\x00' in name or path.is_absolute() or any(not p for p in parts)
            or any(p in ('.', '..') or ':' in p or p.endswith((' ', '.')) or _RESERVED.fullmatch(p)
                   for p in parts)):
        raise UpdateError('The download contains an unsafe file path')
    return parts


def extract_addin(data, destination):
    """Extract only the FusionFreeDeburr/ folder of a release ZIP, refusing anything unsafe."""
    try:
        archive = ZipFile(io.BytesIO(data))
    except BadZipFile as error:
        raise UpdateError('The download is not a valid ZIP file') from error
    with archive:
        entries = archive.infolist()
        if len(entries) > MAX_ENTRIES or sum(e.file_size for e in entries) > MAX_EXPANDED:
            raise UpdateError('The download is larger than expected')
        seen = set()
        for entry in entries:
            parts = _safe_parts(entry.filename)
            if stat.S_ISLNK(entry.external_attr >> 16):
                raise UpdateError('The download contains a link')
            folded = '/'.join(parts).casefold()
            if folded in seen:
                raise UpdateError('The download contains duplicate files')
            seen.add(folded)
            if parts[0] != ADDIN_FOLDER:
                if len(parts) == 1:
                    continue  # README.md, LICENSE beside the add-in folder
                raise UpdateError('The download has an unexpected layout')
            if entry.is_dir() or len(parts) == 1:
                continue
            target = Path(destination).joinpath(*parts[1:])
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(entry) as source, target.open('xb') as output:
                shutil.copyfileobj(source, output)


# ---- Installing ------------------------------------------------------------

def _files(folder):
    """Relative POSIX paths of the files in an add-in folder, minus caches and dot folders."""
    folder = Path(folder)
    found = []
    for path in folder.rglob('*'):
        relative = path.relative_to(folder)
        if any(p.startswith('.') or p == '__pycache__' for p in relative.parts):
            continue
        if path.is_file() and not path.is_symlink():
            found.append(relative.as_posix())
    return sorted(found)


def _copy_into(source, files, destination):
    """Replace each file atomically: write and flush beside it, then rename over it."""
    for relative in files:
        target = Path(destination).joinpath(*relative.split('/'))
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + '.updating')
        with Path(source).joinpath(*relative.split('/')).open('rb') as src, temporary.open('wb') as out:
            shutil.copyfileobj(src, out)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, target)


def _same(a, b, relative):
    parts = relative.split('/')
    target = Path(b).joinpath(*parts)
    return target.is_file() and filecmp.cmp(Path(a).joinpath(*parts), target, shallow=False)


def _remove_caches(folder):
    for cache in Path(folder).rglob('__pycache__'):
        shutil.rmtree(cache, ignore_errors=True)
    for leftover in Path(folder).rglob('*.updating'):
        try:
            leftover.unlink()
        except OSError:
            pass


def apply_staged(store, addin_dir):
    """Install the staged update over the add-in folder. Call only while stopping,
    or at startup before the add-in's other modules are imported.

    Returns the installed version, or None if there was nothing to do. An
    interrupted install (Fusion killed part-way) is finished first. On failure
    the replaced files are put back and UpdateError is raised.
    """
    addin_dir = Path(addin_dir)
    state = store.state()
    if state.get('installing'):
        return _resume(store, addin_dir, state, state['installing'])
    version = state.get('staged')
    if not version or folder_blocker(addin_dir):
        return None
    staged = read_manifest(store.staged_dir)
    if not staged or staged.get('id') != ADDIN_ID or staged.get('version') != version:
        store.discard_staged()
        raise UpdateError('The downloaded update was damaged and has been discarded')
    current = (read_manifest(addin_dir) or {}).get('version')
    kind = state.get('staged_kind') or 'update'
    # Never downgrade: the user may have installed this or a newer version by
    # hand. A rollback only undoes the exact version it was made for.
    if (kind == 'update' and not is_newer(version, current or '0.0.0')) or (
            kind == 'rollback' and current != state.get('staged_replaces')):
        store.discard_staged()
        return None

    new_files = _files(store.staged_dir)
    old_files = _files(addin_dir)
    installed = state.get('installed_files') or []
    # Copy the current files aside first. They become the backup only once the
    # install succeeds, so a failed install never loses the last good backup.
    pending = store.root / PENDING_BACKUP
    shutil.rmtree(pending, ignore_errors=True)
    _copy_into(addin_dir, old_files, pending)
    journal = {
        'version': version, 'kind': kind, 'previous': current, 'addin_dir': str(addin_dir),
        'files': new_files, 'old_files': old_files, 'attempts': 0,
        # Only delete files a previous update installed; never the user's own.
        'remove': sorted(set(installed) - set(new_files)),
        'tracked': (state.get('staged_tracked') or []) if kind == 'rollback' else new_files,
        'backup_tracked': installed,
    }
    state['installing'] = journal
    store.save_state(state)
    return _finish(store, addin_dir, state, journal)


def _resume(store, addin_dir, state, journal):
    """Finish an interrupted install, unless the folder has changed since."""
    current = (read_manifest(addin_dir) or {}).get('version')
    if journal.get('addin_dir') != str(addin_dir) or current not in (journal['previous'], journal['version']):
        # Installed by hand (or moved) since: never overwrite that with the staged files.
        _abandon(store, state)
        return None
    journal['attempts'] = int(journal.get('attempts') or 0) + 1
    store.save_state(state)
    if journal['attempts'] > MAX_INSTALL_FAILURES:
        # Give up, but keep Deburr off until the folder changes (a reinstall),
        # because it may still hold a mix of two versions.
        _abandon(store, state, blocked=_fingerprint(addin_dir))
        raise UpdateError(f'Installing v{journal["version"]} could not be finished')
    return _finish(store, addin_dir, state, journal)


def _abandon(store, state, blocked=None):
    state.update(installing=None, blocked=blocked)
    store.save_state(state)
    store.discard_staged()
    shutil.rmtree(store.root / PENDING_BACKUP, ignore_errors=True)


def _fingerprint(addin_dir):
    digest = hashlib.sha256()
    for relative in _files(addin_dir):
        digest.update(relative.encode() + b'\0')
        digest.update(Path(addin_dir).joinpath(*relative.split('/')).read_bytes())
    return digest.hexdigest()


def _finish(store, addin_dir, state, journal):
    """Copy the staged files in (again, if resuming). Idempotent until the state is saved."""
    pending = store.root / PENDING_BACKUP
    try:
        if not (store.staged_dir / MANIFEST).is_file():
            raise UpdateError('The staged files are missing')
        _copy_into(store.staged_dir, journal['files'], addin_dir)
        for relative in journal['remove']:
            target = addin_dir.joinpath(*_safe_parts(relative))
            if target.is_file():
                target.unlink()
        _remove_caches(addin_dir)
    except Exception as error:
        _restore(store, addin_dir, state, journal, error)
    _swap_backup(pending, store.backup_dir)
    state.update(staged=None, staged_kind=None, staged_replaces=None, staged_tracked=None, installing=None,
                 install_error=None, install_failures=0,
                 installed_files=journal['tracked'], backup_installed_files=journal['backup_tracked'],
                 applied={'version': journal['version'], 'previous': journal['previous'], 'started': False})
    store.save_state(state)
    shutil.rmtree(store.staged_dir, ignore_errors=True)
    return journal['version']


def _swap_backup(pending, backup):
    old = backup.with_name(backup.name + '-old')
    if not pending.exists():
        # A run killed before saving the state already swapped it in.
        shutil.rmtree(old, ignore_errors=True)
        return
    shutil.rmtree(old, ignore_errors=True)
    if backup.exists():
        os.replace(backup, old)
    os.replace(pending, backup)
    shutil.rmtree(old, ignore_errors=True)


def _restore(store, addin_dir, state, journal, error):
    """Put the pre-install files back after a failed install, then raise."""
    pending = store.root / PENDING_BACKUP
    message = f'Installing v{journal["version"]} failed, so the previous version was kept: {error}'
    try:
        # Skip files the install never got to: one that couldn't be replaced
        # (in use) usually can't be rewritten either, and needn't be.
        changed = [f for f in journal['old_files'] if not _same(pending, addin_dir, f)]
        _copy_into(pending, changed, addin_dir)
        for relative in set(journal['files']) - set(journal['old_files']):
            target = addin_dir.joinpath(*_safe_parts(relative))
            if target.is_file():
                target.unlink()
        _remove_caches(addin_dir)
    except Exception as restore_error:
        # Leave the journal: the next start finishes or retries before loading anything.
        state['install_error'] = f'{message}. Restoring the previous files also failed: {restore_error}'
        store.save_state(state)
        raise UpdateError(state['install_error']) from error
    failures = int(state.get('install_failures') or 0) + 1
    state.update(installing=None, install_error=message, install_failures=failures)
    store.save_state(state)
    shutil.rmtree(pending, ignore_errors=True)
    if failures >= MAX_INSTALL_FAILURES:
        store.discard_staged()
        message += f' It failed {failures} times, so the download was discarded.'
        state = store.state()
        state['install_error'] = message
        store.save_state(state)
    raise UpdateError(message) from error


def resume_interrupted(store, addin_dir):
    """At startup, before importing the add-in's modules: finish an install that
    Fusion was killed in the middle of, so old and new modules never mix.
    Returns the version installed, or None. Raises UpdateError while the folder
    may still hold a mix of two versions."""
    state = store.state()
    if state.get('blocked'):
        if state['blocked'] == _fingerprint(addin_dir):
            raise UpdateError('An interrupted update could not be finished')
        state['blocked'] = None  # reinstalled since
        store.save_state(state)
    if not state.get('installing'):
        return None
    return apply_staged(store, addin_dir)
