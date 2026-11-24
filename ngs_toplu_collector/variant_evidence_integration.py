"""NGS evidence v1.1: YZ send, return, masked source copies and review UI.

This additive adapter does NOT change report, Selection, Oncogenicity or IGV decisions.
Existing v1 hooks are upgraded in place by the installer, never stacked twice.
"""
from __future__ import annotations

from contextvars import ContextVar
from functools import wraps
import hashlib
import hmac
import json
from pathlib import Path
import tempfile
import time

from variant_evidence import SCHEMA, _fingerprint, _candidate_rows, enrich_packages
from variant_evidence_privacy import Masker, sanitize_source, sanitized_source_has_direct_identifiers

_ACTIVE = ContextVar('variant_evidence_export',default=None)
INDEX_NAME='VARIANT_EVIDENCE_INDEX.json'
CLINICAL_NAME='YZ_KLINIK_OZET.json'
CLINICAL_MD_NAME='YZ_KLINIK_OZET.md'


def _json_bytes(obj):
    return json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8')


def _small_evidence_record(row):
    evidence=row.get('evidence') or {}
    pubmed=evidence.get('pubmed') or {}
    return {'record_key':row.get('record_key'),'variant_key':row.get('variant_key'),
            'kind':row.get('kind'),'gene':evidence.get('gene'),
            'descriptor':evidence.get('descriptor'),'priority_tier':row.get('priority_tier'),
            'provenance':row.get('provenance'),'evidence_key':row.get('evidence_key'),
            'source_oncogenicity':row.get('source_oncogenicity'),
            'pubmed_status':pubmed.get('status'),
            'pubmed_pmids':[p.get('pmid') for p in (pubmed.get('papers') or [])[:5]],
            'clinvar_status':(evidence.get('clinvar') or {}).get('status'),
            'allele_match_verified':False, 'human_review_required':True}


def _clinical_summary(pkg):
    """Small, stable input for YZ. Detailed sources remain available in the ZIP."""
    records=[_small_evidence_record(x) for x in (pkg.get('variant_evidence_records') or [])]
    by_record={}
    for row in records:
        by_record.setdefault(row['record_key'],[]).append(row)
    patients=[]
    for p in pkg.get('patients') or []:
        tech=p.get('technical_sample') or {}
        ctx=p.get('clinical_context') or {}
        identity=p.get('identity') or {}
        rk=tech.get('record_key')
        selection=p.get('report_selection_by_assay') or {}
        selection_states={}
        for assay in ('SNV_INDEL','CNV','RNA_FUSION'):
            s=selection.get(assay) or {}
            selection_states[assay]={'report_selection_verified':s.get('report_selection_verified') is True,
                'result_status':s.get('result_status') or 'not_scanned',
                'selected_count':s.get('selected_count'),
                'selected_variants':[{k:r.get(k) for k in ('variant_key','event_key','fusion_key','gene','protein','cdna','direction','fusion','oncogenicity') if k in r}
                                     for r in (s.get('selected_variants') or [])[:45]]}
        qc=p.get('qc') or {}
        biom=p.get('biomarkers') or {}
        igv=p.get('igv_scope_summary') or {}
        patients.append({'patient_id':identity.get('patient_id') or identity.get('case_family'),
          'record_key':rk,'sample_type':tech.get('sample_type'),
          'clinical':{'diagnosis':p.get('diagnosis') or ctx.get('diagnosis'),
              'age':p.get('age') or ctx.get('age_at_diagnosis'),'sex':p.get('sex') or ctx.get('sex'),
              'block_id':ctx.get('block_id'),'tumor_percentage':ctx.get('tumor_percentage')},
          'biomarkers':{k:v for k,v in biom.items() if k in ('tmb','msi','hrd','hrd_score','msi_status','tmb_value')},
          'qc':{k:v for k,v in qc.items() if k in ('status','summary','verifybamid_contamination_pct','contamination','qc_adequacy')},
          'igv':{k:igv.get(k) for k in ('requested_count','reviewed_count','complete_count','partial_count')},
          'selection':selection_states,'researched_variant_count':len(by_record.get(rk,[])),
          'literature_candidates':by_record.get(rk,[])[:25],
          'cnv_status':(p.get('cnv') or {}).get('status'),
          'fusion_status':(p.get('fusion_summary') or {}).get('status')})
    return {'schema':'ngs_yz_klinik_ozet_v1','source_schema':pkg.get('schema'),
        'pool_group':pkg.get('pool_group'),'sample_scope':pkg.get('sample_scope'),
        'generator_version':pkg.get('generator_version'),
        'evidence_source_sha256':pkg.get('variant_evidence_source_sha256'),
        'variant_evidence_summary':pkg.get('variant_evidence_summary'),
        'patients':patients,
        'policy':{'purpose':'technical_and_molecular_pre_review_only',
          'selection_is_authoritative_only_if_verified':True,
          'no_auto_report_or_oncogenicity_change':True,
          'absent_literature_is_not_negative':True,
          'igv_not_automatically_approved':True,
          'somatic_and_germline_classifications_distinct':True,
          'publication_and_clinvar_hits_require_allele_and_tumor_review':True}}


