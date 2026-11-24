"""Read-only, record-scoped reporting archive for NGS Mutasyon Havuzu.

Every row is one exact technical record.  This module never seeds clinical
fields, migrates a database, interprets a missing report as negative, or borrows
a comment/signature selection from another assay.  Saved report versions and
the current manual comment deliberately remain different values.
"""
from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from collections import defaultdict
from pathlib import Path

from gennext_selection_store import run_phase, verified_single_mp_label
from manual_report_closure import STATUSES as MANUAL_CLOSURE_LABELS


REPORT_STATUS_LABELS = {
    'draft': 'Taslak metin', 'archived': 'Arşiv raporu var',
    'approval_history': 'Yerel onay geçmişi', 'missing': 'Rapor kaydı yok',
}
ANALYSIS_STATUS_LABELS = {
    'complete': 'Analiz tamamlandı', 'incomplete': 'Analiz tamamlanmadı',
    'review': 'Kontrol gerekli', 'unknown': 'Henüz doğrulanmadı',
}


def _text(value):
    return '' if value is None else str(value).strip()


def normalize_search(value):
    """Case/accent tolerant Turkish search, without changing displayed text."""
    text = unicodedata.normalize('NFKD', _text(value).casefold())
    text = ''.join(c for c in text if not unicodedata.combining(c)).replace('ı', 'i')
    return ' '.join(text.split())


def _object(raw, *, label='JSON'):
    if raw is None or raw == '':
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(label + ' okunamadı; kayıt değiştirilmedi.') from exc
    if not isinstance(value, dict):
        raise ValueError(label + ' biçimi geçersiz; kayıt değiştirilmedi.')
    return value


def _forms(base, provided):
    if provided is not None and not isinstance(provided, dict):
        raise ValueError('Rapor taslağı verileri geçersiz.')
    path = Path(base) / 'DATA' / 'QC_ENLIL' / 'hasta_form_verileri.json'
    saved = {}
    if path.exists():
        try:
            saved = _object(path.read_text(encoding='utf-8-sig'), label='Kayıtlı rapor taslakları')
        except (OSError, UnicodeError) as exc:
            raise ValueError('Kayıtlı rapor taslakları okunamadı; dosya değiştirilmedi.') from exc
    merged = dict(saved)
    dirty = set()
    changed_comments = set()
    for key, value in (provided or {}).items():
        # Only main-thread-captured technical deltas may overlay the disk
        # snapshot. Unrelated disk keys remain authoritative and current.
        if not isinstance(key, str) or not key.startswith('TECH::'):
            continue
        if not isinstance(value, dict):
            raise ValueError('Ekrandaki teknik rapor taslağı biçimi geçersiz.')
        original = saved.get(key, {})
        if not isinstance(original, dict):
            raise ValueError('Kayıtlı teknik rapor taslağı biçimi geçersiz.')
        current = {**original, **value}
        merged[key] = current
        if _text(current.get('diagnosis_comment')) != _text(original.get('diagnosis_comment')):
            changed_comments.add(key)
        def signature(state):
            try:
                return int(state.get('signature_owner', -1))
            except (TypeError, ValueError, OverflowError):
                return -1
        if (key in changed_comments or signature(current) != signature(original)
                or current.get('report_section_overrides', {}) != original.get('report_section_overrides', {})):
            dirty.add(key)
    return merged, dirty, changed_comments


def _doctor_names():
    # Reuse the same local settings path and defaults as the report editor.
    from qc_enlil import DEFAULT_DOCTOR_NAMES, settings_file_path
    path = settings_file_path()
    defaults = list(DEFAULT_DOCTOR_NAMES)
    if not path.exists():
        return defaults, ''
    try:
        settings = _object(path.read_text(encoding='utf-8-sig'), label='Doktor ayarları')
        names = settings.get('doctor_names', defaults)
        if not isinstance(names, list) or len(names) != len(defaults):
            raise ValueError('Doktor listesi geçersiz.')
        return [_text(name) or defaults[i] for i, name in enumerate(names)], ''
    except (OSError, UnicodeError, ValueError):
        # An unreadable name mapping must not silently attribute a signature.
        return [], 'Doktor adı ayarları okunamadı; imza seçimi numarası gösteriliyor.'


