"""Exact, record-scoped GenNext clinical observations (v0.4.81).

Only an explicitly labelled field can supply a value. This module never reads
the OS clipboard, changes a form, infers a specimen from a dropdown option, or
interprets a research variant as report selection. Run Detail is the canonical
clinical source because it exposes both the active sample and Process Type.
"""
from __future__ import annotations

from datetime import datetime, timezone
import math
import re
from urllib.parse import parse_qs, urlparse


PARSER_VERSION = '0.4.81'
FIELD_LABELS = {
    'source_tumor_type': ['Tumor Type', 'Tumour Type'],
    'source_clinical_diagnosis': ['Clinical Diagnosis'],
    'specimen_type': ['Specimen Type'],
    'specimen_site': ['Specimen Site'],
    'requesting_physician': ['Referring Physician(s)', 'Referring Physician'],
    'source_analyst': ['Analyst(s)', 'Analyst'],
    'age_at_reference': ['Age at Diagnosis', 'Patient Age', 'Age'],
    'sex': ['Sex', 'Gender', 'Patient Sex', 'Patient Gender'],
    'block_id': ['Block ID', 'Block No', 'Block Number', 'Specimen Block'],
    'tumor_percentage': ['Tumor Percentage', 'Tumour Percentage', 'Tumor %',
                         'Tumour %', 'Tumor Content', 'Tumour Content'],
    'tmb_value': ['TMB Value'],
    'tmb_status': ['TMB Status'],
    'msi_percentage': ['MSI Percentage'],
    'msi_status': ['MSI Status'],
    'hrd_score': ['HRD Score'],
    'hrd_loh': ['HRD-LOH'],
    'hrd_tai': ['Telomeric AI'],
    'hrd_lst': ['LST'],
}
NUMERIC_FIELDS = {
    'age_at_reference', 'tumor_percentage', 'tmb_value', 'msi_percentage',
    'hrd_score', 'hrd_loh', 'hrd_tai', 'hrd_lst',
}


class ClinicalSourceError(ValueError):
    """The observation is unbound, mixed, incomplete, or otherwise untrusted."""


