from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

from config_loader import config_ready

REMOTE_DIRNAME = 'REMOTE_CONTROL'
COMMANDS_DIRNAME = 'commands'
RESULTS_DIRNAME = 'results'
STATUS_FILENAME = 'app_status.json'


def remote_root(base: Path) -> Path:
    return Path(base) / 'DATA' / REMOTE_DIRNAME


def _atomic_json(path: Path, value: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.tmp-' + uuid.uuid4().hex)
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temp, path)


def read_json(path: Path, default=None):
    try:
        value = json.loads(Path(path).read_text(encoding='utf-8-sig'))
        return value
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return default


def queue_remote_command(base: Path, command: str, source='telegram', user_id=None) -> str:
    if command not in {'scan_runs', 'stop_scan', 'complete_latest', 'complete_missing_latest', 'shutdown_app'}:
        raise ValueError('Unsupported remote command.')
    root = remote_root(base)
    folder = root / COMMANDS_DIRNAME
    folder.mkdir(parents=True, exist_ok=True)
    command_id = f'{time.time_ns()}-{uuid.uuid4().hex}'
    payload = {
        'id': command_id,
        'command': command,
        'source': str(source or 'remote'),
        'user_id': int(user_id) if user_id is not None else None,
        'created_at': time.time(),
    }
    _atomic_json(folder / (command_id + '.json'), payload)
    return command_id


def read_app_status(base: Path) -> dict:
    return read_json(remote_root(base) / STATUS_FILENAME, {}) or {}


def app_status_fresh(base: Path, max_age=8.0) -> bool:
    status = read_app_status(base)
    try:
        return bool(status.get('app_running')) and (time.time() - float(status.get('heartbeat_at') or 0)) <= float(max_age)
    except (TypeError, ValueError):
        return False


