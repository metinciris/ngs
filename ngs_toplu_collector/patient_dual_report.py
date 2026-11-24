"""Read-only DNA/RNA report and prior-finding summaries for one local case.

No same-name matching.  DNA/RNA comparisons require the SAME normalized case
family, even when samples were sequenced in separate runs.  A matching identity
number is only used to show *separately labelled* earlier cases and never to
claim the same block or merge assay reports.
"""
from __future__ import annotations

import json
from pathlib import Path

from patient_workspace_model import case_key
from patient_assay_links import manual_related_records


def _text(value):
    return str(value or '').strip()


def _forms(base: Path, provided=None):
    if provided is not None:
        return provided if isinstance(provided, dict) else {}
    path = Path(base) / 'DATA' / 'QC_ENLIL' / 'hasta_form_verileri.json'
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except (OSError, UnicodeError, ValueError, TypeError):
        return {}


def case_records(db, record_key):
    current = db.get_sample(record_key)
    if not current:
        return []
    family = case_key(dict(current))
    return [dict(row) for row in db.samples()
            if family and case_key(dict(row)) == family
            and _text(row['sample_type']).upper() in ('DNA', 'RNA')]


def linked_case_records(db,base,record_key):
    records=case_records(db,record_key)
    if not records:return []
    return records+manual_related_records(base,db,[r['record_key'] for r in records])


def linked_prior_cases(db, current_record, related):
    """Other MP cases only when a *validated* local identity is consistent.

    Their reports are NOT included in the current DNA/RNA panes.  Also check
    DOB/sex conflicts, which identity-based archive grouping treats as unsafe.
    """
    if not current_record:
        return []
    family = _text(current_record.get('case_family') or current_record.get('patient_id'))
    cm = db.get_case_metadata(family)
    ids = db.related_patient_ids_for_case(family)
    rows = []
    related_keys = {r['record_key'] for r in related}
    seen = set()
    for sample in db.samples():
        row = dict(sample)
        if _text(row.get('patient_id')) not in ids or row['record_key'] in related_keys:
            continue
        another_family = _text(row.get('case_family') or row.get('patient_id'))
        other = db.get_case_metadata(another_family)
        if (cm.get('birth_date') and other.get('birth_date') and
                cm['birth_date'] != other['birth_date']):
            continue
        if (cm.get('sex') and other.get('sex') and
                db.normalize_sex(cm['sex']) != db.normalize_sex(other['sex'])):
            continue
        key = (another_family, row['record_key'])
        if key in seen:
            continue
        seen.add(key)
        rows.append({'record_key': row['record_key'],
                     'patient_id': _text(row.get('patient_id')),
                     'pool_name': _text(row.get('pool_name')),
                     'sample_type': _text(row.get('sample_type')),
                     'diagnosis': _text(other.get('diagnosis'))})
    return rows


def sample_description(db, row):
    md = db.get_sample_metadata(row['record_key'])
    return {'record_key': row['record_key'], 'patient_id': _text(row.get('patient_id')),
            'pool_name': _text(row.get('pool_name')), 'sample_type': _text(row.get('sample_type')),
            'block_id': _text(md.get('block_id')), 'block_source': _text(md.get('block_source')),
            'tumor_percentage': _text(md.get('tumor_percentage')),
            'sample_note': _text(md.get('note')),
            'is_rerun': bool(row.get('is_rerun'))}


def order_for_assay(db, records, assay, current_record_key):
    """Favor the exact selected assay and a confirmed exact same block.

    A missing block must not be interpreted as equality. Never pair by name.
    """
    options = [r for r in records if _text(r.get('sample_type')).upper() == assay]
    current = db.get_sample(current_record_key)
    current_block = _text(db.get_sample_metadata(current_record_key).get('block_id')).casefold() if current else ''
    def rank(r):
        target_block = _text(db.get_sample_metadata(r['record_key']).get('block_id')).casefold()
        return (0 if r['record_key'] == current_record_key else 1,
                0 if current_block and target_block == current_block else 1,
                1 if r.get('is_rerun') else 0,
                -int(r.get('id') or 0))
    return sorted(options, key=rank)