# Both identity-only and full snapshots use exactly the same scoped primitives.
# data-tooltip-html is an explanation, NEVER a field value. Clinical full-text
# tooltips must belong to the value element, not the label or its information icon.
CLINICAL_SNAPSHOT_JS = r'''args => {
  const norm = x => String(x == null ? '' : x).replace(/\s+/g, ' ').trim();
  const key = x => norm(x).replace(/\s*:\s*$/, '').toLowerCase();
  const exclude = 'script,style,template,[role="tooltip"],[aria-hidden="true"],[hidden],.dropdown-menu';
  const all = [...document.querySelectorAll('label,span,div,dt,th,h5,h6')]
    .filter(el => !el.closest(exclude));
  const controlValue = el => {
    if (el.tagName === 'SELECT') return norm([...el.selectedOptions].map(x=>x.textContent).join(' '));
    if (/^(?:checkbox|radio)$/i.test(el.type || '') && !el.checked) return '';
    return norm(el.value);
  };
  const valueOf = (el, tooltip) => {
    if (/^(?:INPUT|SELECT|TEXTAREA)$/.test(el.tagName))
      return {raw:controlValue(el), source_kind:'control'};
    if (tooltip) {
      const values = [...new Set([el, ...el.querySelectorAll('[data-tooltip-content]')]
        .filter(x=>x.hasAttribute('data-tooltip-content') && !x.closest('[role="tooltip"]'))
        .map(x=>norm(x.getAttribute('data-tooltip-content')))
        .filter(x=>x && !/<\/?[a-z][^>]*>/i.test(x)))];
      if (values.length === 1) return {raw:values[0], source_kind:'value_tooltip'};
      if (values.length > 1) return {raw:null, source_kind:'value_tooltip', ambiguous:true};
    }
    return {raw:norm(el.innerText || el.textContent), source_kind:'visible_value'};
  };
  const field = (aliases, tooltip=true) => {
    const keys=aliases.map(key), matches=[];
    const labels=all.filter(el=>keys.includes(key(el.textContent)) &&
      ![...el.children].some(c=>keys.includes(key(c.textContent))));
    for (const label of labels) {
      let values=[];
      if (label.tagName === 'LABEL' && label.htmlFor) {
        const input=document.getElementById(label.htmlFor);
        if (input && /^(?:INPUT|SELECT|TEXTAREA)$/.test(input.tagName)) values=[input];
      }
      if (!values.length) {
        const parent=label.parentElement;
        if (parent) values=[...parent.children].filter(x=>x!==label &&
          !x.matches('i,img,svg,button,script,style,[role="tooltip"]') &&
          (norm(x.textContent) || /^(?:INPUT|SELECT|TEXTAREA)$/.test(x.tagName)));
      }
      // Never ascend into a whole card or use its first number. A value container
      // containing another known field label is a group, not a value.
      values=values.filter(x=>![...x.querySelectorAll('span,div,label,dt,th')]
        .some(n=>Object.values(args.labels).flat().some(k=>key(k)===key(n.textContent))));
      if (values.length===1) matches.push({...valueOf(values[0],tooltip),
        source_label:norm(label.textContent), label_present:true});
      else if (values.length>1) matches.push({raw:null,source_label:norm(label.textContent),
        label_present:true,ambiguous:true,source_kind:'visible_value'});
      else matches.push({raw:null,source_label:norm(label.textContent),
        label_present:true,source_kind:'visible_value'});
    }
    // A standalone input is allowed only with an exact, explicit accessible label.
    for (const el of document.querySelectorAll('input[aria-label],select[aria-label],textarea[aria-label]')) {
      if (!el.closest(exclude) && keys.includes(key(el.getAttribute('aria-label'))))
        matches.push({raw:controlValue(el), source_kind:'control',
          source_label:norm(el.getAttribute('aria-label')), label_present:true});
    }
    if (!matches.length) return {raw:null,source_label:aliases[0],label_present:false};
    const good=matches.filter(x=>x.raw !== null && x.raw !== '');
    const unique=[...new Set(good.map(x=>x.raw))];
    if (matches.some(x=>x.ambiguous) || unique.length>1)
      return {raw:null,source_label:aliases[0],label_present:true,ambiguous:true};
    return good[0] || matches[0];
  };
  const active=[...document.querySelectorAll('.dropdown-item.active span.fw-bold')];
  const heading=[...document.querySelectorAll('h1.page-heading')];
  const process=field(['Process Type'],false);
  const switches=[...document.querySelectorAll('input[value="runIsSolved"]')];
  const statuses=[...document.querySelectorAll('.badge.text-capitalize')]
    .filter(el=>!el.closest('.dropdown-menu'))
    .map(el=>norm(el.textContent))
    .filter(t=>/^(?:Completed|Running|Queued|Processing|Failed|Canceled|Cancelled)$/i.test(t));
  const out={url:location.href, active_sample:active.length===1?norm(active[0].textContent):'',
    active_sample_count:active.length,
    page_heading:heading.length===1?norm(heading[0].textContent):'',
    page_heading_count:heading.length, process_type:process.raw,
    process_type_ambiguous:!!process.ambiguous,
    switch_count:switches.length,
    analyzed_live_checked:args.evidence_kind==='live_dom' && switches.length===1 ? switches[0].checked : null,
    static_checked_attribute_present:switches.length===1 ? switches[0].hasAttribute('checked') : null,
    run_completion_display:statuses.length===1?statuses[0]:null};
  if (!args.identity_only) {
    out.fields={};
    for (const [name,labels] of Object.entries(args.labels))
      out.fields[name]=field(labels,!args.numeric_fields.includes(name));
  }
  return out;
}'''


def _text(value):
    return re.sub(r'\s+', ' ', str(value if value is not None else '')).strip()


