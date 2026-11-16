"""Fusion side of in-app updates: background checks, the update window and the
Check for Updates command. Update logic lives in updater.py.

Network and file work run on worker threads. Their results come back to
Fusion's main thread through a custom event, as in tool_picker.py.
"""

import json
from pathlib import Path
import threading
import time
import traceback
from urllib.parse import urlparse
import webbrowser

import adsk.core

from . import updater

PALETTE_ID = 'additionalToolpaths_updates'
EVENT_ID = 'additionalToolpaths_updateEvent'
CMD_ID = 'additionalToolpaths_checkForUpdates'
CMD_NAME = 'Check for Deburr Updates'
CMD_TOOLTIP = 'Check for a new version of Fusion Free Deburr and change update settings.'
# Not promoted: the command sits in the Add-Ins dropdowns, not on the ribbon.
PANELS = [('CAMEnvironment', 'CAMScriptsAddinsPanel'), ('FusionSolidEnvironment', 'SolidScriptsAddinsPanel')]
WIDTH, HEIGHT = 560, 600
STARTUP_DELAY = 20  # seconds; let Fusion finish starting before the first check

ADDIN_DIR = Path(__file__).resolve().parent
HTML_PATH = str(ADDIN_DIR / 'resources' / 'updater' / 'index.html')
CMD_RESOURCES = str(ADDIN_DIR / 'resources' / 'BallDeburr')

_app: adsk.core.Application = None
_ui: adsk.core.UserInterface = None
_log = print
_handlers = []
_store: updater.Store = None
_installed = None          # version in the add-in folder's manifest
_palette: adsk.core.Palette = None
# Kept across Stop/Run (reload re-runs this module in the same namespace), as
# worker threads from the previous run may still be going.
_lock = globals().get('_lock') or threading.Lock()   # one stage/install at a time
_generation = globals().get('_generation', 0)         # results from older runs are dropped
_view = {'latest': None, 'notes': None, 'busy': None, 'error': None, 'checked': False}


def start(app, ui, log, handlers):
    """Register the command and event; schedule the daily check."""
    global _app, _ui, _log, _handlers, _store, _installed, _generation
    _app, _ui, _log, _handlers = app, ui, log, handlers
    _generation += 1
    _store = updater.Store(updater.data_dir())
    _installed = (updater.read_manifest(ADDIN_DIR) or {}).get('version', '0.0.0')
    _view.update(latest=None, notes=None, busy=None, error=None, checked=False)
    _store.cleanup()
    _remove_ui()  # anything a failed earlier stop() left behind

    event = _app.registerCustomEvent(EVENT_ID)
    on_result = _ResultHandler()
    event.add(on_result)
    _handlers.append(on_result)

    cmd_def = _ui.commandDefinitions.addButtonDefinition(CMD_ID, CMD_NAME, CMD_TOOLTIP, CMD_RESOURCES)
    on_created = _CommandCreatedHandler()
    cmd_def.commandCreated.add(on_created)
    _handlers.append(on_created)
    for panel in _panels():
        if not panel.controls.itemById(CMD_ID):
            panel.controls.addCommand(cmd_def)

    settings = _store.settings(_installed)
    if settings['mode'] != 'off' and time.time() - settings['last_check'] >= updater.CHECK_INTERVAL:
        # The timer only posts back; the check itself starts on the main thread.
        _spawn(_post, {'kind': 'due'}, _generation, delay=STARTUP_DELAY)


def started():
    """Call once the add-in started successfully; confirms a fresh install worked."""
    try:
        version = _store.mark_started() if _store else None
        if version:
            _log(f'Updated to v{version}')
    except Exception:
        _log(f'Could not record update startup:\n{traceback.format_exc()}')


