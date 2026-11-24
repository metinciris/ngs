"""One isolated export process. Polling and cancellation never call Tk or SQLite."""
from __future__ import annotations

import json
import math
import ntpath
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
import uuid


EXPORT_CONFIG_KEYS = frozenset({
    'genome_build', 'spread_source_min_af', 'spread_recipient_max_af',
    'spread_ratio_min', 'spread_pair_min_variants', 'ai_fingerprint_signal_limit',
    'ai_recurrent_variant_limit', 'ai_pair_variant_limit', 'pileup_enrich_ai_export',
    'pileup_alignment_roots', 'alignment_roots', 'pileup_scan_recursive',
    'pileup_bed_path', 'bed_path', 'pileup_bed_roots', 'pileup_bed_name_contains',
    'pileup_full_alignment_sha256', 'pileup_bed_padding_bp', 'pileup_min_mapq',
    'pileup_min_baseq', 'pileup_max_depth', 'pileup_reference_fasta',
    'pileup_softclip_window_bp', 'pileup_coverage_window_bp',
    'pileup_coverage_max_window_bp', 'pileup_coverage_threshold_x',
    'pileup_coverage_cliff_ratio',
})
TERMINAL_STATES = frozenset(('succeeded', 'failed', 'cancelled'))


def windows_extended_path(value):
    """Pure conversion for absolute drive/UNC paths; preserve existing prefixes."""
    value = ntpath.normpath(os.fspath(value))
    if value.startswith('\\\\?\\'):
        return value
    value = ntpath.abspath(value)
    if value.startswith('\\\\'):
        return '\\\\?\\UNC\\' + value[2:]
    return '\\\\?\\' + value


def export_filesystem_path(value):
    """Use extended Windows paths for job/output I/O; leave POSIX unchanged."""
    return Path(windows_extended_path(value) if os.name == 'nt' else value)


def display_path(value):
    """Keep user-facing paths and the last-success pointer ordinary Windows paths."""
    value = os.fspath(value)
    if value.startswith('\\\\?\\UNC\\'):
        return '\\\\' + value[8:]
    if value.startswith('\\\\?\\'):
        return value[4:]
    return value


