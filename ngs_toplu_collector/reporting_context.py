"""Record-scoped reporting values, source history and review revisions.

All writes use a separate SQLite connection and one transaction. A user edit is
never a web value; missing source observations never erase a valid measurement.
No patient, block, assay or report selection is inferred from clinical similarity.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

SCHEMA_VERSION = 1
FIELDS = {
    'source_tumor_type', 'source_clinical_diagnosis', 'reporting_diagnosis',
    'age_at_reference', 'age_reference_date', 'sex', 'block_id', 'block_source',
    'tumor_percentage', 'specimen_type', 'specimen_site', 'requesting_physician',
    'source_analyst', 'reporting_signer', 'tmb_value', 'tmb_unit', 'tmb_status',
    'msi_percentage', 'msi_status', 'hrd_score', 'hrd_loh', 'hrd_tai', 'hrd_lst',
}
NUMBERS = {'age_at_reference', 'tumor_percentage', 'tmb_value', 'msi_percentage',
           'hrd_score', 'hrd_loh', 'hrd_tai', 'hrd_lst'}
CLINICAL_MAP = {
    'tumor_diagnosis': 'reporting_diagnosis', 'age': 'age_at_reference',
    'sex': 'sex', 'block_no': 'block_id', 'tumor_percent': 'tumor_percentage',
    'specimen_type': 'specimen_type', 'specimen_site': 'specimen_site',
    'tumor_type': 'source_tumor_type', 'clinical_diagnosis': 'source_clinical_diagnosis',
    'requesting_physician': 'requesting_physician', 'reporting_signer': 'reporting_signer',
    'age_reference_date': 'age_reference_date', 'block_source': 'block_source',
}
LABELS = {'reporting_diagnosis': 'Tümör tanısı', 'age_at_reference': 'Yaş',
          'sex': 'Cinsiyet', 'block_id': 'Blok', 'tumor_percentage': 'Tümör yüzdesi',
          'hrd_score': 'HRD Score', 'hrd_loh': 'HRD-LOH', 'hrd_tai': 'Telomeric AI',
          'hrd_lst': 'LST', 'tmb_value': 'TMB', 'msi_percentage': 'MSI %',
          'source_tumor_type': 'Web tümör tipi', 'source_clinical_diagnosis': 'Web klinik tanı'}
LABELS.update({'age_reference_date': 'Yaşın referans tarihi', 'block_source': 'Blok kaynağı',
              'specimen_type': 'Numune türü', 'specimen_site': 'Numune yeri',
              'requesting_physician': 'İsteyen doktor', 'source_analyst': 'Kaynak analisti',
              'reporting_signer': 'Rapor imzacısı', 'tmb_unit': 'TMB birimi',
              'tmb_status': 'Kaynak TMB durumu', 'msi_status': 'Kaynak MSI durumu'})


class ContextConflict(ValueError):
    pass


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def normalize_field(name, value):
    if name not in FIELDS:
        raise ValueError('Bilinmeyen klinik alan: ' + str(name))
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(LABELS.get(name, name) + ': geçerli bir değer girin.')
    text = str(value).strip()
    if not text or text.casefold() in {'n/a', 'na', 'null', 'none', 'not specified', 'not available'}:
        return None
    if name in NUMBERS:
        if name in {'tumor_percentage', 'msi_percentage'}:
            text = text.strip('%').strip()
        if not re.fullmatch(r'[+-]?\d+(?:[.,]\d+)?', text):
            raise ValueError(LABELS.get(name, name) + ': sayısal değer girin.')
        n = float(text.replace(',', '.'))
        max_value = 100 if name in {'tumor_percentage', 'msi_percentage'} else 130 if name == 'age_at_reference' else None
        if not math.isfinite(n) or n < 0 or (max_value is not None and n > max_value):
            raise ValueError(LABELS.get(name, name) + ': geçerli aralığın dışında.')
        if name in {'age_at_reference', 'hrd_score', 'hrd_loh', 'hrd_tai', 'hrd_lst'} and not n.is_integer():
            raise ValueError(LABELS.get(name, name) + ': tam sayı girin.')
        return int(n) if n.is_integer() else n
    if name == 'sex':
        sx = text.casefold()
        if sx in {'e', 'm', 'erkek', 'male'}: return 'Erkek'
        if sx in {'k', 'f', 'kadın', 'kadin', 'female'}: return 'Kadın'
        if sx in {'bilinmiyor', 'unknown', 'bilgiye ulaşılamadı'}: return 'Bilinmiyor'
        raise ValueError('Cinsiyet: Erkek / Kadın / Bilinmiyor seçeneklerinden birini kullanın.')
    return text


def _date_key(value):
    if not value: return None
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        # Legacy application times were local wall times in Turkey.
        if dt.tzinfo is None:
            from datetime import timedelta
            dt = dt.replace(tzinfo=timezone(timedelta(hours=3)))
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


@contextmanager
def connection(db):
    cx = sqlite3.connect(str(db.path), timeout=30)
    cx.row_factory = sqlite3.Row
    try:
        cx.execute('PRAGMA busy_timeout=30000')
        yield cx
    finally:
        cx.close()


def ensure_schema(db):
    if getattr(db, '_reporting_schema_v0481', False): return
    with connection(db) as cx:
        cx.executescript('''
        CREATE TABLE IF NOT EXISTS reporting_context_records(
          record_key TEXT PRIMARY KEY, revision INTEGER NOT NULL DEFAULT 0, updated_at TEXT);
        CREATE TABLE IF NOT EXISTS reporting_fields(
          record_key TEXT NOT NULL, field_name TEXT NOT NULL,
          source_json TEXT, manual_json TEXT, last_observation_json TEXT,
          revision INTEGER NOT NULL DEFAULT 0, updated_at TEXT,
          PRIMARY KEY(record_key,field_name));
        CREATE TABLE IF NOT EXISTS reporting_field_history(
          id INTEGER PRIMARY KEY, record_key TEXT NOT NULL, field_name TEXT NOT NULL,
          change_json TEXT NOT NULL, recorded_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS reporting_approvals(
          id INTEGER PRIMARY KEY, record_key TEXT NOT NULL, fingerprint TEXT NOT NULL,
          signer TEXT NOT NULL, payload_json TEXT NOT NULL, state TEXT NOT NULL,
          approved_at TEXT NOT NULL, invalidated_at TEXT, invalidation_reason TEXT);
        CREATE TABLE IF NOT EXISTS clinical_return_imports(
          document_sha256 TEXT PRIMARY KEY, result_json TEXT NOT NULL, imported_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS reporting_legacy_records(record_key TEXT PRIMARY KEY);
        ''')
        if not cx.execute("SELECT 1 FROM meta WHERE key='reporting_context_legacy_inventory_v1'").fetchone():
            cx.execute('INSERT OR IGNORE INTO reporting_legacy_records SELECT record_key FROM samples')
            cx.execute("INSERT INTO meta(key,value) VALUES('reporting_context_legacy_inventory_v1','1')")
        cx.commit()
    db._reporting_schema_v0481 = True


def _sample(cx, rk):
    row = cx.execute('SELECT * FROM samples WHERE record_key=?', (rk,)).fetchone()
    if row is None: raise ValueError('Teknik çalışma bulunamadı: ' + str(rk))
    row = dict(row)
    expected = f"{row['run_uuid']}::{row['sample_uuid']}::{str(row['sample_type']).upper()}"
    if rk != expected: raise ValueError('Teknik çalışma kimliği tutarsız.')
    return row


def _seed(cx, rk):
    s = _sample(cx, rk)
    if cx.execute('SELECT 1 FROM reporting_context_records WHERE record_key=?', (rk,)).fetchone(): return
    cx.execute('INSERT INTO reporting_context_records VALUES(?,0,?)', (rk, utcnow()))
    family = s.get('case_family') or s.get('patient_id') or ''
    row = cx.execute('SELECT * FROM case_metadata WHERE case_family=?', (family,)).fetchone()
    cm = dict(row) if row else {}
    if not cx.execute('SELECT 1 FROM reporting_legacy_records WHERE record_key=?',(rk,)).fetchone():
        # A newly discovered run/block cannot inherit a later case-level tumor
        # diagnosis as if it were an old manually confirmed sample diagnosis.
        cm['diagnosis'] = None
    row = cx.execute('SELECT * FROM sample_metadata WHERE record_key=?', (rk,)).fetchone()
    sm = dict(row) if row else {}
    values = {'reporting_diagnosis': cm.get('diagnosis'), 'age_at_reference': cm.get('age'),
              'sex': cm.get('sex'), 'requesting_physician': cm.get('requesting_physician'),
              'block_id': sm.get('block_id'), 'block_source': sm.get('block_source'),
              'tumor_percentage': sm.get('tumor_percentage')}
    for name, raw in values.items():
        if raw is None or str(raw).strip() == '': continue
        try: value = normalize_field(name, raw)
        except ValueError: continue
        if value is None: continue
        exact = name in {'block_id', 'block_source', 'tumor_percentage'}
        manual = {'value': value, 'origin': 'legacy_manual_record' if exact else 'legacy_case_context',
                  'raw': raw, 'record_key': rk, 'updated_at': (sm if exact else cm).get('updated_at') or '',
                  'legacy_scope': 'technical_record' if exact else 'case_family', 'present': True}
        _put(cx, rk, name, manual=manual)
    row = cx.execute('SELECT * FROM manual_biomarker_overrides WHERE record_key=?', (rk,)).fetchone()
    overrides = dict(row) if row else None
    for name, column in [('tmb_value', 'tmb'), ('msi_percentage', 'msi'), ('hrd_score', 'hrd')]:
        raw = (overrides or s).get(column)
        token = str(raw if raw is not None else '').strip()
        if name == 'hrd_score':
            from hrd_utils import normalize_hrd
            token = normalize_hrd(token, allow_bare=True)
        elif token and token.casefold() != 'n/a':
            pattern = r'^([\d.,]+)\s*(?:muts?/mb|mut/mb)' if name == 'tmb_value' else r'^([\d.,]+)\s*%'
            m = re.search(pattern, token, re.I)
            token = m.group(1) if m else ''
        if not token and overrides is None: continue
        try: value = normalize_field(name, token)
        except ValueError: continue
        payload = {'value': value, 'raw': raw, 'record_key': rk,
                   'origin': 'legacy_manual_record' if overrides else 'legacy_source',
                   'availability': 'not_available' if token == 'N/A' else 'observed' if value is not None else 'missing',
                   'observed_at': (overrides or s).get('updated_at') or '', 'present': bool(overrides)}
        _put(cx, rk, name, **({'manual': payload} if overrides else {'source': payload}))
        # Preserve explicitly labelled status and unit from this exact legacy
        # record. Never derive a classification from a number during migration.
        status_pattern = r'\bTMB[- ]([HL])\b' if name == 'tmb_value' else r'\b(MSS|MSI[- ](?:H|L|HIGH|LOW))\b' if name == 'msi_percentage' else None
        status_match = re.search(status_pattern, str(raw or ''), re.I) if status_pattern else None
        if status_match:
            status_name = 'tmb_status' if name == 'tmb_value' else 'msi_status'
            status_value = ('TMB-' + status_match.group(1)) if name == 'tmb_value' else status_match.group(1).replace(' ', '-')
            status_payload = {**payload, 'value': status_value.upper()}
            _put(cx, rk, status_name, **({'manual': status_payload} if overrides else {'source': status_payload}))
        if name == 'tmb_value' and re.search(r'muts?/mb', str(raw or ''), re.I):
            unit_payload = {**payload, 'value': 'mut/Mb'}
            _put(cx, rk, 'tmb_unit', **({'manual': unit_payload} if overrides else {'source': unit_payload}))
    _bump(cx, rk)


_UNCHANGED = object()


def _put(cx, rk, name, *, source=_UNCHANGED, manual=_UNCHANGED, observation=_UNCHANGED):
    row = cx.execute('SELECT * FROM reporting_fields WHERE record_key=? AND field_name=?', (rk, name)).fetchone()
    old = dict(row) if row else {}
    fields = {'source_json': source, 'manual_json': manual, 'last_observation_json': observation}
    values = {key: old.get(key) if val is _UNCHANGED else None if val is None else dumps(val)
              for key, val in fields.items()}
    if row and all(old.get(k) == v for k, v in values.items()): return False
    stamp = utcnow()
    cx.execute('''INSERT INTO reporting_fields VALUES(?,?,?,?,?,?,?)
      ON CONFLICT(record_key,field_name) DO UPDATE SET source_json=excluded.source_json,
      manual_json=excluded.manual_json,last_observation_json=excluded.last_observation_json,
      revision=excluded.revision,updated_at=excluded.updated_at''',
      (rk, name, values['source_json'], values['manual_json'], values['last_observation_json'],
       int(old.get('revision') or 0) + 1, stamp))
    cx.execute('INSERT INTO reporting_field_history(record_key,field_name,change_json,recorded_at) VALUES(?,?,?,?)',
               (rk, name, dumps({'before': old, 'after': values}), stamp))
    return True


def _bump(cx, rk, invalidate=True):
    stamp = utcnow()
    cx.execute('UPDATE reporting_context_records SET revision=revision+1,updated_at=? WHERE record_key=?', (stamp, rk))
    if invalidate:
        cx.execute("UPDATE reporting_approvals SET state='needs_review',invalidated_at=?,invalidation_reason=? WHERE record_key=? AND state='approved'",
                   (stamp, 'Klinik bilgi veya biyobelirteç kaydı değişti.', rk))


def _fields(cx, rk):
    result = {}
    for r in cx.execute('SELECT * FROM reporting_fields WHERE record_key=?', (rk,)):
        source = json.loads(r['source_json']) if r['source_json'] else {}
        manual = json.loads(r['manual_json']) if r['manual_json'] else None
        last = json.loads(r['last_observation_json']) if r['last_observation_json'] else {}
        effective = manual if manual is not None else source
        conflict = bool(manual is not None and source.get('availability') in {'observed', 'not_available'}
                        and source.get('value') != manual.get('value'))
        conflict |= bool(last.get('simultaneous_source_conflict'))
        conflict |= bool(last.get('manual_import_conflict') and last.get('value') != effective.get('value'))
        result[r['field_name']] = {'source_value': source.get('value'), 'source': source,
          'manual_override': {'present': manual is not None, **(manual or {})},
          'effective_value': effective.get('value'), 'effective_origin': effective.get('origin') or ('user' if manual is not None else 'verified_source'),
          'source_conflict': conflict, 'last_observation': last, 'revision': r['revision'], 'updated_at': r['updated_at']}
    return result


def _resolved_values(fields):
    values = {name: fields.get(name, {}).get('effective_value') for name in FIELDS}
    # A deliberate manual blank remains blank. A broad web diagnosis never
    # overwrites the pathologist's more specific saved diagnosis.
    diagnosis = fields.get('reporting_diagnosis', {})
    if not diagnosis.get('manual_override', {}).get('present') and values['reporting_diagnosis'] is None:
        values['reporting_diagnosis'] = values['source_tumor_type'] or values['source_clinical_diagnosis']
    return values


def _mirror(cx, rk):
    """Keep existing sample-only report/QC readers compatible, without case copying."""
    v = _resolved_values(_fields(cx, rk)); stamp = utcnow()
    text = lambda x: '' if x is None else str(x)
    cx.execute('''INSERT INTO sample_metadata(record_key,block_id,block_source,tumor_percentage,updated_at)
       VALUES(?,?,?,?,?) ON CONFLICT(record_key) DO UPDATE SET block_id=excluded.block_id,
       block_source=excluded.block_source,tumor_percentage=excluded.tumor_percentage,updated_at=excluded.updated_at''',
       (rk, text(v['block_id']), text(v['block_source']), text(v['tumor_percentage']), stamp))
    legacy_values = {}
    has_manual_biomarker = False
    for name, col, unit in [('tmb_value', 'tmb', ' mut/Mb'), ('msi_percentage', 'msi', '%'), ('hrd_score', 'hrd', '')]:
        field = _fields(cx, rk).get(name)
        if not field: continue
        raw = text(v[name]) + unit if v[name] is not None else ''
        payload = field['manual_override'] if field['manual_override']['present'] else field['source']
        if field['manual_override']['present'] and v[name] is not None:
            entered = str(payload.get('raw') if payload.get('raw') is not None else '').strip().strip('%').strip().replace(',','.')
            if re.fullmatch(r'[+]?\d+(?:\.\d+)?',entered): raw = entered + unit
        if v[name] is None and payload.get('availability') == 'not_available': raw = 'N/A'
        # Keep source statuses in the raw legacy display.
        status = v.get('tmb_status' if name == 'tmb_value' else 'msi_status' if name == 'msi_percentage' else '')
        if raw and status and not field['manual_override']['present']: raw += ' | ' + str(status)
        cx.execute(f'UPDATE samples SET {col}=? WHERE record_key=?', (raw, rk))
        legacy_values[col] = raw
        has_manual_biomarker |= field['manual_override']['present']
    if has_manual_biomarker:
        cx.execute('''INSERT INTO manual_biomarker_overrides(record_key,tmb,msi,hrd,updated_at)
          VALUES(?,?,?,?,?) ON CONFLICT(record_key) DO UPDATE SET tmb=excluded.tmb,
          msi=excluded.msi,hrd=excluded.hrd,updated_at=excluded.updated_at''',
          (rk,legacy_values.get('tmb',''),legacy_values.get('msi',''),legacy_values.get('hrd',''),stamp))
    else:
        cx.execute('DELETE FROM manual_biomarker_overrides WHERE record_key=?', (rk,))


def build_record_context(db, sample_or_record_key):
    rk = sample_or_record_key if isinstance(sample_or_record_key, str) else sample_or_record_key['record_key']
    ensure_schema(db)
    with connection(db) as cx, cx:
        _seed(cx, rk)
        fields = _fields(cx, rk); values = _resolved_values(fields)
        diagnosis = fields.get('reporting_diagnosis', {})
        diagnosis_status = 'manual_value' if diagnosis.get('manual_override', {}).get('present') else 'missing'
        if not diagnosis.get('manual_override', {}).get('present') and values['reporting_diagnosis'] is not None:
            proposal_name = 'source_tumor_type' if values['source_tumor_type'] else 'source_clinical_diagnosis'
            proposed = fields.get(proposal_name, {})
            fields['reporting_diagnosis'] = {**proposed, 'effective_value': values['reporting_diagnosis'],
                'effective_origin': 'source_proposal', 'manual_override': {'present': False},
                'proposal_from': proposal_name, 'pathologist_verified': False}
            diagnosis_status = 'source_proposal'
        clinical = {out: values.get(name) for out, name in CLINICAL_MAP.items()}
        biomarkers = {'tmb_value': values['tmb_value'], 'tmb_unit': values['tmb_unit'] or 'mut/Mb',
          'tmb_status': values['tmb_status'], 'msi_percent': values['msi_percentage'], 'msi_status': values['msi_status'],
          'hrd_score': values['hrd_score'], 'hrd_loh': values['hrd_loh'], 'tai': values['hrd_tai'], 'lst': values['hrd_lst']}
        required = ('reporting_diagnosis', 'age_at_reference', 'sex', 'block_id', 'tumor_percentage')
        missing = [name for name in required if values.get(name) is None or values.get(name) == '']
        r = cx.execute('SELECT * FROM reporting_context_records WHERE record_key=?', (rk,)).fetchone()
        a = cx.execute('SELECT id,state,approved_at,signer,fingerprint,invalidation_reason FROM reporting_approvals WHERE record_key=? ORDER BY id DESC LIMIT 1', (rk,)).fetchone()
        return {'record_key': rk, 'clinical': clinical, 'biomarkers': biomarkers, 'field_sources': fields,
          'missing_fields': missing, 'revision': r['revision'], 'updated_at': r['updated_at'],
          'approval': dict(a) if a else {'state': 'draft'}, 'ready_for_clinical_review': not missing,
          'reporting_diagnosis_status': diagnosis_status,
          'scope': 'technical_record', 'automatic_report_release': False}


def save_manual_fields(db, rk, updates, *, expected_revision=None, origin='user', use_source=(), source_details=None):
    normalized = {name: normalize_field(name, value) for name, value in updates.items()}
    ensure_schema(db)
    with connection(db) as cx, cx:
        cx.execute('BEGIN IMMEDIATE'); _seed(cx, rk)
        revision = cx.execute('SELECT revision FROM reporting_context_records WHERE record_key=?', (rk,)).fetchone()[0]
        if expected_revision is not None and revision != expected_revision:
            raise ContextConflict('Bu kayıt başka pencerede veya kaynak taramasında değişti. Girdiğiniz değerler korunuyor; güncel kaydı yeniden açıp karşılaştırın.')
        changed = False
        for name, value in normalized.items():
            old = _fields(cx, rk).get(name, {})
            if old.get('manual_override', {}).get('present') and old.get('effective_value') == value: continue
            raw = updates[name]
            changed |= _put(cx, rk, name, manual={'value': value, 'raw': raw, 'present': True,
                'origin': origin, 'record_key': rk, 'updated_at': utcnow(),
                'source_reference': (source_details or {}).get(name),
                'availability': 'not_available' if str(raw).casefold() == 'n/a' else 'observed' if value is not None else 'explicitly_cleared'})
        for name in use_source:
            if name not in FIELDS: raise ValueError('Bilinmeyen alan.')
            changed |= _put(cx, rk, name, manual=None)
        if changed: _bump(cx, rk); _mirror(cx, rk)
    return build_record_context(db, rk)


def ingest_clinical_observation(db, rk, observation):
    ensure_schema(db)
    record = observation.get('record') or {}; source = observation.get('source_observation') or {}
    if record.get('record_key') != rk or source.get('identity_verified') is not True:
        raise ValueError('Klinik kaynak kimliği doğrulanmadı; kayıt değiştirilmedi.')
    with connection(db) as cx, cx:
        cx.execute('BEGIN IMMEDIATE'); s = _sample(cx, rk); _seed(cx, rk)
        for key in ('run_uuid', 'sample_uuid', 'sample_type'):
            alt = {'run_uuid': 'run_id', 'sample_uuid': 'sample_id', 'sample_type': 'sample_type'}[key]
            if str(record.get(key) or record.get(alt) or '').upper() != str(s[key]).upper():
                raise ValueError('Kaynak başka çalışmaya ait; kayıt değiştirilmedi.')
        before = _resolved_values(_fields(cx, rk)); changed = False
        for name, item in (observation.get('fields') or {}).items():
            if name not in FIELDS or not isinstance(item, dict): continue
            if name in {'reporting_diagnosis', 'reporting_signer'}: continue
            availability = item.get('availability') or 'missing'
            entry = {**source, **item, 'record_key': rk, 'origin': source.get('kind') or 'web', 'ingested_at': utcnow()}
            try: entry['value'] = normalize_field(name, item.get('value'))
            except ValueError: entry['value'] = None; availability = entry['availability'] = 'invalid'
            if availability == 'observed' and entry['value'] is None:
                token=str(item.get('raw') or '').strip().casefold()
                availability = entry['availability'] = 'not_available' if token in {'n/a','not available','not specified'} else 'missing'
            try:
                observed_time=datetime.fromisoformat(str(entry.get('observed_at') or '').replace('Z','+00:00'))
                valid_time=observed_time.tzinfo is not None
            except ValueError:
                valid_time=False
            if availability in {'observed','not_available'} and not valid_time:
                availability=entry['availability']='invalid'
                entry['validation_error']='Kaynak gözlem zamanı doğrulanamadı.'
            current = _fields(cx, rk).get(name, {}).get('source') or {}
            new_at = _date_key(entry.get('observed_at')); old_at = _date_key(current.get('observed_at'))
            newer = not current or (new_at is not None and (old_at is None or new_at >= old_at))
            if current and new_at is not None and new_at==old_at and current.get('value')!=entry.get('value') and availability in {'observed','not_available'}:
                entry['simultaneous_source_conflict']=True
                changed |= _put(cx,rk,name,observation=entry)
                continue
            # Last failed read and last valid measurement are separate records.
            if newer and availability in {'observed', 'not_available'}:
                changed |= _put(cx, rk, name, source=entry, observation=entry)
            elif newer:
                changed |= _put(cx, rk, name, observation=entry)
        if changed:
            after = _resolved_values(_fields(cx, rk))
            _bump(cx, rk, invalidate=before != after); _mirror(cx, rk)
    return build_record_context(db, rk)


def save_metadata_form(db, rk, case_data, sample_data, *, expected_revision=None):
    """Commit the editor's identity, clinical fields and exact block together."""
    mapping = {'diagnosis': 'reporting_diagnosis', 'age': 'age_at_reference', 'sex': 'sex',
               'requesting_physician': 'requesting_physician'}
    updates = {dst: normalize_field(dst, case_data.get(src)) for src, dst in mapping.items()}
    updates.update({key: normalize_field(key, sample_data.get(key)) for key in ('block_id', 'block_source', 'tumor_percentage')})
    ensure_schema(db)
    with connection(db) as cx, cx:
        cx.execute('BEGIN IMMEDIATE'); s = _sample(cx, rk); _seed(cx, rk)
        revision = cx.execute('SELECT revision FROM reporting_context_records WHERE record_key=?', (rk,)).fetchone()[0]
        if expected_revision is not None and revision != expected_revision: raise ContextConflict('Kayıt güncellendi. Değişiklikleriniz kaydedilmedi; son değerleri yeniden açıp karşılaştırın.')
        family = s.get('case_family') or s.get('patient_id') or ''; stamp = utcnow()
        # Freeze legacy siblings before updating the compatibility case view.
        for sibling in cx.execute("SELECT record_key FROM samples WHERE COALESCE(NULLIF(case_family,''),patient_id)=?",(family,)).fetchall():
            _seed(cx,sibling['record_key'])
        columns = ('full_name','national_id','birth_date','age','diagnosis','sex','requesting_physician','reporting_physician','note')
        vals = [str(case_data.get(key) or '') for key in columns]
        sql = 'INSERT INTO case_metadata(case_family,' + ','.join(columns) + ',updated_at) VALUES(' + ','.join('?' for _ in range(len(columns)+2)) + ') ON CONFLICT(case_family) DO UPDATE SET ' + ','.join(key+'=excluded.'+key for key in columns) + ',updated_at=excluded.updated_at'
        cx.execute(sql, [family, *vals, stamp])
        _save_patient_profile(cx, db, case_data, stamp)
        changed = False
        for name, value in updates.items():
            old = _fields(cx, rk).get(name, {})
            if old.get('manual_override', {}).get('present') and old.get('effective_value') == value: continue
            changed |= _put(cx, rk, name, manual={'present': True, 'value': value, 'origin': 'user', 'updated_at': stamp, 'record_key': rk})
        _mirror(cx, rk)
        cx.execute('UPDATE sample_metadata SET note=?,block_section=? WHERE record_key=?',
                   (sample_data.get('note') or '', sample_data.get('block_section') or '', rk))
        if changed: _bump(cx, rk)
    return build_record_context(db, rk)


