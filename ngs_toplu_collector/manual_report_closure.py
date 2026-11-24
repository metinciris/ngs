"""Auditable manual disposition for ONE technical DNA/RNA report.

This is separate from GenNext Analyzed, report Selection, collected files,
Enlil report text and e-signature.  Never infers a clinical negative from an
empty Selection.  No network calls and no changes to external reports.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import sqlite3


STATUSES = {
    'reported_positive': 'RAPORLANDI - POZİTİF (manuel doğrulandı)',
    'reported_negative': 'RAPORLANDI - NEGATİF (manuel doğrulandı)',
    'cancelled': 'İPTAL - inceleme tamamlandı',
    'insufficient': 'YETERSİZ - inceleme tamamlandı',
    'review_closed': 'İnceleme tamamlandı - rapor düzenlenmedi',
    'open': 'YENİDEN AÇILDI - manuel kapanış geri alındı',
}
CLOSED_STATUSES = frozenset(STATUSES) - {'open'}
SCHEMA = """
CREATE TABLE IF NOT EXISTS manual_report_closure (
 record_key TEXT PRIMARY KEY,
 status TEXT NOT NULL CHECK(status IN (
    'reported_positive','reported_negative','cancelled','insufficient','review_closed','open')),
 note TEXT NOT NULL,
 recorded_by TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 last_event_id INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS manual_report_closure_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 record_key TEXT NOT NULL,
 previous_status TEXT NOT NULL,
 new_status TEXT NOT NULL,
 note TEXT NOT NULL,
 recorded_by TEXT NOT NULL,
 changed_at TEXT NOT NULL,
 run_uuid TEXT NOT NULL,
 sample_uuid TEXT NOT NULL,
 sample_type TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_manual_report_closure_events_record
 ON manual_report_closure_events(record_key, id);
"""


def _path(db):
    return Path(getattr(db, 'path', db)).resolve()


def _now():
    return datetime.now().astimezone().isoformat(timespec='seconds')


def _table_exists(cx, table):
    return cx.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def _positive_selection(cx, record_key, sample_type):
    """Fail-safe warning: any stored selected call requires explicit override."""
    sample_type = str(sample_type or '').upper()
    tables = []
    if 'DNA' in sample_type:
        tables.append('gennext_selection_current')
    if 'RNA' in sample_type:
        tables.append('gennext_fusion_current')
    found = []
    for table in tables:
        if not _table_exists(cx, table):
            continue
        row = cx.execute(f'SELECT included_count FROM {table} WHERE record_key=?', (record_key,)).fetchone()
        if row is not None and int(row[0] or 0) > 0:
            found.append(table)
    return found


def save_manual_closure(db, record_key, status, note, recorded_by, *,
                        expected_identity, expected_event_id=None,
                        selection_conflict_ack=False):
    """Atomic status+history write with exact identity and optimistic concurrency.

    No signed-report claims. A closed status is a user assertion about an
    already-issued/closed report, not evidence of an Enlil e-signature.
    """
    if status not in STATUSES:
        raise ValueError('Gecersiz manuel rapor kapanis durumu.')
    record_key = str(record_key or '').strip()
    note = ' '.join(str(note or '').split())
    recorded_by = ' '.join(str(recorded_by or '').split())
    if not record_key or not isinstance(expected_identity, dict):
        raise ValueError('Teknik calisma kimligi dogrulanamadi.')
    if len(note) < 10 or len(note) > 1200:
        raise ValueError('Islem gerekcesi 10-1200 karakter arasinda olmali.')
    if len(recorded_by) < 2 or len(recorded_by) > 100:
        raise ValueError('Kaydi yapan kullanicinin adi veya rumuzu gerekli.')
    if expected_event_id is not None:
        try:
            expected_event_id = int(expected_event_id)
        except (TypeError, ValueError) as exc:
            raise ValueError('Onay gecmisi denetimi gecersiz.') from exc
    cx = sqlite3.connect(_path(db), timeout=15)
    cx.row_factory = sqlite3.Row
    try:
        cx.execute('PRAGMA busy_timeout=15000')
        with cx:
            # Schema and data live in the same local SQLite database; no
            # pre-existing patient, report, task or GenNext rows are edited.
            cx.executescript(SCHEMA)
            cx.execute('BEGIN IMMEDIATE')
            sample = cx.execute('''SELECT record_key,run_uuid,sample_uuid,sample_type,
                                   patient_id,pool_name FROM samples WHERE record_key=?''',
                                (record_key,)).fetchone()
            if sample is None:
                raise ValueError('Teknik ornek bulunamadi; islem yapilmadi.')
            for field in ('run_uuid', 'sample_uuid', 'sample_type', 'patient_id', 'pool_name'):
                if str(sample[field] or '') != str(expected_identity.get(field) or ''):
                    raise ValueError('Teknik kimlik degisti; arsivi yenileyip tekrar deneyin.')
            current = cx.execute('SELECT status,note,recorded_by,last_event_id '
                                 'FROM manual_report_closure WHERE record_key=?',
                                 (record_key,)).fetchone()
            actual_event_id = int(current['last_event_id']) if current else None
            if actual_event_id != expected_event_id:
                raise ValueError('Bu rapor durumu baska bir islemde degisti; arsivi yenileyin.')
            if status == 'open' and (current is None or current['status'] == 'open'):
                raise ValueError('Manuel kapanis kaydi yok; geri alinamadi.')
            if status == 'reported_negative' and _positive_selection(cx, record_key, sample['sample_type']):
                if not selection_conflict_ack:
                    raise ValueError('GenNext Selection icinde secilmis degisiklik var; '
                                     'negatif rapor icin acik gerekce ve ikinci onay gerekli.')
                if len(note) < 25:
                    raise ValueError('Selection ile uyusmayan negatif rapor icin ayrintili gerekce gerekli.')
            if current and (current['status'], current['note'], current['recorded_by']) == (status, note, recorded_by):
                return actual_event_id, False
            stamp = _now()
            cursor = cx.execute('''INSERT INTO manual_report_closure_events
                (record_key,previous_status,new_status,note,recorded_by,changed_at,
                 run_uuid,sample_uuid,sample_type) VALUES (?,?,?,?,?,?,?,?,?)''',
                (record_key, current['status'] if current else 'none', status,
                 note, recorded_by, stamp, sample['run_uuid'] or '',
                 sample['sample_uuid'] or '', sample['sample_type'] or ''))
            event_id = int(cursor.lastrowid)
            cx.execute('''INSERT INTO manual_report_closure
                (record_key,status,note,recorded_by,updated_at,last_event_id)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(record_key) DO UPDATE SET
                  status=excluded.status,note=excluded.note,
                  recorded_by=excluded.recorded_by,updated_at=excluded.updated_at,
                  last_event_id=excluded.last_event_id''',
                (record_key, status, note, recorded_by, stamp, event_id))
        return event_id, True
    finally:
        cx.close()
