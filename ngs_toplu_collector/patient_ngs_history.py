"""Read-only local NGS history by validated identity or Enlil file number.

No name-only matching; no automatic merging of old calls into current tests.
Returned UI data may contain local report text (patient data) and must NEVER be
serialized into an AI ZIP. ai_history_context() returns a strict allow-list.
"""
from __future__ import annotations

import re
import json
import unicodedata
from collections import defaultdict
from pathlib import Path
from previous_report_summary import parse_previous_report, safe_ai_summary, formatted_summary
from manual_ngs_report_store import load_reports
from enlil_lookup import biopsy_key


DRIVER_GENES = frozenset(('EGFR', 'KRAS', 'ALK', 'ROS1', 'RET', 'MET', 'BRAF',
                          'ERBB2', 'NTRK1', 'NTRK2', 'NTRK3', 'NRG1'))


def _t(value):
    return str(value or '').strip()


def _nid(db, value):
    digits = ''.join(x for x in _t(value) if x in '0123456789')
    return digits if db.validate_national_id(digits) else ''


def _file_no(value):
    """Format-only normalisation; reject common placeholders and short IDs."""
    raw = unicodedata.normalize('NFKC', _t(value)).upper()
    if not raw or len(raw) > 50 or not re.fullmatch(r'[A-Z0-9/_. -]+', raw):
        return ''
    result = re.sub(r'[/_. -]', '', raw)
    return result if len(result) >= 5 and len(result) <= 35 and sum(c.isdigit() for c in result) >= 5 and len(set(result)) > 1 else ''


def _demographic_conflict(db, current, other):
    a = _t(current.get('birth_date'))
    b = _t(other.get('birth_date'))
    if a and b and a != b:
        return True
    sx = db.normalize_sex(current.get('sex'))
    sy = db.normalize_sex(other.get('sex'))
    return bool(sx and sy and sx != sy)


def _strict_mp_family(value):
    """Canonicalize only a single, entire MP protocol, including ENLIL //.

    Composite labels, arbitrary free text and patient identity numbers must
    never be converted to an MP match.
    """
    try:
        from enlil_lookup import normalize_biopsy_query
        return normalize_biopsy_query(_t(value))
    except (ValueError,TypeError):
        return ''


def _same_case_family(one, two):
    a,b=_strict_mp_family(one),_strict_mp_family(two)
    if a or b:
        return bool(a and b and a==b)
    from patient_workspace_model import case_key
    return bool(_t(one) and case_key({'case_family':one})==case_key({'case_family':two}))


def _mp_reference(value):
    # Patient labels may be locally edited. Only a protocol-like reference is
    # allowed into AI history, never a free-form name or identity string.
    raw=_t(value).upper()
    return raw if re.fullmatch(r'MP[0-9/-]{3,20}',raw) else 'MP-KIMLIK-GIZLI'


def _variant_safe(value, limit=90):
    """Strict machine-readable genotype token; reject free-text identifiers."""
    x = _t(value)
    return x[:limit] if 0 < len(x) <= limit and re.fullmatch(r'[A-Za-z0-9_.+\-:>/=*?(),\[\]]+', x) else ''