def _read_tables(db):
    path = Path(getattr(db, 'path', db)).resolve()
    cx = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)
    cx.row_factory = sqlite3.Row
    try:
        cx.execute('PRAGMA query_only=ON')
        cx.execute('BEGIN')
        existing = {r[0] for r in cx.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'samples' not in existing:
            raise ValueError('Teknik çalışma tablosu bulunamadı.')
        tables = {}
        for table in ('samples', 'pools', 'case_metadata', 'sample_metadata', 'tasks',
                      'reporting_fields', 'reporting_context_records', 'reporting_legacy_records',
                      'archive_report_documents', 'reporting_approvals',
                      'gennext_selection_status', 'gennext_fusion_status',
                      'manual_report_closure', 'manual_report_closure_events'):
            tables[table] = [dict(r) for r in cx.execute('SELECT * FROM ' + table)] if table in existing else []
        # Only the verified Selection metadata is needed for archive status;
        # do not load candidate JSON or raw variant observations per patient.
        fields = ('record_key', 'analyzed', 'included_count', 'candidate_count',
                  'selection_hash', 'checked_at', 'clinical_scope', 'selected_json')
        for table in ('gennext_selection_current', 'gennext_fusion_current'):
            if table not in existing:
                tables[table] = []
                continue
            available = {r[1] for r in cx.execute('PRAGMA table_info(' + table + ')')}
            columns = ','.join(name for name in fields if name in available)
            tables[table] = [dict(r) for r in cx.execute('SELECT ' + columns + ' FROM ' + table)]
        tables['_existing'] = existing
        return tables
    finally:
        cx.close()


def _effective_fields(rows):
    fields = {}
    for row in rows:
        name = row['field_name']
        manual = row.get('manual_json')
        has_manual = manual is not None
        source = _object(row.get('source_json'), label='Klinik kaynak alanı')
        value = _object(manual, label='Manuel klinik alan') if has_manual else source
        fields[name] = {'value': value.get('value'), 'manual': has_manual,
                        'origin': value.get('origin', ''), 'source': source,
                        'updated_at': row.get('updated_at', '')}
    return fields


def _clinical_values(sample, case, specimen, fields, seeded, legacy):
    def get(name, fallback=None):
        if name in fields:
            return fields[name]['value']
        return None if seeded else fallback

    diagnosis = get('reporting_diagnosis', case.get('diagnosis') if legacy else None)
    f = fields.get('reporting_diagnosis', {})
    if f.get('manual'):
        source = 'Bu teknik çalışmanın manuel tanısı'
        if f.get('origin') == 'legacy_case_context':
            source = 'Eski vaka tanısı · bu çalışmaya aktarılmış'
        if diagnosis is None or diagnosis == '':
            source = 'Bu teknik çalışmada tanı elle temizlenmiş'
    elif diagnosis not in (None, ''):
        source = 'Eski vaka tanısı' if not seeded and not f else 'Kaynak tanısı · patolog onayı bekliyor'
    else:
        source = ''
    if not f.get('manual') and diagnosis is None:
        diagnosis = get('source_tumor_type') or get('source_clinical_diagnosis')
        if diagnosis not in (None, ''):
            source = 'Kaynak tanısı · patolog onayı bekliyor'
    return {
        'diagnosis': _text(diagnosis), 'diagnosis_source': source or 'Tanı kaydı yok',
        'block_id': _text(get('block_id', specimen.get('block_id'))),
        'block_source': _text(get('block_source', specimen.get('block_source'))),
        'tumor_percentage': _text(get('tumor_percentage', specimen.get('tumor_percentage'))),
        'requesting_physician': _text(get('requesting_physician', case.get('requesting_physician'))),
        'record_signer': _text(get('reporting_signer')),
    }


def _signature(form, clinical, case, names):
    raw = form.get('signature_owner')
    try:
        owner = int(raw) if not isinstance(raw, bool) else -1
    except (TypeError, ValueError, OverflowError):
        owner = -1
    if owner in range(4):
        if owner < len(names):
            return names[owner], 'Bu teknik çalışmada seçili imza sahibi', True
        return f'İmza seçimi {owner + 1}', 'Doktor adı ayarı okunamadı', True
    if clinical.get('record_signer'):
        return clinical['record_signer'], 'Teknik kayıtta imzacı alanı · form imzası seçilmemiş', False
    if _text(case.get('reporting_physician')):
        return _text(case['reporting_physician']), 'Vaka kayıtlı doktoru · bu çalışmada imza seçilmemiş', False
    return '', 'İmza sahibi seçilmemiş', False


def _analyzed_indicator(sample, snv_status, fusion_status):
    """The GenNext switch is not a manually confirmed signed report."""
    phase = run_phase(sample)
    if phase != 'ready':
        return 'RUN henüz tamamlanmadı / kontrol gerekli'
    assay = _text(sample.get('sample_type')).upper()
    statuses = []
    if assay in ('DNA', 'DNA+RNA'):
        statuses.append(('DNA', snv_status))
    if assay in ('RNA', 'DNA+RNA'):
        statuses.append(('RNA', fusion_status))
    if not statuses:
        return 'Tür doğrulanmadı'
    result = []
    for label, status in statuses:
        latest = status.get('analyzed')
        checked = _text(status.get('last_run_check'))
        if not checked or latest not in (0, 1):
            result.append(label + ': okunmadı')
        elif latest == 1:
            result.append(label + ': AÇIK (GenNext)')
        else:
            result.append(label + ': KAPALI (GenNext)')
    return ' · '.join(result)


def _report_mutation_lines(comment):
    """Index exact user-authored sentences; never generate a molecular claim."""
    lines = [line.strip() for line in str(comment or '').splitlines()]
    return [line for line in lines if re.search(r'\bSAPTANMIŞTIR\b|\bSAPTANMAM[Iİ]ŞTIR\b', line, re.I)]


def _selection_state(sample, current, status, rna=False):
    phase = run_phase(sample)
    if phase != 'ready':
        return phase
    if not current:
        if status.get('analyzed') == 0:
            return 'analysis_incomplete'
        if not verified_single_mp_label(sample.get('patient_id')):
            return 'identity_review'
        return 'awaiting_selection'
    latest = status.get('analyzed')
    attempt = _text(status.get('last_attempt_at'))
    checked = _text(current.get('checked_at'))
    fresh = not (attempt and checked and attempt > checked)
    verified = (fresh and latest is not None and latest == current.get('analyzed')
                and not status.get('last_error'))
    if rna and current.get('clinical_scope') != 'RNA_FUSION_ONLY':
        verified = False
    if not verified:
        return 'needs_refresh'
    if not current.get('analyzed'):
        return 'analysis_incomplete'
    if not verified_single_mp_label(sample.get('patient_id')):
        return 'identity_review'
    return 'complete'


def _analysis(sample, snv, snv_status, fusion, fusion_status):
    assay = _text(sample.get('sample_type')).upper()
    entries = []
    if assay in ('DNA', 'DNA+RNA'):
        entries.append(('DNA SNV/indel Selection', _selection_state(sample, snv, snv_status)))
    if assay in ('RNA', 'DNA+RNA'):
        entries.append(('RNA Fusion Selection', _selection_state(sample, fusion, fusion_status, True)))
    if not entries:
        entries = [('Çalışma', run_phase(sample))]
    states = {state for _, state in entries}
    if states <= {'complete'}:
        key = 'complete'
    elif states & {'run_failed', 'identity_review', 'needs_refresh'}:
        key = 'review'
    elif states & {'run_incomplete', 'analysis_incomplete'}:
        key = 'incomplete'
    else:
        key = 'unknown'
    labels = {'complete': 'Analyzed ve Selection doğrulandı', 'run_failed': 'RUN / örnek hatalı',
              'run_incomplete': 'RUN / örnek işlemi sürüyor', 'run_unknown': 'RUN durumu doğrulanmadı',
              'analysis_incomplete': 'Analyzed kapalı; analiz bitmemiş',
              'identity_review': 'MP / teknik kimlik kontrolü gerekli',
              'needs_refresh': 'Selection yeniden doğrulanmalı',
              'awaiting_selection': 'Selection henüz doğrulanmadı', 'ready': 'Selection henüz doğrulanmadı'}
    detail = '\n'.join(scope + ': ' + labels.get(state, state) for scope, state in entries)
    return key, detail


def _history(documents, approvals):
    history = []
    for doc in documents:
        confirmed = doc.get('document_status') == 'user_confirmed'
        history.append({
            'id': 'archive:' + str(doc.get('id', '')), 'title': _text(doc.get('title')) or 'Arşiv raporu',
            'status': 'Kullanıcı onaylı yerel sürüm' if confirmed else 'Arşiv taslağı',
            'created_at': _text(doc.get('created_at')), 'text': _text(doc.get('report_text')),
            'source': 'Rapor arşivi', 'signer': '', 'version': doc.get('version'),
            'e_signature_verified': False,
        })
    for approval in approvals:
        payload = _object(approval.get('payload_json'), label='Yerel rapor onay kaydı')
        state = approval.get('state')
        label = {'approved': 'Kaydedilmiş yerel içerik onayı', 'superseded': 'Önceki yerel onay',
                 'needs_review': 'Önceki onay · yeniden kontrol gerekli'}.get(state, 'Yerel onay geçmişi')
        history.append({
            'id': 'approval:' + str(approval.get('id', '')), 'title': 'Rapor içeriği onayı',
            'status': label, 'created_at': _text(approval.get('approved_at')),
            'text': _text(payload.get('text')), 'source': 'Yerel onay geçmişi',
            'signer': _text(approval.get('signer')), 'clinical': payload.get('clinical') or {},
            'biomarkers': payload.get('biomarkers') or {},
            'invalidation_reason': _text(approval.get('invalidation_reason')),
            'e_signature_verified': False,
        })
    return sorted(history, key=lambda row: (row['created_at'], row['id']), reverse=True)


def _related_history(reports):
    """Normalize case-linked imports without asserting a technical match."""
    history = []
    for entry in reports:
        prior_mp = _text(entry.get('prior_mp'))
        assay = _text(entry.get('assay'))
        source_key = _text(entry.get('source'))
        screen_read = source_key == 'enlil_open_screen_jab_read_only'
        source = 'Açık Enlil ekranından aktarılan metin' if screen_read else 'Manuel yapıştırılmış Enlil metni'
        history.append({
            **entry,
            'id': 'prior:' + _text(entry.get('report_id')),
            'title': f"{prior_mp or 'MP belirtilmemiş'} · {assay or 'Tür belirtilmemiş'} · Eski rapor (vaka ilişkisi)",
            'status': ('Ekrandan aktarılmış eski rapor' if screen_read else 'Manuel yapıştırılmış eski rapor')
                      + ' · e-imza doğrulanmadı · teknik eşleşme değil',
            'created_at': _text(entry.get('imported_at')), 'text': _text(entry.get('text')),
            'source': source, 'source_key': source_key, 'signer': '',
            'clinical': {'tumor_diagnosis': _text(entry.get('tumor_diagnosis'))},
            'e_signature_verified': False,
            'scope': 'case_family',
            'scope_label': 'Vakayla ilişkilendirilmiş eski rapor · teknik eşleşme değil',
        })
    return sorted(history, key=lambda row: (row['created_at'], row['id']), reverse=True)


def load_archive_rows(db, base, form_data=None):
    """Read all studies with a dedicated SQLite connection; safe in a worker.

    ``form_data`` is an optional overlay of main-thread-captured *changed*
    TECH states.  Unmentioned keys are read from the latest disk file.  The
    returned current_comment_status/form_saved label any unsaved overlay.
    """
    tables = _read_tables(db)
    forms, dirty_forms, dirty_comments = _forms(base, form_data)
    names, doctor_warning = _doctor_names()
    by_key = lambda table, key='record_key': {r[key]: r for r in tables[table]}
    cases = by_key('case_metadata', 'case_family')
    specimens = by_key('sample_metadata')
    pools = {(r.get('pool_name'), r.get('run_uuid')): r for r in tables['pools']}
    contexts = by_key('reporting_context_records')
    legacy = {r['record_key'] for r in tables['reporting_legacy_records']}
    pre_context_schema = 'reporting_legacy_records' not in tables['_existing']
    grouped = {}
    for table in ('reporting_fields', 'tasks', 'archive_report_documents', 'reporting_approvals'):
        grouped[table] = defaultdict(list)
        for entry in tables[table]:
            grouped[table][entry['record_key']].append(entry)
    snv, snv_status = by_key('gennext_selection_current'), by_key('gennext_selection_status')
    fusion, fusion_status = by_key('gennext_fusion_current'), by_key('gennext_fusion_status')
    manual_closures = by_key('manual_report_closure')
    manual_events = defaultdict(list)
    for event in tables['manual_report_closure_events']:
        manual_events[event['record_key']].append(event)
    from gennext_selection_store import normalize_snapshot
    sample_by_key = {r['record_key']: r for r in tables['samples']}
    selections = {}
    fusion_selections = {}
    from gennext_fusion_selection_store import normalize_fusion_snapshot
    for key, data in fusion.items():
        st = fusion_status.get(key, {})
        record = sample_by_key.get(key, {})
        enriched = dict(data, latest_analyzed=st.get('analyzed'),
                        last_attempt_at=st.get('last_attempt_at'), last_error=st.get('last_error'),
                        run_phase=run_phase(record))
        selection = normalize_fusion_snapshot(enriched, sample=record)
        confirmed = selection['result_status'] == 'fusion_selected_positive'
        fusion_selections[key] = {
            'status': selection['result_status'],
            'checked_at': selection.get('checked_at') or '',
            'included_count': selection.get('included_count') or 0,
            'selected': [dict(fusion=_text(v.get('fusion')), callers=_text(v.get('callers')),
                              key=_text(v.get('variant_key'))) for v in selection.get('selected', [])
                         if isinstance(v, dict)] if confirmed else [],
        }
    for key, data in snv.items():
        st = snv_status.get(key, {})
        record = sample_by_key.get(key, {})
        enriched = dict(data, latest_analyzed=st.get('analyzed'),
                        last_attempt_at=st.get('last_attempt_at'), last_error=st.get('last_error'),
                        run_phase=run_phase(record), patient_id=record.get('patient_id'))
        selection = normalize_snapshot(enriched)
        selections[key] = {
            'status': selection['result_status'],
            'checked_at': selection.get('checked_at') or '',
            'included_count': selection.get('included_count') or 0,
            'selected': [dict(gene=_text(v.get('gene')), protein=_text(v.get('protein')),
                              cdna=_text(v.get('cdna')), vaf=_text(v.get('vaf')),
                              key=_text(v.get('variant_key'))) for v in selection.get('selected', [])
                         if isinstance(v, dict)] if selection['result_status']=='selected_positive' else [],
        }
    related_cache = {}
    result = []
    for sample in tables['samples']:
        rk = sample['record_key']
        family = _text(sample.get('case_family') or sample.get('patient_id'))
        case, specimen = cases.get(family, {}), specimens.get(rk, {})
        fields = _effective_fields(grouped['reporting_fields'][rk])
        clinical = _clinical_values(sample, case, specimen, fields, rk in contexts, rk in legacy or pre_context_schema)
        state = forms.get('TECH::' + rk, {})
        if not isinstance(state, dict):
            raise ValueError('Teknik çalışmanın rapor taslağı biçimi geçersiz: ' + _text(sample.get('patient_id')))
        comment = _text(state.get('diagnosis_comment'))
        report_mutation_lines = _report_mutation_lines(comment)
        signer, doctor_source, signature_selected = _signature(state, clinical, case, names)
        docs = grouped['archive_report_documents'][rk]
        approvals = grouped['reporting_approvals'][rk]
        history = _history(docs, approvals)
        manual_closure = manual_closures.get(rk, {})
        closure_key = _text(manual_closure.get('status')) or 'none'
        closure_label = MANUAL_CLOSURE_LABELS.get(closure_key, 'Manuel kapanis kaydi yok')
        recent_events = sorted(manual_events.get(rk, []),
                               key=lambda item: int(item.get('id') or 0), reverse=True)[:5]
        if any(d.get('document_status') == 'user_confirmed' for d in docs):
            report_key = 'archived'
        elif approvals:
            report_key = 'approval_history'
        elif comment or docs:
            report_key = 'draft'
        else:
            report_key = 'missing'
        analysis_key, analysis_detail = _analysis(sample, snv.get(rk, {}), snv_status.get(rk, {}),
                                                 fusion.get(rk, {}), fusion_status.get(rk, {}))
        statuses = {_text(r.get('status')) for r in grouped['tasks'][rk]}
        if not statuses:
            collection_key, collection_label = 'unknown', 'Toplama kaydı yok'
        elif statuses <= {'done', 'not_available'}:
            collection_key, collection_label = 'complete', 'Toplama tamam'
        elif statuses & {'error', 'blocked'}:
            collection_key, collection_label = 'error', 'Toplama hatası'
        elif 'waiting' in statuses:
            collection_key, collection_label = 'waiting', 'Toplama bekliyor'
        else:
            collection_key, collection_label = 'incomplete', 'Toplama eksik'
        warnings = []
        form_saved = 'TECH::' + rk not in dirty_forms
        if not form_saved:
            warnings.append('Ekrandaki taslak değişiklikleri henüz kaydedilmedi; Raporlama ekranından Taslağı Kaydet düğmesini kullanın.')
        if doctor_warning and signature_selected:
            warnings.append(doctor_warning)
        if closure_key == 'reported_negative' and (
            selections.get(rk, {}).get('status') == 'selected_positive' or
            fusion_selections.get(rk, {}).get('status') == 'fusion_selected_positive'
        ):
            warnings.append('Manuel NEGATİF rapor sonucu ile GenNext Selection pozitif görünüyor; klinik gerekçe kontrol edilmeli.')
        if approvals:
            warnings.append('Yerel onay geçmişi kayıtlı rapor sürümüne aittir; güncel taslağın onayı raporlama ekranında doğrulanır.')
        if family not in related_cache:
            from manual_ngs_report_store import load_reports
            try:
                related_cache[family] = load_reports(base, family) if family else []
            except ValueError:
                related_cache[family] = None
        related = related_cache[family]
        if related is None:
            warnings.append('Vakayla ilişkilendirilmiş eski rapor dosyası okunamadı.')
        related_history = _related_history(related or [])
        updated = max([_text(sample.get('updated_at')), _text(specimen.get('updated_at')),
                       _text(contexts.get(rk, {}).get('updated_at'))] +
                      [h['created_at'] for h in history] + [f['updated_at'] or '' for f in fields.values()] +
                      [_text(manual_closure.get('updated_at'))])
        row = {**sample, **clinical,
               'patient_id': _text(sample.get('patient_id')), 'case_family': family,
               'pool_name': _text(sample.get('pool_name')), 'sample_type': _text(sample.get('sample_type')).upper(),
               'full_name': _text(case.get('full_name')),
               'age': _text(case.get('age')), 'birth_date': _text(case.get('birth_date')),
               'sex': _text(case.get('sex')), 'national_id': _text(case.get('national_id')),
               'run_date': _text(pools.get((sample.get('pool_name'), sample.get('run_uuid')), {}).get('run_date')),
               'signer': signer, 'doctor_source': doctor_source, 'signature_selected': signature_selected,
               'reporting_physician': _text(case.get('reporting_physician')),
               'comment': comment, 'comment_preview': ' '.join(comment.split())[:180],
               'current_comment_status': ('Ekrandaki taslak (kaydedilmedi)' if not form_saved else
                                          'Kayıtlı yorum taslağı' if comment else 'Bu teknik çalışmaya ait yorum kaydı yok'),
               'comment_saved': 'TECH::' + rk not in dirty_comments, 'form_saved': form_saved,
               'report_status_key': report_key, 'report_status_label': REPORT_STATUS_LABELS[report_key],
               'manual_closure_status_key': closure_key,
               'manual_closure_status_label': closure_label,
               'manual_closure_note': _text(manual_closure.get('note')),
               'manual_closure_by': _text(manual_closure.get('recorded_by')),
               'manual_closure_at': _text(manual_closure.get('updated_at')),
               'manual_closure_event_id': manual_closure.get('last_event_id'),
               'manual_closure_history': recent_events,
               'analysis_status_key': analysis_key, 'analysis_status_label': ANALYSIS_STATUS_LABELS[analysis_key],
               'analysis_detail': analysis_detail,
               'gennext_analyzed_label': _analyzed_indicator(sample, snv_status.get(rk, {}), fusion_status.get(rk, {})),
               'verified_selection': selections.get(rk, {}),
               'verified_fusion_selection': fusion_selections.get(rk, {}),
               'report_mutation_lines': report_mutation_lines,
               'collection_status_key': collection_key, 'collection_status_label': collection_label,
               'updated_at': updated, 'history': history, 'related_history': related_history, 'warnings': warnings}
        search_values = [row.get(key) for key in ('patient_id', 'case_family', 'full_name', 'pool_name',
                         'sample_type', 'diagnosis', 'block_id', 'block_source', 'signer', 'national_id',
                         'reporting_physician', 'requesting_physician', 'comment')]
        for selection in (row['verified_selection'], row['verified_fusion_selection']):
            for mutation in selection.get('selected') or []:
                search_values.extend(mutation.get(key) for key in
                                     ('gene', 'protein', 'cdna', 'fusion', 'callers', 'key'))
        search_values.extend(report_mutation_lines)
        search_values.extend([closure_label, _text(manual_closure.get('note'))])
        search_values.extend(h.get(key) for h in history for key in ('text', 'title', 'signer'))
        search_values.extend(h.get(key) for h in related_history
                             for key in ('text', 'title', 'tumor_diagnosis', 'scope_label'))
        row['search_text'] = normalize_search(' '.join(_text(v) for v in search_values))
        result.append(row)
    return sorted(result, key=lambda r: (r['run_date'] or r['updated_at'], int(r.get('id') or 0)), reverse=True)


def filter_archive_rows(rows, query='', pool='', assay='', report_status='', analysis_status='',
                        closure_status=''):
    """AND match search tokens across all displayed/current and exact-history fields."""
    words = normalize_search(query).split()
    requested_assay = _text(assay).upper()
    allowed_assays = {requested_assay, 'DNA+RNA'} if requested_assay in {'DNA', 'RNA'} else {requested_assay}
    matches = []
    for row in rows:
        if pool and row.get('pool_name') != pool:
            continue
        if requested_assay and _text(row.get('sample_type')).upper() not in allowed_assays:
            continue
        if report_status and row.get('report_status_key') != report_status:
            continue
        if analysis_status and row.get('analysis_status_key') != analysis_status:
            continue
        if closure_status and row.get('manual_closure_status_key') != closure_status:
            continue
        if any(word not in row.get('search_text', '') for word in words):
            continue
        matches.append(row)
    return matches


def format_archive_summary(row):
    if not row:
        return ''
    value = lambda key: _text(row.get(key)) or '—'
    dna_sel = row.get('verified_selection') or {}
    rna_sel = row.get('verified_fusion_selection') or {}
    lines = [f"Tümör tanısı: {value('diagnosis')}",
             f"{value('patient_id')} · {value('pool_name')} · {value('sample_type')}",
             f"Hasta: {value('full_name')}",
             f"Blok: {value('block_id')} · Kaynak: {value('block_source')}",
             f"İmzacı / raporlayan: {value('signer')}",
             f"Doktor kaynağı: {value('doctor_source')}",
             f"GenNext Analyzed: {value('gennext_analyzed_label')}",
             f"Analiz/Selection: {value('analysis_status_label')} · Enlil raporu: {value('report_status_label')}",
             f"Manuel rapor kapanisi: {value('manual_closure_status_label')}",
             f"Kaydeden: {value('manual_closure_by')} · Tarih: {value('manual_closure_at')}",
             f"Kapanis notu: {value('manual_closure_note')}",
             f"Güncel yorum durumu: {value('current_comment_status')}",
             '', 'GenNext güncel DNA Selection: ' + (_text(dna_sel.get('status')) or 'doğrulanmadı'),
             'GenNext güncel RNA Selection: ' + (_text(rna_sel.get('status')) or 'doğrulanmadı')]
    for item in dna_sel.get('selected') or []:
        lines.append('  DNA: ' + ' · '.join(filter(None,
            [_text(item.get('gene')), _text(item.get('protein')), _text(item.get('cdna'))])))
    for item in rna_sel.get('selected') or []:
        lines.append('  RNA füzyon: ' + _text(item.get('fusion')) +
                     (' · Caller: '+_text(item.get('callers')) if item.get('callers') else ''))
    report_lines = row.get('report_mutation_lines') or []
    if report_lines:
        lines.extend(['', 'Enlil kaydedilmiş özgün rapor cümleleri:', *report_lines])
    lines.extend(['', 'Raporlama — Mutasyon / Yorum:',
                  row.get('comment') or 'Bu teknik çalışmaya ait mutasyon / yorum kaydı yok.'])
    return '\n'.join(lines)
