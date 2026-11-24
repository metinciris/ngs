"""v0.4.83. Local AI views and decisions; no automatic report/Selection writes."""
from __future__ import annotations
from collections import OrderedDict
from datetime import datetime, timezone
import json
import re

REVIEW_LABELS = {'report': 'Raporlama \u00f6nerisi', 'consider': 'De\u011ferlendir',
 'igv_review': 'IGV kontrol\u00fc', 'hold': 'Beklet', 'do_not_report': 'Raporlama \u00f6nerilmiyor',
 'indeterminate': 'Belirsiz', 'reportable': 'Raporlanabilir aday',
 'unknown_oncogenicity': 'Unknown / bilinmiyor', 'artifact': 'Artefakt g\u00f6r\u00fc\u015f\u00fc'}
RELEASE_LABELS = {'ready': 'YZ: haz\u0131r \u00f6nerisi', 'review': '\u0130nceleme gerekli', 'hold': 'Beklet',
 'rerun': 'Tekrar \u00e7al\u0131\u015fma \u00f6nerisi', 'indeterminate': 'Belirsiz'}
MANUAL_LABELS = {'reported': 'Raporland\u0131 (yerel)', 'not_reported': 'Raporlanmad\u0131 (yerel)', 'artifact': 'Artefakt (yerel)'}
ADJUDICATION_LABELS = {'accepted': 'YZ \u00f6nerisi kabul', 'rejected': 'YZ \u00f6nerisi reddedildi', 'pending': 'Karar bekliyor'}


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def object_json(value, default=None):
    if isinstance(value, (dict, list)): return value
    try: return json.loads(value) if value else ({} if default is None else default)
    except (ValueError, TypeError): return {} if default is None else default


def canonical_key(value):
    """Only trivial SNV prefix/chr/case normalization. No gene-only matching."""
    key = str(value or '').strip()
    raw = re.sub(r'^SNV\|', '', key, flags=re.I)
    p = raw.split('|')
    if len(p) == 4 and re.fullmatch(r'(?:chr)?[0-9XYM]+', p[0], re.I) and p[1].isdigit():
        if all(re.fullmatch(r'[ACGTN-]+', x, re.I) for x in p[2:]):
            return '|'.join(('chr' + re.sub(r'^chr', '', p[0], flags=re.I).upper(), str(int(p[1])), p[2].upper(), p[3].upper()))
    return key


def feature_key(row):
    return str(row.get('variant_key') or row.get('cnv_key') or row.get('fusion_key') or '').strip()


def compatible_build(row, local_build):
    def norm(v):
        s = str(v or '').lower().replace(' ', '')
        return {'grch37': 'hg19', 'grch38': 'hg38'}.get(s, s)
    evidence = row.get('source_evidence') or {}
    a = norm(row.get('genome_build') or (evidence.get('genome_build') if isinstance(evidence, dict) else ''))
    b = norm(local_build)
    return not (a and b and a != b)


def ensure_schema(db):
    db.cx.executescript('''
    CREATE TABLE IF NOT EXISTS ai_record_index_v0483(session_id INTEGER NOT NULL,record_key TEXT NOT NULL,PRIMARY KEY(session_id,record_key));
    CREATE INDEX IF NOT EXISTS idx_ai_record_v0483 ON ai_record_index_v0483(record_key,session_id);
    CREATE TABLE IF NOT EXISTS ai_local_notes_v0483(record_key TEXT PRIMARY KEY,note TEXT NOT NULL,updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS ai_note_history_v0483(id INTEGER PRIMARY KEY,record_key TEXT NOT NULL,note TEXT NOT NULL,created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS ai_adjudications_v0483(session_id INTEGER NOT NULL,record_key TEXT NOT NULL,feature_key TEXT NOT NULL,
      status TEXT NOT NULL,note TEXT NOT NULL,updated_at TEXT NOT NULL,PRIMARY KEY(session_id,record_key,feature_key));
    CREATE TABLE IF NOT EXISTS ai_adjudication_history_v0483(id INTEGER PRIMARY KEY,session_id INTEGER NOT NULL,record_key TEXT NOT NULL,
      feature_key TEXT NOT NULL,status TEXT NOT NULL,note TEXT NOT NULL,created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS variant_decision_history_v0483(id INTEGER PRIMARY KEY,record_key TEXT NOT NULL,variant_key TEXT NOT NULL,
      previous_status TEXT,status TEXT,note TEXT,created_at TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS idx_decision_history_v0483 ON variant_decision_history_v0483(record_key,id);
    CREATE INDEX IF NOT EXISTS idx_ai_patient_record_v0483 ON ai_patient_reviews(record_key,session_id);
    ''')
    db.cx.commit()