def _clinical_markdown(summary):
    lines=['# POOL YZ Klinik On Degerlendirme (v1.1)',
           'Bu belge karar destegidir; GenNext Selection, YZ onerisi ve patolog rapor karari ayridir.',
           'PubMed/ClinVar arama kaydi klinik alel/tumor dogrulamasi DEGILDIR.',
           f"POOL: {summary.get('pool_group')} | Kapsam: {summary.get('sample_scope')}",
           'Kaynak indeks SHA256: '+str(summary.get('evidence_source_sha256') or 'yok')]
    for p in summary.get('patients') or []:
        clinical=p.get('clinical') or {}
        label=str(p.get('patient_id') or '?').replace('\n',' ').replace('\r',' ')
        lines.extend(['',f'## {label} / {p.get("sample_type")}',
            'Tani: '+str(clinical.get('diagnosis') or 'belirtilmemis').replace('\n',' '),
            'Yas/cinsiyet: '+str(clinical.get('age') or '-')+'/'+str(clinical.get('sex') or '-'),
            'Blok/tumor: '+str(clinical.get('block_id') or '-')+' / '+str(clinical.get('tumor_percentage') or '-')+'%',
            'Literatur kaydi: '+str(p.get('researched_variant_count') or 0)])
        for assay,sel in (p.get('selection') or {}).items():
            if assay=='RNA_FUSION' and p.get('sample_type')=='DNA':continue
            lines.append(f"- {assay}: verified={sel.get('report_selection_verified')} status={sel.get('result_status')} selected={sel.get('selected_count')}")
        for e in (p.get('literature_candidates') or [])[:12]:
            lines.append(f"- Arastirma (rapor onayi degil): {e.get('gene')} {e.get('descriptor')} | {e.get('provenance')} | PubMed: {e.get('pubmed_status')} | PMID: {', '.join(str(x) for x in e.get('pubmed_pmids') or []) or '-'}")
        lines.append('IGV: '+str(p.get('igv') or {}))
    lines.extend(['', 'Detay: VARIANT_EVIDENCE_INDEX.json, POOL_AI_COMPACT.json, POOL_AI_FULL.json ve kaynak dosyalari.',
                  'Eksik literatur = negatif analiz veya benign varyant DEGIL. Rapor karari yalniz patologundur.'])
    return '\n'.join(lines)+'\n'