def _sample_type(value):
    """Only explicit DNA/RNA tokens from Process Type establish assay identity."""
    found=set(re.findall(r'(?<![A-Z])(?:DNA|RNA)(?![A-Z])', _text(value).upper()))
    if found == {'DNA','RNA'}:
        return 'DNA+RNA'
    return next(iter(found)) if len(found)==1 else ''


def _identity_url(url):
    parsed=urlparse(str(url or ''))
    if parsed.scheme not in ('http','https') or not parsed.netloc:
        raise ClinicalSourceError('Klinik kaynak URL kimliği doğrulanamadı.')
    match=re.fullmatch(r'/run-detail-somatic/([^/]+)/?',parsed.path,re.I)
    if match:
        sample=(parse_qs(parsed.query).get('sampleUuid') or [])
        if len(sample)!=1:
            raise ClinicalSourceError('Klinik kaynak sampleUuid tekil değil.')
        return parsed.netloc.lower(),match.group(1).lower(),sample[0].lower(),'run'
    match=re.fullmatch(r'/somatic-detail-clinic/([^/&]+)&([^/&]+)&snv/?',parsed.path,re.I)
    if match:
        return parsed.netloc.lower(),match.group(1).lower(),match.group(2).lower(),'clinical'
    raise ClinicalSourceError('Beklenmeyen klinik kaynak sayfası.')


def verify_source_identity(identity, sample, *, require_process_type=True):
    """Verify URL UUIDs and *active* MP; dropdown membership is insufficient."""
    if not isinstance(identity,dict):
        raise ClinicalSourceError('Klinik kaynak kimlik gözlemi yok.')
    host,run,su,page_kind=_identity_url(identity.get('url'))
    expected_run=str(sample.get('run_uuid') or '').lower()
    expected_su=str(sample.get('sample_uuid') or '').lower()
    expected_type=_sample_type(sample.get('sample_type'))
    if not expected_run or not expected_su or (run,su)!=(expected_run,expected_su):
        raise ClinicalSourceError('Klinik kaynak RUN/SAMPLE UUID uyuşmuyor; kayıt yapılmadı.')
    if not expected_type:
        raise ClinicalSourceError('Teknik çalışmanın DNA/RNA türü doğrulanamadı.')
    expected_key=f"{sample.get('run_uuid')}::{sample.get('sample_uuid')}::{expected_type}"
    if sample.get('record_key') and str(sample['record_key']).casefold()!=expected_key.casefold():
        raise ClinicalSourceError('Klinik kaynak çalışma anahtarı uyuşmuyor.')
    if page_kind=='run':
        active=identity.get('active_sample') if identity.get('active_sample_count')==1 else ''
    elif identity.get('active_sample_count')==1:
        active=identity.get('active_sample')
    else:
        active=identity.get('page_heading') if identity.get('page_heading_count')==1 else ''
    expected_mp=_text(sample.get('patient_id')).upper().replace(' ','')
    if not expected_mp or _text(active).upper().replace(' ','')!=expected_mp:
        raise ClinicalSourceError('Klinik kaynak aktif MP uyuşmuyor; kayıt yapılmadı.')
    observed_type=_sample_type(identity.get('process_type'))
    if identity.get('process_type_ambiguous') or (observed_type and observed_type!=expected_type):
        raise ClinicalSourceError('Klinik kaynak Process Type uyuşmuyor; kayıt yapılmadı.')
    if require_process_type and not observed_type:
        raise ClinicalSourceError('Klinik kaynak Process Type henüz doğrulanamadı.')
    return (host,run,su,expected_type,expected_mp,page_kind,observed_type)


def read_source_identity(page, sample, *, require_process_type=True, evidence_kind='live_dom'):
    data=page.evaluate(CLINICAL_SNAPSHOT_JS, {'labels':FIELD_LABELS,
        'numeric_fields':sorted(NUMERIC_FIELDS),'identity_only':True,
        'evidence_kind':evidence_kind})
    verify_source_identity(data,sample,require_process_type=require_process_type)
    return data