def pairing_warning(left, right):
    if not left or not right:
        return 'DNA veya RNA teknik kaydı eksik. Diğer analizin sonucu negatif kabul edilmez.'
    left_id, right_id = _text(left.get('block_id')).casefold(), _text(right.get('block_id')).casefold()
    left_source, right_source = _text(left.get('block_source')).casefold(), _text(right.get('block_source')).casefold()
    if left_id and right_id and left_id != right_id:
        return 'DİKKAT: DNA ve RNA blok numaraları farklı. Sonuçlar otomatik tek blok raporuna birleştirilmez.'
    if left_source and right_source and left_source != right_source:
        return 'DİKKAT: Blok kaynakları farklı. Aynı blok olarak işaretlenmedi.'
    if not (left_id and right_id):
        return 'DNA/RNA blok eşleşmesi doğrulanmadı (en az bir kayıt boş). Aynı blok olduğu varsayılmadı.'
    message = 'Aynı blok numarası kaydedilmiş (teknik kayıtlarda). Bu, klinik onay yerine geçmez.'
    if left.get('diagnosis') and right.get('diagnosis') and left['diagnosis'].casefold()!=right['diagnosis'].casefold():
        message += ' DİKKAT: İki teknik kaydın kayıtlı tümör tanıları farklı; klinik eşleştirmeyi kontrol edin.'
    return message


def saved_comment(db, base, record_key, form_data=None):
    state = _forms(base, form_data).get('TECH::' + record_key, {})
    return _text(state.get('diagnosis_comment')) if isinstance(state, dict) else ''


def _archive_docs(db, record_key):
    if not db._table_exists('archive_report_documents'):
        return []
    return [dict(r) for r in db.cx.execute(
        'SELECT version,title,report_text,document_status,created_at FROM archive_report_documents '
        'WHERE record_key=? ORDER BY CASE document_status WHEN ? THEN 0 ELSE 1 END, version DESC',
        (record_key,'user_confirmed')).fetchall()]


def _assay_state(db, record_key):
    if not db._table_exists('archive_assay_results'):
        return ('', '')
    row = db.cx.execute('SELECT result_state,note FROM archive_assay_results WHERE record_key=?',
                        (record_key,)).fetchone()
    return (_text(row['result_state']), _text(row['note'])) if row else ('', '')


def report_for_record(db, base, record_key, form_data=None):
    """Separate physician-saved report, unconfirmed form draft, and archive calls.

    Raw GenNext fusion candidates are never labelled a signed clinical report.
    """
    row = db.get_sample(record_key)
    if not row:
        return {'record_key': record_key, 'title': 'Kayıt yok', 'text': 'Yerel teknik kayıt bulunamadı.', 'comment': ''}
    d = sample_description(db, dict(row))
    cm=db.get_case_metadata(row['case_family'] or row['patient_id'] or '')
    diagnosis=_text(cm.get('diagnosis'))
    d['diagnosis']=diagnosis
    forms = _forms(base, form_data)
    state = forms.get('TECH::' + record_key, {})
    state = state if isinstance(state, dict) else {}
    comment = _text(state.get('diagnosis_comment'))
    docs = _archive_docs(db, record_key)
    result, result_note = _assay_state(db, record_key)
    prefix = (f"{d['pool_name']} | {d['patient_id']} | {d['sample_type']}\n"
              f"Tümör tanısı (kayıtlı): {diagnosis or 'GİRİLMEMİŞ'}\n"
              f"Blok: {d['block_id'] or 'belirtilmedi'} | Tümör: %{d['tumor_percentage'] or '—'}\n")
    if docs:
        doc = docs[0]
        status = 'KULLANICI ONAYLI YEREL RAPOR SÜRÜMÜ (E-İMZA DOĞRULANMADI)' if doc['document_status'] == 'user_confirmed' else 'ARŞİV TASLAĞI — ONAYLI DEĞİL'
        text = prefix + f"\n[{status} | Sürüm {doc['version']} | {doc['created_at']}]\n\n" + _text(doc['report_text'])
        if comment:
            text += '\n\n[STANDART METİNLER: AYRI KAYITLI YORUM]\n' + comment
        kind = status
    elif (comment or _text(state.get('tmb')) or _text(state.get('msi_pct')) or
          _text(state.get('hrd_score')) or state.get('dna_positive') or state.get('rna_positive')):
        kind = 'STANDART METİNLER TASLAĞI — ONAYLI RAPOR DEĞİL'
        fields = []
        if comment:fields.append('Mutasyon / füzyon yorumu:\n'+comment)
        for key, label in (('tmb','TMB (ham taslak)'),('msi_pct','MSI % (ham taslak)'),('hrd_score','HRD (ham taslak)')):
            if _text(state.get(key)):fields.append(f'{label}: '+_text(state.get(key)))
        if d['sample_type'].upper() == 'DNA' and state.get('dna_positive'):
            fields.append('DNA pozitif seçimleri (kullanıcı taslağı): '+', '.join(map(str,state['dna_positive'])))
        if d['sample_type'].upper() == 'RNA' and state.get('rna_positive'):
            fields.append('RNA füzyon seçimleri (kullanıcı taslağı): '+', '.join(map(str,state['rna_positive'])))
        text = prefix + '\n['+kind+']\n\n' + ('\n\n'.join(fields) or 'Henüz yorum girilmemiş.')
    else:
        kind = 'RAPOR / TASLAK KAYDI YOK'
        text = prefix + '\n[RAPOR / TASLAK KAYDI YOK]\nGenNext ham verisinin toplanmış olması klinik rapor anlamına gelmez.'
    if result:
        text += f'\n\n[MANUEL ARŞİV RAPOR SONUCU: {result}]'
        if result_note:text += '\n'+result_note
    return {**d, 'title': kind, 'text': text, 'comment': comment,
            'archive_count': len(docs), 'assay_result_state': result}