def index_document(db, sid, doc):
    keys = set()
    for field in ('sample_index', 'patient_reviews', 'variant_reviews', 'cnv_reviews', 'fusion_reviews'):
        for row in doc.get(field) or []:
            if isinstance(row, dict) and row.get('record_key'): keys.add(str(row['record_key']))
    with db.cx:
        db.cx.executemany('INSERT OR IGNORE INTO ai_record_index_v0483 VALUES(?,?)', [(sid, rk) for rk in keys])


def install_review_workspace(StateDB):
    old_init, old_import, old_set = StateDB.__init__, StateDB.import_ai_review, StateDB.set_variant_decision
    def initialize(self, *args, **kwargs):
        old_init(self, *args, **kwargs)
        ensure_schema(self)
    def import_return(self, data, source_path='', source_sha256=''):
        result = old_import(self, data, source_path, source_sha256)
        if result.get('session_id') and data.get('schema') in ('ngs_ai_review_return_v1', 'ngs_ai_review_return_v2'):
            index_document(self, int(result['session_id']), data)
        return result
    def set_decision(self, record_key, variant_key, status=None, note=None):
        prior = self.cx.execute('SELECT status,note FROM variant_decisions WHERE record_key=? AND variant_key=?', (record_key, variant_key)).fetchone()
        old_set(self, record_key, variant_key, status, note)
        if (prior['status'] if prior else None) != status or (prior['note'] if prior else None) != note:
            with self.cx:
                self.cx.execute('INSERT INTO variant_decision_history_v0483(record_key,variant_key,previous_status,status,note,created_at) VALUES(?,?,?,?,?,?)',
                                (record_key, variant_key, prior['status'] if prior else None, status, note, stamp()))
    StateDB.__init__, StateDB.import_ai_review, StateDB.set_variant_decision = initialize, import_return, set_decision


class ReviewRepository:
    def __init__(self, db):
        self.db, self.cache = db, OrderedDict()
    def session(self, sid):
        if not sid: return {}, {}
        sid = int(sid)
        if sid not in self.cache:
            r = self.db.cx.execute('SELECT * FROM ai_review_sessions WHERE id=?', (sid,)).fetchone()
            meta = dict(r) if r else {}
            doc = object_json(meta.pop('raw_json', None))
            self.cache[sid] = (meta, doc if isinstance(doc, dict) else {})
            if len(self.cache) > 6: self.cache.popitem(last=False)
        self.cache.move_to_end(sid)
        return self.cache[sid]
    def session_ids(self, rk):
        ids = set()
        for table in ('ai_patient_reviews', 'ai_variant_reviews', 'ai_cnv_reviews_v0481', 'ai_record_index_v0483'):
            if self.db._table_exists(table):
                ids.update(int(r[0]) for r in self.db.cx.execute(f'SELECT DISTINCT session_id FROM {table} WHERE record_key=?', (rk,)))
        return sorted(ids, reverse=True)
    def record(self, rk, sid=None):
        ids = self.session_ids(rk)
        chosen = int(sid) if sid else (ids[0] if ids else None)
        meta, doc = self.session(chosen)
        patient = next((dict(p) for p in doc.get('patient_reviews') or [] if isinstance(p, dict) and p.get('record_key') == rk), {})
        features = []
        for field, kind in (('variant_reviews', 'SNV'), ('cnv_reviews', 'CNV'), ('fusion_reviews', 'RNA')):
            for original in doc.get(field) or []:
                if not isinstance(original, dict) or original.get('record_key') != rk: continue
                row = dict(original)
                row.update(_kind=kind, _session_id=chosen)
                row.setdefault('genome_build', doc.get('genome_build') or '')
                features.append(row)
        return {'record_key': rk, 'session_id': chosen, 'sessions': ids, 'session': meta, 'patient': patient,
                'features': features, 'pool_review': doc.get('pool_review') or {}}
    def summaries(self, sid):
        meta, doc = self.session(sid)
        records = {p['record_key']: dict(p) for p in doc.get('patient_reviews') or [] if isinstance(p, dict) and p.get('record_key')}
        for field in ('variant_reviews', 'cnv_reviews', 'fusion_reviews'):
            for row in doc.get(field) or []:
                if isinstance(row, dict) and row.get('record_key'):
                    records.setdefault(row['record_key'], {'record_key': row['record_key'], 'patient_id': row.get('patient_id'), 'summary': 'Varyant notu mevcut; hasta \u00f6zeti yok.'})
        return sorted(records.values(), key=lambda p: str(p.get('patient_id') or p['record_key']))