def verify_identity_unchanged(before, after, sample, *, require_process_type=True):
    a=verify_source_identity(before,sample,require_process_type=require_process_type)
    b=verify_source_identity(after,sample,require_process_type=require_process_type)
    if a!=b or str(before.get('url'))!=str(after.get('url')):
        raise ClinicalSourceError('Klinik okuma sırasında aktif çalışma değişti; veri kaydedilmedi.')


def normalize_field(name, raw_field):
    raw_field=raw_field if isinstance(raw_field,dict) else {}
    raw=_text(raw_field.get('raw')) if raw_field.get('raw') is not None else None
    result={'raw':raw,'value':None,'source_label':raw_field.get('source_label') or FIELD_LABELS[name][0],
            'source_kind':raw_field.get('source_kind') or 'visible_value','availability':'missing','unit':None}
    if raw_field.get('ambiguous'):
        result.update(availability='invalid',reason='ambiguous_label_value')
        return result
    if not raw_field.get('label_present') or raw in (None,''):
        return result
    if re.fullmatch(r'(?:N\s*/\s*A|N\.?A\.?|Not Specified|Not Available|—|–|-)',raw,re.I):
        result['availability']='not_available'
        return result
    if name in NUMERIC_FIELDS:
        if name=='tmb_value':
            match=re.fullmatch(r'([+]?[0-9]+(?:[.,][0-9]+)?)\s*((?:muts?|mutations?)\s*/\s*mb)?',raw,re.I)
        elif name in ('tumor_percentage','msi_percentage'):
            match=re.fullmatch(r'%?\s*([+]?[0-9]+(?:[.,][0-9]+)?)\s*(%)?',raw)
        elif name=='age_at_reference':
            match=re.fullmatch(r'([+]?[0-9]+(?:[.,][0-9]+)?)\s*(?:years?|yrs?|yaş)?',raw,re.I)
        else:
            match=re.fullmatch(r'([+]?[0-9]+(?:[.,][0-9]+)?)',raw)
        if match:
            value=float(match.group(1).replace(',','.'))
            if not math.isfinite(value):
                match=None
            elif name in ('tumor_percentage','msi_percentage') and value>100:
                match=None
            elif name=='age_at_reference' and (not value.is_integer() or value>130):
                match=None
            elif name in ('hrd_score','hrd_loh','hrd_tai','hrd_lst') and not value.is_integer():
                match=None
            else:
                result['value']=int(value) if value.is_integer() else value
                if name in ('tumor_percentage','msi_percentage'):
                    result['unit']='%'
                    result['scale']='percent'
                elif name=='tmb_value':
                    result['unit']=_text(match.group(2)) or None
        if not match:
            result.update(availability='invalid',reason='invalid_numeric_value',value=None)
            return result
    else:
        if '...' in raw or '…' in raw:
            result.update(availability='invalid',reason='truncated_value')
            return result
        if name=='sex':
            values={'male':'male','m':'male','erkek':'male','female':'female','f':'female',
                    'kadın':'female','kadin':'female','unknown':'unknown','bilinmiyor':'unknown'}
            value=values.get(raw.lower())
            if value is None:
                result.update(availability='invalid',reason='unknown_source_sex_code')
                return result
            result['value']=value
        else:
            result['value']=raw
    result['availability']='observed'
    return result