def install_ai_export(ns):
    """Wrap final v0.4.83+ runtime entrypoints once; no base source replacement."""
    previous_build=ns['build_pool_ai_package']
    previous_markdown=ns['build_ai_markdown']
    previous_write=ns['write_pool_ai_bundle']
    previous_inventory=ns['_source_inventory']
    previous_privacy_hits=ns['_source_privacy_hits']
    previous_zip=ns['_write_bundle_archive']

    @wraps(previous_inventory)
    def masked_inventory(base,db,sample,scope):
        entries,copies=previous_inventory(base,db,sample,scope)
        state=_ACTIVE.get()
        if state is None or not state.get('enable_sources'):
            return entries,copies
        prepared=[]
        # Source filenames are part of both SOURCE_INVENTORY and patient JSON.
        # Mask them independently of whether the payload is eligible for copy.
        for entry in entries:
            if isinstance(entry,dict) and entry.get('file_name'):
                entry['file_name']=state['masker'].name(entry['file_name'])
        for src,arc in copies:
            safe=sanitize_source(src,arc,state['masker'],state['stage'])
            if safe is None:
                # Original safety gate will omit unsupported files; never copy blind.
                prepared.append((src,arc))
                state['blocked_source_paths'].add(str(src))
                state['skipped_source_count']+=1
                continue
            src_safe,arc_safe,detail=safe
            if sanitized_source_has_direct_identifiers(src_safe,state['masker']):
                prepared.append((src,arc))
                state['blocked_source_paths'].add(str(src))
                state['skipped_source_count']+=1
                continue
            prepared.append((src_safe,arc_safe))
            state['safe_source_paths'].add(str(src_safe))
            state['sanitized_source_count']+=1
            state['omitted_zip_members']+=detail['omitted_binary_or_unsupported_members']
            for entry in entries:
                if entry.get('bundle_path')==arc:
                    entry.update({'bundle_path':arc_safe,'sha256':detail['source_sha256_after_redaction'],
                        'size_bytes':detail['redacted_bytes'],'privacy_transform':'masked_name_tc_email',
                        'original_source_sha256':detail['source_sha256_before_redaction'],
                        'omitted_binary_or_unsupported_members':detail['omitted_binary_or_unsupported_members']})
        return entries,prepared

    @wraps(previous_privacy_hits)
    def check_masked_source(path,names,tcs,**kwargs):
        state=_ACTIVE.get()
        if state is not None and str(path) in state.get('blocked_source_paths',set()):
            return ['unsafe_or_unreadable_source_not_exported']
        if state is not None and str(path) in state.get('safe_source_paths',set()):
            return ['sanitized_source_still_contains_identifier'] if sanitized_source_has_direct_identifiers(path,state['masker']) else []
        return previous_privacy_hits(path,names,tcs,**kwargs)

    @wraps(previous_build)
    def build_with_literature(base,db,cfg,pool_group,*args,**kwargs):
        compact,full,sources=previous_build(base,db,cfg,pool_group,*args,**kwargs)
        active=str((cfg or {}).get('variant_evidence_auto',True)).lower() not in ('0','false','no','off')
        if active:
            try:
                compact,full=enrich_packages(compact,full,base,cfg)
            except Exception as exc:
                for pkg in (compact,full):
                    pkg['variant_evidence_schema']=SCHEMA
                    pkg['variant_evidence_records']=[]
                    pkg['variant_evidence_source_sha256']=_fingerprint([])
                    pkg['variant_evidence_summary']={'status':'failed_open',
                        'error_type':type(exc).__name__, 'auto_research_enabled':True,
                        'human_verification_required':True}
        else:
            for pkg in (compact,full):
                pkg['variant_evidence_schema']=SCHEMA
                pkg['variant_evidence_records']=[]
                pkg['variant_evidence_source_sha256']=_fingerprint([])
                pkg['variant_evidence_summary']={'status':'disabled_by_local_config',
                    'auto_research_enabled':False}
        state=_ACTIVE.get()
        if state is not None:
            state['evidence_index']={'schema':'ngs_variant_evidence_index_v2',
                'pool_group':compact.get('pool_group'), 'sample_scope':compact.get('sample_scope'),
                'source_sha256':compact.get('variant_evidence_source_sha256'),
                'records':compact.get('variant_evidence_records') or [],
                'verification_note':'Verified SHA256 refers to the local search-record list, not to clinical allele validity.'}
            state['clinical_summary']=_clinical_summary(compact)
        return compact,full,sources

    @wraps(previous_markdown)
    def markdown_with_literature(pkg):
        original=previous_markdown(pkg).rstrip()
        summary=pkg.get('variant_evidence_summary') or {}
        note=['','', '## OTOMATIK LITERATUR / VARYANT KANITI v1.1',
          'Bu kapsamli promptun yaninda YZ_KLINIK_OZET.json dosyasi bulunur; once klinik ozet incelenmelidir.',
          'VARIANT_EVIDENCE_INDEX.json envanteri ayri SHA256 indeksidir, YZ donuste 200MB COMPACT tekrar okunmaz.',
          'GenNext Selection dogrulanmamis ise rapora giren varyantlar belirlenmis SAYILMAZ.',
          'Araştırma adaylari asla otomatik Selection veya pozitif rapora eklenmez.',
          'PubMed adaylari varyant adi gorulse bile molekuler alel ve tumor baglaminda dogrulanmamis olabilir.',
          'ClinVar germline patojenitesi somatik onkojenisite veya tedavi kaniti değildir.',
          'YZ donusunde record_key+variant_key+tur eșlesmesi ile kaynak, uyusmazlik ve IGV durumu belirt.',
          'Oncogenicity Unknown korunur; negatif klinik test sonucu ancak analitik kanitla olabilir.',
          f"Taranan: {summary.get('examined',0)} / aday: {summary.get('candidate_total',0)}; ertelenen: {summary.get('deferred',0)}.",
          f"Selection doğrulanmayan örnek: {summary.get('unverified_selection_sample_count','?')}."]
        return original+'\n'+'\n'.join(note)+'\n'

    @wraps(previous_zip)
    def zip_with_sidecars(outdir,zip_path):
        state=_ACTIVE.get()
        if state is not None and state.get('evidence_index'):
            out=Path(outdir)
            index_path=out/INDEX_NAME
            clinical_path=out/CLINICAL_NAME
            clinical_md=out/CLINICAL_MD_NAME
            index_path.write_bytes(_json_bytes(state['evidence_index']))
            clinical_path.write_bytes(_json_bytes(state['clinical_summary']))
            clinical_md.write_text(_clinical_markdown(state['clinical_summary']),encoding='utf-8')
            manifest_path=out/'MANIFEST.json'
            if manifest_path.is_file():
                manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
                manifest['variant_evidence_index_schema']='ngs_variant_evidence_index_v2'
                manifest['variant_evidence_index_sha256']=hashlib.sha256(index_path.read_bytes()).hexdigest()
                manifest['variant_evidence_source_sha256']=state['evidence_index']['source_sha256']
                manifest['clinical_summary_included']=True
                manifest['source_privacy_redacted_count']=state['sanitized_source_count']
                manifest['source_omitted_zip_members']=state['omitted_zip_members']
                manifest['source_unsanitizable_count']=state['skipped_source_count']
                manifest['privacy_note']=(
                    'Patient and physician names, national IDs and emails are masked in supported text source copies. '
                    'Original source files never leave in unmasked form. Unsupported binaries and unsafe/unreadable '
                    'source files are omitted. MP, age, block, diagnosis and molecular data remain and may be '
                    'indirect identifiers; share only with authorized clinical staff.')
                # Regenerate hashes AFTER all new sidecars; never duplicate ZIP entries.
                manifest['files']=[{'name':p.relative_to(out).as_posix(),
                    'size_bytes':p.stat().st_size,'sha256':_hash_file(p)}
                    for p in sorted(out.rglob('*')) if p.is_file() and p != manifest_path]
                manifest['manifest_scope']='all_final_files_except_manifest_itself'
                manifest_path.write_bytes(_json_bytes(manifest))
        return previous_zip(outdir,zip_path)

    @wraps(previous_write)
    def write_with_masked_sources(base,db,cfg,*args,**kwargs):
        base=Path(base)
        (base/'DATA').mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='NGS_KANIT_MASK_',dir=base/'DATA') as stage:
            try:
                names,tcs=ns['_privacy_sensitive_values'](db,[dict(x) for x in db.samples()])
            except Exception:
                names,tcs=[],[]
            state={'stage':stage,'masker':Masker(names,tcs), 'safe_source_paths':set(),
                   'enable_sources':bool(kwargs.get('include_source_files',True)),
                   'blocked_source_paths':set(),
                   'sanitized_source_count':0,'skipped_source_count':0,'omitted_zip_members':0,
                   'evidence_index':None,'clinical_summary':None}
            token=_ACTIVE.set(state)
            try:
                return previous_write(base,db,cfg,*args,**kwargs)
            finally:
                _ACTIVE.reset(token)

    ns['_source_inventory']=masked_inventory
    ns['_source_privacy_hits']=check_masked_source
    ns['build_pool_ai_package']=build_with_literature
    ns['build_ai_markdown']=markdown_with_literature
    ns['_write_bundle_archive']=zip_with_sidecars
    ns['write_pool_ai_bundle']=write_with_masked_sources