def review_status(row):
    key = row.get('review_status') or row.get('decision') or row.get('status') or ''
    return REVIEW_LABELS.get(key, str(key) or 'YZ notu')


def selection_for_record(db, rk, assay='DNA'):
    if assay == 'RNA':
        from gennext_fusion_selection_store import current_fusion_status
        return current_fusion_status(db, rk) or {}
    from gennext_selection_store import current_status
    return current_status(db, rk) or {}


def selection_label(snapshot, key, kind='SNV'):
    if kind == 'CNV': return 'CNV Selection ayr\u0131'
    if not snapshot: return 'Okunmad\u0131'
    if not snapshot.get('currently_verified'): return 'G\u00fcncelleme gerekli'
    if snapshot.get('result_status') in ('identity_review', 'fusion_identity_review'): return 'Kimlik kontrol\u00fc gerekli'
    canon = canonical_key(key)
    for label, field in (('Se\u00e7ili', 'selected'), ('Aday / se\u00e7ilmemi\u015f', 'candidates')):
        if canon and any(canonical_key(feature_key(r)) == canon for r in snapshot.get(field) or []):
            return label + (' (incelenmedi)' if not snapshot.get('analyzed') else '')
    return 'Se\u00e7ili de\u011fil'


def decision_for_key(db, rk, key):
    rows = db.cx.execute('SELECT variant_key,status,note,updated_at FROM variant_decisions WHERE record_key=? ORDER BY updated_at DESC,rowid DESC', (rk,))
    return next((dict(r) for r in rows if canonical_key(r['variant_key']) == canonical_key(key)), {})


def local_note(db, rk):
    row = db.cx.execute('SELECT note FROM ai_local_notes_v0483 WHERE record_key=?', (rk,)).fetchone()
    return row[0] if row else ''


def save_local_note(db, rk, note, expected_note=None):
    if not db.get_sample(rk): raise ValueError('Ge\u00e7erli teknik kay\u0131t se\u00e7ilmedi.')
    if len(note) > 100000: raise ValueError('Not 100.000 karakteri a\u015famaz.')
    current = local_note(db, rk)
    if current == note: return
    if expected_note is not None and current != expected_note:
        raise ValueError('Not ba\u015fka sekmede de\u011fi\u015fti. Metninizi kopyalay\u0131p hastay\u0131 yeniden a\u00e7\u0131n; yeni notun \u00fczerine yaz\u0131lmad\u0131.')
    with db.cx:
        db.cx.execute('INSERT INTO ai_local_notes_v0483 VALUES(?,?,?) ON CONFLICT(record_key) DO UPDATE SET note=excluded.note,updated_at=excluded.updated_at', (rk, note, stamp()))
        db.cx.execute('INSERT INTO ai_note_history_v0483(record_key,note,created_at) VALUES(?,?,?)', (rk, note, stamp()))


def adjudication(db, sid, rk, key):
    row = db.cx.execute('SELECT * FROM ai_adjudications_v0483 WHERE session_id=? AND record_key=? AND feature_key=?', (sid or 0, rk, canonical_key(key))).fetchone()
    return dict(row) if row else {}