def _save_patient_profile(cx, db, data, stamp):
    """Keep the existing verified identity merge in the same editor transaction."""
    nid = ''.join(c for c in str(data.get('national_id') or '') if c.isdigit())
    if not nid: return
    if not db.validate_national_id(nid): raise ValueError('Geçerli TC Kimlik No gerekli.')
    row = cx.execute('SELECT * FROM patient_profiles WHERE national_id=?', (nid,)).fetchone()
    old = dict(row) if row else {}
    name = str(data.get('full_name') or '').strip() or old.get('full_name') or ''
    dob = str(data.get('birth_date') or '').strip() or old.get('birth_date') or ''
    sex = db.normalize_sex(str(data.get('sex') or '').strip() or old.get('sex') or '')
    cx.execute('''INSERT INTO patient_profiles(national_id,full_name,birth_date,sex,updated_at) VALUES(?,?,?,?,?)
      ON CONFLICT(national_id) DO UPDATE SET full_name=excluded.full_name,birth_date=excluded.birth_date,
      sex=excluded.sex,updated_at=excluded.updated_at''', (nid, name, dob, sex, stamp))
    linked = cx.execute("""SELECT case_family,full_name,birth_date,sex FROM case_metadata
      WHERE REPLACE(REPLACE(REPLACE(TRIM(COALESCE(national_id,'')),' ',''),'-',''),'.','')=?""", (nid,)).fetchall()
    for row in linked:
        previous = (str(old.get('full_name') or '').strip(), str(old.get('birth_date') or '').strip(), db.normalize_sex(old.get('sex')))
        current = (str(row['full_name'] or '').strip(), str(row['birth_date'] or '').strip(), db.normalize_sex(row['sex']))
        values = (name, dob, sex)
        if all(not cur or not prior or cur in {prior, new} for cur, prior, new in zip(current, previous, values)):
            cx.execute('UPDATE case_metadata SET full_name=?,birth_date=?,sex=?,updated_at=? WHERE case_family=?', (*values, stamp, row['case_family']))