def _hash_file(p):
    h=hashlib.sha256()
    with Path(p).open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):
            h.update(chunk)
    return h.hexdigest()


def _reference(pkg):
    refs=[]
    for row in (pkg.get('variant_evidence_records') or [])[:90]:
        ev=row.get('evidence') or {}
        refs.append({'record_key':row.get('record_key'),'variant_key':row.get('variant_key'),
            'kind':row.get('kind'),'evidence_key':row.get('evidence_key'),
            'provenance':row.get('provenance'),'priority_tier':row.get('priority_tier'),
            'source_pmids':[p.get('pmid') for p in ((ev.get('pubmed') or {}).get('papers') or [])],
            'clinvar_candidate_ids':[c.get('variation_id') for c in ((ev.get('clinvar') or {}).get('candidates') or [])],
            'evidence_status':(ev.get('pubmed') or {}).get('status')})
    return {'schema':SCHEMA,'source_sha256':pkg.get('variant_evidence_source_sha256'),
        'source_record_count':len(pkg.get('variant_evidence_records') or []),
        'records':refs,'source_verified_locally':False,
        'instruction':'Always match exact technical sample + source variant key + variant kind. '
                      'PMID presence in source index does not verify clinical relevance.'}


def _read_local_index(base,expected_hash,pool='',scope=''):
    """Match compact verified SHA by small sidecar, never open 200MB COMPACT."""
    if not isinstance(expected_hash,str) or not re_sha(expected_hash):return None
    roots=[Path(base)/'AI_EXPORT']
    config=Path(base)/'config.local.json'
    if config.is_file():
        try:
            cfg=json.loads(config.read_text(encoding='utf-8'))
            v=cfg.get('ai_export_dir')
            if isinstance(v,str) and v.strip():
                path=Path(v)
                roots.insert(0,path if path.is_absolute() else Path(base)/path)
        except (ValueError,TypeError,OSError):pass
    all_files=[]
    try:
        for root in roots:
            if root.is_dir():
                all_files.extend(p for p in root.glob('*/'+INDEX_NAME) if p.is_file())
        all_files=sorted(set(all_files),key=lambda p:p.stat().st_mtime,reverse=True)[:250]
    except OSError:return None
    for p in all_files:
        try:
            if p.stat().st_size>12*1024*1024:continue
            idx=json.loads(p.read_text(encoding='utf-8'))
            digest=idx.get('source_sha256') or ''
            if not hmac.compare_digest(digest,expected_hash):continue
            if pool and idx.get('pool_group')!=pool:continue
            if scope and idx.get('sample_scope')!=scope:continue
            records=idx.get('records') or []
            if _fingerprint(records)==expected_hash:
                return idx
        except (ValueError,TypeError,OSError):continue
    return None