def prior_findings(db, current_record, related=None):
    """Confirmed local history + separately labelled GenNext selected observations."""
    current = db.get_sample(current_record)
    if not current:
        return []
    related = related if related is not None else case_records(db, current_record)
    keys = {r['record_key'] for r in related}
    if not keys:
        return []
    if not db._table_exists('reported_variant_history'):
        return []
    placeholders = ','.join('?' for _ in keys)
    sql = ('SELECT record_key,patient_id,sample_type,pool_name,gene,cdna,protein,feature,variant_type,'
           'source,source_text,created_at,confirmed FROM reported_variant_history '
           f'WHERE record_key IN ({placeholders}) ORDER BY id DESC LIMIT 300')
    result = []
    for row in db.cx.execute(sql, sorted(keys)).fetchall():
        d=dict(row)
        if d.get('confirmed'):
            result.append(d)
    # The viewer's manual 'reported' decision is not equivalent to a signed
    # report. Show it separately and do not call it confirmed clinical history.
    if db._table_exists('variant_decisions'):
        known={(r['record_key'],r.get('variant_key')) for r in result}
        sql_dec=(f'SELECT record_key,variant_key,status,note,updated_at FROM variant_decisions '
                 f'WHERE record_key IN ({placeholders}) AND status=? ORDER BY updated_at DESC LIMIT 300')
        for row in db.cx.execute(sql_dec,sorted(keys)+['reported']).fetchall():
            d=dict(row)
            if (d['record_key'],d['variant_key']) in known:continue
            sample=db.get_sample(d['record_key']) or {}
            result.append({'record_key':d['record_key'],'patient_id':sample['patient_id'] if sample else '',
                           'sample_type':sample['sample_type'] if sample else '',
                           'pool_name':sample['pool_name'] if sample else '',
                           'gene':d['variant_key'],'cdna':'','protein':'','feature':'',
                           'variant_type':'Manuel karar','source':'MANUEL RAPOR KARARI — ONAYLI RAPOR DEĞİL',
                           'source_text':d.get('note') or '', 'created_at':d.get('updated_at') or '',
                           'confirmed':0})
    # GenNext Analyzed + Selection is shown as its own unsigned observation.
    # It never becomes a manually written report or an automatically signed variant.
    if db._table_exists('gennext_selection_current'):
        from gennext_selection_store import current_status
        for rk in sorted(keys):
            gn=current_status(db,rk)
            if not gn or gn.get('result_status')!='selected_positive':continue
            sample=db.get_sample(rk)
            if not sample:continue
            for v in gn.get('selected') or []:
                result.append({'record_key':rk,'patient_id':sample.get('patient_id') or '',
                               'sample_type':sample.get('sample_type') or '',
                               'pool_name':sample.get('pool_name') or '',
                               'gene':v.get('gene') or '', 'cdna':v.get('cdna') or '',
                               'protein':v.get('protein') or '', 'feature':v.get('genomic') or '',
                               'variant_type':'GenNext SNV/indel Selection',
                               'source':'GenNext Analyzed + Selection - imzali Enlil raporu degil',
                               'source_text':'Platformdaki secili varyant; manuel metne kopyalanmadi',
                               'created_at':gn.get('checked_at') or '', 'confirmed':0})
    return result


def build_context(db, base, current_record, form_data=None):
    current = db.get_sample(current_record)
    if not current:return {'found': False, 'related': [], 'DNA': [], 'RNA': [], 'prior': [], 'linked': []}
    current = dict(current)
    records = linked_case_records(db,base,current_record)
    family = _text(current.get('case_family') or current.get('patient_id'))
    cm = db.get_case_metadata(family)
    return {'found': True, 'current': current, 'case_family': family,
            'diagnosis': _text(cm.get('diagnosis')),
            'age': _text(cm.get('age')),
            'case_note': _text(cm.get('note')),
            'related': records,
            'DNA': order_for_assay(db, records, 'DNA', current_record),
            'RNA': order_for_assay(db, records, 'RNA', current_record),
            'prior': prior_findings(db, current_record, records),
            'linked': linked_prior_cases(db, current, records),
            'comments': {r['record_key']: saved_comment(db, base, r['record_key'], form_data)
                         for r in records}}