def offer_rollback(error_text):
    """After a failed start, offer to restore the version an update replaced.

    Returns True if the user was asked (so the caller needn't show the error again).
    """
    try:
        store = _store or updater.Store(updater.data_dir())
        candidate = store.rollback_candidate()
        if not candidate or not _ui:
            return False
        failed, previous = candidate
        answer = _ui.messageBox(
            f'Fusion Free Deburr v{failed} failed to start:\n\n{error_text}\n\n'
            f'Restore the previous version, v{previous}? It is put back when you close Fusion, '
            'or when you Stop and Run the add-in in Scripts and Add-Ins (Shift+S).',
            'Fusion Free Deburr', adsk.core.MessageBoxButtonTypes.YesNoButtonType,
            adsk.core.MessageBoxIconTypes.WarningIconType)
        if answer == adsk.core.DialogResults.DialogYes:
            if not _lock.acquire(timeout=5):
                raise updater.UpdateError('An update download is still being saved; try again')
            try:
                store.stage_rollback()
            finally:
                _lock.release()
            store.update_settings(_installed or previous, skipped_version=failed)
            _log(f'v{previous} will be restored when the add-in stops')
        return True
    except Exception:
        _log(f'Rollback offer failed:\n{traceback.format_exc()}')
        return False


def stop():
    """Remove the UI, then install a downloaded update. Fusion calls stop() on exit too."""
    global _generation
    _generation += 1
    try:
        _remove_ui()
    except Exception:
        _log(f'Updater cleanup failed:\n{traceback.format_exc()}')
    install_staged()


def _remove_ui():
    global _palette
    _palette = None
    if _ui:
        palette = _ui.palettes.itemById(PALETTE_ID)
        if palette:
            palette.deleteMe()
        for panel in _panels():
            control = panel.controls.itemById(CMD_ID)
            if control:
                control.deleteMe()
        cmd_def = _ui.commandDefinitions.itemById(CMD_ID)
        if cmd_def:
            cmd_def.deleteMe()
    if _app:
        try:
            _app.unregisterCustomEvent(EVENT_ID)
        except Exception:
            pass  # not registered


def install_staged():
    """Swap in a staged update. Safe to call when nothing is staged."""
    store = _store or updater.Store(updater.data_dir())
    # Only staging holds the lock, and briefly, so this wait is short.
    if not _lock.acquire(timeout=3):
        _log('An update is still being saved; it will be installed next time')
        return
    try:
        version = updater.apply_staged(store, ADDIN_DIR)
        if version:
            _log(f'Installed v{version}; it runs from the next start')
    except Exception as error:
        _log(f'Update install failed: {error}')
        if not isinstance(error, updater.UpdateError):
            _log(traceback.format_exc())
    finally:
        _lock.release()


# ---- Workers (background threads) -------------------------------------------

def _spawn(target, *args, delay=0):
    timer = threading.Timer(delay, target, args)
    timer.daemon = True
    timer.start()


def _post(result, generation):
    """Hand a worker's result to the main thread, unless the add-in has stopped since."""
    if generation != _generation:
        return
    try:
        _app.fireCustomEvent(EVENT_ID, json.dumps(dict(result, generation=generation)))
    except Exception:
        pass  # stopping; Fusion's API (including logging) is main-thread only


def _check_worker(manual, channel, generation):
    try:
        meta, notes = updater.latest_release(channel)
        if notes is None and updater.is_newer(meta['version'], _installed):
            notes = updater.release_notes(meta['version'])
        _post({'kind': 'checked', 'manual': manual, 'channel': channel, 'meta': meta, 'notes': notes}, generation)
    except Exception as error:
        _post(_failure('check', manual, error), generation)


def _download_worker(meta, manual, generation):
    try:
        updater.download_and_stage(_store, meta, lock=_lock)
        _post({'kind': 'staged', 'manual': manual, 'version': meta['version']}, generation)
    except Exception as error:
        _post(_failure('download', manual, error), generation)


def _failure(step, manual, error):
    """Runs on a worker thread, so it must not call Fusion; the traceback is logged on the main thread."""
    expected = isinstance(error, updater.UpdateError)
    return {'kind': 'failed', 'step': step, 'manual': manual,
            'error': str(error) if expected else f'Unexpected error: {error}',
            'detail': None if expected else traceback.format_exc()}