def re_sha(value):
    return isinstance(value,str) and len(value)==64 and all(c in '0123456789abcdefABCDEF' for c in value)


def _get_cited_pmids(record):
    raw=record.get('cited_pmids') or []
    return [str(x) for x in raw] if isinstance(raw,list) else []


def _return_pair(row,kind):
    return (row.get('record_key'), row.get('variant_key') or row.get('event_key') or
            row.get('cnv_key') or row.get('fusion_key'), kind)


def _annotate_return(data,base):
    if not isinstance(data,dict):return data
    refs=data.get('variant_evidence_reference') or {}
    src=data.get('source_bundle') or {}
    v1=refs.get('source_sha256') or ''
    v2=src.get('variant_evidence_source_sha256') or ''
    hash_conflict=bool(v1 and v2 and v1 != v2)
    expected='' if hash_conflict else (v1 or v2)
    idx=_read_local_index(base,expected,src.get('pool_group') or '',src.get('sample_scope') or '')
    found={(r.get('record_key'),r.get('variant_key'),r.get('kind')):r
           for r in ((idx or {}).get('records') or []) if isinstance(r,dict)}
    reviews={(r.get('record_key'),r.get('variant_key'),r.get('kind')):r
             for r in (data.get('variant_evidence_reviews') or []) if isinstance(r,dict)}
    matched=0
    extra=set()
    for collection,kind in (('variant_reviews','SNV'),('cnv_reviews','CNV'),('fusion_reviews','RNA')):
        for row in data.get(collection) or []:
            if not isinstance(row,dict):continue
            pair=_return_pair(row,kind)
            source=found.get(pair)
            review=reviews.get(pair) or reviews.get((pair[0],pair[1],None)) or {}
            if source:
                matched+=1
                row['literature_evidence']=source.get('evidence')
                row['literature_evidence_key']=source.get('evidence_key')
                row['literature_evidence_provenance']=source.get('provenance')
            if review: row['variant_evidence_review']=dict(review)
            cited=_get_cited_pmids(review)
            present={str(p.get('pmid')) for p in ((source or {}).get('evidence') or {}).get('pubmed',{}).get('papers',[])}
            local=[pmid for pmid in cited if pmid in present]
            unknown=[pmid for pmid in cited if pmid not in present]
            extra.update(unknown)
            row['literature_reference_check']={
                'status': 'local_index_sha256_verified_not_clinically_verified' if source else
                          ('source_index_hash_disagreement' if hash_conflict else 'local_source_index_not_found'),
                'source_hash_verified':bool(idx), 'pmids_in_local_bundle':local,
                'pmids_not_in_local_bundle':unknown,
                'new_pmids_require_external_review':bool(unknown),
                'source_did_not_verify_allele':True,
                'report_decision_unmodified':True}
    data['variant_evidence_return_audit']={
        'schema':SCHEMA,'local_source_matched':bool(idx),
        'source_index_hash_verified':bool(idx),'source_hash_conflict':hash_conflict,
        'matched_variant_count':matched,
        'new_pmids_require_review':sorted(extra),'source_hash':expected or None,
        'report_decision_unmodified':True,'selection_unmodified':True,
        'note':'Search-index SHA verified, but allele-level clinical evidence still needs review.'}
    return data


