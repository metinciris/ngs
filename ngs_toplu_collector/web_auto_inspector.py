"""v0.4.65: opt-in, low-impact GenNext observation during NGS collection.

The BrowserWorker is the ONLY owner of Playwright calls. The Tk thread may
change a threading.Event and a mode string; it may not inspect the browser.
An observation failure must never alter an NGS task's result or output.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from web_interface_inspector import (
    AUTO_SNAP_JS, WebInspectorSession, _allowed_origin, _safe_json,
    page_type_from_url,
)

AUTO_COMMANDS = frozenset(('scan_runs', 'run_queue', 'complete_latest'))
AUTO_TASKS = frozenset(('run', 'clinical', 'snv', 'cnv', 'qc', 'fusion'))
AUTO_STATUSES = frozenset(('done', 'review', 'error', 'waiting', 'blocked', 'stopped', 'not_available'))
MAX_AUTO_SNAPSHOTS = 12
MIN_SAMPLE_SECONDS = 24.0
SLOW_EVALUATE_MS = 1000


class AutoCollectionInspector:
    def __init__(self, collector, enabled_event, mode_getter, snapshot_requested=None, *, log=lambda message: None):
        self.collector = collector
        self.enabled = enabled_event
        self.mode_getter = mode_getter
        self.snapshot_requested = snapshot_requested
        self.log = log
        self.session = None
        self.active_command = ''
        self.started = 0.0
        self.last_capture = 0.0
        self.snapshots = 0
        self.tasks_seen = 0
        self.alerts = []
        self.last_result = None
        self.suspended = False
        self.manual_marks_captured = 0
        self.job_summary = {}

    def begin(self, command):
        self.active_command = command if command in AUTO_COMMANDS else ''
        self.started = time.monotonic()
        self.last_capture = 0.0
        self.snapshots = self.tasks_seen = 0
        self.alerts = []
        self.last_result = None
        self.suspended = False
        self.manual_marks_captured = 0
        self.job_summary = {}
        # No browsing/open_browser call: do not change user login or route.
        if self.active_command and self.enabled.is_set():
            self._ensure()

    def _ensure(self):
        if not self.active_command or not self.enabled.is_set() or self.session:
            return
        context = getattr(self.collector, 'context', None)
        if context is None:
            return
        try:
            self.session = WebInspectorSession(
                context, Path(self.collector.base) / 'DIAGNOSTICS' / 'WEB_ARAYUZ',
                str(self.collector.cfg.get('runs_url') or ''), log=lambda m: None,
            )
            self.session._wire(passive=True)
            self.log('WEB AUTO: Izleme aktif; veri toplama oncelikli, koklayici salt okunur.')
        except Exception:
            self.suspended = True
            self.session = None
            self.log('WEB AUTO: Izleme baslatilamadi; NGS toplama etkilenmedi.')

    def record_job_outcome(self, status, counters=None):
        # Reject arbitrary caller-supplied strings/paths and private sample IDs.
        fields = ('processed', 'done', 'review', 'error', 'waiting', 'blocked',
                  'stopped', 'waiting_remaining', 'review_remaining', 'error_remaining')
        counters = counters if isinstance(counters, dict) else {}
        outcome = status if status in ('completed', 'stopped', 'error') else 'error'
        result = {'status': outcome}
        for name in fields:
            value = counters.get(name)
            if type(value) is int and 0 <= value <= 1000000:
                result[name] = value
        self.job_summary = result

    def task_completed(self, page, task_type, status):
        """Called from process_task.finally BEFORE the page is closed."""
        if not self.active_command or task_type not in AUTO_TASKS:
            return
        self.tasks_seen += 1
        if not self.enabled.is_set():
            # A running collection is never stopped by disabling observation.
            if self.session:
                self._close_session()
            return
        if self.suspended:
            return
        self._ensure()
        if self.session is None:
            return
        safe_status = status if status in AUTO_STATUSES else 'error'
        is_alert = safe_status in ('review', 'error')
        if is_alert and len(self.alerts) < 50:
            self.alerts.append({'kind': 'collection_task_needs_review',
                'task_type': task_type, 'status': safe_status})
        forced = bool(self.snapshot_requested and self.snapshot_requested.is_set())
        sampling = self.mode_getter() == 'sample'
        due = time.monotonic() - self.last_capture >= MIN_SAMPLE_SECONDS
        if not (is_alert or forced or (sampling and due)):
            return
        if self.snapshots >= MAX_AUTO_SNAPSHOTS or (self.session._resource_pressure and not is_alert):
            return
        try:
            if page is None or page.is_closed() or not _allowed_origin(page.url, self.session.runs_url):
                return
            tp = page_type_from_url(page.url)
            if tp == 'other':
                tp = 'runs' if task_type == 'run' else task_type
            # One frame, selector-only, no DOM walk or raw page text.
            added = self.session._capture_page(page, page.main_frame, tp, 0,
                                               snapshot_js=AUTO_SNAP_JS)
            if added:
                self.snapshots += 1
                self.last_capture = time.monotonic()
                if forced:self.manual_marks_captured += 1
            if forced and self.snapshot_requested:self.snapshot_requested.clear()
            if self.session._capture_timings and self.session._capture_timings[-1] >= SLOW_EVALUATE_MS:
                self.suspended = True
                self.log('WEB AUTO: Yuksek gecikme olculdu; ek DOM koklamasi durduruldu, NGS toplama suruyor.')
        except Exception:
            self.suspended = True
            self.log('WEB AUTO: Koklayici hatasi; kalan taramada koklama durduruldu, NGS toplama suruyor.')

    def _close_session(self):
        ses = self.session
        self.session = None
        if ses is None:
            return None
        try:
            result = ses._finish('auto_collection', time.monotonic() - self.started)
            folder = Path(result['session_dir'])
            meta_path = folder / 'SESSION.json'
            doc = json.loads(meta_path.read_text(encoding='utf-8'))
            doc['auto_collection'] = {
                'command': self.active_command, 'mode': self.mode_getter(),
                'job_outcome': self.job_summary,
                'tasks_observed': self.tasks_seen, 'lightweight_snapshots': self.snapshots,
                'task_alerts': len(self.alerts), 'manual_marks_captured': self.manual_marks_captured,
                'suspended_due_to_resource_pressure': self.suspended,
                'disclosure': 'Automatic snapshots contain only selected control presence, not the full DOM.',
            }
            _safe_json(meta_path, doc)
            if self.alerts:
                issue_file = folder / 'ISSUES.json'
                issue_doc = json.loads(issue_file.read_text(encoding='utf-8'))
                issue_doc['issues'].extend(self.alerts)
                _safe_json(issue_file, issue_doc)
                doc['issues_count'] = len(issue_doc['issues'])
                _safe_json(meta_path, doc)
            result['issues'] = doc['issues_count']
            self.last_result = result
            self.log('WEB AUTO: Kayit tamam; %d hafif goruntu, %d gorev uyari kaydi.' % (
                self.snapshots, len(self.alerts)))
            return result
        except Exception:
            self.log('WEB AUTO: Kayit kapatma hatasi; NGS toplama sonuclari degismedi.')
            return None
        finally:
            try: ses._unwire()
            except Exception: pass

    def finish(self):
        try:
            # If the user requested a snapshot while scanning RUNS (without
            # process_task), capture the already-open RUNS page once at the end.
            if (self.active_command and self.enabled.is_set() and not self.suspended and
                self.snapshot_requested is not None and self.snapshot_requested.is_set()):
                self._ensure()
                page = getattr(self.collector, 'main', None)
                if page is not None and not page.is_closed():
                    self.task_completed(page, 'run', 'done')

            if self.active_command and self.enabled.is_set() and not self.suspended:
                self._ensure()
            if self.session:
                self._close_session()
            return self.last_result
        finally:
            self.active_command = ''
            self.last_result = None
            self.session = None