def _validated_return_records(db, data):
    items = data.get('patient_reviews') or []
    result = []; seen = set()
    for pr in items:
        context = pr.get('clinical_context')
        if not isinstance(context, dict): continue
        rk = pr.get('record_key') or ''
        if rk in seen: raise ValueError('Yinelenen klinik çalışma kimliği.')
        seen.add(rk)
        s = db.get_sample(rk)
        if not s: raise ValueError('Klinik dönüşteki çalışma yerel listede yok; önce RUNS taraması yapılmalı: '+str(pr.get('patient_id') or rk))
        canonical = lambda x: re.sub(r'[/_]', '-', str(x).upper().strip())
        if canonical(pr.get('patient_id')) != canonical(s.get('patient_id')) or str(pr.get('sample_type')).upper() != str(s.get('sample_type')).upper():
            raise ValueError('YZ klinik dönüşünde MP / çalışma türü uyuşmuyor.')
        for src, dst in [('diagnosis','reporting_diagnosis'), ('age_at_diagnosis','age_at_reference'),('sex','sex'),('block_id','block_id'),('tumor_percentage','tumor_percentage')]:
            if context.get(src) is not None: normalize_field(dst, context[src])
        result.append((s, pr))
    return result


def import_clinical_return(db, data):
    records = _validated_return_records(db, data); ensure_schema(db)
    digest = hashlib.sha256(dumps({'records': [{'record_key':s['record_key'], 'clinical_context':pr.get('clinical_context'),
        'biomarkers':pr.get('biomarkers')} for s,pr in records]}).encode()).hexdigest()
    with connection(db) as cx:
        row = cx.execute('SELECT result_json FROM clinical_return_imports WHERE document_sha256=?', (digest,)).fetchone()
        if row: return {**json.loads(row[0]), 'duplicate': True}
    count = 0; warnings = []
    for s, pr in records:
        rk = s['record_key']; c = pr['clinical_context']; context = build_record_context(db, rk)
        manual = {}; references = {}; import_conflicts = {}
        for src, dst in [('diagnosis','reporting_diagnosis'), ('age_at_diagnosis','age_at_reference'),('sex','sex'),('block_id','block_id'),('tumor_percentage','tumor_percentage'),('age_reference_date','age_reference_date')]:
            if c.get(src) is None: continue
            # NGS_IMPORT_SOURCE_TYPE_GUARD_V2
            sources = c.get('sources')
            source = sources.get(src) if isinstance(sources, dict) else {}
            if isinstance(source, str):
                source = {'type': source}
            elif not isinstance(source, dict):
                source = {}
            if src != 'age_reference_date' and source.get('type') != 'user_message': continue
            field = context['field_sources'].get(dst, {})
            value = normalize_field(dst, c[src])
            if field.get('manual_override', {}).get('present') and field.get('effective_value') != value:
                warnings.append(f"{s.get('patient_id')} / {LABELS.get(dst,dst)}: yerel düzeltme korundu; dönüşteki değer farklı.")
                import_conflicts[dst] = {'value': value, 'raw': c[src], 'record_key': rk,
                    'origin': 'user_message_import', 'availability': 'observed',
                    'source_reference': source, 'ingested_at': utcnow(), 'manual_import_conflict': True}
                continue
            manual[dst] = c[src]
            references[dst] = source
        if import_conflicts:
            with connection(db) as cx, cx:
                cx.execute('BEGIN IMMEDIATE')
                for name, entry in import_conflicts.items(): _put(cx, rk, name, observation=entry)
                _bump(cx, rk, invalidate=False)
            context = build_record_context(db, rk)
        if manual:
            save_manual_fields(db, rk, manual, expected_revision=context['revision'], origin='user_message_import', source_details=references); count += 1
        fields = {}
        observed_at = ''
        hrd = (pr.get('biomarkers') or {}).get('HRD') or {}; hsource = hrd.get('source') or {}
        for src, dst in [('tumor_type_web','source_tumor_type'),('clinical_diagnosis_web','source_clinical_diagnosis'),('specimen_type','specimen_type'),('specimen_site','specimen_site')]:
            source = (c.get('sources') or {}).get(src) or {}
            if c.get(src) is None or source.get('type') != 'user_provided_mhtml': continue
            if source.get('record_key') and source['record_key']!=rk:
                warnings.append(f"{s.get('patient_id')} / {src}: kaynak çalışma kimliği uyuşmuyor.");continue
            if source.get('patient_id') and source['patient_id']!=pr.get('patient_id'):
                warnings.append(f"{s.get('patient_id')} / {src}: kaynak MP kimliği uyuşmuyor.");continue
            linked = source.get('record_key')==rk or (hsource.get('record_key')==rk and
                source.get('file') and source.get('file')==hsource.get('file') and
                _date_key(source.get('observed_at'))==_date_key(hsource.get('observed_at')) and
                hrd.get('score_read_directly') is True and bool(hsource.get('sha256')))
            if not linked:
                warnings.append(f"{s.get('patient_id')} / {src}: doğrulanmış kaynak bağlantısı yok; etkin değer değiştirilmedi.");continue
            observed_at = source.get('observed_at') or observed_at
            fields[dst] = {'value': c[src], 'raw': c[src], 'availability': 'observed', **source}
        if hrd.get('score_read_directly') is True and hrd.get('derived_from_component_sum') is False and hsource.get('record_key') == rk:
            observed_at = hsource.get('observed_at') or observed_at
            for src, dst in [('score','hrd_score'),('loh','hrd_loh'),('tai','hrd_tai'),('lst','hrd_lst')]:
                if hrd.get(src) is not None: fields[dst] = {'value':hrd[src], 'raw':str(hrd[src]), 'availability':'observed', **hsource}
        if fields:
            ingest_clinical_observation(db, rk, {'record':{'record_key':rk,'run_uuid':s['run_uuid'],'sample_uuid':s['sample_uuid'],'sample_type':s['sample_type']},
                'source_observation':{'kind':'user_supplied_source_reference','identity_verified':True,'observed_at':observed_at,'scope':'one_loaded_sample'}, 'fields':fields})
    result = {'clinical_records': count, 'warnings': warnings, 'duplicate': False}
    with connection(db) as cx, cx:
        cx.execute('INSERT OR IGNORE INTO clinical_return_imports VALUES(?,?,?)', (digest, dumps(result), utcnow()))
    return result