def install_ai_review(ns):
    previous_template=ns['build_ai_review_return_template']
    previous_schema=ns['ai_review_return_json_schema']
    previous_validate=ns['validate_ai_review_return']
    previous_load=ns['load_ai_review_return']
    base=Path(ns.get('__file__','')).resolve().parent

    @wraps(previous_template)
    def template_with_references(pkg):
        template=previous_template(pkg)
        refs=_reference(pkg)
        template['variant_evidence_reference']=refs
        template.setdefault('source_bundle',{})['variant_evidence_source_sha256']=refs['source_sha256']
        template['variant_evidence_reviews']=[{
            'record_key':r.get('record_key'),'variant_key':r.get('variant_key'),'kind':r.get('kind'),
            'evidence_key':r.get('evidence_key'),'cited_pmids':[],
            'clinical_relevance':'indeterminate','supports':[],'conflicts':[],
            'assessment':'','additional_pmids_unverified':[]}
            for r in refs['records'][:60]]
        template['instructions']=list(template.get('instructions') or [])+[
            'Literatur: VARIANT_EVIDENCE_INDEX.json SHA256 ile ilgili POOL yerel arşivinde doğrulanır.',
            'Oncelik YZ_KLINIK_OZET.json: once hasta, tumor tipi, Selection/QC/IGV kontrol edilir.',
            'Yalniz kaynak PMID listesindeki PMID klinik kanit olarak otomatik kabul edilmez; allel ve tumor baglami teyit edilir.',
            'Yeni PMID eklenirse additional_pmids_unverified olarak bildir; kaynak uydurma.',
            'Selection not_verified pozitif ya da negatif rapor seçimi değildir. Unknown korunur.']
        return template

    @wraps(previous_schema)
    def schema_with_references():
        schema=previous_schema()
        props=dict(schema.get('properties') or {})
        props['variant_evidence_reference']={'type':'object',
            'description':'SHA256 indexed research leads; not somatic oncogenicity assignments.'}
        props['variant_evidence_reviews']={'type':'array','items':{'type':'object',
            'required':['record_key','variant_key'],
            'properties':{'record_key':{'type':'string'},'variant_key':{'type':'string'},
             'kind':{'type':'string'},'evidence_key':{'type':'string'},
             'cited_pmids':{'type':'array','items':{'type':'string','pattern':'^[0-9]{1,9}$'}},
             'clinical_relevance':{'type':'string'},'assessment':{'type':'string'},
             'supports':{'type':'array'},'conflicts':{'type':'array'},
             'additional_pmids_unverified':{'type':'array'}}},
            'description':'Research review keyed by technical sample + variant + kind.'}
        schema['properties']=props
        return schema

    @wraps(previous_validate)
    def validate_with_references(data):
        errors,warnings=previous_validate(data)
        if not isinstance(data,dict):return errors,warnings
        entries=data.get('variant_evidence_reviews') or []
        if not isinstance(entries,list):return errors+['variant_evidence_reviews liste olmali'],warnings
        keys=set()
        for i,r in enumerate(entries):
            if not isinstance(r,dict):
                errors.append(f'variant_evidence_reviews[{i}] nesne olmali')
                continue
            key=(r.get('record_key'),r.get('variant_key'),r.get('kind'))
            if not key[0] or not key[1]:errors.append(f'variant_evidence_reviews[{i}] record_key+variant_key zorunlu')
            if key in keys:errors.append(f'variant_evidence_reviews[{i}] duplicate')
            keys.add(key)
            pmids=_get_cited_pmids(r)
            if any(not x.isdigit() or len(x)>9 for x in pmids):
                errors.append(f'variant_evidence_reviews[{i}] PMID sayisal liste olmali')
        return errors,warnings

    @wraps(previous_load)
    def load_with_evidence(path):
        data,source,sha=previous_load(path)
        if isinstance(data,dict) and (data.get('variant_evidence_reference') or
                data.get('variant_evidence_reviews') or
                (data.get('source_bundle') or {}).get('variant_evidence_source_sha256')):
            data=_annotate_return(data,base)
        return data,source,sha

    ns['build_ai_review_return_template']=template_with_references
    ns['ai_review_return_json_schema']=schema_with_references
    ns['validate_ai_review_return']=validate_with_references
    ns['load_ai_review_return']=load_with_evidence