def atomic_json(path, value):
    path = export_filesystem_path(path)
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        json.dump(value, stream, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    # A Windows reader can briefly deny replacement; do not make one poll fatal.
    for attempt in range(6):
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(0.01 * (attempt + 1))


def worker_command(job_file):
    executable = Path(sys.executable)
    if executable.name.lower() == 'pythonw.exe':
        console_python = executable.with_name('python.exe')
        if console_python.is_file():
            executable = console_python
    return [str(executable), str(Path(__file__).with_name('ai_export_worker.py')), '--job', str(job_file)]


def cleanup_publication(job_dir, output_root, job_id):
    """Undo only this job's journalled publication after an interrupted commit."""
    try:
        journal = json.loads((Path(job_dir) / 'publish.json').read_text(encoding='utf-8'))
        if journal.get('job_id') != job_id:
            return
        output_root = export_filesystem_path(output_root).resolve()
        owned = []
        for key in ('folder', 'zip'):
            path = export_filesystem_path(journal[key])
            if path.parent.resolve() != output_root or job_id not in path.name:
                return
            owned.append(path)
        pointer = output_root / 'SON_YZ_PAKETI.txt'
        if pointer.is_file() and display_path(pointer.read_text(encoding='utf-8').strip()) == display_path(owned[1]):
            previous = journal.get('previous_pointer') or ''
            if previous and export_filesystem_path(previous.strip()).is_file():
                temporary = Path(job_dir) / 'restore_pointer.txt'
                temporary.write_text(previous, encoding='utf-8')
                os.replace(temporary, pointer)
            else:
                pointer.unlink(missing_ok=True)
        owned[1].unlink(missing_ok=True)
        if owned[0].is_dir():
            shutil.rmtree(owned[0])
    except (OSError, ValueError, KeyError, TypeError):
        # Never broaden deletion when a journal is absent or cannot be verified.
        return


class ExportJob:
    def __init__(self, base, db_path, cfg, options):
        self.base = Path(base).resolve()
        self.db_path = Path(db_path).resolve()
        root = Path(str(cfg.get('ai_export_dir') or 'AI_EXPORT'))
        self.output_root = export_filesystem_path(root if root.is_absolute() else self.base / root).resolve()
        self.job_id = uuid.uuid4().hex
        self.job_dir = self.output_root / '.jobs' / self.job_id
        self._cfg = {key: cfg[key] for key in EXPORT_CONFIG_KEYS if key in cfg}
        self.options = {
            'pool_group': str(options.get('pool_group') or options.get('pool') or '').strip(),
            'sample_scope': str(options.get('sample_scope') or options.get('scope') or 'DNA'),
            'include_pii': False,
            'include_full_qc': bool(options.get('include_full_qc', True)),
            'include_source_files': bool(options.get('include_source_files', True)),
            'reindex_selected': bool(options.get('reindex_selected', True)),
            'selected_run_uuids': [str(x) for x in (options.get('selected_run_uuids') or []) if str(x)],
        }
        try:
            timeout = float(cfg.get('ai_export_hard_timeout_seconds', 0))
            if not math.isfinite(timeout):
                raise ValueError('Export timeout must be finite')
            self.timeout_seconds = max(0.0, timeout)
        except (ValueError, TypeError):
            self.timeout_seconds = 0.0
        self._process = None
        self._started_at = None
        self._cleanup_started = False
        self._status = {'state': 'running', 'stage': 'queued', 'current': None,
                        'total': None, 'message': 'Paket işlemi başlatılıyor.'}

    def start(self):
        if self._started_at is not None or self._status['state'] in TERMINAL_STATES:
            return self
        self._started_at = time.monotonic()
        try:
            if not self.options['pool_group']:
                raise ValueError('Bir POOL seçilmelidir.')
            if self.options['sample_scope'] not in ('DNA', 'RNA', 'DNA+RNA'):
                raise ValueError('Geçersiz örnek kapsamı.')
            self.job_dir.mkdir(parents=True, mode=0o700, exist_ok=False)
            os.chmod(self.job_dir, 0o700)
            request = {'job_id': self.job_id, 'base': str(self.base), 'db_path': str(self.db_path),
                       'output_root': str(self.output_root), 'cfg': self._cfg,
                       'options': self.options, 'timeout_seconds': self.timeout_seconds}
            job_file = self.job_dir / 'job.json'
            atomic_json(job_file, request)
            with (self.job_dir / 'worker.log').open('wb') as log:
                os.chmod(self.job_dir / 'worker.log', 0o600)
                self._process = subprocess.Popen(worker_command(job_file), cwd=str(self.base),
                    stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), close_fds=True)
        except Exception as exc:
            self._finish('failed', 'İşlem başlatılamadı: ' + str(exc), remove_outputs=True)
        return self

    def _read_worker_status(self):
        status_path = self.job_dir / 'status.json'
        try:
            if status_path.is_file() and status_path.stat().st_size <= 1024 * 1024:
                update = json.loads(status_path.read_text(encoding='utf-8'))
                if update.get('state') in TERMINAL_STATES | {'running'}:
                    return update
        except (OSError, ValueError):
            pass
        return None

    def _accept_worker_status(self):
        update = self._read_worker_status()
        if update is not None:
            self._status = update
            if update['state'] in TERMINAL_STATES:
                self._reap(remove_outputs=update['state'] != 'succeeded')
                return True
        return False

    def poll(self):
        if self._status['state'] in TERMINAL_STATES:
            return dict(self._status)
        if self._started_at is None:
            return dict(self._status)
        if self._accept_worker_status():
            return dict(self._status)
        if self._process is not None and self._process.poll() is not None:
            # The child can publish its terminal status between our first read
            # and this exit check. Never discard that completed result.
            if self._accept_worker_status():
                return dict(self._status)
            self._finish('failed', 'Paket işlemi sonuç üretmeden sonlandı (çıkış kodu '
                         + str(self._process.returncode) + ').', remove_outputs=True)
        elif self.timeout_seconds > 0 and time.monotonic() - self._started_at >= self.timeout_seconds:
            if self._accept_worker_status():
                return dict(self._status)
            self._finish('failed', f'Paket işlemi {self.timeout_seconds:g} saniyelik süre sınırını aştı. '
                         'İşlem durduruldu; önceki paket korundu.', remove_outputs=True, stop=True)
        return dict(self._status)

    def cancel(self):
        if self.poll()['state'] in TERMINAL_STATES:
            return dict(self._status)
        try:
            (self.job_dir / 'CANCEL').touch()
        except OSError:
            pass
        self._finish('cancelled', 'Paket oluşturma iptal edildi; önceki paket korundu.',
                     remove_outputs=True, stop=True)
        return dict(self._status)

    def _finish(self, state, message, *, remove_outputs, stop=False):
        self._status = {'state': state, 'stage': self._status.get('stage') or state,
                        'current': self._status.get('current'), 'total': self._status.get('total'),
                        'message': message}
        if stop and self._process is not None and self._process.poll() is None:
            try:
                self._process.terminate()
            except OSError:
                pass
        self._reap(remove_outputs)

    def _reap(self, remove_outputs):
        if self._cleanup_started:
            return
        self._cleanup_started = True
        def clean():
            if self._process is not None:
                try:
                    self._process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    try:
                        self._process.kill()
                        self._process.wait(timeout=2)
                    except (OSError, subprocess.TimeoutExpired):
                        return
            if remove_outputs:
                cleanup_publication(self.job_dir, self.output_root, self.job_id)
            shutil.rmtree(self.job_dir, ignore_errors=True)
        threading.Thread(target=clean, name='NGS-export-cleanup', daemon=True).start()

# BEGIN NGS_VARIANT_EVIDENCE_v1_1_export_config_keys
EXPORT_CONFIG_KEYS = frozenset(set(EXPORT_CONFIG_KEYS) | {'variant_evidence_auto','variant_evidence_max_items','variant_evidence_max_seconds','variant_evidence_http_timeout'})
# END NGS_VARIANT_EVIDENCE_v1_1_export_config_keys