def report_fingerprint(db, rk, text, signer):
    context = build_record_context(db, rk)
    from gennext_selection_store import current_status
    from gennext_fusion_selection_store import current_fusion_status
    snv = current_status(db, rk); rna = current_fusion_status(db, rk)
    # Include values and current source decisions. Re-reading identical values
    # does not revoke approval; changed results or text do.
    def decisions(doc):
        return {k:v for k,v in (doc or {}).items() if k not in {'checked_at','last_attempt_at','updated_at'}}
    payload = {'clinical':context['clinical'], 'biomarkers':context['biomarkers'],
               'snv_selection':decisions(snv),'rna_selection':decisions(rna), 'text':text,'signer':signer}
    return hashlib.sha256(dumps(payload).encode()).hexdigest(), payload, context


def approve_report(db, rk, text, signer, *, expected_fingerprint=None, expected_revision=None):
    if not str(signer or '').strip(): raise ValueError('İmza sahibi seçilmedi.')
    fingerprint, payload, context = report_fingerprint(db, rk, text, signer)
    if (expected_fingerprint is not None and expected_fingerprint != fingerprint) or (expected_revision is not None and expected_revision != context['revision']):
        raise ContextConflict('Onay penceresi açıkken raporun kaynak bilgileri değişti. Güncel içeriği yeniden açıp değerlendirin.')
    if context['missing_fields']:
        raise ValueError('Eksik klinik alanlar: ' + ', '.join(LABELS.get(n,n) for n in context['missing_fields']))
    with connection(db) as cx, cx:
        cx.execute('BEGIN IMMEDIATE')
        current, _, current_context = report_fingerprint(db,rk,text,signer)
        if current != fingerprint or current_context['revision'] != context['revision']:
            raise ContextConflict('Rapor onaylanırken kaynak değişti. Güncel içeriği yeniden değerlendirin.')
        cx.execute("UPDATE reporting_approvals SET state='superseded' WHERE record_key=? AND state='approved'", (rk,))
        cx.execute("INSERT INTO reporting_approvals(record_key,fingerprint,signer,payload_json,state,approved_at) VALUES(?,?,?,?,'approved',?)",
                   (rk, fingerprint, signer, dumps(payload), utcnow()))
    return fingerprint