class RemoteAppBridge:
    """Small file-queue bridge between the local Tk app and the Telegram agent.

    No patient identifiers, pool names, diagnoses, variants, or QC values are written to
    this bridge. The status file intentionally contains only application/scan state.
    """
    POLL_MS = 600
    HEARTBEAT_SEC = 1.5

    def __init__(self, app, base: Path, cfg: dict, version: str):
        self.app = app
        self.base = Path(base)
        self.cfg = cfg
        self.version = str(version)
        self.root = remote_root(base)
        self.commands = self.root / COMMANDS_DIRNAME
        self.results = self.root / RESULTS_DIRNAME
        self.root.mkdir(parents=True, exist_ok=True)
        self.commands.mkdir(parents=True, exist_ok=True)
        self.results.mkdir(parents=True, exist_ok=True)
        self.pending_scan = False
        self.pending_action = ''
        self.active_action = ''
        self.remote_active = False
        self.last_event = {'id': '', 'kind': 'app_started', 'at': time.time(), 'technical_records': None}
        self._last_heartbeat = 0.0
        self._closed = False
        self.write_status(force=True)
        self.app.after(self.POLL_MS, self._tick)

    def _event(self, kind: str, *, technical_records=None):
        self.last_event = {
            'id': uuid.uuid4().hex,
            'kind': str(kind),
            'at': time.time(),
            'technical_records': int(technical_records) if technical_records is not None else None,
        }
        self.write_status(force=True)

    def status_payload(self, app_running=True) -> dict:
        # Do not add patient-/pool-level fields here. Telegram reads this file.
        return {
            'schema': 1,
            'app_running': bool(app_running),
            'version': self.version,
            'app_pid': os.getpid(),
            'heartbeat_at': time.time(),
            'browser_ready': bool(getattr(self.app, 'browser_ready', False)),
            'job_busy': bool(getattr(self.app, 'job_busy', False)),
            'current_command': str(getattr(self.app, 'current_command', '') or ''),
            'runs_scanned': bool(getattr(self.app, 'runs_scanned', False)),
            'remote_active': bool(self.remote_active),
            'remote_scan_pending': bool(self.pending_scan),
            'remote_action': str(self.active_action or self.pending_action or ''),
            'last_event': dict(self.last_event),
        }

    def write_status(self, force=False):
        now = time.time()
        if not force and now - self._last_heartbeat < self.HEARTBEAT_SEC:
            return
        _atomic_json(self.root / STATUS_FILENAME, self.status_payload(True))
        self._last_heartbeat = now

    def _result(self, command_id, status, message):
        _atomic_json(self.results / (command_id + '.json'), {
            'id': command_id,
            'status': status,
            'message': message,
            'at': time.time(),
        })
        # Retain only a small technical audit trail.
        rows = sorted(self.results.glob('*.json'), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in rows[40:]:
            try: old.unlink()
            except OSError: pass

    def _process_command_file(self, path: Path):
        data = read_json(path, {}) or {}
        command_id = str(data.get('id') or path.stem)
        command = str(data.get('command') or '')
        try:
            created = float(data.get('created_at') or 0)
            if not created or time.time() - created > 180:
                self._result(command_id, 'expired', 'Eski uzaktan komut calistirilmadi.')
                return
            if command == 'scan_runs':
                status, message = self.request_scan()
            elif command == 'complete_latest':
                status, message = self.request_complete_latest()
            elif command == 'complete_missing_latest':
                status, message = self.request_missing_latest()
            elif command == 'stop_scan':
                status, message = self.request_stop()
            elif command == 'shutdown_app':
                status, message = self.request_shutdown()
            else:
                status, message = 'rejected', 'Desteklenmeyen uzaktan komut.'
            self._result(command_id, status, message)
        except Exception:
            # Never serialize raw exception text: it could contain URLs or local details.
            self._result(command_id, 'error', 'Uzaktan komut islenemedi; PC uzerinde programi kontrol edin.')
            self._event('remote_error')
        finally:
            try: path.unlink()
            except OSError: pass

    def _request_with_login(self, action, accepted_message):
        if self.pending_scan or self.remote_active:
            return 'busy', 'Uzaktan NGS istegi zaten isleniyor.'
        if not config_ready(self.cfg):
            self._event('remote_error')
            return 'error', 'GenNext RUNS adresi ayarli degil.'
        if bool(getattr(self.app, 'job_busy', False)):
            return 'busy', 'Baska bir tarayici isi zaten calisiyor.'
        if action == 'complete_missing_latest':
            try:
                keys = list(self.app._latest_record_keys())
            except Exception:
                keys = []
            if not keys:
                return 'error', 'Son calisma yerelde bulunamadi; once RUNS Tara veya Son POOL Tamamla kullanin.'
        self.remote_active = True
        self.pending_scan = True
        self.pending_action = str(action)
        self.active_action = ''
        if self.app._submit('open_browser', auto_login=True):
            self._event('browser_starting')
            return 'accepted', accepted_message
        self.pending_scan = False
        self.pending_action = ''
        self.remote_active = False
        return 'busy', 'Tarayici acilamadi; baska bir is calisiyor olabilir.'

    def request_scan(self):
        return self._request_with_login(
            'scan_runs',
            'GenNext tarayicisi ve kayitli oturum kontrol ediliyor; sonra RUNS otomatik taranacak.',
        )

    def request_complete_latest(self):
        return self._request_with_login(
            'complete_latest',
            'GenNext oturumu kontrol ediliyor; sonra en yeni POOL yeniden bulunup eksikleri tamamlanacak.',
        )

    def request_missing_latest(self):
        return self._request_with_login(
            'complete_missing_latest',
            'GenNext oturumu kontrol ediliyor; sonra son bilinen calismanin yalniz eksik adimlari denenecek.',
        )

    def request_stop(self):
        if self.pending_scan or self.pending_action:
            self.pending_scan = False
            self.pending_action = ''
            self.active_action = ''
            self.remote_active = False
            self._event('scan_stopped')
            return 'accepted', 'Bekleyen uzaktan NGS islemi iptal edildi.'
        if bool(getattr(self.app, 'job_busy', False)) and str(getattr(self.app, 'current_command', '')) in ('scan_runs', 'complete_latest', 'run_queue'):
            self.app.browser.request_stop(False)
            self._event('stop_requested')
            return 'accepted', 'Durdurma istendi; mevcut guvenli adimdan sonra duracak.'
        return 'idle', 'Calisan bir uzaktan RUNS/POOL tamamlama islemi yok.'

    def request_shutdown(self):
        if bool(getattr(self.app, 'job_busy', False)) or self.pending_scan or self.remote_active:
            return 'busy', 'NGS isi calisiyor; once /tarama_durdur kullanin.'
        try:
            meta = getattr(self.app, 'meta_tab', None)
            if meta is not None and callable(getattr(meta, 'is_dirty', None)) and meta.is_dirty():
                return 'blocked', 'Kaydedilmemis Hasta/Blok degisikligi var; uzaktan restart yapilmadi.'
        except Exception:
            return 'blocked', 'Kaydedilmemis form durumu dogrulanamadi; uzaktan restart yapilmadi.'
        self._event('restart_requested')
        self.app.after(350, self.app.on_close)
        return 'accepted', 'NGS guvenli kapatma istegi aldi.'

    def on_job_done(self, info):
        info = info or {}
        command = str(info.get('command') or '')
        outcome = str(info.get('status') or '')
        if command == 'open_browser' and self.pending_scan:
            auth_verified = bool(info.get('auth_verified'))
            if outcome == 'completed' and auth_verified:
                self.pending_scan = False
                self.app.after(400, self._start_pending_action)
            else:
                self.pending_scan = False
                self.pending_action = ''
                self.active_action = ''
                self.remote_active = False
                self._event('login_failed' if outcome == 'completed' or 'Sign In' in str(info.get('message') or '') else 'remote_error')
            return
        action = self.active_action
        # Backward-compatible state recovery: v0.4.50 status/tests may mark a
        # remote scan active without the newer active_action field.
        if not action and self.remote_active and command == 'scan_runs':
            action = 'scan_runs'
        expected = {
            'scan_runs': 'scan_runs',
            'complete_latest': 'complete_latest',
            'complete_missing_latest': 'run_queue',
        }.get(action)
        if not self.remote_active or not expected or command != expected:
            return
        self.pending_scan = False
        self.pending_action = ''
        self.active_action = ''
        self.remote_active = False
        if outcome == 'stopped':
            self._event('scan_stopped')
        elif outcome != 'completed':
            self._event('remote_error')
        elif action == 'scan_runs':
            self._event('scan_completed', technical_records=info.get('technical_records'))
        elif action == 'complete_latest':
            if bool(info.get('all_complete')):
                self._event('latest_completed')
            elif int(info.get('waiting_remaining', 0) or 0) > 0 and int(info.get('error_remaining', 0) or 0) == 0 and int(info.get('blocked_remaining', 0) or 0) == 0:
                self._event('latest_waiting')
            else:
                self._event('latest_attention')
        elif action == 'complete_missing_latest':
            if bool(info.get('all_complete')):
                self._event('missing_completed')
            else:
                self._event('missing_attention')
        self.write_status(force=True)

    def _start_pending_action(self):
        if self._closed or bool(getattr(self.app, 'job_busy', False)):
            if not self._closed:
                self.app.after(400, self._start_pending_action)
            return
        action = self.pending_action
        if not action:
            self.remote_active = False
            return
        self.pending_action = ''
        ok = False
        if action == 'scan_runs':
            ok = self.app._submit('scan_runs')
            event = 'scan_started'
        elif action == 'complete_latest':
            ok = self.app._submit('complete_latest')
            event = 'latest_started'
        elif action == 'complete_missing_latest':
            try:
                keys = list(self.app._latest_record_keys())
            except Exception:
                keys = []
            if keys:
                ok = self.app._submit('run_queue', dna=True, rna=True, only_missing=True, record_keys=keys)
            event = 'missing_started'
        else:
            event = 'remote_error'
        if ok:
            self.active_action = action
            self.remote_active = True
            self._event(event)
        else:
            self.active_action = ''
            self.remote_active = False
            self._event('remote_error')

    def suppress_modal_error(self) -> bool:
        return bool(self.remote_active or self.pending_scan)

    def _tick(self):
        if self._closed:
            return
        try:
            paths = sorted(self.commands.glob('*.json'), key=lambda p: p.name)
            for path in paths[:6]:
                self._process_command_file(path)
        finally:
            self.write_status()
            self.app.after(self.POLL_MS, self._tick)

    def close(self):
        self._closed = True
        payload = self.status_payload(False)
        payload['heartbeat_at'] = time.time()
        _atomic_json(self.root / STATUS_FILENAME, payload)