def observation_from_snapshots(before, snapshot, after, sample, *, evidence_kind='live_dom',
                               observed_at=None, source_file=None):
    """Pure validation/normalization shared by live reads and explicit offline QA."""
    if evidence_kind not in ('live_dom','offline_mhtml'):
        raise ClinicalSourceError('Desteklenmeyen klinik gözlem türü.')
    verify_identity_unchanged(before,snapshot,sample)
    verify_identity_unchanged(snapshot,after,sample)
    when=observed_at or (datetime.now(timezone.utc).isoformat(timespec='microseconds') if evidence_kind=='live_dom' else None)
    if not when:
        raise ClinicalSourceError('Statik kaynağın gözlem zamanı açıkça verilmelidir.')
    try:
        timestamp=datetime.fromisoformat(str(when).replace('Z','+00:00'))
        if timestamp.tzinfo is None:raise ValueError('timezone')
    except (ValueError,TypeError) as exc:
        raise ClinicalSourceError('Kaynak gözlem zamanı saat dilimiyle doğrulanmalıdır.') from exc
    record_type=_sample_type(sample.get('sample_type'))
    rk=sample.get('record_key') or f"{sample['run_uuid']}::{sample['sample_uuid']}::{record_type}"
    fields={name:normalize_field(name,(snapshot.get('fields') or {}).get(name)) for name in FIELD_LABELS}
    checked=snapshot.get('analyzed_live_checked')
    checked_verified=(evidence_kind=='live_dom' and snapshot.get('switch_count')==1 and isinstance(checked,bool))
    # Run completion, Analyzed, and Selection are independent observations.
    return {
        'schema':'gennext_clinical_observation_v1',
        'record':{'record_key':rk,'run_uuid':sample['run_uuid'],'sample_uuid':sample['sample_uuid'],
                  'sample_type':record_type,'display_mp':sample.get('patient_id')},
        'source_observation':{'kind':evidence_kind,'source_url':snapshot.get('url'),
            'source_file':source_file,'observed_at':when,'identity_verified':True,
            'scope':'one_loaded_sample','parser_version':PARSER_VERSION},
        'fields':fields,
        'analysis_observation':{'run_completion_display':snapshot.get('run_completion_display'),
            'analyzed_live_checked':checked if checked_verified else None,
            'live_checked_verified':checked_verified,
            'static_checked_attribute_present':snapshot.get('static_checked_attribute_present'),
            'observed_at':when,'evidence_kind':evidence_kind},
        'selection_observation':{kind:{'status':'not_observed_in_this_snapshot',
            'selected_count':None,'selected_variants':None} for kind in ('snv_indel','cnv','rna_fusion')},
    }


def read_clinical_observation(page, sample):
    """Read a live, typed sample before/after capturing exact field containers."""
    before=read_source_identity(page,sample)
    snapshot=page.evaluate(CLINICAL_SNAPSHOT_JS, {'labels':FIELD_LABELS,
        'numeric_fields':sorted(NUMERIC_FIELDS),'identity_only':False,'evidence_kind':'live_dom'})
    after=read_source_identity(page,sample)
    return observation_from_snapshots(before,snapshot,after,sample)


def legacy_hrd_card(observation):
    """Retain old export readers without merging missing and explicit N/A."""
    fields=observation.get('fields') or {}
    def old_value(name):
        field=fields.get(name) or {}
        if field.get('availability')=='not_available':return 'N/A'
        if field.get('availability')=='observed' and field.get('value') is not None:
            return str(field['value'])
        return ''
    return {'score':old_value('hrd_score'),'loh':old_value('hrd_loh'),
            'telomeric_ai':old_value('hrd_tai'),'lst':old_value('hrd_lst'),
            'source':'GenNext Run Detail > HRD > exact labelled fields',
            'observed_at':(observation.get('source_observation') or {}).get('observed_at'),
            'availability':{key:(fields.get(key) or {}).get('availability','missing')
                            for key in ('hrd_score','hrd_loh','hrd_tai','hrd_lst')}}


def legacy_patient_context(observation):
    pairs={'clinicalDiagnosis':'source_clinical_diagnosis','tumorType':'source_tumor_type',
           'sex':'sex','age':'age_at_reference','blockId':'block_id',
           'tumorPercentage':'tumor_percentage','specimenType':'specimen_type',
           'specimenSite':'specimen_site'}
    fields=observation.get('fields') or {}
    result={old:str(fields[new]['value']) for old,new in pairs.items()
            if (fields.get(new) or {}).get('availability')=='observed'}
    result['source']='GenNext Run Detail exact fields / active sample verified'
    return result