def adjudicate(db, sid, rk, key, status, note=''):
    if status not in ADJUDICATION_LABELS: raise ValueError('Ge\u00e7ersiz YZ karar\u0131.')
    if not db.get_sample(rk) or not key or not sid: raise ValueError('Teknik kay\u0131t / varyant e\u015fle\u015fmedi.')
    payload = ReviewRepository(db).record(rk, sid)
    canon = canonical_key(key)
    if not any(canonical_key(feature_key(r)) == canon for r in payload['features']):
        raise ValueError('Bu YZ oturumunda tam varyant anahtar\u0131 bulunamad\u0131.')
    with db.cx:
        db.cx.execute('INSERT INTO ai_adjudications_v0483 VALUES(?,?,?,?,?,?) ON CONFLICT(session_id,record_key,feature_key) DO UPDATE SET status=excluded.status,note=excluded.note,updated_at=excluded.updated_at', (sid, rk, canon, status, note, stamp()))
        db.cx.execute('INSERT INTO ai_adjudication_history_v0483(session_id,record_key,feature_key,status,note,created_at) VALUES(?,?,?,?,?,?)', (sid, rk, canon, status, note, stamp()))


def readable(value, indent=0):
    if value is None: return 'Veri yok / null'
    if isinstance(value, bool): return 'true' if value else 'false'
    if isinstance(value, dict):
        return '\n'.join(' '*indent + str(k) + ': ' + ('\n' + readable(v, indent+2) if isinstance(v, (dict, list)) else readable(v)) for k, v in value.items())
    if isinstance(value, list): return '\n'.join(' '*indent + '- ' + readable(v, indent+2) for v in value)
    return str(value)


def patient_text(payload):
    p, meta = payload['patient'], payload['session']
    if not payload['session_id']: return 'Bu teknik \u00e7al\u0131\u015fma i\u00e7in YZ de\u011ferlendirmesi yok. Negatif sonu\u00e7 anlam\u0131na gelmez.'
    lines = [f"YZ #{payload['session_id']} | {meta.get('imported_at', '')}",
             'YZ \u00f6nerisidir; nihai rapor ve GenNext Selection de\u011fi\u015ftirilmez.', '',
             RELEASE_LABELS.get(p.get('release_recommendation'), p.get('release_recommendation') or 'Karar belirtilmemi\u015f'), '',
             str(p.get('summary') or 'Hasta \u00f6zeti yok; varyant notlar\u0131 ayr\u0131 sekmede.'), '', readable(p.get('notes') or [])]
    for label, key in (('Teknik de\u011ferlendirme', 'technical_review'), ('\u00d6nceki DNA / RNA', 'prior_ngs_history')):
        if p.get(key): lines += ['', label, readable(p[key])]
    return '\n'.join(lines)


def feature_text(row, db=None, rk='', snapshot=None):
    lines = [str(row.get('gene') or row.get('fusion') or '') + ' ' + str(row.get('change') or row.get('protein') or ''),
             feature_key(row), '', 'YZ: ' + review_status(row),
             'Raporlama \u00f6nerisi: ' + readable(row.get('report_recommendation') or row.get('recommendation') or ''),
             'Gerek\u00e7e: ' + readable(row.get('rationale') or row.get('reason') or ''),
             'IGV: ' + ('isteniyor' if row.get('igv_required') is True else 'zorunluluk belirtilmemi\u015f'),
             readable(row.get('igv_reason') or ''), 'G\u00fcven (YZ beyan\u0131): ' + str(row.get('confidence') or 'belirtilmedi')]
    if db is not None:
        key = feature_key(row)
        d, a = decision_for_key(db, rk, key), adjudication(db, row.get('_session_id'), rk, key)
        lines += ['', 'GenNext: ' + selection_label(snapshot or {}, key, row.get('_kind', 'SNV')),
                  'Yerel rapor karar\u0131: ' + MANUAL_LABELS.get(d.get('status'), 'Karar yok'),
                  'YZ karar\u0131: ' + ADJUDICATION_LABELS.get(a.get('status'), 'Karar bekliyor'), 'Karar notu: ' + str(a.get('note') or '')]
    lines += ['YZ referans\u0131: ' + str(row.get('genome_build') or 'belirtilmemi\u015f; genom / koordinat uyumu kontrol edilmeli.')]
    return '\n'.join(lines)

# BEGIN NGS_VARIANT_EVIDENCE_v1_1_install_workspace
from variant_evidence_integration import install_workspace as _install_ngs_variant_evidence
_install_ngs_variant_evidence(globals())
# END NGS_VARIANT_EVIDENCE_v1_1_install_workspace