def report_is_approved(db, rk, text, signer):
    fingerprint, _, context = report_fingerprint(db, rk, text, signer)
    a = context['approval']
    ok = a.get('state') == 'approved' and a.get('fingerprint') == fingerprint
    if not ok and a.get('state') == 'approved':
        with connection(db) as cx, cx:
            cx.execute("UPDATE reporting_approvals SET state='needs_review',invalidated_at=?,invalidation_reason=? WHERE id=?", (utcnow(),'Rapor metni, imzacı veya Selection değişti.',a['id']))
    return ok


def install_reporting_context(StateDB):
    StateDB.get_reporting_context = build_record_context
    StateDB.ingest_clinical_observation = ingest_clinical_observation
    StateDB.save_reporting_fields = save_manual_fields
    StateDB.save_metadata_form = save_metadata_form
    previous_import = StateDB.import_ai_review
    def import_return(self, data, source_path='', source_sha256=''):
        _validated_return_records(self, data)
        result = previous_import(self, data, source_path, source_sha256)
        clinical = import_clinical_return(self, data)
        result['clinical_records'] = clinical['clinical_records']
        result['clinical_duplicate'] = clinical['duplicate']
        result.setdefault('warnings', []).extend(clinical['warnings'])
        return result
    StateDB.import_ai_review = import_return
    def save_biomarkers(self, rk, tmb, msi_pct, hrd, expected_revision=None):
        sample = self.get_sample(rk)
        if not sample or sample.get('sample_type') != 'DNA': raise ValueError('DNA teknik kaydı seçilmelidir.')
        save_manual_fields(self, rk, {'tmb_value':tmb,'msi_percentage':msi_pct,'hrd_score':hrd}, expected_revision=expected_revision)
        return True
    StateDB.save_manual_dna_biomarkers = save_biomarkers