def _tables(db):
    return {r[0] for r in db.cx.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _case_matches(db, current_family):
    cm = db.get_case_metadata(current_family)
    tc = _nid(db, cm.get('national_id'))
    fno = _file_no(cm.get('enlil_file_no'))
    result = []
    excluded = defaultdict(int)
    if not tc and not fno:
        return result, dict(excluded), 'identity_missing'
    rows = [dict(r) for r in db.cx.execute(
        'SELECT case_family,national_id,enlil_file_no,birth_date,sex,diagnosis '
        'FROM case_metadata WHERE case_family<>?', (current_family,))]
    # A file-number-only candidate must not be interpreted as verified when it
    # points to several distinct valid local TCs (even when current TC missing).
    file_tc_set = {_nid(db, r['national_id']) for r in rows
                   if fno and _file_no(r.get('enlil_file_no')) == fno and _nid(db, r.get('national_id'))}
    ambiguous_file = len(file_tc_set) > 1 and not tc
    for other in rows:
        tc_other = _nid(db, other.get('national_id'))
        tcm = bool(tc and tc_other == tc)
        fm = bool(fno and _file_no(other.get('enlil_file_no')) == fno)
        if not (tcm or fm):
            continue
        if tc and tc_other and tc != tc_other:
            excluded['conflicting_tc'] += 1
            continue
        if _demographic_conflict(db, cm, other):
            excluded['conflicting_demographics'] += 1
            continue
        if fm and not tcm and ambiguous_file:
            excluded['ambiguous_file_number'] += 1
            continue
        basis = 'validated_tc' if tcm else 'enlil_file_no_review_required'
        result.append({'case_family': other['case_family'], 'identity_basis': basis,
                       'verified_identity': bool(tcm),
                       'diagnosis': _t(other.get('diagnosis'))})
    return result, dict(excluded), 'matched' if result else 'no_match'


def _latest_local_document(db, tables, record_key):
    if 'archive_report_documents' not in tables:
        return None
    # Confirmed version outranks even a newer unconfirmed draft.
    row=db.cx.execute('SELECT version,report_text,created_at,document_status FROM archive_report_documents '
      'WHERE record_key=? ORDER BY CASE document_status WHEN ? THEN 0 ELSE 1 END, version DESC LIMIT 1',
      (record_key,'user_confirmed')).fetchone()
    return dict(row) if row else None


def _saved_form_states(base, provided=None):
    if isinstance(provided,dict):return provided
    if not base:return {}
    f=Path(base)/'DATA'/'QC_ENLIL'/'hasta_form_verileri.json'
    try:
        data=json.loads(f.read_text(encoding='utf-8'))
        return data if isinstance(data,dict) else {}
    except (OSError,UnicodeError,ValueError,TypeError):return {}


def _report_form(form_states, sample):
    """Only exact TECH key can be attached to a concrete DNA/RNA record."""
    key='TECH::'+sample['record_key']
    state=form_states.get(key)
    if not isinstance(state,dict):return None
    report_type=str(state.get('report_type') or 'AUTO').upper()
    assay=str(sample.get('sample_type') or '').upper()
    if report_type not in ('AUTO',assay):return None
    if not (str(state.get('diagnosis_comment') or '').strip() or any(str(state.get(k) or '').strip() for k in ('tmb','msi_pct','hrd_score'))):return None
    return state


def _legacy_forms_for_family(states, family, samples):
    """Old MP-keyed shared drafts are NOT silently assigned to DNA or RNA."""
    result=[]
    for key,value in states.items():
        if not isinstance(value,dict) or key.startswith(('TECH::','__')):continue
        if not _same_case_family(key,family):continue
        comment=str(value.get('diagnosis_comment') or '').strip()
        if not comment and not any(value.get(x) for x in ('tmb','msi_pct','hrd_score')):continue
        result.append((key,value))
    return result


def _form_text(state):
    comment=str(state.get('diagnosis_comment') or '').strip()
    parts=[comment] if comment else []
    for key,label in (('tmb','TMB'),('msi_pct','MSI %'),('hrd_score','HRD')):
        value=str(state.get(key) or '').strip()
        if value:parts.append(f'- {label}: {value}')
    return '\n'.join(parts)


def _summary_with_form(report_text, form):
    summary=parse_previous_report(report_text)
    if not form:return summary
    if summary.get('tmb') is None and str(form.get('tmb') or '').strip():
        try:summary['tmb']=float(str(form['tmb']).replace(',','.'))
        except ValueError:pass
    if summary.get('msi_percent') is None and str(form.get('msi_pct') or '').strip():
        try:summary['msi_percent']=float(str(form['msi_pct']).replace(',','.'))
        except ValueError:pass
    if summary.get('hrd') is None and str(form.get('hrd_score') or '').strip():
        value=str(form['hrd_score']).strip()
        try:summary['hrd']=float(value.replace(',','.'))
        except ValueError:
            if value.upper() in ('N/A','NA'):summary['hrd']='N/A'
    return summary


def _confirmed_variants(db, tables, record_key, limit=75):
    if 'reported_variant_history' not in tables:
        return []
    rows = db.cx.execute('''SELECT variant_type,gene,transcript,cdna,protein,feature,created_at,source
        FROM reported_variant_history WHERE record_key=? AND confirmed=1
        ORDER BY id DESC LIMIT ?''', (record_key, limit)).fetchall()
    out = []
    seen = set()
    for row in rows:
        row=dict(row)
        gene=_variant_safe(row.get('gene'), 24).upper()
        if not gene or not re.fullmatch('[A-Z][A-Z0-9.-]{0,23}',gene):
            continue
        datum = {'gene': gene,
                 'variant_type': _variant_safe(row.get('variant_type'), 25),
                 'transcript': _variant_safe(row.get('transcript'), 42),
                 'cdna': _variant_safe(row.get('cdna')),
                 'protein': _variant_safe(row.get('protein')),
                 'feature': _variant_safe(row.get('feature'))}
        key=tuple(datum.items())
        if key in seen:continue
        seen.add(key)
        out.append(datum)
    return out


def find_previous_ngs(db, record_key, max_records=45, base=None, form_data=None):
    """Find ALL locally available evidence, including saved standard drafts.

    Exact same-MP technical records do not require TC/file-number proof.
    OTHER MP cases require existing identity/demographic conflict checks.
    Enlil-only (not locally copied) reports cannot be discovered by this method.
    """
    current=db.get_sample(record_key)
    if not current:return {'status':'current_record_missing','records':[],'warnings':[]}
    current=dict(current)
    family=_t(current.get('case_family') or current.get('patient_id'))
    cm=db.get_case_metadata(family)
    base=Path(base) if base is not None else Path(db.path).parent.parent
    states=_saved_form_states(base,form_data)
    matches, exclusions, status = _case_matches(db,family)
    case_info={m['case_family']:m for m in matches}
    case_info[family]={'case_family':family,'identity_basis':'same_mp_local_source',
                       'verified_identity':True,'diagnosis':_t(cm.get('diagnosis'))}
    samples=[dict(s) for s in db.samples()]
    # Include case-family aliases present only in the technical sample table.
    # Some old runs contain MP173//26 but have no separate case_metadata row.
    families=set(_t(s.get('case_family') or s.get('patient_id')) for s in samples)
    families.update(_t(row['case_family']) for row in db.cx.execute(
        'SELECT case_family FROM case_metadata'))
    for alias in sorted(families):
        if not alias or alias==family or not _same_case_family(alias,family):continue
        other=db.get_case_metadata(alias)
        if (_demographic_conflict(db,cm,other) or
            (_nid(db,cm.get('national_id')) and _nid(db,other.get('national_id')) and
             _nid(db,cm['national_id'])!=_nid(db,other['national_id']))):
            exclusions['same_mp_identity_conflict']=exclusions.get('same_mp_identity_conflict',0)+1
            case_info.pop(alias,None)
            continue
        case_info[alias]={'case_family':alias,
            'identity_basis':'same_mp_normalized_local_source',
            'verified_identity':True,'diagnosis':_t(other.get('diagnosis'))}
    # Reports for an MP absent from the NGS technical database are stored
    # under the canonical MP. When an older spelling like MP173--26 appears
    # later, use exact normalized MP to show the previously imported report.
    # Never create or infer a cross-MP patient identity from a pasted report.
    for case_id, info in list(case_info.items()):
        canonical=biopsy_key(case_id)
        if not canonical or canonical in case_info:continue
        try:has_copy=bool(load_reports(base,canonical))
        except ValueError:has_copy=False
        if has_copy:
            case_info[canonical]={**info,'case_family':canonical,
                'identity_basis':('same_mp_local_bulk_source'
                   if case_id==family else info['identity_basis'])}
    result=[]
    tables=_tables(db)
    for sample in samples:
        sample_family=_t(sample.get('case_family') or sample.get('patient_id'))
        info=case_info.get(sample_family)
        if not info:continue
        rk=sample['record_key']
        doc=_latest_local_document(db,tables,rk)
        state=_report_form(states,sample)
        reported=_confirmed_variants(db,tables,rk)
        # Separate read-only platform choices from genuinely confirmed/local variants.
        gennext=None
        if 'gennext_selection_status' in tables:
            try:
                from gennext_selection_store import current_status
                gennext=current_status(db,rk)
            except Exception:gennext=None
        gen_state=(gennext or {}).get('result_status')
        gennext_selected=[]
        if gen_state=='selected_positive':
            gennext_selected=[{k:v.get(k,'') for k in ('gene','protein','cdna','genomic','vaf','tier','variant_key')}
                              for v in (gennext.get('selected') or [])]
        # A verified analyzed-negative SNV selection is relevant but does not establish
        # an entire DNA/RNA panel negative result.
        if not doc and not state and not reported and gen_state not in ('selected_positive','no_snv_selection'):continue
        extra=db.get_sample_metadata(rk)
        confirmed=bool(doc and doc['document_status']=='user_confirmed')
        if doc:
            source_status=('user_confirmed_local_not_e_signed' if confirmed else 'local_archived_draft_not_e_signed')
            report_text=_t(doc.get('report_text'))
        elif state:
            source_status='saved_standard_text_draft_not_e_signed'
            report_text=_form_text(state)
        else:
            source_status=('gennext_selection_only_not_signed' if gen_state in ('selected_positive','no_snv_selection')
                           else 'structured_confirmed_history_without_local_report')
            report_text=''
        if doc and state and state.get('diagnosis_comment') and state['diagnosis_comment'] not in report_text:
            # Separate below so old confirmed original stays primary source.
            secondary_comment=str(state['diagnosis_comment']).strip()
        else:secondary_comment=''
        report_date=_t(doc.get('created_at'))[:10] if doc else ''
        summary=_summary_with_form(report_text,state)
        result.append({'record_key':rk,'patient_id':_t(sample.get('patient_id')),
          'sample_type':_t(sample.get('sample_type')).upper(),
          'pool_name':_t(sample.get('pool_name')),'diagnosis':info['diagnosis'],
          'block_id':_t(extra.get('block_id')),'identity_basis':info['identity_basis'],
          'verified_identity':info['verified_identity'],'source_scope':('same_mp' if info['identity_basis'].startswith('same_mp_') else 'previous_mp'),
          'report_status':source_status,'report_version':doc['version'] if doc else None,
          'report_date':report_date,'variants':reported,'parsed_report':summary,
          'gennext_selection_status':gen_state or 'not_scanned',
          'gennext_selected_variants':gennext_selected,
          'gennext_selection_scope':'Clinical SNVs / indel only, not signed or full panel',
          'is_current_record':rk==record_key,
          'parsed_report_summary':formatted_summary(summary),
          'secondary_comment':secondary_comment,
          'local_report_text':report_text})
    # Pre-v0.4.49 QC/Enlil saved a single form under the MP number, without
    # separating DNA and RNA. Show it independently, never assign to both.
    for case_id,info in case_info.items():
        related=[s for s in samples if _t(s.get('case_family') or s.get('patient_id'))==case_id]
        for key,form in _legacy_forms_for_family(states,case_id,related):
            txt=_form_text(form)
            if not txt:continue
            parsed=_summary_with_form(txt,form)
            result.append({'record_key':'','patient_id':key,'sample_type':'BELİRSİZ DNA/RNA',
             'pool_name':'Eski ortak Standart Metinler','diagnosis':info['diagnosis'],
             'block_id':'','identity_basis':info['identity_basis'],
             'verified_identity':info['verified_identity'],
             'source_scope':'legacy_unassigned',
             'report_status':'legacy_shared_draft_assay_unconfirmed',
             'report_version':None,'report_date':'','variants':[],
             'is_current_record':False,
             'parsed_report':parsed,'parsed_report_summary':formatted_summary(parsed),
             'secondary_comment':'','local_report_text':txt})
    # Enlil reports manually copied into the local history, not yet verified
    # against the Enlil e-signature; no fabricated archive or signed status.
    for case_id,info in case_info.items():
        try:manual_reports=load_reports(base,case_id)
        except ValueError:
            # Corrupt optional manual-copy file must not hide existing technical
            # reports or stop the NGS/AI workflow. Preserve file for recovery.
            exclusions['manual_report_file_unreadable']=exclusions.get('manual_report_file_unreadable',0)+1
            continue
        for item in manual_reports:
            parsed=parse_previous_report(item['text'])
            result.append({'record_key':'','patient_id':item['prior_mp'],
              'sample_type':item['assay'],'pool_name':('Enlil JAB yerel kopya' if item.get('source')=='enlil_open_screen_jab_read_only' else 'Enlil manuel kopya'),
              'diagnosis':_t(item.get('tumor_diagnosis')) or info['diagnosis'],'block_id':'',
              'identity_basis':info['identity_basis'],'verified_identity':info['verified_identity'],
              'source_scope':'user_attached_previous_report',
              'report_status':('enlil_jab_local_copy_unverified' if item.get('source')=='enlil_open_screen_jab_read_only' else 'manual_enlil_paste_unverified'),
              'report_version':None,'report_date':item.get('imported_at','')[:10],
              'is_current_record':False,'variants':[],'parsed_report':parsed,
              'parsed_report_summary':formatted_summary(parsed),
              'secondary_comment':'','local_report_text':item['text']})
    # Stable groups first, newest report date; otherwise no inserted ordering
    # can turn a newer draft into an older confirmed report.
    result.sort(key=lambda r:(r['report_date'],r['patient_id'],r['sample_type']),reverse=True)
    if len(result)>max_records:exclusions['records_clipped']=len(result)-max_records
    own=any(r.get('source_scope')=='same_mp' for r in result)
    new_status='local_mp_found' if own and status=='identity_missing' else status
    return {'status':new_status,'current_family':family,'current_diagnosis':_t(cm.get('diagnosis')),
            'record_count':len(result),'records':result[:max_records],
            'warnings':[{'kind':k,'count':v} for k,v in exclusions.items() if v],
            'identity_available':bool(_nid(db,cm.get('national_id')) or _file_no(cm.get('enlil_file_no')))}


def _is_khdak(diagnosis):
    d=unicodedata.normalize('NFKD',_t(diagnosis).casefold())
    d=''.join(ch for ch in d if not unicodedata.combining(ch)).replace('ı','i')
    return any(term in d for term in ('khdak', 'nsclc', 'non-small cell',
        'kucuk hucreli disi', 'akciger adenokarsinom', 'akciğer adenokarsinom'))


def ai_history_context(history):
    """Strict PII-free history. Report text NEVER enters AI bundles."""
    from previous_report_summary import safe_ai_summary
    records=[]
    for r in history.get('records') or []:
        variants=[dict(v) for v in (r.get('variants') or [])[:35]]
        records.append({'prior_mp':_mp_reference(r.get('patient_id')),
          'prior_pool':re.sub(r'[^A-Za-z0-9 ._/-]','',r.get('pool_name') or '')[:60],
          'sample_type':r['sample_type'],'diagnosis':r['diagnosis'][:140],
          'block_id':r['block_id'][:50],'report_status':r['report_status'],
          'report_date':r['report_date'],'identity_basis':r['identity_basis'],
          'identity_verified':bool(r['verified_identity']),
          'source_scope':r.get('source_scope') or 'previous_mp',
          'is_current_record':bool(r.get('is_current_record')),
          'source_report_exists':bool(r['local_report_text']),
          'structured_reported_variants': variants,
          'historical_text_extraction':safe_ai_summary(r.get('parsed_report'))})
    # EGFR prompts require historic evidence AND verified same-patient context,
    # and always remain requests for investigation, NOT current mutation calls.
    trusted=[r for r in records if r['identity_verified'] and not r.get('is_current_record') and r['report_status'] in
      ('user_confirmed_local_not_e_signed','structured_confirmed_history_without_local_report')]
    genes={v['gene'] for r in trusted for v in r['structured_reported_variants']}
    genes.update(v['gene'] for r in trusted for v in r['historical_text_extraction']['positive_variants'])
    prompts=[]
    if _is_khdak(history.get('current_diagnosis')) and 'EGFR' in genes:
        prompts=[
          'KHDAK/adenokarsinomda yerel onaylı önceki raporda EGFR bulgusu var: eski cDNA/protein değişikliğini güncel analizde yalnız hedefli karşılaştırma için kullan.',
          'EGFR exon19 delesyonu, L858R veya diğer değişiklikleri aynı kabul etme; güncel DNA sonucu ve kapsama verilerini kontrol et.',
          'Tedavi öyküsü varsa T790M/C797S, MET amplifikasyonu ve diğer direnç mekanizmalarını koşullu değerlendir; yokluğunu varsayma.',
          'Örnek tarihleri, bloklar, primer/metastaz ve histolojik dönüşüm/ikinci primer olasılığını klinikle karşılaştır.',
        ]
    elif _is_khdak(history.get('current_diagnosis')) and genes & DRIVER_GENES:
        prompts=['KHDAK için yerel onaylı geçmiş sürücü bulguları ('+', '.join(sorted(genes&DRIVER_GENES))+') güncel testle karşılaştırılmalı.']
    if any(not r['identity_verified'] for r in records):
        prompts.append('Yalnız dosya no ile eşleşen kayıtlarda hasta kimliği doğrulanmadan eski genotipi aynı hastaya ait klinik gerçek olarak kullanma.')
    if any('draft' in r['report_status'] or 'unverified' in r['report_status'] for r in records):
        prompts.append('Yerel taslak / manuel Enlil kopyasında yapılandırılmış genler BULGU ÇIKARIMIDIR; imzalı rapor veya güncel pozitif çağrı değildir.')
    return {'schema':'ngs_prior_ngs_history_v1','status':history.get('status') or 'unknown',
      'matching_policy':'same_mp_or_validated_tc_or_enlil_file_no_with_conflict_checks',
      'source_scope':'local_confirmed_or_draft_or_manual_paste_or_structured_history',
      'identity_review_required':any(not r['identity_verified'] for r in records),
      'previous_report_count':len(records),'previous_reports':records,
      'warnings':list(history.get('warnings') or []),
      'clinical_review_prompts':prompts,
      'limitations':'Historical report text is never transmitted. Historical genotype NOT current positive call. Draft/manual pasted report is NOT Enlil e-signed; only original source can confirm signature.'}


def ui_summary(history, limit=4):
    records=history.get('records') or []
    if not records:
        if history.get('status')=='identity_missing':
            return 'Yerel MP raporu/taslağı bulunamadı; TC/dosya no kayıtsız olduğu için diğer MP geçmişi aranamadı. Enlil raporları ayrıca içe alınmalıdır.'
        return 'Yerel kayıtlı NGS raporu/taslağı yok. Enlil’de yalnız imzalı bulunan raporlar henüz otomatik aktarılmıyor; negatif sonuç değildir.'
    out=[]
    for r in records[:limit]:
        variants=r.get('variants') or []
        v=[x['gene']+' '+(x.get('protein') or x.get('cdna') or x.get('feature') or '') for x in variants[:4]]
        parsed=r.get('parsed_report') or {}
        v.extend(x['gene']+' '+(x.get('protein') or x.get('cdna') or '') for x in parsed.get('positives',[])[:4])
        label=('ONAYLI YEREL' if r['report_status']=='user_confirmed_local_not_e_signed' else
               'STANDART METİN TASLAĞI' if 'draft' in r['report_status'] else
               'MANUEL ENLIL KOPYASI' if 'manual_enlil' in r['report_status'] else
               'YAPILANDIRILMIŞ BULGU')
        flag=' · KİMLİK DOĞRULA' if not r['verified_identity'] else ''
        out.append(f"{r['patient_id']} {r['sample_type']} [{label}]{flag}: "+(', '.join(v[:5]) or 'rapor içeriği var; tam metni açın'))
    if len(records)>limit:out.append(f'+{len(records)-limit} diğer yerel rapor/taslak')
    return '\n'.join(out)