def install_workspace(ns):
    previous_feature=ns['feature_text']
    previous_patient=ns['patient_text']

    @wraps(previous_feature)
    def feature_with_evidence(row,*args,**kwargs):
        original=previous_feature(row,*args,**kwargs)
        evidence=row.get('literature_evidence') or {}
        review=row.get('variant_evidence_review') or {}
        audit=row.get('literature_reference_check') or {}
        if not (evidence or review or audit):return original
        lines=['','--- LITERATUR / KANIT ON DEGERLENDIRME ---',
            'Yerel kaynak SHA256: '+str(audit.get('status') or 'inceleme gerekli'),
            'Varyant aleli: YALNIZ GEN / PROTEIN ESLESMESI; GENOMIK ALLEL DOGRULANMADI',
            'Spesifiklik: '+str(evidence.get('specificity') or 'bilinmiyor')]
        for c in ((evidence.get('clinvar') or {}).get('candidates') or [])[:4]:
            lines.append('ClinVar: '+str(c.get('title') or '')+' | '+str(c.get('url') or '')+' [alel teyidi yok]')
        for paper in ((evidence.get('pubmed') or {}).get('papers') or [])[:5]:
            lines.append('PMID '+str(paper.get('pmid'))+' '+str(paper.get('title') or '')+' '+str(paper.get('url') or ''))
            if paper.get('abstract_excerpt'):
                lines.append('  Ozet: '+str(paper['abstract_excerpt'])[:500])
        if review:lines.append('YZ literatur yorumu: '+str(review.get('assessment') or 'belirtilmedi'))
        if audit.get('pmids_not_in_local_bundle'):
            lines.append('Yerel pakette olmayan PMID: '+', '.join(audit['pmids_not_in_local_bundle']))
        lines.append('Selection, Oncogenicity, IGV ve rapor karari DEGISTIRILMEDI.')
        return original+'\n'.join(lines)

    ns['feature_text']=feature_with_evidence
    ns['patient_text']=previous_patient