# ---- Main-thread handling ---------------------------------------------------

def _on_checked(result):
    meta, manual = result['meta'], result['manual']
    _view['busy'] = None
    settings = _store.settings(_installed)
    if result['channel'] != settings['channel']:
        _check_now(manual)  # the channel changed while this check ran
        return
    settings = _store.update_settings(_installed, last_check=time.time())
    version = meta['version']
    _view.update(error=None, checked=True)
    if not updater.is_newer(version, _installed):
        _view.update(latest=None, notes=None)
        _log(f'Up to date (v{_installed}, {settings["channel"]} channel)')
        _show_or_refresh(manual)
        return
    _view.update(latest=meta, notes=result.get('notes'))
    if _store.staged_version() == version:
        _show_or_refresh(manual)
        return
    if not manual and settings['skipped_version'] == version:
        _log(f'v{version} is available (skipped)')
        _refresh()
        return
    if not manual and updater.is_checkout(ADDIN_DIR):
        # Developers update with git; don't interrupt them on every start.
        _log(f'v{version} is available; this copy is a git checkout, so update it with git')
        _refresh()
        return
    blocker = updater.install_blocker(meta, ADDIN_DIR)
    if settings['mode'] == 'auto' and not blocker and not manual:
        _log(f'Downloading v{version} in the background')
        _view['busy'] = 'downloading'
        _spawn(_download_worker, meta, False, _generation)
        _refresh()
        return
    _log(f'v{version} is available')
    _show()


def _show_or_refresh(manual):
    if manual:
        _show()
    else:
        _refresh()


def _on_staged(result):
    _view.update(busy=None, error=None)
    _log(f'v{result["version"]} downloaded and verified; it installs when the add-in stops')
    _refresh()


def _on_failed(result):
    _view.update(busy=None, error=result['error'])
    _log(f'Update {result["step"]} failed: {result["error"]}')
    if result.get('detail'):
        _log(result['detail'])
    if result['manual'] or _visible():
        _show()


class _ResultHandler(adsk.core.CustomEventHandler):
    def notify(self, args):
        try:
            result = json.loads(args.additionalInfo)
            if result.get('generation') != _generation:
                return  # from before a Stop/Run
            if result['kind'] == 'due':
                _check_now(False)
                return
            {'checked': _on_checked, 'staged': _on_staged, 'failed': _on_failed}[result['kind']](result)
        except Exception:
            _log(f'Update handling failed:\n{traceback.format_exc()}')


class _CommandCreatedHandler(adsk.core.CommandCreatedEventHandler):
    def notify(self, args: adsk.core.CommandCreatedEventArgs):
        try:
            # No inputs, so the command executes straight away.
            on_execute = _ExecuteHandler()
            args.command.execute.add(on_execute)
            _handlers.append(on_execute)
        except Exception:
            _ui.messageBox(f'Check for updates failed:\n{traceback.format_exc()}')


class _ExecuteHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        try:
            _check_now(True)
            _show()
        except Exception:
            _ui.messageBox(f'Check for updates failed:\n{traceback.format_exc()}')


def _check_now(manual):
    """Start a check unless one or a download is running (a running check
    notices a channel change itself and checks again)."""
    if _view['busy']:
        return
    _view.update(busy='checking', error=None)
    _spawn(_check_worker, manual, _store.settings(_installed)['channel'], _generation)
    _refresh()


# ---- Update window ------------------------------------------------------------

def _payload():
    settings = _store.settings(_installed)
    latest = _view['latest']
    state = _store.state()
    staged = state.get('staged')
    return json.dumps({
        'installed': _installed,
        'latest': latest and {
            'version': latest['version'],
            'channel': latest.get('channel'),
            'notes': _view['notes'],
            'notesUrl': latest.get('release_notes_url'),
        },
        'staged': staged,
        'rollback': bool(staged) and state.get('staged_kind') == 'rollback',
        'blocker': updater.install_blocker(latest, ADDIN_DIR) if latest else None,
        'busy': _view['busy'],
        'error': _view['error'] or state.get('install_error'),
        'checked': _view['checked'],
        'skipped': bool(latest and settings['skipped_version'] == latest['version']),
        'settings': {'mode': settings['mode'], 'channel': settings['channel']},
    })


def _visible():
    return bool(_palette and _palette.isValid and _palette.isVisible)


def _show():
    global _palette
    _palette = _ui.palettes.itemById(PALETTE_ID)
    if not _palette:
        # Created visible, as in tool_picker.py: hidden-then-shown palettes can
        # stay stuck at a tiny size.
        _palette = _ui.palettes.add(PALETTE_ID, 'Fusion Free Deburr Updates', HTML_PATH.replace('\\', '/'),
                                    True, True, True, WIDTH, HEIGHT)
        handler = _IncomingHandler()
        _palette.incomingFromHTML.add(handler)
        _handlers.append(handler)
        # Size and place it once; later calls keep wherever the user moved it.
        _palette.setMinimumSize(420, 360)
        _palette.setSize(WIDTH, HEIGHT)
        viewport = _app.activeViewport
        center = viewport.viewToScreen(adsk.core.Point2D.create(viewport.width / 2, viewport.height / 2)) if viewport else None
        if center:
            _palette.setPosition(int(center.x - WIDTH / 2), int(center.y - HEIGHT / 2))
    else:
        _refresh()
        _palette.isVisible = True


def _refresh():
    if _palette and _palette.isValid:
        _palette.sendInfoToHTML('state', _payload())


def _hide():
    if _palette and _palette.isValid:
        _palette.isVisible = False


def _open_url(url):
    parsed = urlparse(url or '')
    if parsed.scheme == 'https' and parsed.hostname == 'github.com':
        webbrowser.open(url)


class _IncomingHandler(adsk.core.HTMLEventHandler):
    def notify(self, args: adsk.core.HTMLEventArgs):
        try:
            action = args.action
            data = json.loads(args.data) if args.data else {}
            latest = _view['latest']
            if action == 'ready':
                args.returnData = _payload()
                return
            if action == 'install' and latest and not _view['busy']:
                if updater.install_blocker(latest, ADDIN_DIR) is None:
                    _view.update(busy='downloading', error=None)
                    _spawn(_download_worker, latest, True, _generation)
            elif action == 'cancelStaged' and _lock.acquire(blocking=False):
                try:
                    _store.discard_staged()
                finally:
                    _lock.release()
            elif action == 'skip' and latest:
                _store.update_settings(_installed, skipped_version=latest['version'])
                _hide()
            elif action == 'later':
                _hide()
            elif action == 'check':
                _check_now(True)
            elif action == 'settings':
                before = _store.settings(_installed)
                changes = {k: data[k] for k in ('mode', 'channel') if k in data}
                after = _store.update_settings(_installed, **changes)
                if after['channel'] != before['channel']:
                    _view.update(latest=None, notes=None)
                    _check_now(True)
            elif action == 'openNotes':
                _open_url((latest or {}).get('release_notes_url'))
            elif action == 'openReleases':
                _open_url((latest or {}).get('release_notes_url') or updater.RELEASES_PAGE)
            args.returnData = _payload()
        except Exception as error:
            _log(f'Update window failed:\n{traceback.format_exc()}')
            _view['error'] = str(error) if isinstance(error, updater.UpdateError) else f'Unexpected error: {error}'
            try:
                args.returnData = _payload()  # so the window shows the saved settings again
            except Exception:
                pass


def _panels():
    found = []
    for ws_id, panel_id in PANELS:
        ws = _ui.workspaces.itemById(ws_id) if _ui else None
        panel = ws.toolbarPanels.itemById(panel_id) if ws else None
        if panel:
            found.append(panel)
    return found
