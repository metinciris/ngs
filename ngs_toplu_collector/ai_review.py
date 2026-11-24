from __future__ import annotations

import hashlib
import json
import os
import re
import zipfile
import shutil
import time
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

RETURN_SCHEMA = 'ngs_ai_review_return_v1'
RELEASE_VALUES = ('ready', 'review', 'hold', 'rerun', 'indeterminate')
REVIEW_VALUES = ('report', 'consider', 'igv_review', 'hold', 'do_not_report', 'indeterminate')
CONFIDENCE_VALUES = ('high', 'medium', 'low', 'indeterminate')
IGV_PRIORITY_VALUES = ('high', 'normal', 'low')
POOL_RISK_VALUES = ('low', 'moderate', 'high', 'indeterminate')
ARTIFACT_REVIEW_VALUES = ('known_artifact', 'recurrent_technical_suspect', 'likely_biological_or_germline', 'carryover_suspect', 'indeterminate')


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def _region_from_variant_key(variant_key: str) -> str:
    parts = str(variant_key or '').split('|')
    if len(parts) < 4:
        return ''
    chrom, start, ref, alt = parts[:4]
    try:
        s = int(start)
    except Exception:
        return ''
    ref = ref or '-'
    end = s if ref == '-' else s + max(1, len(ref)) - 1
    return f'{chrom}:{s}-{end}'


def _location_from_variant(v: dict) -> dict:
    ann = v.get('annotations') or {}
    region = v.get('igv_region') or ann.get('igv_region') or _region_from_variant_key(v.get('variant_key'))
    return {
        'variant_key': v.get('variant_key') or '',
        'gene': v.get('gene') or '',
        'change': v.get('change') or '',
        'igv_region': region,
    }


def build_ai_review_return_template(pkg: dict) -> dict:
    """Small machine-readable return file template included in every AI bundle.

    The AI should preserve identity keys and add only clinically/technically reviewed items.
    It should not duplicate the entire raw variant table.
    """
    patient_reviews = []
    sample_index = []
    for p in pkg.get('patients') or []:
        ident = p.get('identity') or {}
        ts = p.get('technical_sample') or {}
        cc = p.get('clinical_context') or {
            'age_at_diagnosis': p.get('age'),
            'sex': p.get('sex'),
            'diagnosis': p.get('diagnosis'),
            'block_id': ts.get('block_id'),
            'tumor_percentage': ts.get('tumor_percentage'),
            'sample_type': ts.get('sample_type'),
            'missing_context': [],
        }
        entry = {
            'patient_link_id': ident.get('patient_link_id') or '',
            'patient_id': ident.get('patient_id') or ident.get('case_family') or '',
            'record_key': ts.get('record_key') or '',
            'sample_type': ts.get('sample_type') or '',
            'clinical_context': cc,
            'release_recommendation': 'indeterminate',
            'summary': '',
            'notes': [],
        }
        patient_reviews.append(entry)
        sample_index.append({
            'patient_link_id': entry['patient_link_id'],
            'patient_id': entry['patient_id'],
            'record_key': entry['record_key'],
            'sample_type': entry['sample_type'],
            'masked_full_name': ident.get('masked_full_name') or '',
            'clinical_context': cc,
        })
    return {
        'schema': RETURN_SCHEMA,
        'source_bundle': {
            'pool_group': pkg.get('pool_group') or '',
            'sample_scope': pkg.get('sample_scope') or '',
            'generated_at': pkg.get('generated_at') or '',
            'ai_schema': pkg.get('schema') or '',
        },
        'instructions': [
            'Bu dosyayı geçerli UTF-8 JSON olarak doldur; Markdown kod çiti veya yorum ekleme.',
            'sample_index içindeki patient_link_id, patient_id ve record_key değerlerini değiştirme.',
            'sample_index/patient_reviews içindeki clinical_context alanını değiştirme veya PII gerekçesiyle düşürme; MP/protokol, yaş, cinsiyet, tanı, blok, tümör yüzdesi ve DNA/RNA bağlamı değerlendirme için korunmalıdır.',
            'patient_reviews içindeki her teknik örnek için kısa sonuç ve release_recommendation doldur.',
            'variant_reviews içine yalnız raporlama/teknik karar açısından anlamlı SNV/indel kayıtlarını ekle; tüm ham listeyi kopyalama.',
            'IGV öneriyorsan variant_reviews.igv_required=true yap ve ayrıca igv_requests içine aynı record_key+variant_key ile istek ekle.',
            'TC isteme veya üretme. Maskeli isimleri açmaya çalışma. Maskeli adın bulunmaması clinical_context alanlarını etkilemez. patient_link_id eşitliği aynı hasta bağlantısı için kullanılabilir.',
            'YZ önerisi nihai klinik karar değildir; belirsizliği indeterminate/hold/igv_review ile koru.',
            'known_artifact_history ve recurrent_pool_variants içinden anlamlı olanları artifact_reviews içine koy; tüm tekrarlayan listeyi kopyalama.',
        ],
        'allowed_values': {
            'pool_risk_level': list(POOL_RISK_VALUES),
            'release_recommendation': list(RELEASE_VALUES),
            'variant_review_status': list(REVIEW_VALUES),
            'confidence': list(CONFIDENCE_VALUES),
            'igv_priority': list(IGV_PRIORITY_VALUES),
            'artifact_review_status': list(ARTIFACT_REVIEW_VALUES),
        },
        'sample_index': sample_index,
        'pool_review': {
            'risk_level': 'indeterminate',
            'summary': '',
            'contamination_review': '',
            'technical_actions': [],
        },
        'patient_reviews': patient_reviews,
        'variant_reviews': [
            {
                '_example_only_remove_or_replace': True,
                'patient_link_id': '',
                'patient_id': '',
                'record_key': '',
                'variant_key': 'chrN|position|REF|ALT',
                'gene': '',
                'change': '',
                'review_status': 'igv_review',
                'confidence': 'indeterminate',
                'report_recommendation': '',
                'rationale': '',
                'igv_required': True,
                'igv_region': 'chrN:start-end',
                'igv_reason': '',
                'igv_priority': 'normal',
            }
        ],
        'cnv_reviews': [],
        'igv_requests': [
            {
                '_example_only_remove_or_replace': True,
                'patient_link_id': '',
                'patient_id': '',
                'record_key': '',
                'variant_key': 'chrN|position|REF|ALT',
                'gene': '',
                'change': '',
                'region': 'chrN:start-end',
                'priority': 'normal',
                'reason': '',
            }
        ],
        'artifact_reviews': [
            {
                '_example_only_remove_or_replace': True,
                'variant_key': 'chrN|position|REF|ALT',
                'gene': '',
                'change': '',
                'status': 'indeterminate',
                'confidence': 'indeterminate',
                'reason': '',
                'affected_patient_ids': [],
            }
        ],
        'final_checklist': [],
    }


def ai_review_return_json_schema() -> dict:
    return {
        '$schema': 'https://json-schema.org/draft/2020-12/schema',
        'title': 'NGS AI Review Return',
        'type': 'object',
        'required': ['schema', 'source_bundle', 'pool_review', 'patient_reviews', 'variant_reviews', 'igv_requests'],
        'properties': {
            'schema': {'const': RETURN_SCHEMA},
            'source_bundle': {'type': 'object'},
            'pool_review': {'type': 'object'},
            'patient_reviews': {'type': 'array'},
            'variant_reviews': {'type': 'array'},
            'cnv_reviews': {'type': 'array'},
            'igv_requests': {'type': 'array'},
            'artifact_reviews': {'type': 'array'},
            'final_checklist': {'type': 'array'},
        },
        'additionalProperties': True,
    }


def _strip_examples(data: dict) -> dict:
    out = json.loads(json.dumps(data, ensure_ascii=False))
    for key in ('variant_reviews', 'igv_requests', 'artifact_reviews'):
        out[key] = [x for x in (out.get(key) or []) if not (isinstance(x, dict) and x.get('_example_only_remove_or_replace'))]
    return out



def validate_ai_review_return(data: dict) -> tuple[list[str], list[str]]:
    errors, warnings = [], []
    if not isinstance(data, dict):
        return ['Kök JSON nesne olmalıdır.'], []
    if data.get('schema') != RETURN_SCHEMA:
        errors.append(f"schema '{RETURN_SCHEMA}' olmalıdır.")
    for k in ('source_bundle', 'pool_review'):
        if not isinstance(data.get(k), dict): errors.append(f'{k} nesne olmalıdır.')
    for k in ('patient_reviews', 'variant_reviews', 'igv_requests'):
        if not isinstance(data.get(k), list): errors.append(f'{k} liste olmalıdır.')
    if 'artifact_reviews' in data and not isinstance(data.get('artifact_reviews'), list): errors.append('artifact_reviews liste olmalıdır.')
    if errors:
        return errors, warnings
    risk = str((data.get('pool_review') or {}).get('risk_level') or 'indeterminate')
    if risk not in POOL_RISK_VALUES: warnings.append(f'Bilinmeyen pool risk_level: {risk}')
    for i, pr in enumerate(data.get('patient_reviews') or []):
        if not isinstance(pr, dict): errors.append(f'patient_reviews[{i}] nesne değil.'); continue
        if not pr.get('record_key'): warnings.append(f'patient_reviews[{i}] record_key boş.')
        rv = str(pr.get('release_recommendation') or 'indeterminate')
        if rv not in RELEASE_VALUES: warnings.append(f'patient_reviews[{i}] release_recommendation={rv}')
    for i, vr in enumerate(data.get('variant_reviews') or []):
        if not isinstance(vr, dict): errors.append(f'variant_reviews[{i}] nesne değil.'); continue
        if not vr.get('record_key') or not vr.get('variant_key'):
            errors.append(f'variant_reviews[{i}] record_key ve variant_key zorunlu.')
        st = str(vr.get('review_status') or 'indeterminate')
        if st not in REVIEW_VALUES: warnings.append(f'variant_reviews[{i}] review_status={st}')
        cf = str(vr.get('confidence') or 'indeterminate')
        if cf not in CONFIDENCE_VALUES: warnings.append(f'variant_reviews[{i}] confidence={cf}')
    for i, req in enumerate(data.get('igv_requests') or []):
        if not isinstance(req, dict): errors.append(f'igv_requests[{i}] nesne değil.'); continue
        if not req.get('record_key') or not req.get('variant_key'):
            errors.append(f'igv_requests[{i}] record_key ve variant_key zorunlu.')
        pr = str(req.get('priority') or 'normal')
        if pr not in IGV_PRIORITY_VALUES: warnings.append(f'igv_requests[{i}] priority={pr}')
        if not req.get('region'):
            req['region'] = _region_from_variant_key(req.get('variant_key'))
    for i, ar in enumerate(data.get('artifact_reviews') or []):
        if not isinstance(ar, dict): errors.append(f'artifact_reviews[{i}] nesne değil.'); continue
        if not ar.get('variant_key'): errors.append(f'artifact_reviews[{i}] variant_key zorunlu.')
        st=str(ar.get('status') or 'indeterminate')
        if st not in ARTIFACT_REVIEW_VALUES: warnings.append(f'artifact_reviews[{i}] status={st}')
        cf=str(ar.get('confidence') or 'indeterminate')
        if cf not in CONFIDENCE_VALUES: warnings.append(f'artifact_reviews[{i}] confidence={cf}')
    return errors, warnings


def load_ai_review_return(path: Path) -> tuple[dict, str, str]:
    path = Path(path)
    if path.suffix.lower() == '.zip':
        with zipfile.ZipFile(path, 'r') as z:
            candidates = [n for n in z.namelist() if Path(n).name.upper() == 'AI_REVIEW_RETURN.JSON']
            if not candidates:
                raise ValueError('ZIP içinde AI_REVIEW_RETURN.json bulunamadı.')
            raw = z.read(candidates[0]).decode('utf-8-sig', errors='strict')
        data = json.loads(raw)
        source = f'{path}!{candidates[0]}'
    else:
        data = json.loads(path.read_text(encoding='utf-8-sig'))
        source = str(path)
    data = _strip_examples(data)
    errors, warnings = validate_ai_review_return(data)
    if errors:
        raise ValueError('YZ dönüş dosyası doğrulanamadı:\n- ' + '\n- '.join(errors))
    return data, source, _sha256(path)


def _igv_session_row(db, session_id=None):
    db.ensure_ai_review_schema()
    if session_id is None:
        rows=db.ai_review_sessions(1)
        return dict(rows[0]) if rows else None
    row=db.cx.execute('SELECT * FROM ai_review_sessions WHERE id=?',(int(session_id),)).fetchone()
    return dict(row) if row else None


def _igv_rows_for_session(db, session_id):
    rows=[dict(r) for r in db.igv_queue_rows('Tümü')]
    if session_id is None:return rows
    return [r for r in rows if int(r.get('source_session_id') or 0)==int(session_id)]


def _source_call_context(db, row):
    db.ensure_analysis_schema()
    key=str(row.get('variant_key') or '')
    keys=[key, key[4:] if key.upper().startswith('SNV|') else 'SNV|'+key]
    for k in keys:
        hit=db.cx.execute('''SELECT gene,change,af,ad,dp,filter,oncogenic,highest_level,chrom,start,ref,alt
          FROM oncogenic_observations WHERE record_key=? AND variant_key=? LIMIT 1''',(row.get('record_key') or '',k)).fetchone()
        if hit:return dict(hit)
    return {}


def igv_variant_tumor_context(db, row):
    """Read this technical record's current context; never derive expected VAF."""
    from reporting_context import build_record_context
    rk = str(row.get('record_key') or '')
    if not rk:
        raise ValueError('IGV isteğinin teknik kayıt kimliği eksik.')
    context = build_record_context(db, rk)
    field = context['field_sources'].get('tumor_percentage') or {}
    session = db.cx.execute('SELECT raw_json FROM ai_review_sessions WHERE id=?',
                            (row.get('source_session_id'),)).fetchone()
    source_doc = json.loads(session['raw_json'] or '{}') if session else {}
    normalize_key = lambda value: re.sub(r'^SNV\|', '', str(value or ''), flags=re.I)
    variant = next((v for v in source_doc.get('variant_reviews', [])
                    if v.get('record_key') == rk and normalize_key(v.get('variant_key')) == normalize_key(row.get('variant_key'))), {})
    original = variant.get('source_evidence') or {}
    if original.get('genome_build') and row.get('genome_build') and original['genome_build'] != row['genome_build']:
        original = {}
    call = _source_call_context(db, row)
    if call:
        af = call.get('af')
        metrics = {'vaf_percent': af * 100 if isinstance(af, (int, float)) else None,
                   'af_fraction': af, 'ad': call.get('ad'), 'dp': call.get('dp'), 'filter': call.get('filter')}
        metrics_origin = 'Bu teknik kaydın kaynak varyant tablosu'
    else:
        metrics = {'vaf_percent': original.get('vaf_percent_original'),
                   'af_fraction': original.get('vaf_fraction_original'), 'ad': original.get('AD_original'),
                   'dp': original.get('DP_original'), 'filter': original.get('Filter_original')}
        if metrics['vaf_percent'] is None and isinstance(metrics['af_fraction'], (int, float)):
            metrics['vaf_percent'] = metrics['af_fraction'] * 100
        metrics_origin = 'YZ dönüş paketinde aktarılan özgün kaynak değerleri'
    patient = next((p for p in source_doc.get('patient_reviews', []) if p.get('record_key') == rk), {})
    qc = dict(patient.get('qc_summary') or {})
    technical = patient.get('technical_review') or {}
    for name in ('q30_pct', 'target_ge_50x_pct', 'target_ge_100x_pct', 'verifybamid_snps',
                 'verifybamid_marker_average_depth_x', 'freemix_percent_from_export'):
        if name in technical:
            qc.setdefault(name, technical[name])
    return {'record_key': rk, 'variant_key': row.get('variant_key'),
            'tumor_percentage': context['clinical'].get('tumor_percent'),
            'tumor_percentage_source': 'manual' if field.get('manual_override', {}).get('present') else field.get('effective_origin'),
            'clinical_revision': context['revision'], 'source_metrics': metrics,
            'source_metrics_origin': metrics_origin, 'source_qc_summary': qc,
            'local_cnv_status': 'not_verified_for_this_locus', 'local_loh_status': 'not_verified_for_this_locus',
            'interpretation_note': 'Tümör yüzdesi/2, VAF için otomatik kabul veya ret kuralı değildir. '
                'Tümör saflığı tek başına bu varyantın beklenen VAF değerini belirlemez.',
            'locus_note': 'Bu lokusta CNV/LOH doğrulanmış değil; yorum için lokusa özgü doğrulanmış veri gerekir. '
                'Genel HRD-LOH değeri bu varyantta yerel LOH kanıtı olarak kullanılmaz.'}


def _igv_variant_tumor_context_lines(context):
    def display(value):
        if value is None or value == '':
            return 'belirtilmemiş'
        return format(value, '.12g') if isinstance(value, (int, float)) else str(value)
    metrics = context['source_metrics']
    tumor = context['tumor_percentage']
    origin = 'kaydedilmiş manuel değer' if context['tumor_percentage_source'] == 'manual' else 'kayıtlı kaynak'
    lines = ['VARYANT / TÜMÖR BAĞLAMI',
             'Teknik kayıt: ' + context['record_key'],
             'Varyant: ' + str(context['variant_key'] or ''),
             'Güncel tümör yüzdesi: ' + ('%' + display(tumor) + ' · ' + origin if tumor is not None else 'belirtilmemiş'),
             context['source_metrics_origin'] + ' (IGV üzerinden yeniden ölçülmedi):',
             'VAF: ' + ('%' + display(metrics['vaf_percent']) if metrics['vaf_percent'] is not None else 'belirtilmemiş')
             + ' · AD: ' + display(metrics['ad']) + ' · DP: ' + display(metrics['dp']) + ' · Filter: ' + display(metrics['filter']),
             '', context['interpretation_note'], context['locus_note']]
    if context['source_qc_summary']:
        lines += ['', 'Kayıtlı kaynak QC (YZ dönüş paketindeki değerler; null = kaynakta belirtilmemiş):',
                  json.dumps(context['source_qc_summary'], ensure_ascii=False, indent=2),
                  'QC değerleri mevcut laboratuvarın doğrulanmış ölçütleriyle değerlendirilir; bu görünüm yeni eşik uygulamaz.']
    return lines


def _safe_name(value,limit=70):
    return re.sub(r'[^A-Za-z0-9._-]+','_',str(value or 'NA'))[:limit]


def _write_patholog_html(out: Path, items: list, title: str, missing: list):
    cards=[]
    for x in items:
        src=x['image']
        reason=(x.get('original_ai_reason') or '').replace('&','&amp;').replace('<','&lt;').replace('>','&gt;')
        cards.append(f'''<article><h2>{x.get('patient_id','')} · {x.get('gene','')} {x.get('change','')}</h2>
        <p><b>{x.get('region','')}</b> · zoom {x.get('auto_settings',{}).get('window_size_bp') or x.get('auto_settings',{}).get('window_size') or '-'} · Duplicate ON · Sort by base</p>
        <p>{reason}</p><a href="{src}"><img src="{src}" loading="lazy"></a></article>''')
    misses=''.join(f'<li>{m.get("patient_id","")} · {m.get("gene","")} {m.get("change","")} — {m.get("capture_error") or "IGV hazır değil"}</li>' for m in missing)
    html=f'''<!doctype html><html lang="tr"><meta charset="utf-8"><title>{title}</title><style>
    body{{font:15px/1.45 system-ui;margin:24px;background:#eef3f7;color:#17324d}}header{{position:sticky;top:0;background:#eef3f7;padding:4px 0 12px;z-index:2}}
    .grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(620px,1fr));gap:18px}}article{{background:white;border:1px solid #ccd5df;border-radius:10px;padding:14px}}
    article h2{{font-size:18px;margin:0 0 6px}}article p{{margin:5px 0}}img{{width:100%;height:auto;border:1px solid #ddd;background:white}}.warn{{background:#fff3cd;padding:12px;border-radius:8px}}
    </style><header><h1>{title}</h1><p>Patolog görsel kontrolü. Görüntü tek başına read sayısı/base kalite ölçümü yerine geçmez; kaynak AD/DP/VAF ve QC ile birlikte değerlendirilir.</p></header>
    {('<section class="warn"><b>Hazır olmayan IGV:</b><ul>'+misses+'</ul></section>') if missing else ''}<main class="grid">{''.join(cards)}</main></html>'''
    (out/'PATHOLOG_VIEW.html').write_text(html,encoding='utf-8')


def write_igv_ai_bundle(base: Path, db, cfg: dict, session_id=None, require_complete=True):
    base=Path(base);session=_igv_session_row(db,session_id)
    if not session:raise ValueError('IGV paketi için YZ dönüş session kaydı bulunamadı.')
    sid=int(session['id']);rows=_igv_rows_for_session(db,sid)
    if not rows:raise ValueError('Bu YZ dönüşünde IGV isteği yok.')
    ready=[];missing=[]
    for r in rows:
        p=Path(r.get('screenshot_path') or '')
        if int(r.get('ai_ready') or 0) and p.exists():ready.append(r)
        else:missing.append(r)
    if require_complete and missing:
        labels=', '.join(f"{r.get('patient_id') or '?'} {r.get('gene') or r.get('change') or ''}" for r in missing[:10])
        raise ValueError(f'YZ paketi henüz tam değil: {len(ready)}/{len(rows)} IGV AI-READY. Eksikler: {labels}. Önce Tüm IGV’leri Otomatik Hazırla ile yeniden deneyin.')
    if not ready:raise ValueError('AI-READY IGV görüntüsü yok.')
    stamp=time.strftime('%Y%m%d_%H%M%S');pool=_safe_name(session.get('pool_group') or 'POOL');scope=_safe_name(session.get('sample_scope') or 'ALL');stem=f'IGV_AI_REVIEW_{pool}_{scope}_{stamp}'
    root=base/str(cfg.get('ai_export_dir') or 'AI_EXPORT')/'IGV_READY'/f'{pool}_{scope}';root.mkdir(parents=True,exist_ok=True)
    out=root/stem;out.mkdir(parents=True,exist_ok=False);images=out/'IMAGES';images.mkdir();items=[]
    for i,r in enumerate(ready,1):
        src=Path(r['screenshot_path']);name=f'{i:03d}_{_safe_name(r.get("patient_id"))}_{_safe_name(r.get("gene") or "VAR")}_{_safe_name(r.get("change") or r.get("region"))}.png';shutil.copy2(src,images/name)
        try:settings=json.loads(r.get('auto_settings_json') or '{}')
        except Exception:settings={}
        ai_ctx=db.igv_queue_ai_context(r['id']) or {};source_call=_source_call_context(db,r)
        items.append({'queue_id':r['id'],'patient_link_id':r.get('patient_link_id') or '','patient_id':r.get('patient_id') or '','record_key':r.get('record_key') or '',
            'sample_type':r.get('sample_type') or '','variant_key':r.get('variant_key') or '','gene':r.get('gene') or '','change':r.get('change') or '',
            'region':r.get('region') or _region_from_variant_key(r.get('variant_key')),'priority':r.get('priority') or '','original_ai_reason':r.get('reason') or '',
            'image':f'IMAGES/{name}','capture_ai_ready':True,'capture_error':'','captured_at':r.get('captured_at') or '','auto_settings':settings,'source_call':source_call,
            'previous_ai_review':{k:ai_ctx.get(k) for k in ('review_status','confidence','report_recommendation','rationale','igv_reason') if ai_ctx.get(k)}})
    missing_public=[{'queue_id':r['id'],'patient_id':r.get('patient_id') or '','gene':r.get('gene') or '','change':r.get('change') or '','region':r.get('region') or '',
                     'capture_error':r.get('capture_error') or '','status':r.get('status') or ''} for r in missing]
    evidence={'schema':'ngs_igv_ai_evidence_v2','generated_at':time.strftime('%Y-%m-%dT%H:%M:%S'),'source_ai_session_id':sid,
        'source_pool_group':session.get('pool_group') or '','source_sample_scope':session.get('sample_scope') or '','requested_count':len(rows),'item_count':len(items),
        'ai_ready_count':len(items),'missing_count':len(missing_public),'missing_items':missing_public,'items':items,
        'privacy':{'national_id':False,'full_name':False,'patient_id_is_lab_sample_code':True},
        'interpretation_boundary':'IGV screenshots are supporting evidence. Exact counts come from source_call/NGS source, not pixel guessing. AI must not overwrite human/manual report decisions.'}
    (out/'IGV_EVIDENCE.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf-8')
    try:source_ai=json.loads(session.get('raw_json') or '{}')
    except Exception:source_ai={}
    (out/'SOURCE_AI_REVIEW_RETURN.json').write_text(json.dumps(source_ai,ensure_ascii=False,indent=2),encoding='utf-8')
    template={'schema':'ngs_igv_ai_return_v2','source_bundle':stem,'reviews':[{'_example_only_remove_or_replace':True,'queue_id':0,'visual_assessment':'uncertain','confidence':'indeterminate','observations':[],'reason':'','recommended_manual_action':''}],
        'allowed_visual_assessment':['supported','artifact_suspect','uncertain','insufficient_image'],'allowed_confidence':['high','medium','low','indeterminate']}
    (out/'AI_IGV_RETURN_TEMPLATE.json').write_text(json.dumps(template,ensure_ascii=False,indent=2),encoding='utf-8')
    prompt=(
        '# IGV görsel ikinci değerlendirme paketi\n\nSOURCE_AI_REVIEW_RETURN.json ilk YZ değerlendirmesini; IGV_EVIDENCE.json ise her queue_id için görüntü ve kaynak varyant metriklerini içerir. Her PNG dosyasını aynı queue_id ile eşleştir.\n\n'
        '- Bu ZIP yalnız AI-READY görüntüleri içerir; Duplicate Reads=ON, hedef varyant merkezli yakın zoom, Sort by base ve stabil alignment render doğrulanmıştır.\n'
        '- source_call içindeki AF/AD/DP/filter kaynak NGS değerleridir. Görüntüden mutlak read sayısı uydurma.\n'
        '- Görsel destek, strand/orientation paterni, mapping şüphesi ve düşük destek açısından yorum yap; belirsiz görüntüyü zorla destekli/artefakt sınıfına koyma.\n'
        '- İlk YZ önerisini körlemesine onaylama; IGV bulgusuyla çelişiyorsa açıkça belirt.\n'
        '- YZ sonucu nihai patoloji kararı değildir ve manuel kararı otomatik değiştirmemelidir.\n\nAI_IGV_RETURN_TEMPLATE.json yapısında yalnız geçerli UTF-8 JSON üret.\n')
    (out/'PROMPT.md').write_text(prompt,encoding='utf-8')
    _write_patholog_html(out,items,f'{session.get("pool_group") or "POOL"} {session.get("sample_scope") or ""} · IGV Patolog Galerisi',missing_public)
    manifest={'schema':'ngs_igv_ai_bundle_v2','generated_at':evidence['generated_at'],'source_ai_session_id':sid,'files':[]}
    for f in sorted(out.rglob('*')):
        if f.is_file():manifest['files'].append({'path':f.relative_to(out).as_posix(),'size':f.stat().st_size,'sha256':_sha256(f)})
    (out/'MANIFEST.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    zpath=root/(stem+'.zip')
    with zipfile.ZipFile(zpath,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as zf:
        for f in sorted(out.rglob('*')):
            if f.is_file():zf.write(f,arcname=f'{stem}/{f.relative_to(out).as_posix()}')
    (root/'SON_IGV_YZ_PAKETI.txt').write_text(str(zpath),encoding='utf-8')
    return {'folder':str(out),'zip':str(zpath),'items':len(items),'ai_ready':len(items),'requested':len(rows),'missing':len(missing_public),'gallery':str(out/'PATHOLOG_VIEW.html'),'session_id':sid}


def write_igv_pathologist_gallery(base: Path, db, cfg: dict, session_id=None):
    base=Path(base);session=_igv_session_row(db,session_id)
    if not session:raise ValueError('Patolog galerisi için YZ dönüş session kaydı yok.')
    rows=_igv_rows_for_session(db,int(session['id']));ready=[];missing=[];stamp=time.strftime('%Y%m%d_%H%M%S');pool=_safe_name(session.get('pool_group') or 'POOL');scope=_safe_name(session.get('sample_scope') or 'ALL')
    out=base/str(cfg.get('igv_snapshot_dir') or 'IGV_SNAPSHOTS')/'PATHOLOG_GALLERY'/f'{pool}_{scope}_{stamp}';images=out/'IMAGES';images.mkdir(parents=True,exist_ok=False)
    for i,r in enumerate(rows,1):
        p=Path(r.get('screenshot_path') or '')
        if int(r.get('ai_ready') or 0) and p.exists():
            name=f'{i:03d}_{_safe_name(r.get("patient_id"))}_{_safe_name(r.get("gene") or "VAR")}_{_safe_name(r.get("change") or r.get("region"))}.png';shutil.copy2(p,images/name)
            try:settings=json.loads(r.get('auto_settings_json') or '{}')
            except Exception:settings={}
            ready.append({'patient_id':r.get('patient_id') or '','gene':r.get('gene') or '','change':r.get('change') or '','region':r.get('region') or '','original_ai_reason':r.get('reason') or '','image':f'IMAGES/{name}','auto_settings':settings})
        else:missing.append({'patient_id':r.get('patient_id') or '','gene':r.get('gene') or '','change':r.get('change') or '','capture_error':r.get('capture_error') or ''})
    _write_patholog_html(out,ready,f'{session.get("pool_group") or "POOL"} {session.get("sample_scope") or ""} · IGV Patolog Galerisi',missing)
    return {'folder':str(out),'html':str(out/'PATHOLOG_VIEW.html'),'ready':len(ready),'missing':len(missing)}


def clear_pathologist_decisions(db, item_ids):
    """Clear only human/pathologist IGV decisions; preserve images and AI evidence."""
    cleared=0
    for raw_id in item_ids or []:
        try:item=db.get_igv_queue_item(int(raw_id))
        except Exception:item=None
        if not item or not (item.get('manual_decision') or '').strip():continue
        fallback='captured' if (item.get('screenshot_path') or '').strip() else ('opened' if item.get('status')=='opened' else 'pending')
        db.update_igv_queue_item(item['id'],manual_decision='',status=fallback)
        cleared+=1
    return cleared


def clear_igv_capture_artifacts(base: Path, db, item_ids, cfg=None):
    """Delete selected derived IGV capture files and reset queue rows for recapture.

    Manual/pathologist decisions are preserved. Generated pathologist galleries for the
    affected AI session(s) are invalidated because they are reproducible derivatives.
    """
    base=Path(base).resolve();cfg=cfg or {};deleted_files=0;reset_rows=0;errors=[];session_ids=set()
    for raw_id in item_ids or []:
        try:item=db.get_igv_queue_item(int(raw_id))
        except Exception:item=None
        if not item:continue
        if item.get('source_session_id'):session_ids.add(int(item['source_session_id']))
        paths=[]
        for key in ('screenshot_path','capture_meta_path'):
            if item.get(key):paths.append(Path(item[key]))
        meta_raw=item.get('capture_meta_path') or ''
        meta_path=Path(meta_raw) if meta_raw else None
        if meta_path and meta_path.is_file():
            try:
                meta=json.loads(meta_path.read_text(encoding='utf-8'))
                for key in ('screenshot_path','overview_path','zoom_path'):
                    if meta.get(key):paths.append(Path(meta[key]))
                for comp in meta.get('comparison_components') or []:
                    if isinstance(comp,dict) and comp.get('path'):paths.append(Path(comp['path']))
            except Exception as e:errors.append(f"#{item.get('id')}: metadata okunamadı: {e}")
        seen=set()
        for fp in paths:
            try:
                rp=(fp if fp.is_absolute() else base/fp).resolve()
                if rp in seen:continue
                seen.add(rp)
                if rp!=base and base not in rp.parents:
                    errors.append(f"#{item.get('id')}: çalışma klasörü dışı dosya atlandı: {rp}");continue
                if rp.is_file():rp.unlink();deleted_files+=1
            except Exception as e:errors.append(f"#{item.get('id')}: {fp}: {e}")
        db.update_igv_queue_item(item['id'],status='pending',screenshot_path='',capture_meta_path='',auto_settings_json='',ai_ready=0,capture_error='',captured_at='')
        reset_rows+=1
    gallery_root=base/str(cfg.get('igv_snapshot_dir') or 'IGV_SNAPSHOTS')/'PATHOLOG_GALLERY'
    removed_galleries=0
    if gallery_root.exists() and session_ids:
        prefixes=[]
        for sid in session_ids:
            try:r=db.cx.execute('SELECT pool_group,sample_scope FROM ai_review_sessions WHERE id=?',(sid,)).fetchone()
            except Exception:r=None
            if r:
                prefixes.append(f"{_safe_name(r['pool_group'] or 'POOL')}_{_safe_name(r['sample_scope'] or 'ALL')}_")
        for d in list(gallery_root.iterdir()):
            if not any(d.name.startswith(prefix) for prefix in prefixes):continue
            try:
                if d.is_dir():shutil.rmtree(d);removed_galleries+=1
                elif d.is_file():d.unlink();deleted_files+=1
            except Exception as e:errors.append(f'Galeri önbelleği: {d}: {e}')
    return {'reset_rows':reset_rows,'deleted_files':deleted_files,'removed_galleries':removed_galleries,'errors':errors}


class AIReviewIGV(ttk.Frame):
    def __init__(self, parent, base: Path, db, cfg: dict, browser_submit=None):
        super().__init__(parent)
        self.base = Path(base); self.db = db; self.cfg = cfg; self.browser_submit = browser_submit
        self.filter_var = tk.StringVar(value='Tümü')
        self.summary_var = tk.StringVar(value='Henüz YZ dönüş dosyası içe aktarılmadı.')
        self.live_status_var = tk.StringVar(value='Canlı işleme bekleniyor.')
        self.live_counts_var = tk.StringVar(value='')
        self.live_progress_var = tk.DoubleVar(value=0.0)
        self._live_active = False
        self._live_after_id = None
        self._build(); self.refresh()

    def _build(self):
        top = ttk.Frame(self, padding=10); top.pack(fill='x')
        ttk.Label(top, text='YZ Dönüşü + IGV Kuyruğu', font=('Segoe UI', 13, 'bold')).pack(side='left')
        ttk.Button(top, text='AI_REVIEW_RETURN.json İçe Al', command=self.import_file).pack(side='left', padx=12)
        ttk.Button(top, text='Yenile', command=self.refresh).pack(side='left')
        ttk.Label(top, text='Filtre:').pack(side='left', padx=(18,4))
        cb=ttk.Combobox(top,textvariable=self.filter_var,state='readonly',width=12,values=('Tümü','pending','opened','captured','reviewed'))
        cb.pack(side='left'); cb.bind('<<ComboboxSelected>>',lambda e:self.refresh())
        ttk.Label(self,textvariable=self.summary_var,padding=(12,0,12,4),foreground='#475569').pack(fill='x')
        live=ttk.LabelFrame(self,text='Canlı IGV İşleme Paneli',padding=(10,8))
        live.pack(fill='x',padx=10,pady=(0,6))
        ttk.Label(live,textvariable=self.live_status_var).pack(anchor='w')
        ttk.Progressbar(live,maximum=100,variable=self.live_progress_var).pack(fill='x',pady=4)
        ttk.Label(live,textvariable=self.live_counts_var,foreground='#475569').pack(anchor='w')

        cols=('status','auto','priority','patient','type','gene','change','region','reason','decision','image')
        area=ttk.Frame(self); area.pack(fill='both',expand=True,padx=10,pady=(0,6))
        self.tree=ttk.Treeview(area,columns=cols,show='headings',selectmode='extended')
        labels={'status':'DURUM','auto':'IGV HAZIR','priority':'ÖNCELİK','patient':'HASTA / MP','type':'TİP','gene':'GEN','change':'VARYANT','region':'IGV BÖLGESİ','reason':'YZ NEDENİ','decision':'PATOLOG KARARI','image':'GÖRÜNTÜ'}
        widths={'status':90,'auto':90,'priority':75,'patient':150,'type':55,'gene':95,'change':150,'region':165,'reason':380,'decision':125,'image':220}
        for c in cols:
            self.tree.heading(c,text=labels[c],anchor='w');self.tree.column(c,width=widths[c],anchor='w')
        ys=ttk.Scrollbar(area,orient='vertical',command=self.tree.yview);xs=ttk.Scrollbar(area,orient='horizontal',command=self.tree.xview)
        self.tree.configure(yscrollcommand=ys.set,xscrollcommand=xs.set)
        self.tree.grid(row=0,column=0,sticky='nsew');ys.grid(row=0,column=1,sticky='ns');xs.grid(row=1,column=0,sticky='ew')
        area.rowconfigure(0,weight=1);area.columnconfigure(0,weight=1)
        self.tree.bind('<Double-1>',lambda e:self.open_igv())

        bot=ttk.Frame(self,padding=(10,3,10,10));bot.pack(fill='x')
        capture_bar=ttk.Frame(bot);capture_bar.pack(fill='x')
        ttk.Button(capture_bar,text='IGV Aç',command=self.open_igv).pack(side='left')
        ttk.Button(capture_bar,text='IGV Görüntüsü Al',command=self.capture_igv).pack(side='left',padx=5)
        ttk.Button(capture_bar,text='Seçilileri Yeniden Yakala',command=self.auto_capture_selected).pack(side='left',padx=5)
        ttk.Button(capture_bar,text='Tüm IGV’leri Otomatik Hazırla',command=self.auto_capture_all).pack(side='left',padx=5)
        ttk.Button(capture_bar,text='Patolog Galerisi',command=self.open_pathologist_gallery).pack(side='left',padx=5)
        ttk.Button(capture_bar,text='Seçililerin Galerisini Sil',command=self.delete_selected_gallery).pack(side='left',padx=5)
        ttk.Button(capture_bar,text='YZ’ye Gönderilecek Tek ZIP',command=self.export_igv_ai).pack(side='right')
        review_bar=ttk.Frame(bot);review_bar.pack(fill='x',pady=(5,0))
        ttk.Label(review_bar,text='Patolog kararı:').pack(side='left',padx=(0,5))
        for txt,val in [('Destekli','supported'),('Artefakt','artifact'),('Belirsiz','uncertain')]:
            ttk.Button(review_bar,text=txt,command=lambda v=val:self.set_decision(v)).pack(side='left',padx=3)
        ttk.Button(review_bar,text='Kararı İptal',command=self.clear_decision).pack(side='left',padx=(8,3))
        ttk.Button(review_bar,text='İnceleme Tamam',command=lambda:self.set_status('reviewed')).pack(side='left',padx=8)
        ttk.Button(review_bar,text='Tekrar Bekleyen',command=lambda:self.set_status('pending')).pack(side='left')
        ttk.Button(review_bar,text='Görüntüyü Aç',command=self.open_snapshot).pack(side='right')

    def import_file(self):
        path=filedialog.askopenfilename(title='YZ dönüş dosyasını seçin',filetypes=[('AI review JSON/ZIP',('*.json','*.zip')),('JSON','*.json'),('ZIP','*.zip'),('Tüm dosyalar','*.*')])
        if not path:return
        try:
            data,source,sha=load_ai_review_return(Path(path))
            errors,warnings=validate_ai_review_return(data)
            result=self.db.import_ai_review(data,source,sha)
            allwarn=(warnings or [])+(result.get('warnings') or [])
            self.refresh()
            msg=f"YZ dönüşü alındı.\nSession: {result['session_id']}\nHasta özeti: {result['patient_reviews']}\nVaryant inceleme: {result['variant_reviews']}\nArtefakt inceleme: {result.get('artifact_reviews',0)}\nIGV kuyruğu: {result['igv_requests']}"
            if allwarn: msg+='\n\nUyarılar:\n- '+'\n- '.join(allwarn[:12])
            messagebox.showinfo('YZ dönüşü içe alındı',msg)
        except Exception as e:
            messagebox.showerror('YZ dönüşü alınamadı',str(e))

    def _update_live_panel(self, rows):
        total=len(rows)
        counts={'pending':0,'opened':0,'captured':0,'reviewed':0}
        ai_ready=0;with_image=0
        latest=None
        for r in rows:
            d=dict(r);st=(d.get('status') or '').strip().lower();counts[st]=counts.get(st,0)+1
            if int(d.get('ai_ready') or 0):ai_ready+=1
            if d.get('screenshot_path'):with_image+=1
            if not latest or str(d.get('updated_at') or '')>str(latest.get('updated_at') or ''):latest=d
        done=counts.get('captured',0)+counts.get('reviewed',0)
        progress=(done*100.0/total) if total else 0.0
        self.live_progress_var.set(progress)
        self.live_counts_var.set(f"Toplam {total} · AI-READY {ai_ready} · Görüntü {with_image} · Bekleyen {counts.get('pending',0)} · Açık {counts.get('opened',0)} · Yakalandı {counts.get('captured',0)} · İncelendi {counts.get('reviewed',0)}")
        if self._live_active:
            if latest:
                name=(latest.get('patient_id') or latest.get('gene') or 'IGV')
                change=latest.get('change') or latest.get('region') or ''
                self.live_status_var.set(f"Canlı izleme açık · son güncellenen: {name} · {change} · durum={latest.get('status') or '-'}")
            else:
                self.live_status_var.set('Canlı izleme açık · henüz güncelleme yok.')
        else:
            self.live_status_var.set('Canlı işleme bekleniyor.' if total==0 else f'Canlı panel hazır · son durum özeti gösteriliyor ({done}/{total}).')

    def refresh(self):
        if not hasattr(self,'tree'):return
        for x in self.tree.get_children():self.tree.delete(x)
        rows=self.db.igv_queue_rows(self.filter_var.get())
        for r in rows:
            d=dict(r);img=Path(d.get('screenshot_path') or '').name if d.get('screenshot_path') else ''
            patient_label=((d.get('full_name') or '').strip() + (' · ' if (d.get('full_name') or '').strip() and (d.get('patient_id') or '').strip() else '') + (d.get('patient_id') or '').strip()) or (d.get('patient_id') or '')
            auto=('AI-READY' if int(d.get('ai_ready') or 0) else ('KONTROL' if d.get('screenshot_path') else '—'))
            self.tree.insert('', 'end', iid=str(d['id']), values=(d.get('status') or '',auto,d.get('priority') or '',patient_label,d.get('sample_type') or '',d.get('gene') or '',d.get('change') or d.get('variant_key') or '',d.get('region') or '',d.get('reason') or '',d.get('manual_decision') or '',img))
        sessions=self.db.ai_review_sessions(1)
        ready=sum(1 for r in rows if int(dict(r).get('ai_ready') or 0))
        if sessions:
            s=dict(sessions[0]);self.summary_var.set(f"Son YZ dönüşü #{s['id']} · {s.get('pool_group') or '-'} / {s.get('sample_scope') or '-'} · risk={s.get('pool_risk') or '-'} · {len(rows)} IGV · AI-READY {ready}")
        else:self.summary_var.set(f'{len(rows)} IGV satırı · AI-READY {ready} · henüz YZ dönüş session kaydı yok.')
        self._update_live_panel(rows)


    def _stop_live_watch(self):
        self._live_active=False
        if self._live_after_id:
            try:self.after_cancel(self._live_after_id)
            except Exception:pass
            self._live_after_id=None
        self.refresh()

    def _live_tick(self):
        self._live_after_id=None
        if not self._live_active:return
        self.refresh()
        self._live_after_id=self.after(1200,self._live_tick)

    def _start_live_watch(self):
        self._live_active=True
        if self._live_after_id is None:
            self._live_after_id=self.after(250,self._live_tick)
        self.refresh()

    def _selected(self):
        sel=self.tree.selection()
        if not sel:
            messagebox.showwarning('IGV','Önce bir IGV satırı seçin.');return None
        return self.db.get_igv_queue_item(int(sel[0]))

    def _submit(self,command,item):
        if not self.browser_submit:
            messagebox.showwarning('IGV','Tarayıcı worker bağlantısı yok.');return
        region=item.get('region') or _region_from_variant_key(item.get('variant_key'))
        if not region:
            messagebox.showwarning('IGV','Bu varyant için IGV koordinatı üretilemedi.');return
        ok=self.browser_submit(command,record_key=item['record_key'],region=region,variant_type='snv',queue_id=item['id'],gene=item.get('gene') or '',change=item.get('change') or '')
        if not ok: messagebox.showwarning('IGV','Tarayıcı meşgul veya hazır değil.')

    def open_igv(self):
        item=self._selected()
        if item:self._submit('igv_open',item)

    def capture_igv(self):
        item=self._selected()
        if item:self._submit('igv_capture',item)

    def auto_capture_selected(self):
        if not self.browser_submit:
            messagebox.showwarning('IGV','Tarayıcı worker bağlantısı yok.');return
        ids=[int(x) for x in self.tree.selection()]
        if not ids:
            messagebox.showwarning('IGV','Bir veya birden fazla IGV satırı seçin.');return
        ok=self.browser_submit('igv_batch_capture',queue_ids=ids,recapture=True)
        if ok:self._start_live_watch()
        else:messagebox.showwarning('IGV','Tarayıcı meşgul veya hazır değil.')

    def _latest_session_id(self):
        sessions=self.db.ai_review_sessions(1)
        return int(sessions[0]['id']) if sessions else None

    def auto_capture_all(self):
        if not self.browser_submit:
            messagebox.showwarning('IGV','Tarayıcı worker bağlantısı yok.');return
        sid=self._latest_session_id()
        if not sid:
            messagebox.showwarning('IGV','Önce AI_REVIEW_RETURN.json içe alınmalı.');return
        # Re-capture the entire current return so old 39 bp/partial images are replaced
        # by the stricter zoomed/stable v2 capture.
        ok=self.browser_submit('igv_batch_capture',session_id=sid,recapture=True)
        if ok:self._start_live_watch()
        else:messagebox.showwarning('IGV','Tarayıcı meşgul veya hazır değil.')

    def export_igv_ai(self):
        try:
            res=write_igv_ai_bundle(self.base,self.db,self.cfg,session_id=self._latest_session_id(),require_complete=True)
            messagebox.showinfo('YZ’ye Gönderilecek IGV ZIP',f"Tek dosya hazır:\n{res['zip']}\n\nIGV: {res['ai_ready']}/{res['requested']}\n\nBu ZIP doğrudan YZ/ChatGPT’ye yüklenmek üzere hazırlanmıştır.")
        except Exception as e:messagebox.showerror('IGV YZ Paketi',str(e))

    def open_pathologist_gallery(self):
        try:
            res=write_igv_pathologist_gallery(self.base,self.db,self.cfg,session_id=self._latest_session_id())
            try:os.startfile(res['html'])
            except Exception:pass
            messagebox.showinfo('Patolog IGV Galerisi',f"Hazır görüntü: {res['ready']}\nBekleyen: {res['missing']}\n\n{res['html']}")
        except Exception as e:messagebox.showerror('Patolog IGV Galerisi',str(e))

    def delete_selected_gallery(self):
        ids=[int(x) for x in self.tree.selection()]
        if not ids:
            messagebox.showwarning('IGV Galerisi','Silmek/yeniden yakalamak için bir veya daha fazla satır seçin.');return
        if not messagebox.askyesno('IGV Galerisi',f'{len(ids)} seçili IGV görüntüsü silinsin ve yeniden yakalama için BEKLEYEN durumuna alınsın mı?\n\nPatolog kararı korunur.'):
            return
        try:
            res=clear_igv_capture_artifacts(self.base,self.db,ids,self.cfg);self.refresh()
            msg=f"Yeniden bekleyen: {res['reset_rows']}\nSilinen dosya: {res['deleted_files']}\nGeçersiz kılınan eski galeri: {res['removed_galleries']}"
            if res['errors']:msg+='\n\nUyarılar:\n- '+'\n- '.join(res['errors'][:8])
            messagebox.showinfo('IGV Galerisi',msg+'\n\nŞimdi Seçilileri Yeniden Yakala ile tekrar oluşturabilirsiniz.')
        except Exception as e:messagebox.showerror('IGV Galerisi',str(e))

    def set_decision(self,val):
        item=self._selected()
        if not item:return
        self.db.update_igv_queue_item(item['id'],manual_decision=val,status='reviewed')
        self.refresh()

    def clear_decision(self):
        ids=[int(x) for x in self.tree.selection()]
        if not ids:
            messagebox.showwarning('Patolog kararı','Kararı iptal etmek için bir veya daha fazla satır seçin.');return
        rows=[self.db.get_igv_queue_item(i) for i in ids]
        rows=[r for r in rows if r and (r.get('manual_decision') or '').strip()]
        if not rows:
            messagebox.showinfo('Patolog kararı','Seçili satırlarda iptal edilecek patolog kararı yok.');return
        if not messagebox.askyesno('Patolog kararı',f'{len(rows)} seçili patolog kararı iptal edilsin mi?\n\nIGV görüntüsü ve YZ değerlendirmesi korunur.'):
            return
        clear_pathologist_decisions(self.db,[r['id'] for r in rows])
        self.refresh()

    def set_status(self,val):
        item=self._selected()
        if not item:return
        self.db.update_igv_queue_item(item['id'],status=val)
        self.refresh()

    def open_snapshot(self):
        item=self._selected()
        if not item:return
        p=Path(item.get('screenshot_path') or '')
        if not p.exists():
            messagebox.showwarning('IGV','Kaydedilmiş görüntü bulunamadı.');return
        try: os.startfile(str(p))
        except Exception as e: messagebox.showerror('IGV',str(e))

    def on_browser_result(self,info: dict):
        if info.get('command')=='igv_batch_capture':
            self._stop_live_watch()
            self.refresh()
            sid=info.get('session_id') or self._latest_session_id()
            total=int(info.get('session_total',0) or 0);ready=int(info.get('session_ready',0) or 0);missing=int(info.get('session_missing',0) or 0)
            if total and missing==0:
                try:
                    res=write_igv_ai_bundle(self.base,self.db,self.cfg,session_id=sid,require_complete=True)
                    self.summary_var.set(f'IGV otomatik hazırlama TAMAM · {ready}/{total} AI-READY · YZ tek ZIP hazır: {res["zip"]}')
                    messagebox.showinfo('IGV tamam · YZ ZIP hazır',f"Tüm IGV görüntüleri hazır: {ready}/{total}\n\nYZ’ye göndereceğiniz tek dosya:\n{res['zip']}")
                except Exception as e:
                    self.summary_var.set(f'IGV {ready}/{total} hazır · ZIP oluşturulamadı: {e}')
            elif total:
                self.summary_var.set(f'IGV otomatik hazırlama: {ready}/{total} AI-READY · {missing} görüntü yeniden denenmeli. YZ ZIP henüz oluşturulmadı.')
            return
        qid=info.get('queue_id')
        if not qid:return
        if info.get('command')=='igv_capture' and info.get('screenshot_path'):
            self.db.update_igv_queue_item(qid,status='captured',screenshot_path=info.get('screenshot_path'),
                capture_meta_path=info.get('capture_meta_path'),auto_settings_json=json.dumps(info.get('auto_settings') or {},ensure_ascii=False),
                ai_ready=1 if info.get('ai_ready') else 0,capture_error=(info.get('auto_settings') or {}).get('error') or '',captured_at=info.get('captured_at'))
        elif info.get('command')=='igv_open':
            self.db.update_igv_queue_item(qid,status='opened',auto_settings_json=json.dumps(info.get('auto_settings') or {},ensure_ascii=False),ai_ready=1 if info.get('ai_ready') else 0)
        self.refresh()

# === NGS_AI_JSON_V2_V0432 ===
# Backward-compatible V2 contract layered over the existing importer.
RETURN_SCHEMA_V2 = 'ngs_ai_review_return_v2'
IGV_REQUEST_SCHEMA_V2 = 'ngs_igv_request_v2'
AI_DOCUMENT_TYPE_V2 = 'AI_REVIEW_RETURN'
IGV_DOCUMENT_TYPE_V2 = 'IGV_REQUEST'
LEGACY_RETURN_SCHEMAS_V0432 = ('ngs_ai_review_return_v1', RETURN_SCHEMA_V2)
DECISION_VALUES_V0432 = (
    'reportable','likely_real_needs_confirmation','indeterminate','likely_artifact',
    'artifact','germline_candidate','unknown_oncogenicity'
)
REVIEW_MODE_VALUES_V0432 = ('standard_variant_review','caller_missed_variant_search','manual_hotspot_scan','bed_coverage_audit')

_ngs_v1_build_return_v0432 = build_ai_review_return_template
_ngs_v1_schema_v0432 = ai_review_return_json_schema


def _ngs_request_example_v0432():
    return {
        '_example_only_remove_or_replace': True,
        'request_id': 'MP183_TP53_R248_REGION_YYYYMMDD_HHMMSS_v1',
        'request_revision': 1,
        'patient_link_id': '', 'patient_id': '', 'record_key': '',
        'variant_key': 'MANUAL_SCAN|TP53|chr17:7674170-7674220|R248_REGION',
        'gene': 'TP53', 'change': 'R248 region',
        'review_mode': 'caller_missed_variant_search',
        'region': 'chr17:7674170-7674220',
        'coordinates': {
            'genome_build': 'hg38', 'chrom': 'chr17', 'position': 7674220,
            'ref': '', 'alt': '', 'region': 'chr17:7674170-7674220', 'sort_anchor': 7674220,
        },
        'execution': {
            'action': 'generate_new_snapshot', 'reuse_existing_snapshot': False,
            'force_new_task': True, 'reset_manual_decision': False,
        },
        'priority': 'high', 'reason': '',
        'return_metrics': ['dp','alt_ad','vaf','forward_alt','reverse_alt','duplicate_excluded_alt','median_alt_baseq','median_alt_mapq'],
        'coverage_audit': {
            'target_status': 'unknown', 'mean_depth': None, 'min_depth': None,
            'coverage_cliff': None, 'padding_only': None,
        },
        'pileup': {
            'dp': None, 'alt_ad': None, 'vaf': None, 'forward_alt': None, 'reverse_alt': None,
            'duplicate_excluded_alt': None, 'median_alt_baseq': None, 'median_alt_mapq': None,
        },
    }


def build_ai_review_return_template(pkg: dict) -> dict:
    out = _ngs_v1_build_return_v0432(pkg)
    out['schema'] = RETURN_SCHEMA_V2
    out['document_type'] = AI_DOCUMENT_TYPE_V2
    out['schema_version'] = 2
    out['instructions'] = list(out.get('instructions') or []) + [
        'Her yeni IGV isteğinde benzersiz request_id ve artan request_revision üret.',
        'variant_key hiçbir durumda boş olamaz. Caller-missed/manual taramada MANUAL_SCAN|GEN|REGION|LABEL biçiminde sentetik benzersiz anahtar üret.',
        'IGV isteğini variant_reviews içinde aynı record_key+variant_key ile çapraz referansla.',
        'Yeni görüntü gerekiyorsa execution.action=generate_new_snapshot, reuse_existing_snapshot=false ve force_new_task=true kullan.',
        'Koordinatı tek kaynakta coordinates altında belirt; region ve sort_anchor birbiriyle uyumlu olsun.',
        'Caller tarafından kaçırılmış varyant aramasında review_mode=caller_missed_variant_search kullan; VCF dışı düşük VAF mismatch/indel/MNV ve BED coverage durumunu ayrıca değerlendir.',
        'Unknown/onkojenisitesi bilinmeyen varyantları düşürme; decision=unknown_oncogenicity ile koru.',
        'pileup alanlarını yalnız ölçülebilen verilerle doldur; görüntüden read sayısı uydurma.',
    ]
    allowed = dict(out.get('allowed_values') or {})
    allowed['decision'] = list(DECISION_VALUES_V0432)
    allowed['review_mode'] = list(REVIEW_MODE_VALUES_V0432)
    out['allowed_values'] = allowed
    for vr in out.get('variant_reviews') or []:
        if isinstance(vr, dict):
            vr.setdefault('decision','indeterminate')
            vr.setdefault('review_mode','standard_variant_review')
            vr.setdefault('coverage_audit', {'target_status':'unknown','mean_depth':None,'min_depth':None,'coverage_cliff':None,'padding_only':None})
            vr.setdefault('pileup', {'dp':None,'alt_ad':None,'vaf':None,'forward_alt':None,'reverse_alt':None,'duplicate_excluded_alt':None,'median_alt_baseq':None,'median_alt_mapq':None})
    out['igv_requests'] = [_ngs_request_example_v0432()]
    out['validation_policy'] = {
        'schema_validation_required': True,
        'cross_reference_validation_required': True,
        'duplicate_key_validation_required': True,
    }
    return out


def ai_review_return_json_schema() -> dict:
    return {
        '$schema':'https://json-schema.org/draft/2020-12/schema',
        'title':'NGS AI Review Return V2', 'type':'object',
        'required':['schema','document_type','source_bundle','pool_review','patient_reviews','variant_reviews','igv_requests'],
        'properties':{
            'schema':{'enum':['ngs_ai_review_return_v1',RETURN_SCHEMA_V2]},
            'document_type':{'enum':[AI_DOCUMENT_TYPE_V2]},
            'source_bundle':{'type':'object'}, 'pool_review':{'type':'object'},
            'patient_reviews':{'type':'array'}, 'variant_reviews':{'type':'array'},
            'cnv_reviews':{'type':'array'}, 'igv_requests':{'type':'array'},
            'artifact_reviews':{'type':'array'}, 'final_checklist':{'type':'array'},
        }, 'additionalProperties':True,
    }


def build_igv_request_template(pkg: dict) -> dict:
    return {
        'schema': IGV_REQUEST_SCHEMA_V2, 'document_type': IGV_DOCUMENT_TYPE_V2, 'schema_version': 2,
        'source_bundle': {
            'pool_group': pkg.get('pool_group') or '', 'sample_scope': pkg.get('sample_scope') or '',
            'generated_at': pkg.get('generated_at') or '', 'ai_schema': pkg.get('schema') or '',
        },
        'instructions': [
            'Bu dosya yalnız yeni IGV görevleri içindir; AI_REVIEW_RETURN ile karıştırma.',
            'Her requests satırında request_id, request_revision, record_key ve variant_key zorunludur.',
            'Yeni tekrar görüntü isteğinde yeni request_id üret ve request_revision değerini artır.',
            'Caller-missed varyantta sentetik variant_key kullan; boş variant_key üretme.',
            'execution.force_new_task=true eski tamamlanmış görüntüyü yeniden bekleyen göreve çevirir.',
        ],
        'requests': [_ngs_request_example_v0432()],
    }


def igv_request_json_schema() -> dict:
    return {
        '$schema':'https://json-schema.org/draft/2020-12/schema','title':'NGS IGV Request V2','type':'object',
        'required':['schema','document_type','source_bundle','requests'],
        'properties':{
            'schema':{'const':IGV_REQUEST_SCHEMA_V2},'document_type':{'const':IGV_DOCUMENT_TYPE_V2},
            'source_bundle':{'type':'object'},'requests':{'type':'array'},
        },'additionalProperties':True,
    }


def _ngs_region_valid_v0432(region):
    if not str(region or '').strip(): return True
    return bool(re.fullmatch(r'(?:chr)?[A-Za-z0-9_.-]+:\d[\d,]*-\d[\d,]*', str(region).strip()))


def validate_ai_review_return(data: dict):
    errors, warnings = [], []
    if not isinstance(data, dict): return ['Kök JSON nesne olmalıdır.'], []
    schema = str(data.get('schema') or '')
    if schema not in LEGACY_RETURN_SCHEMAS_V0432:
        errors.append(f"schema 'ngs_ai_review_return_v1' veya '{RETURN_SCHEMA_V2}' olmalıdır.")
    if schema == RETURN_SCHEMA_V2 and data.get('document_type') != AI_DOCUMENT_TYPE_V2:
        errors.append(f'document_type {AI_DOCUMENT_TYPE_V2} olmalıdır.')
    for k in ('source_bundle','pool_review'):
        if not isinstance(data.get(k),dict): errors.append(f'{k} nesne olmalıdır.')
    for k in ('patient_reviews','variant_reviews','igv_requests'):
        if not isinstance(data.get(k),list): errors.append(f'{k} liste olmalıdır.')
    if 'artifact_reviews' in data and not isinstance(data.get('artifact_reviews'),list): errors.append('artifact_reviews liste olmalıdır.')
    if errors: return errors,warnings

    sample_keys={str(x.get('record_key') or '').strip() for x in (data.get('sample_index') or []) if isinstance(x,dict) and x.get('record_key')}
    variant_pairs=set(); duplicate_pairs=set()
    for i,vr in enumerate(data.get('variant_reviews') or []):
        if not isinstance(vr,dict): errors.append(f'variant_reviews[{i}] nesne değil.'); continue
        rk=str(vr.get('record_key') or '').strip();vk=str(vr.get('variant_key') or '').strip()
        if not rk or not vk: errors.append(f'variant_reviews[{i}] record_key ve variant_key zorunlu.'); continue
        pair=(rk,vk)
        if pair in variant_pairs: duplicate_pairs.add(pair)
        variant_pairs.add(pair)
        if sample_keys and rk not in sample_keys: warnings.append(f'variant_reviews[{i}] record_key sample_index içinde yok: {rk}')
        decision=str(vr.get('decision') or '')
        if decision and decision not in DECISION_VALUES_V0432: errors.append(f'variant_reviews[{i}] decision geçersiz: {decision}')
        mode=str(vr.get('review_mode') or 'standard_variant_review')
        if mode not in REVIEW_MODE_VALUES_V0432: errors.append(f'variant_reviews[{i}] review_mode geçersiz: {mode}')
    if duplicate_pairs: errors.append('variant_reviews içinde yinelenen record_key+variant_key var: '+', '.join(f'{a}|{b}' for a,b in sorted(duplicate_pairs)[:6]))

    request_ids=set();request_pairs=set()
    for i,req in enumerate(data.get('igv_requests') or []):
        if not isinstance(req,dict): errors.append(f'igv_requests[{i}] nesne değil.'); continue
        rk=str(req.get('record_key') or '').strip();vk=str(req.get('variant_key') or '').strip()
        if not rk or not vk: errors.append(f'igv_requests[{i}] record_key ve variant_key zorunlu.')
        if schema == RETURN_SCHEMA_V2:
            rid=str(req.get('request_id') or '').strip()
            if not rid: errors.append(f'igv_requests[{i}] request_id zorunlu.')
            elif rid in request_ids: errors.append(f'igv_requests[{i}] request_id yineleniyor: {rid}')
            else: request_ids.add(rid)
            try:
                if int(req.get('request_revision') or 0) < 1: raise ValueError()
            except Exception: errors.append(f'igv_requests[{i}] request_revision pozitif tam sayı olmalıdır.')
            if (rk,vk) not in variant_pairs: errors.append(f'igv_requests[{i}] record_key+variant_key variant_reviews içinde bulunamadı.')
        pair=(rk,vk)
        if pair in request_pairs: errors.append(f'igv_requests[{i}] yinelenen record_key+variant_key: {rk}|{vk}')
        request_pairs.add(pair)
        region=req.get('region') or (req.get('coordinates') or {}).get('region') or ''
        if region and not _ngs_region_valid_v0432(region): errors.append(f'igv_requests[{i}] region geçersiz: {region}')
        execution=req.get('execution') or {}
        if execution and execution.get('action') not in (None,'generate_new_snapshot','reuse_snapshot'):
            errors.append(f'igv_requests[{i}] execution.action geçersiz.')
    for i,ar in enumerate(data.get('artifact_reviews') or []):
        if not isinstance(ar,dict): errors.append(f'artifact_reviews[{i}] nesne değil.'); continue
        if not str(ar.get('variant_key') or '').strip(): errors.append(f'artifact_reviews[{i}] variant_key zorunlu.')
    return errors,warnings


def validate_igv_request_v2(data: dict):
    errors=[];warnings=[]
    if not isinstance(data,dict): return ['Kök JSON nesne olmalıdır.'],[]
    if data.get('schema') != IGV_REQUEST_SCHEMA_V2: errors.append(f'schema {IGV_REQUEST_SCHEMA_V2} olmalıdır.')
    if data.get('document_type') != IGV_DOCUMENT_TYPE_V2: errors.append(f'document_type {IGV_DOCUMENT_TYPE_V2} olmalıdır.')
    reqs=data.get('requests')
    if not isinstance(reqs,list): errors.append('requests liste olmalıdır.'); return errors,warnings
    ids=set();pairs=set()
    for i,req in enumerate(reqs):
        if not isinstance(req,dict): errors.append(f'requests[{i}] nesne değil.');continue
        rk=str(req.get('record_key') or '').strip();vk=str(req.get('variant_key') or '').strip();rid=str(req.get('request_id') or '').strip()
        if not rk or not vk: errors.append(f'requests[{i}] record_key ve variant_key zorunlu.')
        if not rid: errors.append(f'requests[{i}] request_id zorunlu.')
        elif rid in ids: errors.append(f'requests[{i}] request_id yineleniyor: {rid}')
        ids.add(rid)
        try:
            if int(req.get('request_revision') or 0)<1: raise ValueError()
        except Exception: errors.append(f'requests[{i}] request_revision pozitif tam sayı olmalıdır.')
        pair=(rk,vk)
        if pair in pairs: errors.append(f'requests[{i}] yinelenen record_key+variant_key: {rk}|{vk}')
        pairs.add(pair)
        region=req.get('region') or (req.get('coordinates') or {}).get('region') or ''
        if region and not _ngs_region_valid_v0432(region): errors.append(f'requests[{i}] region geçersiz: {region}')
    return errors,warnings


def _ngs_igv_to_ai_return_v0432(doc):
    reqs=[x for x in (doc.get('requests') or []) if isinstance(x,dict) and not x.get('_example_only_remove_or_replace')]
    variant_reviews=[]
    for req in reqs:
        variant_reviews.append({
            'patient_link_id':req.get('patient_link_id') or '', 'patient_id':req.get('patient_id') or '',
            'record_key':req.get('record_key') or '', 'variant_key':req.get('variant_key') or '',
            'gene':req.get('gene') or '', 'change':req.get('change') or '',
            'review_status':'igv_review','decision':'indeterminate','confidence':'indeterminate',
            'report_recommendation':'','rationale':req.get('reason') or '', 'igv_required':True,
            'igv_region':req.get('region') or (req.get('coordinates') or {}).get('region') or '',
            'igv_reason':req.get('reason') or '', 'igv_priority':req.get('priority') or 'normal',
            'review_mode':req.get('review_mode') or 'standard_variant_review',
        })
    return {
        'schema':RETURN_SCHEMA_V2,'document_type':AI_DOCUMENT_TYPE_V2,'schema_version':2,
        'source_bundle':dict(doc.get('source_bundle') or {}),
        'sample_index':list(doc.get('sample_index') or []),
        'pool_review':{'risk_level':'indeterminate','summary':'IGV request import','contamination_review':'','technical_actions':[]},
        'patient_reviews':[], 'variant_reviews':variant_reviews, 'cnv_reviews':[],
        'igv_requests':reqs, 'artifact_reviews':[], 'final_checklist':[],
    }


def load_ai_review_return(path: Path):
    path=Path(path);member=''
    if path.suffix.lower()=='.zip':
        with zipfile.ZipFile(path,'r') as z:
            names=z.namelist();preferred=('AI_REVIEW_RETURN.JSON','IGV_REQUEST.JSON')
            candidates=[]
            for want in preferred:
                candidates.extend([n for n in names if Path(n).name.upper()==want])
            if not candidates: raise ValueError('ZIP içinde AI_REVIEW_RETURN.json veya IGV_REQUEST.json bulunamadı.')
            member=candidates[0];raw=z.read(member).decode('utf-8-sig',errors='strict')
        doc=json.loads(raw);source=f'{path}!{member}'
    else:
        doc=json.loads(path.read_text(encoding='utf-8-sig'));source=str(path)
    doc=_strip_examples(doc)
    if doc.get('document_type')==IGV_DOCUMENT_TYPE_V2 or doc.get('schema')==IGV_REQUEST_SCHEMA_V2:
        errors,warnings=validate_igv_request_v2(doc)
        if errors: raise ValueError('IGV istek dosyası doğrulanamadı:\n- '+'\n- '.join(errors))
        data=_ngs_igv_to_ai_return_v0432(doc)
    else:
        data=doc
    errors,warnings=validate_ai_review_return(data)
    if errors: raise ValueError('YZ dönüş dosyası doğrulanamadı:\n- '+'\n- '.join(errors))
    return data,source,_sha256(path)


def validate_generated_templates(pkg: dict):
    review=_strip_examples(build_ai_review_return_template(pkg))
    r_errors,r_warnings=validate_ai_review_return(review)
    igv=build_igv_request_template(pkg);igv['requests']=[]
    i_errors,i_warnings=validate_igv_request_v2(igv)
    return {
        'schema_validation':'PASS' if not r_errors and not i_errors else 'FAIL',
        'cross_reference_validation':'PASS' if not r_errors else 'FAIL',
        'duplicate_key_validation':'PASS' if not any('yinelen' in x for x in (r_errors+i_errors)) else 'FAIL',
        'errors':r_errors+i_errors,'warnings':r_warnings+i_warnings,
    }

RETURN_SCHEMA = RETURN_SCHEMA_V2

# === NGS_AI_READ_EVIDENCE_V0433 ===
# Contract metadata, request lineage, source identity and BAM/BED evidence.
JSON_GENERATOR_VERSION_V0433='0.4.33'
_ngs_build_return_v0433_prev=build_ai_review_return_template
_ngs_build_igv_request_v0433_prev=build_igv_request_template
_ngs_return_schema_v0433_prev=ai_review_return_json_schema
_ngs_igv_schema_v0433_prev=igv_request_json_schema
_ngs_validate_return_v0433_prev=validate_ai_review_return
_ngs_validate_igv_v0433_prev=validate_igv_request_v2
_ngs_igv_convert_v0433_prev=_ngs_igv_to_ai_return_v0432
_ngs_write_igv_bundle_v0433_prev=write_igv_ai_bundle


def _ngs_contract_meta_v0433(pkg):
    from pileup_engine import canonical_build
    created=str((pkg or {}).get('generated_at') or time.strftime('%Y-%m-%dT%H:%M:%S'))
    build=canonical_build((pkg or {}).get('genome_build') or 'hg38')
    return {'schema_version':2,'generator_version':JSON_GENERATOR_VERSION_V0433,'genome_build':build,'created_at':created}


def _ngs_expand_pileup_v0433(value=None):
    out={
        'status':'not_computed','variant_type':None,'dp':None,'ad_ref':None,'alt_ad':None,'vaf':None,
        'forward_alt':None,'reverse_alt':None,'alt_read1':None,'alt_read2':None,
        'duplicate_excluded_dp':None,'duplicate_excluded_alt_ad':None,'duplicate_excluded_vaf':None,
        'median_alt_baseq':None,'median_alt_mapq':None,
        'read_position':{'median_alt_fraction':None,'near_read_end_alt':None,'alt_count_with_position':None},
        'softclip_support':{'left':None,'right':None},
    }
    if isinstance(value,dict):out.update(value)
    return out


def _ngs_expand_coverage_v0433(value=None):
    out={'target_status':'unknown','coding_region_covered':None,'mean_depth':None,'median_depth':None,'min_depth':None,'max_depth':None,'ge_100x_pct':None,'padding_only':None,'coverage_cliff':None}
    if isinstance(value,dict):out.update(value)
    return out


def build_ai_review_return_template(pkg: dict) -> dict:
    out=_ngs_build_return_v0433_prev(pkg);out.update(_ngs_contract_meta_v0433(pkg))
    out['instructions']=list(out.get('instructions') or [])+[
        'Root schema_version/generator_version/genome_build/created_at alanlarını koru; genome build koordinatlarla uyumlu olmalıdır.',
        'request_revision>1 ise parent_request_id ile önceki isteği bağla.',
        'BAM/CRAM ve BED kaynak kimliği varsa source_files altında aynen geri taşı; rerun kaynağını tahmin etme.',
        'pileup gerçek BAM/CRAM ölçümüdür; görüntüden DP/AD/VAF tahmin etme. status!=ok ise null değerleri koru.',
        'SNV, INDEL ve MNV aynı sayım mantığı değildir; variant_type ve softclip_support alanlarını koru.',
    ]
    for vr in out.get('variant_reviews') or []:
        if isinstance(vr,dict):
            vr['pileup']=_ngs_expand_pileup_v0433(vr.get('pileup'));vr['coverage_audit']=_ngs_expand_coverage_v0433(vr.get('coverage_audit'))
    for req in out.get('igv_requests') or []:
        if isinstance(req,dict):
            req.setdefault('parent_request_id','');req.setdefault('source_files',{'alignment':{},'bed':{}})
            req['pileup']=_ngs_expand_pileup_v0433(req.get('pileup'));req['coverage_audit']=_ngs_expand_coverage_v0433(req.get('coverage_audit'))
    return out


def build_igv_request_template(pkg: dict) -> dict:
    out=_ngs_build_igv_request_v0433_prev(pkg);out.update(_ngs_contract_meta_v0433(pkg))
    out['instructions']=list(out.get('instructions') or [])+[
        'request_revision 2 veya daha büyükse parent_request_id önceki request_id olmalıdır.',
        'source_files.alignment ve source_files.bed kimliğini mevcutsa koru; başka BAM/BED uydurma.',
        'Pileup ve coverage_audit gerçek dosya ölçümü yoksa null/not_computed kalır.',
    ]
    for req in out.get('requests') or []:
        if isinstance(req,dict):
            req.setdefault('parent_request_id','');req.setdefault('source_files',{'alignment':{},'bed':{}})
            req['pileup']=_ngs_expand_pileup_v0433(req.get('pileup'));req['coverage_audit']=_ngs_expand_coverage_v0433(req.get('coverage_audit'))
    return out


def validate_ai_review_return(data: dict):
    errors,warnings=_ngs_validate_return_v0433_prev(data)
    if not isinstance(data,dict):return errors,warnings
    if data.get('schema')==RETURN_SCHEMA_V2:
        for k in ('schema_version','generator_version','genome_build','created_at'):
            if not data.get(k):warnings.append(f'V2 root metadata eksik: {k}')
        root_build=str(data.get('genome_build') or '')
        for i,req in enumerate(data.get('igv_requests') or []):
            if not isinstance(req,dict):continue
            try:rev=int(req.get('request_revision') or 0)
            except Exception:rev=0
            if rev>1 and not str(req.get('parent_request_id') or '').strip():warnings.append(f'igv_requests[{i}] request_revision={rev} fakat parent_request_id boş.')
            cb=str((req.get('coordinates') or {}).get('genome_build') or '')
            if root_build and cb and root_build.lower()!=cb.lower():errors.append(f'igv_requests[{i}] genome_build root ile uyumsuz: {cb} != {root_build}')
    return errors,warnings


def validate_igv_request_v2(data: dict):
    errors,warnings=_ngs_validate_igv_v0433_prev(data)
    if not isinstance(data,dict):return errors,warnings
    for k in ('schema_version','generator_version','genome_build','created_at'):
        if not data.get(k):warnings.append(f'IGV V2 root metadata eksik: {k}')
    root_build=str(data.get('genome_build') or '')
    for i,req in enumerate(data.get('requests') or []):
        if not isinstance(req,dict):continue
        try:rev=int(req.get('request_revision') or 0)
        except Exception:rev=0
        if rev>1 and not str(req.get('parent_request_id') or '').strip():warnings.append(f'requests[{i}] request_revision={rev} fakat parent_request_id boş.')
        cb=str((req.get('coordinates') or {}).get('genome_build') or '')
        if root_build and cb and root_build.lower()!=cb.lower():errors.append(f'requests[{i}] genome_build root ile uyumsuz: {cb} != {root_build}')
    return errors,warnings


def _ngs_igv_to_ai_return_v0432(doc):
    out=_ngs_igv_convert_v0433_prev(doc)
    for k in ('schema_version','generator_version','genome_build','created_at'):
        if doc.get(k) not in (None,''):out[k]=doc.get(k)
    return out


def _ngs_rebuild_zip_v0433(folder: Path,zpath: Path):
    if zpath.exists():zpath.unlink()
    with zipfile.ZipFile(zpath,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as zf:
        for f in sorted(folder.rglob('*')):
            if f.is_file():zf.write(f,arcname=f'{folder.name}/{f.relative_to(folder).as_posix()}')


def write_igv_ai_bundle(base: Path, db, cfg: dict, session_id=None, require_complete=True):
    result=_ngs_write_igv_bundle_v0433_prev(base,db,cfg,session_id=session_id,require_complete=require_complete)
    from pileup_engine import compute_pileup_and_coverage, canonical_build, GENERATOR_VERSION as PILEUP_GENERATOR_VERSION
    out=Path(result['folder']);ev_path=out/'IGV_EVIDENCE.json'
    try:evidence=json.loads(ev_path.read_text(encoding='utf-8'))
    except Exception:return result
    session=_igv_session_row(db,result.get('session_id')) or {}
    try:source_doc=json.loads(session.get('raw_json') or '{}')
    except Exception:source_doc={}
    req_map={}
    for req in source_doc.get('igv_requests') or []:
        if isinstance(req,dict):req_map[(str(req.get('record_key') or ''),str(req.get('variant_key') or ''))]=req
    evidence.update({'schema_version':2,'generator_version':JSON_GENERATOR_VERSION_V0433,'pileup_generator_version':PILEUP_GENERATOR_VERSION,
                     'genome_build':canonical_build(source_doc.get('genome_build') or cfg.get('genome_build') or 'hg38'),'created_at':time.strftime('%Y-%m-%dT%H:%M:%S')})
    for item in evidence.get('items') or []:
        rk=str(item.get('record_key') or '');vk=str(item.get('variant_key') or '');req=req_map.get((rk,vk),{})
        row=db.get_igv_queue_item(int(item.get('queue_id') or 0)) or {}
        coords=dict(req.get('coordinates') or {})
        if not coords:
            sc=item.get('source_call') or {};coords={'genome_build':evidence['genome_build'],'chrom':sc.get('chrom') or '','position':sc.get('start') or 0,'ref':sc.get('ref') or '','alt':sc.get('alt') or '','region':item.get('region') or '','sort_anchor':sc.get('start') or 0}
        read_ev=compute_pileup_and_coverage(Path(base),db,cfg,rk,coordinates=coords,region=item.get('region') or '',variant_key=vk,source_call=item.get('source_call') or {})
        item['request_lineage']={'request_id':row.get('request_id') or req.get('request_id') or '','request_revision':int(row.get('request_revision') or req.get('request_revision') or 0),'parent_request_id':row.get('parent_request_id') or req.get('parent_request_id') or ''}
        item['source_files']=read_ev.get('sources') or {};item['pileup']=read_ev.get('pileup') or {};item['coverage_audit']=read_ev.get('coverage_audit') or {};item['coordinates']=read_ev.get('coordinates') or coords
        try:
            db.ensure_igv_audit_schema();db.cx.execute('UPDATE igv_queue SET source_identity_json=?,pileup_json=?,coverage_audit_json=? WHERE id=?',
                (json.dumps(item['source_files'],ensure_ascii=False),json.dumps(item['pileup'],ensure_ascii=False),json.dumps(item['coverage_audit'],ensure_ascii=False),int(item.get('queue_id') or 0)));db.cx.commit()
        except Exception:pass
    evidence['interpretation_boundary']='IGV pixels are visual confirmation only. pileup/coverage_audit values are computed from resolved BAM/CRAM/BED sources when status=ok; never infer missing read counts from screenshots.'
    ev_path.write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf-8')
    prompt=out/'PROMPT.md'
    if prompt.exists():
        with prompt.open('a',encoding='utf-8') as f:
            f.write('\n## BAM/CRAM pileup + BED audit\n- IGV_EVIDENCE.json içindeki pileup/coverage_audit gerçek dosya ölçümüdür; status=ok değilse sayı uydurma.\n- duplicate_excluded_* metriklerini, strand ve read1/read2 dağılımını birlikte değerlendir.\n- INDEL CIGAR event, MNV aynı read haplotipi ile sayılır; SNV base sayımıyla karıştırma.\n- source_files kimliği rerun/BAM karışmasını denetlemek içindir.\n')
    manifest_path=out/'MANIFEST.json';manifest={'schema':'ngs_igv_ai_bundle_v2','generated_at':evidence.get('generated_at') or evidence.get('created_at'),'source_ai_session_id':result.get('session_id'),'files':[]}
    for f in sorted(out.rglob('*')):
        if f.is_file() and f.name!='MANIFEST.json':manifest['files'].append({'path':f.relative_to(out).as_posix(),'size':f.stat().st_size,'sha256':_sha256(f)})
    manifest['pileup_engine']={'version':PILEUP_GENERATOR_VERSION,'genome_build':evidence['genome_build']}
    manifest_path.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    zpath=Path(result['zip']);_ngs_rebuild_zip_v0433(out,zpath)
    return result

# v0.4.33 UI: configure read-evidence sources without editing JSON by hand.
_NGSAIReviewIGVV0433 = AIReviewIGV
class AIReviewIGV(_NGSAIReviewIGVV0433):
    def _build(self):
        super()._build()
        self.pileup_root_var=tk.StringVar(value='; '.join(str(x) for x in (self.cfg.get('pileup_alignment_roots') or [])))
        self.pileup_bed_var=tk.StringVar(value=str(self.cfg.get('pileup_bed_path') or ''))
        try:
            import pysam as _pysam
            dep=f'pysam {_pysam.__version__}'
        except Exception:dep='pysam YÜKLÜ DEĞİL · KURULUM.bat / pip install pysam'
        self.pileup_status_var=tk.StringVar(value=f'BAM/CRAM pileup: {dep}')
        panel=ttk.LabelFrame(self,text='BAM/CRAM pileup + BED coverage audit',padding=7)
        try:panel.pack(fill='x',padx=10,pady=(0,6),before=self.tree.master)
        except Exception:panel.pack(fill='x',padx=10,pady=(0,6))
        panel.columnconfigure(1,weight=1)
        ttk.Label(panel,text='BAM/CRAM kökü:').grid(row=0,column=0,sticky='w')
        ttk.Entry(panel,textvariable=self.pileup_root_var,state='readonly').grid(row=0,column=1,sticky='ew',padx=5)
        ttk.Button(panel,text='Klasör Seç',command=self.choose_pileup_root).grid(row=0,column=2,padx=3)
        ttk.Label(panel,text='BED:').grid(row=1,column=0,sticky='w',pady=(4,0))
        ttk.Entry(panel,textvariable=self.pileup_bed_var,state='readonly').grid(row=1,column=1,sticky='ew',padx=5,pady=(4,0))
        ttk.Button(panel,text='BED Seç',command=self.choose_pileup_bed).grid(row=1,column=2,padx=3,pady=(4,0))
        ttk.Button(panel,text='Kaynakları Temizle',command=self.clear_pileup_sources).grid(row=0,column=3,rowspan=2,padx=(8,0))
        ttk.Label(panel,textvariable=self.pileup_status_var,foreground='#475569').grid(row=2,column=0,columnspan=4,sticky='w',pady=(5,0))

    def _save_pileup_cfg(self):
        path=self.base/'config.local.json'
        try:
            local=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
            if not isinstance(local,dict):local={}
        except Exception:local={}
        local['pileup_alignment_roots']=list(self.cfg.get('pileup_alignment_roots') or [])
        local['pileup_bed_path']=str(self.cfg.get('pileup_bed_path') or '')
        local.setdefault('genome_build',str(self.cfg.get('genome_build') or 'hg38'))
        path.write_text(json.dumps(local,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')

    def choose_pileup_root(self):
        folder=filedialog.askdirectory(title='BAM/CRAM kök klasörünü seçin')
        if not folder:return
        roots=list(self.cfg.get('pileup_alignment_roots') or [])
        if folder not in roots:roots.append(folder)
        self.cfg['pileup_alignment_roots']=roots;self.pileup_root_var.set('; '.join(roots));self._save_pileup_cfg();self.pileup_status_var.set('BAM/CRAM kökü kaydedildi. YZ IGV ZIP oluştururken gerçek pileup hesaplanacak.')

    def choose_pileup_bed(self):
        path=filedialog.askopenfilename(title='Panel BED dosyasını seçin',filetypes=[('BED','*.bed'),('Tüm dosyalar','*.*')])
        if not path:return
        self.cfg['pileup_bed_path']=path;self.pileup_bed_var.set(path);self._save_pileup_cfg();self.pileup_status_var.set('BED kaydedildi. Coverage audit bu BED ile yapılacak.')

    def clear_pileup_sources(self):
        self.cfg['pileup_alignment_roots']=[];self.cfg['pileup_bed_path']='';self.pileup_root_var.set('');self.pileup_bed_var.set('');self._save_pileup_cfg();self.pileup_status_var.set('Pileup kaynakları temizlendi.')

# === NGS_WEB_IGV_AI_V0434 ===
# Export structured web-IGV evidence without requiring screenshots or local BAM/BED.
_ngs_ai_review_v0434_prev_class = AIReviewIGV


def _ngs_read_web_meta_v0434(row):
    p=Path(str(row.get('capture_meta_path') or ''))
    if not p.exists():return {}
    try:
        doc=json.loads(p.read_text(encoding='utf-8'))
        return doc if isinstance(doc,dict) else {}
    except Exception:return {}


def _ngs_write_web_view_v0434(out: Path, items: list, missing: list, title: str):
    def esc(x):
        import html as _html
        return _html.escape(str(x or ''))
    cards=[]
    for x in items:
        w=x.get('web_igv') or {};caps=w.get('track_capabilities') or {};src=x.get('source_call') or {};probes=w.get('probes') or {}
        probe_bits=[]
        for name,label in [('variant','VCF'),('target_regions','Target/Padding'),('read_depth','Read depth')]:
            p=probes.get(name) or {};txt=p.get('raw_text') or ''
            probe_bits.append(f'<details><summary>{label} web probe</summary><pre>{esc(txt or "Web popup verisi açığa çıkmadı")}</pre></details>')
        image=f'<p><a href="{esc(x.get("image"))}"><img src="{esc(x.get("image"))}" style="max-width:100%"></a></p>' if x.get('image') else ''
        cards.append(f'''<article><h2>{esc(x.get('patient_id'))} · {esc(x.get('gene'))} {esc(x.get('change'))}</h2>
        <p><b>{esc(x.get('region'))}</b> · genome={esc(w.get('genome_build'))} · locus={esc(w.get('visible_locus'))}</p>
        <p>Kaynak çağrı: AF/VAF={esc(src.get('af'))} · AD={esc(src.get('ad'))} · DP={esc(src.get('dp'))} · filter={esc(src.get('filter'))}</p>
        <p>Web track: alignment={caps.get('alignment_track')} · VCF={caps.get('variant_track')} · target/padding={caps.get('target_padding_track')} · read-depth={caps.get('read_depth_track')}</p>
        <p><b>Exact web pileup:</b> {esc(w.get('exact_pileup_status'))}. Sayı görüntüden tahmin edilmez.</p>{''.join(probe_bits)}{image}</article>''')
    miss=''.join(f'<li>{esc(m.get("patient_id"))} · {esc(m.get("gene"))} {esc(m.get("change"))} — {esc(m.get("capture_error") or "Web IGV veri hazır değil")}</li>' for m in missing)
    html=f'''<!doctype html><html lang="tr"><meta charset="utf-8"><title>{esc(title)}</title><style>
    body{{font:15px/1.45 system-ui;margin:24px;background:#eef3f7;color:#17324d}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(520px,1fr));gap:16px}}article{{background:white;border:1px solid #ccd5df;border-radius:10px;padding:14px}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;background:#f5f7f9;padding:10px}}.warn{{background:#fff3cd;padding:12px;border-radius:8px}}details{{margin-top:8px}}</style>
    <h1>{esc(title)}</h1><p>Web IGV yapılandırılmış veri görünümü. Yerel BAM/BAI/BED kullanılmaz. Source-call AD/DP/AF ile web track/probe verisi birlikte gösterilir.</p>
    {('<section class="warn"><b>Hazır olmayanlar:</b><ul>'+miss+'</ul></section>') if missing else ''}<main class="grid">{''.join(cards)}</main></html>'''
    path=out/'WEB_IGV_VIEW.html';path.write_text(html,encoding='utf-8');return path


def write_igv_ai_bundle(base: Path, db, cfg: dict, session_id=None, require_complete=True):
    base=Path(base);session=_igv_session_row(db,session_id)
    if not session:raise ValueError('Web IGV paketi için YZ dönüş session kaydı bulunamadı.')
    sid=int(session['id']);rows=_igv_rows_for_session(db,sid)
    if not rows:raise ValueError('Bu YZ dönüşünde IGV isteği yok.')
    ready=[];missing=[]
    for r in rows:
        meta=_ngs_read_web_meta_v0434(r)
        if int(r.get('ai_ready') or 0) and meta.get('evidence_mode')=='web_igv_only' and (meta.get('web_igv') or {}).get('data_ready'):
            ready.append((r,meta))
        elif int(r.get('ai_ready') or 0) and r.get('screenshot_path') and Path(r.get('screenshot_path')).exists():
            # Backward compatibility for evidence captured before v0.4.34. New captures are web-data first.
            ready.append((r,{'evidence_mode':'legacy_screenshot','web_igv':{'data_ready':False,'exact_pileup':False,'exact_pileup_status':'legacy_visual_only','tracks':[],'probes':{},'track_capabilities':{}},'screenshot_path':r.get('screenshot_path') or '','warnings':['v0.4.34 öncesi legacy screenshot; yeniden Web IGV Verisini Al önerilir']}))
        else:missing.append(r)
    if require_complete and missing:
        labels=', '.join(f"{r.get('patient_id') or '?'} {r.get('gene') or r.get('change') or ''}" for r in missing[:10])
        raise ValueError(f'Web IGV veri paketi henüz tam değil: {len(ready)}/{len(rows)} WEB-DATA. Eksikler: {labels}. Önce Tüm Web IGV Verisini Topla ile yeniden deneyin.')
    if not ready:raise ValueError('Hazır Web IGV yapılandırılmış verisi yok.')
    stamp=time.strftime('%Y%m%d_%H%M%S');pool=_safe_name(session.get('pool_group') or 'POOL');scope=_safe_name(session.get('sample_scope') or 'ALL');stem=f'WEB_IGV_AI_{pool}_{scope}_{stamp}'
    root=base/str(cfg.get('ai_export_dir') or 'AI_EXPORT')/'WEB_IGV_READY'/f'{pool}_{scope}';root.mkdir(parents=True,exist_ok=True)
    out=root/stem;out.mkdir(parents=True,exist_ok=False);images=None;items=[]
    for i,(r,meta) in enumerate(ready,1):
        image='';src_img=Path(str(meta.get('screenshot_path') or ''))
        if src_img.exists():
            if images is None:images=out/'IMAGES';images.mkdir(exist_ok=True)
            name=f'{i:03d}_{_safe_name(r.get("patient_id"))}_{_safe_name(r.get("gene") or "VAR")}.png';shutil.copy2(src_img,images/name);image=f'IMAGES/{name}'
        try:settings=json.loads(r.get('auto_settings_json') or '{}')
        except Exception:settings=meta.get('auto_settings') or {}
        ai_ctx=db.igv_queue_ai_context(r['id']) or {};source_call=_source_call_context(db,r);web=meta.get('web_igv') or {}
        items.append({'queue_id':r['id'],'patient_link_id':r.get('patient_link_id') or '','patient_id':r.get('patient_id') or '','record_key':r.get('record_key') or '',
            'sample_type':r.get('sample_type') or '','variant_key':r.get('variant_key') or '','gene':r.get('gene') or '','change':r.get('change') or '',
            'region':r.get('region') or _region_from_variant_key(r.get('variant_key')),'priority':r.get('priority') or '','original_ai_reason':r.get('reason') or '',
            'evidence_mode':meta.get('evidence_mode') or 'web_igv_only','web_igv':web,'source_call':source_call,'auto_settings':settings,'warnings':meta.get('warnings') or [],
            'image':image,'captured_at':r.get('captured_at') or meta.get('captured_at') or '',
            'previous_ai_review':{k:ai_ctx.get(k) for k in ('review_status','confidence','report_recommendation','rationale','igv_reason') if ai_ctx.get(k)}})
    missing_public=[{'queue_id':r['id'],'patient_id':r.get('patient_id') or '','gene':r.get('gene') or '','change':r.get('change') or '','region':r.get('region') or '',
        'capture_error':r.get('capture_error') or '','status':r.get('status') or ''} for r in missing]
    evidence={'schema':'ngs_web_igv_ai_evidence_v1','schema_version':1,'generator_version':'0.4.34','generated_at':time.strftime('%Y-%m-%dT%H:%M:%S'),
        'evidence_mode':'web_igv_only','source_ai_session_id':sid,'source_pool_group':session.get('pool_group') or '','source_sample_scope':session.get('sample_scope') or '',
        'requested_count':len(rows),'item_count':len(items),'web_data_ready_count':len(items),'missing_count':len(missing_public),'missing_items':missing_public,'items':items,
        'local_file_policy':{'bam':False,'bai':False,'cram':False,'bed':False,'p2p_download':False},
        'interpretation_boundary':'source_call AD/DP/AF comes from the collected GenNext variant export. web_igv is extracted from the already-rendered GenNext IGV DOM/popups. Exact pileup is NOT inferred from pixels and remains unavailable unless the web UI explicitly exposes it.'}
    evidence_text=json.dumps(evidence,ensure_ascii=False,indent=2)
    (out/'IGV_WEB_EVIDENCE.json').write_text(evidence_text,encoding='utf-8')
    (out/'IGV_EVIDENCE.json').write_text(evidence_text,encoding='utf-8')  # backward-compatible alias
    try:source_ai=json.loads(session.get('raw_json') or '{}')
    except Exception:source_ai={}
    (out/'SOURCE_AI_REVIEW_RETURN.json').write_text(json.dumps(source_ai,ensure_ascii=False,indent=2),encoding='utf-8')
    template={'schema':'ngs_web_igv_ai_return_v1','source_bundle':stem,'reviews':[{'_example_only_remove_or_replace':True,'queue_id':0,
        'web_evidence_assessment':'uncertain','confidence':'indeterminate','source_call_consistency':'indeterminate','observations':[],
        'exact_pileup_available':False,'reason':'','recommended_manual_action':''}],
        'allowed_web_evidence_assessment':['supports_source_call','artifact_suspect','uncertain','insufficient_web_data'],
        'allowed_confidence':['high','medium','low','indeterminate']}
    (out/'AI_IGV_RETURN_TEMPLATE.json').write_text(json.dumps(template,ensure_ascii=False,indent=2),encoding='utf-8')
    prompt='''# GenNext Web IGV ikinci değerlendirme paketi\n\nBu paket BAM/BAI/BED indirmez ve yerel pileup yapmaz. IGV_WEB_EVIDENCE.json içindeki `source_call` GenNext varyant dışa aktarımından gelen AF/AD/DP/filter bilgisidir; `web_igv` ise açık GenNext IGV sayfasının render edilmiş track/DOM/popup verisidir.\n\n- `web_igv.exact_pileup=false` ise IGV görüntüsünden veya canvas piksellerinden read sayısı üretme.\n- `probes.*.raw_text` yalnız web IGV popup tarafından gerçekten açığa çıkarılan metindir. Boşsa veri yoktur; tahmin etme.\n- Targeted & Padding Regions ve Kit Regions & Read Depth tracklerinin varlığı BED dosyasına sahip olduğumuz anlamına gelmez; yalnız web IGV track kanıtıdır.\n- `source_call` AD/DP/VAF ile web track bulguları çelişirse açıkça belirt.\n- Alignment read_probe örneklenmiş popup verisidir; tam pileup değildir.\n- Unknown/onkojenisitesi bilinmeyen varyantı sessizce eleme.\n- Nihai karar patolog/laboratuvar uzmanına aittir.\n\nAI_IGV_RETURN_TEMPLATE.json biçiminde geçerli UTF-8 JSON üret.\n'''
    (out/'PROMPT.md').write_text(prompt,encoding='utf-8')
    view=_ngs_write_web_view_v0434(out,items,missing_public,f'{session.get("pool_group") or "POOL"} {session.get("sample_scope") or ""} · Web IGV Veri Görünümü')
    manifest={'schema':'ngs_web_igv_ai_bundle_v1','generated_at':evidence['generated_at'],'source_ai_session_id':sid,'files':[]}
    for f in sorted(out.rglob('*')):
        if f.is_file():manifest['files'].append({'path':f.relative_to(out).as_posix(),'size':f.stat().st_size,'sha256':_sha256(f)})
    (out/'MANIFEST.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    zpath=root/(stem+'.zip')
    with zipfile.ZipFile(zpath,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as zf:
        for f in sorted(out.rglob('*')):
            if f.is_file():zf.write(f,arcname=f'{stem}/{f.relative_to(out).as_posix()}')
    (root/'SON_WEB_IGV_YZ_PAKETI.txt').write_text(str(zpath),encoding='utf-8')
    return {'folder':str(out),'zip':str(zpath),'items':len(items),'ai_ready':len(items),'requested':len(rows),'missing':len(missing_public),'gallery':str(view),'session_id':sid,'evidence_mode':'web_igv_only'}


def write_igv_pathologist_gallery(base: Path, db, cfg: dict, session_id=None):
    # In v0.4.34 this button becomes a structured-data view, not a mandatory screenshot gallery.
    res=write_igv_ai_bundle(base,db,cfg,session_id=session_id,require_complete=False)
    return {'html':res['gallery'],'ready':res['ai_ready'],'missing':res['missing'],'zip':res['zip']}


def _ngs_walk_widgets_v0434(widget):
    out=[]
    try:children=widget.winfo_children()
    except Exception:children=[]
    for c in children:
        out.append(c);out.extend(_ngs_walk_widgets_v0434(c))
    return out


class AIReviewIGV(_ngs_ai_review_v0434_prev_class):
    def _build(self):
        super()._build()
        # Hide the v0.4.33 local BAM/CRAM/BED panel if that hotfix is installed.
        for w in _ngs_walk_widgets_v0434(self):
            try:text=str(w.cget('text') or '')
            except Exception:text=''
            if 'BAM/CRAM pileup' in text or 'BED coverage audit' in text:
                try:w.pack_forget()
                except Exception:
                    try:w.grid_remove()
                    except Exception:pass
        ren={'IGV Görüntüsü Al':'Web IGV Verisini Al','Seçilileri Otomatik Hazırla':'Seçili Web IGV Verisini Topla',
             'Tüm IGV’leri Otomatik Hazırla':'Tüm Web IGV Verisini Topla','YZ’ye Gönderilecek Tek ZIP':'Web IGV Verisi ZIP',
             'Patolog Galerisi':'Web IGV Veri Görünümü','Görüntüyü Aç':'Kanıt Dosyasını Aç'}
        for w in _ngs_walk_widgets_v0434(self):
            try:t=str(w.cget('text') or '')
            except Exception:continue
            if t in ren:
                try:w.configure(text=ren[t])
                except Exception:pass

    def refresh(self):
        super().refresh()
        if not hasattr(self,'tree'):return
        for iid in self.tree.get_children():
            try:
                row=self.db.get_igv_queue_item(int(iid)) or {};vals=list(self.tree.item(iid,'values'))
                if len(vals)>1:
                    meta=_ngs_read_web_meta_v0434(row)
                    vals[1]='WEB-DATA' if int(row.get('ai_ready') or 0) and meta.get('evidence_mode')=='web_igv_only' else vals[1]
                    self.tree.item(iid,values=vals)
            except Exception:pass
        rows=[dict(r) for r in self.db.igv_queue_rows(self.filter_var.get())];ready=sum(1 for r in rows if int(r.get('ai_ready') or 0) and _ngs_read_web_meta_v0434(r).get('evidence_mode')=='web_igv_only')
        sessions=self.db.ai_review_sessions(1)
        if sessions:
            s=dict(sessions[0]);self.summary_var.set(f"Son YZ dönüşü #{s['id']} · {s.get('pool_group') or '-'} / {s.get('sample_scope') or '-'} · {len(rows)} IGV · WEB-DATA {ready}")

    def export_igv_ai(self):
        try:
            res=write_igv_ai_bundle(self.base,self.db,self.cfg,session_id=self._latest_session_id(),require_complete=True)
            messagebox.showinfo('YZ’ye Gönderilecek Web IGV ZIP',f"Tek dosya hazır:\n{res['zip']}\n\nWeb IGV veri: {res['ai_ready']}/{res['requested']}\n\nBAM/BAI/BED indirilmedi; ekran görüntüsü zorunlu değildir.")
        except Exception as e:messagebox.showerror('Web IGV YZ Paketi',str(e))

    def open_pathologist_gallery(self):
        try:
            res=write_igv_pathologist_gallery(self.base,self.db,self.cfg,session_id=self._latest_session_id())
            try:os.startfile(res['html'])
            except Exception:pass
            messagebox.showinfo('Web IGV Veri Görünümü',f"Hazır veri: {res['ready']}\nBekleyen: {res['missing']}\n\n{res['html']}")
        except Exception as e:messagebox.showerror('Web IGV Veri Görünümü',str(e))

    def open_snapshot(self):
        item=self._selected()
        if not item:return
        p=Path(item.get('capture_meta_path') or '')
        if not p.exists():p=Path(item.get('screenshot_path') or '')
        if not p.exists():messagebox.showwarning('Web IGV','Kaydedilmiş web IGV kanıt dosyası bulunamadı.');return
        try:os.startfile(str(p))
        except Exception as e:messagebox.showerror('Web IGV',str(e))

    def on_browser_result(self,info: dict):
        if info.get('command')=='igv_batch_capture':
            self.refresh();sid=info.get('session_id') or self._latest_session_id();total=int(info.get('session_total',0) or 0);ready=int(info.get('session_ready',0) or 0);missing=int(info.get('session_missing',0) or 0)
            if total and missing==0:
                try:
                    res=write_igv_ai_bundle(self.base,self.db,self.cfg,session_id=sid,require_complete=True)
                    self.summary_var.set(f'Web IGV veri TAMAM · {ready}/{total} WEB-DATA · YZ ZIP: {res["zip"]}')
                    messagebox.showinfo('Web IGV tamam · YZ ZIP hazır',f"Tüm web IGV verileri hazır: {ready}/{total}\n\nYZ’ye göndereceğiniz tek dosya:\n{res['zip']}")
                except Exception as e:self.summary_var.set(f'Web IGV {ready}/{total} hazır · ZIP oluşturulamadı: {e}')
            elif total:self.summary_var.set(f'Web IGV veri toplama: {ready}/{total} WEB-DATA · {missing} yeniden denenmeli.')
            return
        # Collector already persisted single-capture metadata; refresh is sufficient even without screenshot_path.
        self.refresh()

# === NGS_WEB_IGV_SIMPLE_UI_V0435 ===
# Current-session-only Web IGV workspace: no pathologist decision controls, no stale AI JSON context.
_ngs_ai_review_v0435_prev_class = AIReviewIGV


def _ngs_web_current_rows_v0435(db, session_id=None):
    if session_id is None:
        sessions = db.ai_review_sessions(1)
        session_id = int(sessions[0]['id']) if sessions else None
    rows = [dict(r) for r in db.igv_queue_rows('Tümü')]
    if session_id is not None:
        rows = [r for r in rows if int(r.get('source_session_id') or 0) == int(session_id)]
    else:
        rows = []
    return [r for r in rows if str(r.get('status') or '') != 'dismissed']


def _ngs_web_row_state_v0435(row):
    status = str(row.get('status') or 'pending')
    meta = _ngs_read_web_meta_v0434(row)
    web = meta.get('web_igv') or {}
    if status == 'collecting': return ('collecting', '● TOPLANIYOR', 'TOPLANIYOR', meta)
    if int(row.get('ai_ready') or 0) and web.get('data_ready'): return ('complete', '● TAMAM', 'WEB DATA', meta)
    if int(row.get('ai_ready') or 0) and row.get('screenshot_path') and Path(str(row.get('screenshot_path'))).exists(): return ('complete', '● TAMAM', 'LEGACY KANIT', meta)
    if status == 'error': return ('error', '● HATA', 'HATA', meta)
    if row.get('capture_error') and str(row.get('capture_error')).strip().lower() not in ('kullanıcı durdurdu','kullanici durdurdu'):
        return ('error', '● HATA', 'HATA', meta)
    return ('pending', '● BEKLİYOR', 'DEVAM / TEKRAR', meta)


def _ngs_web_make_ai_debug_v0435(row, meta):
    web = (meta or {}).get('web_igv') or {}
    dbg = web.get('ai_debug_request')
    if isinstance(dbg, dict): return dbg
    err = str(row.get('capture_error') or '').strip()
    if not err: return None
    return {'request_type':'WEB_IGV_EXTRACTOR_JS_REPAIR','generator_version':'0.4.35','failed_stage':'unknown',
        'error':err,'requested_region':row.get('region') or '',
        'instruction':'GenNext web IGV içinde bu başarısız alan için BAM/BAI/BED kullanmadan güvenli page.evaluate JavaScript öner. Pikselden read/DP/AD/VAF tahmin etme; alan yoksa null+error döndür.'}


def write_igv_ai_bundle(base: Path, db, cfg: dict, session_id=None, require_complete=False, queue_ids=None):
    base = Path(base); session = _igv_session_row(db, session_id)
    if not session: raise ValueError('Web IGV paketi için güncel YZ dönüş session kaydı bulunamadı.')
    sid = int(session['id']); rows = _ngs_web_current_rows_v0435(db, sid)
    wanted = {int(x) for x in (queue_ids or []) if str(x).isdigit()}
    if wanted: rows = [r for r in rows if int(r['id']) in wanted]
    if not rows: raise ValueError('Gönderilecek Web IGV satırı seçilmedi / bulunamadı.')
    items=[]; missing=[]; js_repairs=[]; legacy_images=[]
    for r in rows:
        state, lamp, web_status, meta = _ngs_web_row_state_v0435(r)
        web = (meta or {}).get('web_igv') or {}
        source_call = _source_call_context(db, r)
        debug = _ngs_web_make_ai_debug_v0435(r, meta)
        try:auto_settings=json.loads(r.get('auto_settings_json') or '{}')
        except Exception:auto_settings={}
        item = {
            'queue_id': r['id'], 'patient_id': r.get('patient_id') or '', 'record_key': r.get('record_key') or '',
            'sample_type': r.get('sample_type') or '', 'variant_key': r.get('variant_key') or '',
            'gene': r.get('gene') or '', 'change': r.get('change') or '',
            'region': r.get('region') or _region_from_variant_key(r.get('variant_key')),
            'web_status': state, 'capture_error': r.get('capture_error') or '',
            'source_call': source_call, 'web_igv': web, 'auto_settings': auto_settings,
            'ai_debug_request': debug, 'captured_at': r.get('captured_at') or (meta or {}).get('captured_at') or '',
        }
        shot=Path(str(r.get('screenshot_path') or '')) if str(r.get('screenshot_path') or '').strip() else None
        if shot and shot.exists(): legacy_images.append((item,shot))
        items.append(item)
        if state != 'complete':
            missing.append({'queue_id':r['id'],'patient_id':r.get('patient_id') or '','gene':r.get('gene') or '',
                'change':r.get('change') or '','region':item['region'],'status':state,'error':r.get('capture_error') or ''})
        if debug:
            js_repairs.append({'queue_id':r['id'],'patient_id':r.get('patient_id') or '','gene':r.get('gene') or '',
                'change':r.get('change') or '','region':item['region'],'diagnostic':debug})
    if require_complete and missing:
        raise ValueError(f'{len(missing)} seçili Web IGV satırı henüz tam değil. İsterseniz tamamlananları seçip “Seçilileri YZ İçin Gönder” kullanın.')
    stamp=time.strftime('%Y%m%d_%H%M%S'); pool=_safe_name(session.get('pool_group') or 'POOL'); scope=_safe_name(session.get('sample_scope') or 'ALL')
    mode='SELECTED' if wanted else 'CURRENT'; stem=f'WEB_IGV_{mode}_{pool}_{scope}_{stamp}'
    root=base/str(cfg.get('ai_export_dir') or 'AI_EXPORT')/'WEB_IGV_READY'/f'{pool}_{scope}'; root.mkdir(parents=True,exist_ok=True)
    out=root/stem; out.mkdir(parents=True,exist_ok=False)
    evidence={'schema':'ngs_web_igv_ai_evidence_v2','schema_version':2,'generator_version':'0.4.35','generated_at':time.strftime('%Y-%m-%dT%H:%M:%S'),
        'evidence_mode':'web_igv_only','source_session':{'id':sid,'pool_group':session.get('pool_group') or '','sample_scope':session.get('sample_scope') or ''},
        'selection_mode':'selected_rows' if wanted else 'current_session','requested_count':len(rows),'complete_count':sum(1 for x in items if x['web_status']=='complete'),
        'error_or_pending_count':len(missing),'items':items,
        'local_file_policy':{'bam':False,'bai':False,'cram':False,'bed':False,'p2p_download':False},
        'old_ai_json_policy':'Raw/previous AI review JSON is intentionally not copied into this package. Only current Web IGV task identifiers, source-call fields, current web evidence and extraction diagnostics are included.',
        'interpretation_boundary':'Web IGV verisi yalnız render edilmiş GenNext DOM/popup alanlarından gelir. Pikselden read sayısı üretilmez.'}
    evidence_text=json.dumps(evidence,ensure_ascii=False,indent=2)
    (out/'IGV_WEB_EVIDENCE.json').write_text(evidence_text,encoding='utf-8')
    (out/'IGV_EVIDENCE.json').write_text(evidence_text,encoding='utf-8')
    if legacy_images:
        images=out/'IMAGES';images.mkdir(exist_ok=True)
        for idx,(item,shot) in enumerate(legacy_images,1):
            name=f'{idx:03d}_{_safe_name(item.get("patient_id"))}_{_safe_name(item.get("gene") or "VAR")}.png'
            shutil.copy2(shot,images/name);item['image']=f'IMAGES/{name}'
        evidence_text=json.dumps(evidence,ensure_ascii=False,indent=2)
        (out/'IGV_WEB_EVIDENCE.json').write_text(evidence_text,encoding='utf-8')
        (out/'IGV_EVIDENCE.json').write_text(evidence_text,encoding='utf-8')
    if js_repairs:
        (out/'JS_REPAIR_REQUESTS.json').write_text(json.dumps({'schema':'ngs_web_igv_js_repair_requests_v1','generator_version':'0.4.35','requests':js_repairs},ensure_ascii=False,indent=2),encoding='utf-8')
    template={'schema':'ngs_web_igv_extractor_feedback_v1','source_bundle':stem,'reviews':[{'_example_only_remove_or_replace':True,'queue_id':0,
        'data_status':'web_data_sufficient','observations':[],'extractor_issue':'','needs_new_js':False,'suggested_js':'','reason':''}],
        'allowed_data_status':['web_data_sufficient','retry_web_igv','needs_new_js','insufficient_web_data']}
    (out/'AI_WEB_IGV_RETURN_TEMPLATE.json').write_text(json.dumps(template,ensure_ascii=False,indent=2),encoding='utf-8')
    prompt='''# GenNext Web IGV veri / extractor değerlendirmesi\n\nBu paket yalnız güncel seçili Web IGV satırlarını içerir. Önceki AI review JSON'u ve patolog karar alanları bilerek pakete konmamıştır.\n\n- `source_call`: GenNext varyant exportundan gelen AF/AD/DP/filter.\n- `web_igv`: açık GenNext IGV sayfasının render edilmiş DOM/popup verisi.\n- `ai_debug_request`: veri çıkarımı başarısız/eksik olduğunda yeni JS extractor tasarlamak için teknik tanı bilgisi.\n- Hata alanı varsa gerekirse yalnız o alan için yeni Playwright `page.evaluate` JavaScript öner. BAM/BAI/BED/P2P isteme.\n- Piksel veya ekran görüntüsünden read, DP, AD veya VAF tahmin etme.\n- Var olan çalışan alanları bozacak tüm-sayfa yeniden yazım yerine hedefli, fail-safe extractor öner.\n- `suggested_js` yalnız teknik veri çıkarımı içindir; klinik/patolog kararı üretme.\n\nAI_WEB_IGV_RETURN_TEMPLATE.json biçimini kullan.\n'''
    (out/'PROMPT.md').write_text(prompt,encoding='utf-8')
    files=[]
    for p in sorted(out.rglob('*')):
        if p.is_file(): files.append({'path':p.relative_to(out).as_posix(),'size':p.stat().st_size,'sha256':_sha256(p)})
    manifest={'schema':'ngs_web_igv_bundle_v2','generated_at':evidence['generated_at'],'source_session_id':sid,'files':files}
    (out/'MANIFEST.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    zpath=root/(stem+'.zip')
    with zipfile.ZipFile(zpath,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as zf:
        for p in sorted(out.rglob('*')):
            if p.is_file(): zf.write(p,arcname=f'{out.name}/{p.relative_to(out).as_posix()}')
    return {'folder':str(out),'zip':str(zpath),'requested':len(rows),'ai_ready':evidence['complete_count'],'missing':len(missing),'js_repair_requests':len(js_repairs)}


class AIReviewIGV(_ngs_ai_review_v0435_prev_class):
    def _build(self):
        self.filter_var=tk.StringVar(value='Tümü')
        self.summary_var=tk.StringVar(value='Yeni YZ JSON bekleniyor.')
        self._batch_active=False
        top=ttk.Frame(self,padding=10);top.pack(fill='x')
        ttk.Label(top,text='YZ Dönüşü / Web IGV',font=('Segoe UI',13,'bold')).pack(side='left')
        ttk.Button(top,text='Genomik YZ JSON/ZIP İçe Al',command=self.import_file).pack(side='left',padx=10)
        ttk.Button(top,text='Yenile',command=self.refresh).pack(side='left')
        ttk.Label(top,text='Göster:').pack(side='left',padx=(18,4))
        cb=ttk.Combobox(top,textvariable=self.filter_var,state='readonly',width=12,values=('Tümü','Tamam','Devam','Hata'))
        cb.pack(side='left');cb.bind('<<ComboboxSelected>>',lambda e:self.refresh())
        ttk.Label(self,textvariable=self.summary_var,padding=(12,0,12,7),foreground='#475569').pack(fill='x')
        cols=('lamp','patient','type','gene','change','region','web','error')
        area=ttk.Frame(self);area.pack(fill='both',expand=True,padx=10,pady=(0,6))
        self.tree=ttk.Treeview(area,columns=cols,show='headings',selectmode='extended')
        labels={'lamp':'DURUM','patient':'HASTA / MP','type':'TİP','gene':'GEN','change':'VARYANT','region':'WEB IGV BÖLGESİ','web':'WEB VERİ','error':'HATA / YZ JS NOTU'}
        widths={'lamp':105,'patient':160,'type':55,'gene':95,'change':170,'region':180,'web':130,'error':520}
        for c in cols:self.tree.heading(c,text=labels[c],anchor='w');self.tree.column(c,width=widths[c],anchor='w')
        self.tree.tag_configure('complete',foreground='#087a2c');self.tree.tag_configure('collecting',foreground='#1d4ed8')
        self.tree.tag_configure('pending',foreground='#9a5b00');self.tree.tag_configure('error',foreground='#b42318')
        ys=ttk.Scrollbar(area,orient='vertical',command=self.tree.yview);xs=ttk.Scrollbar(area,orient='horizontal',command=self.tree.xview)
        self.tree.configure(yscrollcommand=ys.set,xscrollcommand=xs.set);self.tree.grid(row=0,column=0,sticky='nsew');ys.grid(row=0,column=1,sticky='ns');xs.grid(row=1,column=0,sticky='ew')
        area.rowconfigure(0,weight=1);area.columnconfigure(0,weight=1);self.tree.bind('<Double-1>',lambda e:self.open_igv())
        bot=ttk.Frame(self,padding=(10,3,10,10));bot.pack(fill='x')
        ttk.Button(bot,text='Web IGV Aç',command=self.open_igv).pack(side='left')
        ttk.Button(bot,text='Seçili Web IGV Verisini Topla',command=self.auto_capture_selected).pack(side='left',padx=4)
        ttk.Button(bot,text='Tüm Bekleyenleri Topla',command=self.auto_capture_all).pack(side='left',padx=4)
        tk.Button(bot,text='■ Toplamayı Durdur',command=self.stop_web_collection,bg='#b42318',fg='white',activebackground='#912018',activeforeground='white',relief='flat',bd=0,padx=10,pady=5,cursor='hand2').pack(side='left',padx=(10,4))
        ttk.Separator(bot,orient='vertical').pack(side='left',fill='y',padx=8)
        ttk.Button(bot,text='Seçilileri YZ İçin Gönder',command=self.export_selected_for_ai).pack(side='left',padx=4)
        ttk.Button(bot,text='Tüm Güncel Listeyi YZ İçin Gönder',command=self.export_igv_ai).pack(side='left',padx=4)
        ttk.Button(bot,text='İncelenmesi Bitti · Seçilileri Kaldır',command=self.remove_selected).pack(side='left',padx=8)
        ttk.Button(bot,text='Kanıt JSON Aç',command=self.open_snapshot).pack(side='right')

    def _latest_session_id(self):
        sessions=self.db.ai_review_sessions(1);return int(sessions[0]['id']) if sessions else None

    def _rows_current(self):
        return _ngs_web_current_rows_v0435(self.db,self._latest_session_id())

    def _selected_ids(self, warn=True):
        ids=[int(x) for x in self.tree.selection() if str(x).isdigit()]
        if not ids and warn:messagebox.showwarning('Web IGV','Bir veya birden fazla satır seçin.')
        return ids

    def _selected(self):
        ids=self._selected_ids(True);return self.db.get_igv_queue_item(ids[0]) if ids else None

    def import_file(self):
        path=filedialog.askopenfilename(title='Yeni YZ dönüş JSON/ZIP seçin',filetypes=[('AI review JSON/ZIP',('*.json','*.zip')),('JSON','*.json'),('ZIP','*.zip'),('Tüm dosyalar','*.*')])
        if not path:return
        try:
            # A Web UI diagnostic review is NOT a genomic patient/variant review.
            # Route it to the dedicated read-only panel, even when the user
            # selected the wrong button. No changes to RNA/DNA clinical tables.
            from web_interface_panel import is_web_ai_review_file
            if is_web_ai_review_file(Path(path)):
                host=self.winfo_toplevel()
                panel=getattr(host,'web_panel',None)
                if panel is None:raise ValueError('Web Arayüzü sekmesi bu uygulamada bulunamadı.')
                host.workspaces.select(panel)
                panel.import_ai_file(path)
                return
            data,source,sha=load_ai_review_return(Path(path));errors,warnings=validate_ai_review_return(data)
            if errors:raise ValueError('\n'.join(errors))
            result=self.db.import_ai_review(data,source,sha);sid=int(result['session_id'])
            # The database reconciles immutable request identity. A clinical metadata
            # refresh or duplicate return must retain images and manual decisions.
            self.refresh();allwarn=(warnings or [])+(result.get('warnings') or [])
            msg=(f"YZ JSON alındı.\nHasta değerlendirmeleri: {result.get('patient_reviews',0)}"
                 f"\nVaryant değerlendirmeleri: {result.get('variant_reviews',0)}"
                 f"\nKlinik bilgileri kaydedilen çalışma: {result.get('clinical_records',0)}"
                 f"\nWeb IGV istekleri: {result.get('igv_requests',0)}\n"
                 "Web IGV listesi yalnız yeni oturumun IGV görevlerini gösterir."
                 "\nHasta sonuçları aşağıdaki 'Hasta bazlı YZ değerlendirmelerini aç' düğmesinden görülebilir.")
            if result.get('igv_reviews_imported') or result.get('igv_reviews_duplicate') or result.get('igv_reviews_unresolved'):
                msg += (f"\nIGV değerlendirmesi: {result.get('igv_reviews_imported',0)} alındı; "
                        f"{result.get('igv_reviews_duplicate',0)} zaten kayıtlı; "
                        f"{result.get('igv_reviews_unresolved',0)} kimlik kontrolü bekliyor.")
            if result.get('duplicate_import'):
                msg += "\nAynı dönüş yeniden çoğaltılmadı; görüntüler ve patolog kararı korundu."
            if not result.get('igv_requests',0):
                msg += "\n\nIGV=0: doğrulanmış füzyon geni/kırılma noktası yoksa beklenen durumdur; negatif füzyon anlamına gelmez."
            if allwarn:msg+='\n\nUyarı:\n- '+'\n- '.join(allwarn[:8])
            messagebox.showinfo('YZ Dönüş / Web IGV',msg)
        except Exception as e:messagebox.showerror('YZ dönüşü alınamadı',str(e))

    def refresh(self):
        if not hasattr(self,'tree'):return
        selected=set(self.tree.selection())
        for x in self.tree.get_children():self.tree.delete(x)
        rows=self._rows_current();counts={'complete':0,'collecting':0,'pending':0,'error':0}
        filt=self.filter_var.get() if hasattr(self,'filter_var') else 'Tümü'
        fmap={'Tamam':'complete','Devam':'pending','Hata':'error'}
        for r in rows:
            state,lamp,web_status,meta=_ngs_web_row_state_v0435(r);counts[state]=counts.get(state,0)+1
            if filt!='Tümü':
                want=fmap.get(filt)
                if want=='pending' and state not in ('pending','collecting'):continue
                if want!='pending' and state!=want:continue
            patient_label=((r.get('full_name') or '').strip()+(' · ' if (r.get('full_name') or '').strip() and (r.get('patient_id') or '').strip() else '')+(r.get('patient_id') or '').strip()) or (r.get('patient_id') or '')
            err=str(r.get('capture_error') or '')
            dbg=_ngs_web_make_ai_debug_v0435(r,meta)
            if dbg and not err:err=str(dbg.get('error') or '')
            self.tree.insert('', 'end',iid=str(r['id']),values=(lamp,patient_label,r.get('sample_type') or '',r.get('gene') or '',r.get('change') or r.get('variant_key') or '',r.get('region') or '',web_status,err),tags=(state,))
        for iid in selected:
            if self.tree.exists(iid):self.tree.selection_add(iid)
        sid=self._latest_session_id();total=sum(counts.values())
        self.summary_var.set(f'Güncel YZ JSON/session #{sid or "-"} · {total} satır · ● TAMAM {counts.get("complete",0)} · ● TOPLANIYOR {counts.get("collecting",0)} · ● BEKLİYOR {counts.get("pending",0)} · ● HATA {counts.get("error",0)}')

    def _begin_poll(self):
        if self._batch_active:return
        self._batch_active=True
        self.after(350,self._poll_progress)

    def _poll_progress(self):
        if not self._batch_active:return
        try:self.refresh()
        except Exception:pass
        self.after(500,self._poll_progress)

    def auto_capture_selected(self):
        if not self.browser_submit:messagebox.showwarning('Web IGV','Tarayıcı worker bağlantısı yok.');return
        ids=self._selected_ids(True)
        if not ids:return
        ok=self.browser_submit('igv_batch_capture',queue_ids=ids,recapture=True,session_id=self._latest_session_id())
        if ok:self._begin_poll()
        else:messagebox.showwarning('Web IGV','Tarayıcı meşgul veya hazır değil.')

    def auto_capture_all(self):
        if not self.browser_submit:messagebox.showwarning('Web IGV','Tarayıcı worker bağlantısı yok.');return
        sid=self._latest_session_id()
        if not sid:messagebox.showwarning('Web IGV','Önce yeni YZ JSON içe alınmalı.');return
        ok=self.browser_submit('igv_batch_capture',session_id=sid,recapture=False)
        if ok:self._begin_poll()
        else:messagebox.showwarning('Web IGV','Tarayıcı meşgul veya hazır değil.')

    def stop_web_collection(self):
        if not self.browser_submit:return
        ok=self.browser_submit('igv_stop_now')
        if ok:self.summary_var.set('■ Web IGV durdurma istendi · mevcut kısa web adımı kesilir kesilmez duracak.')

    def remove_selected(self):
        ids=self._selected_ids(True)
        if not ids:return
        if not messagebox.askyesno('Listeden kaldır',f'{len(ids)} seçili satır “incelenmesi bitti” olarak güncel listeden kaldırılsın mı?\n\nKanıt JSON dosyaları diskten silinmez.'):return
        for qid in ids:self.db.update_igv_queue_item(qid,status='dismissed',manual_decision='',manual_note='')
        self.refresh()

    def export_selected_for_ai(self):
        ids=self._selected_ids(True)
        if not ids:return
        try:
            res=write_igv_ai_bundle(self.base,self.db,self.cfg,session_id=self._latest_session_id(),require_complete=False,queue_ids=ids)
            self.summary_var.set(f'Seçili {res["requested"]} satır için YZ ZIP hazır · tamam {res["ai_ready"]} · JS hata/onarım isteği {res["js_repair_requests"]} · {res["zip"]}')
            messagebox.showinfo('Seçililer YZ için hazır',f"ZIP:\n{res['zip']}\n\nSeçili: {res['requested']}\nWeb IGV tamam: {res['ai_ready']}\nHata/JS repair: {res['js_repair_requests']}")
        except Exception as e:messagebox.showerror('Web IGV YZ Paketi',str(e))

    def export_igv_ai(self):
        try:
            res=write_igv_ai_bundle(self.base,self.db,self.cfg,session_id=self._latest_session_id(),require_complete=False)
            self.summary_var.set(f'Güncel liste YZ ZIP hazır · {res["zip"]}')
            messagebox.showinfo('Web IGV YZ Paketi',f"ZIP:\n{res['zip']}\n\nSatır: {res['requested']}\nTamam: {res['ai_ready']}\nHata/JS repair: {res['js_repair_requests']}")
        except Exception as e:messagebox.showerror('Web IGV YZ Paketi',str(e))

    def open_snapshot(self):
        item=self._selected()
        if not item:return
        p=Path(item.get('capture_meta_path') or '')
        if not p.exists():messagebox.showwarning('Web IGV','Bu satır için henüz kanıt JSON dosyası yok.');return
        try:os.startfile(str(p))
        except Exception as e:messagebox.showerror('Web IGV',str(e))

    def on_browser_result(self,info: dict):
        self._batch_active=False;self.refresh()
        if info.get('command')=='igv_batch_capture':
            total=int(info.get('session_total',0) or 0);ready=int(info.get('session_ready',0) or 0);err=int(info.get('session_error',0) or info.get('error',0) or 0)
            if info.get('status')=='stopped':self.summary_var.set(f'■ Web IGV toplama durduruldu · tamam {ready}/{total} · hata {err}. Kalanlar listede bekliyor.')
            else:self.summary_var.set(f'Web IGV toplama bitti · tamam {ready}/{total} · hata {err}. İsterseniz seçili satırları şimdi YZ için gönderebilirsiniz.')

# === NGS_IGV_FOUR_IMAGE_UI_V0436 ===
# Simple IGV UI + image-first AI package. Clinical reportability remains outside this layer.
_ngs_ai_review_v0436_prev_class = AIReviewIGV
_ngs_write_igv_bundle_v0436_prev = write_igv_ai_bundle


def _ngs_igv_capture_meta_v0436(row):
    p=Path(str(row.get('capture_meta_path') or '')) if str(row.get('capture_meta_path') or '').strip() else None
    if not p or not p.exists():return {}
    try:
        d=json.loads(p.read_text(encoding='utf-8'))
        return d if d.get('schema') in ('ngs_igv_capture_v1','ngs_igv_capture_v2') else {}
    except Exception:return {}


def _ngs_igv_zoom_status_v0436(meta):
    by={str(x.get('zoom')):x for x in (meta.get('images') or []) if isinstance(x,dict)}
    vals=[];n=0
    for z in ('Z1','Z2','Z3','Z4'):
        x=by.get(z) or {};ok=x.get('status')=='complete' and Path(str(x.get('path') or '')).is_file();n+=1 if ok else 0;vals.append(('✓' if ok else '✕')+' '+z)
    return '  '.join(vals),n

def _ngs_igv_row_state_v0436(row):
    from igv_workflow_v0481 import capture_matches_request
    meta=_ngs_igv_capture_meta_v0436(row)
    if not capture_matches_request(meta,row,row.get('record_key') or '',row.get('variant_key') or ''):meta={}
    zs,n=_ngs_igv_zoom_status_v0436(meta)
    if str(row.get('status') or '')=='collecting':state='collecting';lamp='● TOPLANIYOR'
    elif n==4:state='complete';lamp='● READY'
    elif n>0:state='partial';lamp='● PARTIAL'
    elif str(row.get('status') or '')=='error':state='error';lamp='● HATA'
    else:state='pending';lamp='● BEKLİYOR'
    data='-'
    if meta.get('variant_popup') and meta.get('read_popup'):data='VAR+READ'
    elif meta.get('variant_popup'):data='VAR'
    elif meta.get('read_popup'):data='READ'
    return state,lamp,zs,data,meta


def _ngs_request_id_v0436(row):
    return str(row.get('request_id') or '').strip() or _safe_name(f"{row.get('patient_id') or 'SAMPLE'}_{row.get('gene') or 'VAR'}_{row.get('change') or row.get('variant_key') or row.get('id')}",100)


def write_igv_ai_bundle(base: Path, db, cfg: dict, session_id=None, require_complete=False, queue_ids=None):
    """Export request-scoped images with source metrics and independent capture state."""
    from igv_capture_v1 import SCHEMA_REQUEST, SCHEMA_CAPTURE, SCHEMA_REVIEW, APP_VERSION, CAPTURE_ENGINE_VERSION
    from igv_workflow_v0481 import export_request, request_identity, capture_matches_request, igv_scope_summary, current_igv_review
    base=Path(base);session=_igv_session_row(db,session_id)
    if not session:raise ValueError('IGV paketi için güncel YZ session kaydı bulunamadı.')
    sid=int(session['id']);rows=_igv_rows_for_session(db,sid)
    rows=[r for r in rows if r.get('status')!='dismissed']
    wanted={int(x) for x in (queue_ids or []) if str(x).isdigit()}
    if wanted:rows=[r for r in rows if int(r['id']) in wanted]
    if not rows:raise ValueError('IGV paketine eklenecek satır yok.')
    stamp=time.strftime('%Y%m%d_%H%M%S');pool=_safe_name(session.get('pool_group') or 'POOL');scope=_safe_name(session.get('sample_scope') or 'ALL')
    stem=f'IGV_PACKAGE_{pool}_{scope}_{stamp}';root=base/str(cfg.get('ai_export_dir') or 'AI_EXPORT')/'IGV_PACKAGES'/f'{pool}_{scope}';root.mkdir(parents=True,exist_ok=True)
    out=root/stem;out.mkdir(parents=True,exist_ok=False);images_dir=out/'IMAGES';images_dir.mkdir()
    source_bundle={'pool_group':session.get('pool_group') or '', 'sample_scope':session.get('sample_scope') or '', 'generated_at':time.strftime('%Y-%m-%dT%H:%M:%S')}
    versions={'app_version':APP_VERSION,'generator_version':APP_VERSION,'export_schema_version':2,'capture_engine_version':CAPTURE_ENGINE_VERSION}
    request_doc={'schema':SCHEMA_REQUEST,'document_type':'IGV_REQUEST','schema_version':2,**versions,
                 'source_bundle':source_bundle,'created_at':source_bundle['generated_at'],'pool':session.get('pool_group') or '','source_session_id':sid,'requests':[]}
    capture_doc={'schema':SCHEMA_CAPTURE,'schema_version':2,**versions,'pool':session.get('pool_group') or '','source_session_id':sid,'captures':[]}
    ready=partial=missing=0
    for row in rows:
        payload,row=export_request(db,row)
        identity=request_identity(payload)
        meta=_ngs_igv_capture_meta_v0436(row);source=_source_call_context(db,row);rid=_ngs_request_id_v0436(row)
        if not capture_matches_request(meta,row,row['record_key'],row['variant_key']):
            meta={}
        request_linkage=meta.get('request_linkage') or payload.get('legacy_capture_linkage') or {
            'origin':'explicit_request_identity' if meta.get('request_id') else 'request_identity_only_no_capture',
            'explicit_capture_revision_verified':bool(meta.get('request_id'))}
        if not source:source=dict(meta.get('source_call') or {})
        coord=payload.get('coordinates') or {}
        target=(meta.get('target') or {})
        chrom=source.get('chrom') or coord.get('chrom') or target.get('chrom') or '';pos=source.get('start') or coord.get('position') or target.get('pos') or None
        request_doc['requests'].append({**payload,**identity,'id':rid,'queue_id':row['id'],'sample':row.get('patient_id') or '','gene':row.get('gene') or source.get('gene') or '',
            'chrom':str(chrom).replace('chr','',1) if str(chrom).lower().startswith('chr') else str(chrom),'pos':pos,'ref':source.get('ref') or target.get('ref') or '',
            'alt':source.get('alt') or coord.get('alt') or target.get('alt') or '','vaf':source.get('af'),'ad':source.get('ad'),'dp':source.get('dp'),'filter':source.get('filter') or '',
            'source_call':source,'clinical_report_release':False})
        if not request_doc['requests'][-1]['ref']:request_doc['requests'][-1]['ref']=coord.get('ref') or ''
        out_images=[];image_meta=[];complete_count=0;stage_warnings=[]
        for x in meta.get('images') or []:
            if not isinstance(x,dict):continue
            src_path=Path(str(x.get('path') or ''));zoom=str(x.get('zoom') or '');rel_name=''
            if x.get('status')=='complete' and src_path.is_file() and zoom in ('Z1','Z2','Z3','Z4') and not any(y.get('zoom')==zoom and y.get('image') for y in image_meta):
                image_id=_safe_name(rid,100)+'_'+hashlib.sha256(rid.encode('utf-8')).hexdigest()[:8]
                name=f'{image_id}_{zoom}.png';shutil.copy2(src_path,images_dir/name);rel_name=f'IMAGES/{name}';out_images.append(rel_name);complete_count+=1
            elif zoom:stage_warnings.append(f'missing_zoom:{zoom}')
            errs=[str(e) for e in (x.get('errors') or []) if str(e).strip()]
            if errs:stage_warnings.extend([f'{zoom}:{e}' for e in errs[-3:]])
            image_meta.append({'zoom':zoom,'status':x.get('status') or '','image':rel_name or None,
                'requested_locus':x.get('locus') or '','visible_locus':x.get('visible_locus') or '',
                'window_bp':x.get('window_bp'),'attempt':x.get('attempt'),'stable_delta':x.get('stable_delta'),
                'after_reload':bool(x.get('after_reload')),'capture_engine_version':x.get('capture_engine_version') or meta.get('capture_engine_version') or meta.get('generator_version') or 'unknown','errors':errs})
        status='complete' if complete_count==4 else ('partial' if complete_count else 'missing')
        if status=='complete':ready+=1
        elif status=='partial':partial+=1
        else:missing+=1
        merged_warnings=[]
        for w in list(meta.get('warnings') or [])+stage_warnings:
            w=str(w).strip()
            if w and w not in merged_warnings:merged_warnings.append(w)
        capture_doc['captures'].append({**identity,'id':rid,'queue_id':row['id'],'request_linkage':request_linkage,'status':status,'images':out_images,'image_meta':image_meta,
            'variant_popup':meta.get('variant_popup') or None,'read_popup':meta.get('read_popup') or None,'warnings':merged_warnings,
            'source_call':source,'independently_measured_metrics':{'status':'not_computed','dp':None,'alt_ad':None,'vaf':None,'forward_alt':None,'reverse_alt':None},
            'clinical_report_release':False,'technical_review':current_igv_review(db,row),
            'capture_meta':{'generator_version':meta.get('generator_version') or '','app_version':meta.get('app_version') or '',
                'capture_engine_version':meta.get('capture_engine_version') or meta.get('generator_version') or '', 'reload_used':bool(meta.get('reload_used')),
                'requested_region':meta.get('requested_region') or '','capture_status':meta.get('status') or ''}})
        db.cx.execute('INSERT OR REPLACE INTO igv_bundle_identity_v0481(bundle_name,legacy_id,request_id,identity_json) VALUES(?,?,?,?)',
                      (stem,rid,identity['request_id'],json.dumps({**identity,'capture_status':status,
                         'available_zooms':[x['zoom'] for x in image_meta if x.get('image')]},ensure_ascii=False)))
    db.cx.commit()
    if require_complete and (partial or missing):raise ValueError(f'IGV paketi tam değil: complete {ready}, partial {partial}, missing {missing}.')
    scope_summary=igv_scope_summary(db,session_id=sid)
    request_doc['scope']={'session_requested_count':scope_summary['requested_count'],'exported_count':len(rows),'truncated':len(rows)<scope_summary['requested_count']}
    capture_doc['scope']={**request_doc['scope'],'complete_count':ready,'partial_count':partial,'missing_count':missing,
                          'clinical_report_release':False,'note':f'{len(rows)}/{scope_summary["requested_count"]} istek bu pakette temsil ediliyor.'}
    builds={r.get('genome_build') for r in request_doc['requests'] if r.get('genome_build')}
    if len(builds)==1:request_doc['genome_build']=next(iter(builds))
    (out/'IGV_REQUEST.json').write_text(json.dumps(request_doc,ensure_ascii=False,indent=2),encoding='utf-8')
    (out/'IGV_CAPTURE.json').write_text(json.dumps(capture_doc,ensure_ascii=False,indent=2),encoding='utf-8')
    example_identity=request_identity(request_doc['requests'][0])
    template={'schema':SCHEMA_REVIEW,'schema_version':2,'source_bundle':stem,'reviews':[{'_example_only_remove_or_replace':True,
        **example_identity,'evidence':'indeterminate','confidence':'medium','manual_review':True,'capture_status':capture_doc['captures'][0]['status'],'clinical_report_release':False,'note':''}],
        'allowed_evidence':['supports_call','does_not_support','indeterminate'],'allowed_confidence':['high','medium','low']}
    (out/'AI_IGV_REVIEW_TEMPLATE.json').write_text(json.dumps(template,ensure_ascii=False,indent=2),encoding='utf-8')
    prompt='''# IGV teknik görsel değerlendirme\n\nIGV_REQUEST.json kaynak çağrıyı; IGV_CAPTURE.json yalnız bu paketteki görüntüleri içerir. Her dönüş satırında request_id, request_revision, record_key, variant_key ve genome_build aynen korunmalıdır. queue_id oturuma özgüdür.\n\nHer istek için evidence = supports_call/does_not_support/indeterminate; confidence = high/medium/low; manual_review = true/false verin. Çekim complete/partial/missing durumu görsel kanıt değerlendirmesinden ayrıdır. Tek varyantın sonucu başka isteklere taşınmaz.\n\nKaynak source_call AD/DP/VAF ölçümleri bağımsız IGV ölçümü değildir; görüntüden sayısal pileup üretmeyin. Eksik zoom mevcut görüntü desteğini silmez, ancak tam teknik doğrulamaya dönüşmez. Klinik rapor onayı bu katmanın dışındadır; clinical_report_release=false kalmalıdır.\n\nAI_IGV_REVIEW_TEMPLATE.json yapısında ngs_igv_review_v2 JSON üretin.\n'''
    (out/'PROMPT.md').write_text(prompt,encoding='utf-8')
    files=[]
    for p in sorted(out.rglob('*')):
        if p.is_file():files.append({'path':p.relative_to(out).as_posix(),'size':p.stat().st_size,'sha256':_sha256(p)})
    (out/'MANIFEST.json').write_text(json.dumps({'schema':'ngs_igv_package_v2',**versions,'generated_at':time.strftime('%Y-%m-%dT%H:%M:%S'),'files':files},ensure_ascii=False,indent=2),encoding='utf-8')
    zpath=root/(stem+'.zip')
    with zipfile.ZipFile(zpath,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as zf:
        for p in sorted(out.rglob('*')):
            if p.is_file():zf.write(p,arcname=f'{stem}/{p.relative_to(out).as_posix()}')
    return {'folder':str(out),'zip':str(zpath),'requested':len(rows),'ai_ready':ready,'partial':partial,'missing':missing,'js_repair_requests':0,'session_id':sid}


def _reveal_file_in_explorer_v0437(path):
    try:
        p=Path(str(path or '')).resolve()
        if os.name=='nt':
            import subprocess
            try:
                subprocess.Popen(['explorer.exe','/select,',str(p)],close_fds=True)
                return True
            except Exception:
                os.startfile(str(p.parent));return True
        return False
    except Exception:return False


class AIReviewIGV(_ngs_ai_review_v0436_prev_class):
    def _build(self):
        self.filter_var=tk.StringVar(value='Tümü');self.summary_var=tk.StringVar(value='Yeni YZ JSON bekleniyor.');self._batch_active=False
        top=ttk.Frame(self,padding=10);top.pack(fill='x');ttk.Label(top,text='YZ Dönüş / IGV',font=('Segoe UI',13,'bold')).pack(side='left')
        ttk.Button(top,text='Genomik YZ JSON/ZIP İçe Al',command=self.import_file).pack(side='left',padx=10);ttk.Button(top,text='Yenile',command=self.refresh).pack(side='left')
        ttk.Label(top,text='Göster:').pack(side='left',padx=(18,4));cb=ttk.Combobox(top,textvariable=self.filter_var,state='readonly',width=12,values=('Tümü','Tamam','Partial','Bekleyen','Hata'))
        cb.pack(side='left');cb.bind('<<ComboboxSelected>>',lambda e:self.refresh())
        ttk.Label(self,textvariable=self.summary_var,padding=(12,0,12,7),foreground='#475569').pack(fill='x')
        cols=('lamp','patient','type','gene','change','zoom','data','error');area=ttk.Frame(self);area.pack(fill='both',expand=True,padx=10,pady=(0,6))
        self.tree=ttk.Treeview(area,columns=cols,show='headings',selectmode='extended')
        labels={'lamp':'IGV','patient':'HASTA / MP','type':'TİP','gene':'GEN','change':'VARYANT','zoom':'4 GÖRÜNTÜ','data':'DATA','error':'HATA / UYARI'}
        widths={'lamp':105,'patient':175,'type':55,'gene':95,'change':190,'zoom':235,'data':80,'error':480}
        for c in cols:self.tree.heading(c,text=labels[c],anchor='w');self.tree.column(c,width=widths[c],anchor='w')
        for tag,color in [('complete','#087a2c'),('collecting','#1d4ed8'),('partial','#9a5b00'),('pending','#64748b'),('error','#b42318')]:self.tree.tag_configure(tag,foreground=color)
        ys=ttk.Scrollbar(area,orient='vertical',command=self.tree.yview);xs=ttk.Scrollbar(area,orient='horizontal',command=self.tree.xview);self.tree.configure(yscrollcommand=ys.set,xscrollcommand=xs.set)
        self.tree.grid(row=0,column=0,sticky='nsew');ys.grid(row=0,column=1,sticky='ns');xs.grid(row=1,column=0,sticky='ew');area.rowconfigure(0,weight=1);area.columnconfigure(0,weight=1)
        bot=ttk.Frame(self,padding=(10,3,10,10));bot.pack(fill='x')
        ttk.Button(bot,text='Seçili IGV’leri Hazırla',command=self.auto_capture_selected).pack(side='left')
        ttk.Button(bot,text='Eksik/Hatalı IGV’leri Tekrarla',command=self.retry_missing).pack(side='left',padx=6)
        ttk.Button(bot,text='IGV Paketini Oluştur',command=self.export_igv_ai).pack(side='left',padx=6)
        tk.Button(bot,text='■ Durdur',command=self.stop_web_collection,bg='#b42318',fg='white',activebackground='#912018',activeforeground='white',relief='flat',bd=0,padx=10,pady=5,cursor='hand2').pack(side='left',padx=(12,4))
        ttk.Button(bot,text='Kanıt JSON Aç',command=self.open_snapshot).pack(side='right')

    def refresh(self):
        if not hasattr(self,'tree'):return
        selected=set(self.tree.selection())
        for x in self.tree.get_children():self.tree.delete(x)
        rows=self._rows_current();counts={'complete':0,'collecting':0,'partial':0,'pending':0,'error':0};filt=self.filter_var.get() if hasattr(self,'filter_var') else 'Tümü'
        fmap={'Tamam':'complete','Partial':'partial','Bekleyen':'pending','Hata':'error'}
        for r in rows:
            state,lamp,zs,data,meta=_ngs_igv_row_state_v0436(r);counts[state]=counts.get(state,0)+1
            if filt!='Tümü' and state!=fmap.get(filt):continue
            patient_label=((r.get('full_name') or '').strip()+(' · ' if (r.get('full_name') or '').strip() and (r.get('patient_id') or '').strip() else '')+(r.get('patient_id') or '').strip()) or (r.get('patient_id') or '')
            err=str(r.get('capture_error') or '');warnings=meta.get('warnings') or []
            if not err and warnings:err='; '.join(str(x) for x in warnings[-3:])
            self.tree.insert('', 'end',iid=str(r['id']),values=(lamp,patient_label,r.get('sample_type') or '',r.get('gene') or '',r.get('change') or r.get('variant_key') or '',zs,data,err),tags=(state,))
        for iid in selected:
            if self.tree.exists(iid):self.tree.selection_add(iid)
        sid=self._latest_session_id();total=sum(counts.values());self.summary_var.set(f'Session #{sid or "-"} · {total} varyant · READY {counts.get("complete",0)} · PARTIAL {counts.get("partial",0)} · BEKLİYOR {counts.get("pending",0)} · HATA {counts.get("error",0)}')

    def auto_capture_selected(self):
        if not self.browser_submit:messagebox.showwarning('IGV','Tarayıcı worker bağlantısı yok.');return
        ids=self._selected_ids(True)
        if not ids:return
        ok=self.browser_submit('igv_batch_capture',queue_ids=ids,recapture=False,session_id=self._latest_session_id())
        if ok:self._begin_poll()
        else:messagebox.showwarning('IGV','Tarayıcı meşgul veya hazır değil.')

    def retry_missing(self):
        if not self.browser_submit:messagebox.showwarning('IGV','Tarayıcı worker bağlantısı yok.');return
        ids=self._selected_ids(False)
        if not ids:
            ids=[]
            for r in self._rows_current():
                state,*_=_ngs_igv_row_state_v0436(r)
                if state in ('partial','pending','error'):ids.append(int(r['id']))
        if not ids:messagebox.showinfo('IGV','Eksik/hatalı IGV yok.');return
        ok=self.browser_submit('igv_batch_capture',queue_ids=ids,recapture=True,session_id=self._latest_session_id())
        if ok:self._begin_poll()
        else:messagebox.showwarning('IGV','Tarayıcı meşgul veya hazır değil.')

    def export_igv_ai(self):
        try:
            ids=self._selected_ids(False)
            res=write_igv_ai_bundle(self.base,self.db,self.cfg,session_id=self._latest_session_id(),require_complete=False,queue_ids=ids or None)
            self._last_igv_package_zip=res['zip']
            self.summary_var.set(f'IGV ZIP hazır · complete {res["ai_ready"]} · partial {res["partial"]} · missing {res["missing"]} · {res["zip"]}')
            opened=_reveal_file_in_explorer_v0437(res['zip'])
            extra='\n\nDosya Windows Gezgini’nde seçildi.' if opened else ''
            messagebox.showinfo('IGV Paketi',f"ZIP:\n{res['zip']}\n\nComplete: {res['ai_ready']}\nPartial: {res['partial']}\nMissing: {res['missing']}{extra}")
        except Exception as e:messagebox.showerror('IGV Paketi',str(e))

    def on_browser_result(self,info: dict):
        self._batch_active=False;self.refresh()
        if info.get('command')=='igv_batch_capture':
            total=int(info.get('session_total',0) or 0);ready=int(info.get('session_ready',0) or 0);err=int(info.get('session_error',0) or 0)
            if info.get('status')=='stopped':self.summary_var.set(f'■ IGV durduruldu · READY {ready}/{total} · hata {err}. Kaldığı yerden devam edilebilir.')
            else:self.summary_var.set(f'IGV hazırlama bitti · READY {ready}/{total} · hata {err}. Eksik zoomlar tekrar denenebilir.')

# === NGS_RNA_PATIENT_REVIEW_UI_V0454 ===
# The IGV queue displays IGV requests only. RNA reviews with no genomic
# breakpoint legitimately contain zero IGV tasks, but patient decisions must
# remain easy to inspect in the desktop application.
_NGSPreRNAFixReviewUI = AIReviewIGV


class AIReviewIGV(_NGSPreRNAFixReviewUI):
    def _build(self):
        self.rna_patient_count_var=tk.StringVar(value='Hasta değerlendirmesi: 0')
        super()._build()
        bar=ttk.Frame(self,padding=(10,0,10,7))
        bar.pack(fill='x')
        ttk.Button(bar,text='Hasta bazlı YZ değerlendirmelerini aç',command=self.show_patient_reviews).pack(side='left')
        ttk.Label(bar,textvariable=self.rna_patient_count_var,foreground='#475569').pack(side='left',padx=12)

    def refresh(self):
        super().refresh()
        if not hasattr(self,'rna_patient_count_var'):return
        sid=self._latest_session_id()
        if not sid:
            self.rna_patient_count_var.set('Hasta değerlendirmesi: 0')
            return
        try:
            count=self.db.cx.execute('SELECT COUNT(*) FROM ai_patient_reviews WHERE session_id=?',(sid,)).fetchone()[0]
            self.rna_patient_count_var.set(f'Hasta değerlendirmesi: {count} · güncel oturum #{sid}')
        except Exception:
            self.rna_patient_count_var.set('Hasta değerlendirmesi: okunamadı')

    def import_file(self):
        previous=self._latest_session_id()
        super().import_file()
        sid=self._latest_session_id()
        # Only open automatically when a genuinely new session was imported,
        # there are patient decisions, and no unresolved IGV locations exist.
        if sid and sid!=previous:
            try:
                patients=self.db.cx.execute('SELECT COUNT(*) FROM ai_patient_reviews WHERE session_id=?',(sid,)).fetchone()[0]
                requests=self.db.cx.execute('SELECT COUNT(*) FROM igv_queue WHERE source_session_id=?',(sid,)).fetchone()[0]
                if patients and not requests:
                    self.show_patient_reviews()
            except Exception:
                pass

    def show_patient_reviews(self):
        sid=self._latest_session_id()
        if not sid:
            messagebox.showinfo('Hasta değerlendirmeleri','Önce YZ dönüş JSON dosyasını içe alın.')
            return
        records=[dict(x) for x in self.db.cx.execute('''SELECT record_key,patient_id,sample_type,release_recommendation,summary,notes_json
            FROM ai_patient_reviews WHERE session_id=? ORDER BY patient_id''',(sid,)).fetchall()]
        if not records:
            messagebox.showinfo('Hasta değerlendirmeleri','Bu YZ oturumunda hasta değerlendirmesi bulunamadı.')
            return
        context={}
        prior_ngs={}
        try:
            row=self.db.cx.execute('SELECT raw_json FROM ai_review_sessions WHERE id=?',(sid,)).fetchone()
            doc=json.loads(row['raw_json']) if row and row['raw_json'] else {}
            for value in doc.get('patient_reviews') or []:
                if isinstance(value,dict) and value.get('record_key'):
                    context[value['record_key']]=value.get('clinical_context') or {}
                    prior_ngs[value['record_key']]=value.get('prior_ngs_history') or {}
        except (ValueError,TypeError,KeyError):
            pass
        win=tk.Toplevel(self)
        win.title(f'POOL YZ · Hasta değerlendirmeleri · oturum #{sid}')
        win.geometry('1250x650')
        win.minsize(780,420)
        outer=ttk.Frame(win,padding=10);outer.pack(fill='both',expand=True)
        ttk.Label(outer,text=f'YZ hasta değerlendirmeleri · {len(records)} örnek',font=('Segoe UI',12,'bold')).pack(anchor='w')
        ttk.Label(outer,text='IGV isteği olmaması negatif füzyon kararı değildir. Bu liste nihai klinik onay yerine geçmez.',foreground='#64748b').pack(anchor='w',pady=(0,8))
        listing=ttk.Frame(outer);listing.pack(fill='both',expand=True)
        cols=('mp','age','sex','diagnosis','block','tumor','decision','summary')
        labels={'mp':'MP','age':'Yaş','sex':'Cinsiyet','diagnosis':'Tanı','block':'Blok','tumor':'Tümör %','decision':'Öneri','summary':'Değerlendirme'}
        widths={'mp':100,'age':50,'sex':55,'diagnosis':180,'block':80,'tumor':70,'decision':80,'summary':680}
        tree=ttk.Treeview(listing,columns=cols,show='headings',selectmode='browse')
        for name in cols:
            tree.heading(name,text=labels[name]);tree.column(name,width=widths[name],anchor='w')
        scroll=ttk.Scrollbar(listing,orient='vertical',command=tree.yview);tree.configure(yscrollcommand=scroll.set)
        tree.pack(side='left',fill='both',expand=True);scroll.pack(side='right',fill='y')
        notes=tk.Text(outer,height=7,wrap='word',font=('Segoe UI',10))
        notes.pack(fill='x',pady=(8,0))
        notes.configure(state='disabled')
        by_row={}
        for i,r in enumerate(records):
            cc=context.get(r['record_key']) or {}
            def show(value):return '-' if value is None or value=='' else str(value)
            tree.insert('', 'end', iid=str(i), values=(r.get('patient_id') or '',show(cc.get('age_at_diagnosis')),
                show(cc.get('sex')),show(cc.get('diagnosis')),show(cc.get('block_id')),
                show(cc.get('tumor_percentage')),r.get('release_recommendation') or '-',r.get('summary') or ''))
            by_row[str(i)]=r
        def chosen(_evt=None):
            selection=tree.selection()
            if not selection:return
            r=by_row.get(selection[0]) or {}
            try:items=json.loads(r.get('notes_json') or '[]')
            except (ValueError,TypeError):items=[]
            if not isinstance(items,list):items=[items]
            value=(r.get('summary') or '')+'\n\n'+ '\n'.join(f'• {x}' for x in items if str(x).strip())
            old=prior_ngs.get(r.get('record_key')) or {}
            if old.get('previous_reports'):
                value+='\n\nÖNCEKİ NGS BULGULARI (GÜNCEL POZİTİFLİK DEĞİLDİR):'
                for old_report in (old.get('previous_reports') or [])[:12]:
                    chk='doğrulanmış TC' if old_report.get('identity_verified') else 'DOSYA NO · KİMLİK KONTROLÜ'
                    value+='\n- '+str(old_report.get('prior_mp') or '')+' '+str(old_report.get('sample_type') or '')+' · '+chk
                    value+=' · '+str(old_report.get('report_status') or '')
                    for pv in (old_report.get('structured_reported_variants') or [])[:12]:
                        value+='\n   · '+str(pv.get('gene') or '')+' '+str(pv.get('protein') or pv.get('cdna') or pv.get('feature') or '')
                    extract=old_report.get('historical_text_extraction') or {}
                    for pv in (extract.get('positive_variants') or [])[:12]:
                        value+='\n   · RAPOR METNİNDEN (ONAY DURUMUNA BAK): '+str(pv.get('gene') or '')+' '+str(pv.get('cdna') or pv.get('protein') or '')
                    if extract.get('negative_genes'):
                        value+='\n   · Negatif genler (yalnız geçmiş rapor): '+', '.join(extract['negative_genes'][:25])
                    markers=[str(k)+'='+str(extract.get(k)) for k in ('msi_status','msi_percent','tmb','tmb_category','hrd') if extract.get(k) is not None]
                    if markers:value+='\n   · Geçmiş biyobelirteçler: '+', '.join(markers)
                for hint in (old.get('clinical_review_prompts') or [])[:5]:
                    value+='\nKONTROL: '+str(hint)
            notes.configure(state='normal');notes.delete('1.0','end');notes.insert('1.0',value);notes.configure(state='disabled')
        tree.bind('<<TreeviewSelect>>',chosen)
        if records:
            tree.selection_set('0');chosen()


# === NGS_PRIOR_REPORT_CONTEXT_RETURN_V0467 ===
_NGS_PreviousTemplateV0467 = build_ai_review_return_template

def build_ai_review_return_template(pkg: dict) -> dict:
    out = _NGS_PreviousTemplateV0467(pkg)
    by_record = {str((p.get('technical_sample') or {}).get('record_key') or ''):
                 p.get('prior_ngs_history') or {} for p in (pkg.get('patients') or [])}
    for target in ('patient_reviews', 'sample_index'):
        for row in out.get(target) or []:
            if isinstance(row,dict):
                row['prior_ngs_history'] = by_record.get(str(row.get('record_key') or ''),{})
    out['instructions'] = list(out.get('instructions') or []) + [
        'prior_ngs_history önceki hastaya ait yerel geçmiş kayıtlarıdır; GÜNCEL örneğe pozitif/negatif varyant otomatik ekleme.',
        'identity_review_required=true veya identity_verified=false olan dosya-no eşleşmelerini doğrulanmış hasta geçmişi olarak kullanma.',
        'Önceki EGFR rapor kaydı bulunan KHDAK olgusunda yeni EGFR çağrısını, DNA kapsamını, varsa tedavi ilişkili direnç mekanizmalarını ve blok/tanı uyumunu karşılaştır.',
        'Önceki kullanıcı onaylı yerel rapor, Enlil elektronik imzalı resmi rapor olarak doğrulanmış değildir.',
        'TC, Enlil dosya no ve gerçek hasta adı isteme, üretme veya geri dönüş JSON’una yazma.'
    ]
    return out


# v0.4.81: typed IGV feedback and bundle sidecars are loaded by content/schema.
# Supplemental evidence resolves legacy identities; it never creates Selection.
_validate_ai_review_before_v0481 = validate_ai_review_return


def validate_ai_review_return(data: dict):
    from igv_workflow_v0481 import REVIEW_SCHEMAS, validate_review_document
    if isinstance(data, dict) and data.get('schema') in REVIEW_SCHEMAS:
        return validate_review_document(data), []
    return _validate_ai_review_before_v0481(data)


def _json_no_duplicate_keys_v0481(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('JSON içinde yinelenen anahtar: ' + key)
            result[key] = value
        return result
    return json.loads(text, object_pairs_hook=pairs,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError('Geçersiz JSON sayısı: ' + value)))


def load_ai_review_return(path: Path):
    from igv_workflow_v0481 import REVIEW_SCHEMAS
    path = Path(path)
    companions = {}
    preferred = ('AI_REVIEW_RETURN.JSON', 'IGV_REQUEST_V2.JSON', 'IGV_REQUEST.JSON', 'AI_IGV_REVIEW.JSON')
    if path.suffix.lower() == '.zip':
        with zipfile.ZipFile(path, 'r') as z:
            names = z.namelist()
            member = None
            for wanted in preferred:
                found = [n for n in names if Path(n).name.upper() == wanted]
                if len(found) > 1:
                    raise ValueError('ZIP içinde aynı adlı birden fazla belge var: ' + wanted)
                if found:
                    member = found[0]
                    break
            if not member:
                raise ValueError('ZIP içinde desteklenen YZ dönüşü / IGV isteği / IGV değerlendirmesi bulunamadı.')
            def read_member(name):
                if z.getinfo(name).file_size > 32 * 1024 * 1024:
                    raise ValueError('JSON belgesi 32 MB sınırını aşıyor.')
                return _json_no_duplicate_keys_v0481(z.read(name).decode('utf-8-sig'))
            doc = read_member(member)
            parent = Path(member).parent
            for sidecar in ('AI_IGV_REVIEW.JSON', 'KANIT_OZETI.JSON'):
                found = [n for n in names if Path(n).name.upper() == sidecar and Path(n).parent == parent and n != member]
                if len(found) > 1:
                    raise ValueError('ZIP içindeki ek belge tekil değil: ' + sidecar)
                if found:
                    companions[sidecar] = read_member(found[0])
        source = f'{path}!{member}'
    else:
        doc = _json_no_duplicate_keys_v0481(path.read_text(encoding='utf-8-sig'))
        source = str(path)
        if isinstance(doc, dict) and doc.get('schema') in REVIEW_SCHEMAS:
            sidecar = path.with_name('KANIT_OZETI.json')
            if sidecar.is_file():
                companions['KANIT_OZETI.JSON'] = _json_no_duplicate_keys_v0481(sidecar.read_text(encoding='utf-8-sig'))
    if not isinstance(doc, dict):
        raise ValueError('Kök JSON nesne olmalıdır.')
    doc = _strip_examples(doc)
    if isinstance(doc.get('reviews'), list):
        doc['reviews'] = [row for row in doc['reviews'] if not (isinstance(row, dict) and row.get('_example_only_remove_or_replace'))]
    if doc.get('document_type') == IGV_DOCUMENT_TYPE_V2 or doc.get('schema') == IGV_REQUEST_SCHEMA_V2:
        errors, warnings = validate_igv_request_v2(doc)
        if errors:
            raise ValueError('IGV istek dosyası doğrulanamadı:\n- ' + '\n- '.join(errors))
        data = _ngs_igv_to_ai_return_v0432(doc)
        for key in ('request_set_update_policy', 'clinical_update', 'clinical_update_history', 'received_review_summary'):
            if key in doc:
                data[key] = doc[key]
        if doc.get('request_set_update_policy'):
            data['igv_request_set_update_policy'] = doc['request_set_update_policy']
    else:
        data = doc
    evidence_doc = companions.get('KANIT_OZETI.JSON') or {}
    evidence = {'igv_visual_review': evidence_doc['igv_visual_review']} if isinstance(evidence_doc, dict) and isinstance(evidence_doc.get('igv_visual_review'), dict) else {}
    if data.get('schema') in REVIEW_SCHEMAS:
        data['_igv_evidence'] = evidence
    elif isinstance(companions.get('AI_IGV_REVIEW.JSON'), dict):
        data['_igv_review_supplement'] = {'review': companions['AI_IGV_REVIEW.JSON'], 'evidence': evidence}
    errors, warnings = validate_ai_review_return(data)
    if errors:
        raise ValueError('YZ dönüş dosyası doğrulanamadı:\n- ' + '\n- '.join(errors))
    return data, source, _sha256(path)


_IGVReviewUIBefore0481 = AIReviewIGV


class AIReviewIGV(_IGVReviewUIBefore0481):
    def _build(self):
        super()._build()
        bar = ttk.Frame(self, padding=(10, 0, 10, 7))
        bar.pack(fill='x')
        ttk.Button(bar, text='Seçili IGV değerlendirmesini aç', command=self.show_igv_review_result).pack(side='left')
        ttk.Button(bar, text='IGV istek geçmişi', command=self.show_igv_request_history).pack(side='left', padx=6)
        ttk.Button(bar, text='Varyant / tümör bağlamı', command=self.show_igv_variant_tumor_context).pack(side='left', padx=6)
        self.igv_evidence_scope_var = tk.StringVar(value='IGV değerlendirmesi: henüz alınmadı')
        ttk.Label(self, textvariable=self.igv_evidence_scope_var, foreground='#475569').pack(anchor='w', padx=12, pady=(0, 7))

    def refresh(self):
        super().refresh()
        if not hasattr(self, 'igv_evidence_scope_var'):
            return
        try:
            from igv_workflow_v0481 import igv_scope_summary
            summary = igv_scope_summary(self.db, session_id=self._latest_session_id())
            self.igv_evidence_scope_var.set(summary['scope_note'])
        except Exception as exc:
            self.igv_evidence_scope_var.set('IGV değerlendirme kapsamı okunamadı: ' + str(exc))

    def show_igv_variant_tumor_context(self):
        row = self._selected()
        if not row:
            return
        lines = _igv_variant_tumor_context_lines(igv_variant_tumor_context(self.db, row))
        win = tk.Toplevel(self); win.title('Varyant / tümör bağlamı'); win.geometry('850x660')
        outer = ttk.Frame(win, padding=12); outer.pack(fill='both', expand=True)
        text = tk.Text(outer, wrap='word', font=('Segoe UI', 10), padx=10, pady=10)
        scroll = ttk.Scrollbar(outer, orient='vertical', command=text.yview)
        text.configure(yscrollcommand=scroll.set); scroll.pack(side='right', fill='y'); text.pack(fill='both', expand=True)
        text.insert('1.0', '\n'.join(lines)); text.configure(state='disabled')

    def show_igv_review_result(self):
        from igv_workflow_v0481 import ensure_igv_workflow_schema, current_igv_review, igv_scope_summary
        row = self._selected()
        if not row:
            return
        ensure_igv_workflow_schema(self.db)
        review = current_igv_review(self.db, row)
        if not review:
            messagebox.showinfo('IGV değerlendirmesi', 'Bu isteğin güncel revizyonuna bağlı IGV değerlendirmesi henüz alınmadı.')
            return
        evidence = {'supports_call': 'Çağrıyı destekliyor', 'does_not_support': 'Çağrıyı desteklemiyor', 'indeterminate': 'Belirsiz'}
        confidence = {'high': 'Yüksek', 'medium': 'Orta', 'low': 'Düşük', 'indeterminate': 'Belirsiz'}
        state = {'complete': 'Tam', 'partial': 'Kısmi', 'missing': 'Görüntü yok', 'unknown': 'Kaynakta belirtilmemiş', 'pending': 'Bekliyor', 'error': 'Hatalı'}
        raw = review.get('raw') or {}
        capture = raw.get('source_capture') or {}
        lines = [f"{row.get('patient_id') or ''} · {row.get('gene') or ''} {row.get('change') or ''}", '',
                 'Kaynak paketteki çekim: ' + state.get(review['capture_status'], review['capture_status']),
                 'Teknik görsel kanıt: ' + evidence.get(review['evidence'], review['evidence']),
                 'Güven: ' + confidence.get(review['confidence'], review['confidence']),
                 'Manuel inceleme: ' + ('Gerekli' if review['manual_review'] else 'YZ tarafından ek inceleme istenmemiş'),
                 'Klinik rapor onayı: Bu teknik değerlendirmeyle verilmedi.', '']
        if capture.get('available_zooms'):
            lines.append('Kaynakta bulunan görüntüler: ' + ', '.join(capture['available_zooms']))
        if capture.get('missing_zooms'):
            lines.append('Kaynakta eksik görüntüler: ' + ', '.join(capture['missing_zooms']))
        metrics=review.get('source_metrics') or {}
        source_ad=metrics.get('AD',metrics.get('ad'));source_dp=metrics.get('DP',metrics.get('dp'))
        source_vaf=metrics.get('AF_percent',metrics.get('vaf_percent'))
        if source_vaf is None and isinstance(metrics.get('AF_fraction'),(int,float)):
            source_vaf=metrics['AF_fraction']*100
        measured=review.get('measured_metrics') or {}
        values=measured.get('values') or measured
        measurement_text='Bağımsız AD/DP/VAF ölçümü yapılmamış.'
        if measured.get('status') in ('ok','measured'):
            measurement_text=f"Bağımsız ölçüm: DP {values.get('dp')} · alternatif AD {values.get('alt_ad')} · VAF {values.get('vaf')} (kaynak ölçeğinde)."
        lines += ['', (raw.get('review') or {}).get('note') or '', '',
                  'Kaynak çağrı ölçümleri (IGV görüntüsünden yeniden ölçülmedi):',
                  f"VAF: {'%'+str(round(source_vaf,3)) if isinstance(source_vaf,(int,float)) else 'belirtilmemiş'} · AD: {source_ad if source_ad is not None else 'belirtilmemiş'} · DP: {source_dp if source_dp is not None else 'belirtilmemiş'}",
                  '', measurement_text]
        if (review.get('linkage') or {}).get('origin', '').startswith('reconstructed'):
            lines += ['', 'Eski paket bağlantısı tam varyant/çalışma bilgileriyle tekil olarak yeniden kuruldu; istek revizyonu özgün çekimde açıkça yer almıyordu.']
        lines += ['', ''] + _igv_variant_tumor_context_lines(igv_variant_tumor_context(self.db, row))
        win = tk.Toplevel(self); win.title('IGV teknik değerlendirmesi'); win.geometry('850x640')
        outer = ttk.Frame(win, padding=12); outer.pack(fill='both', expand=True)
        text = tk.Text(outer, wrap='word', font=('Segoe UI', 10), padx=10, pady=10)
        scroll = ttk.Scrollbar(outer, orient='vertical', command=text.yview)
        text.configure(yscrollcommand=scroll.set); scroll.pack(side='right', fill='y'); text.pack(fill='both', expand=True)
        text.insert('1.0', '\n'.join(lines)); text.configure(state='disabled')

    def show_igv_request_history(self):
        from igv_workflow_v0481 import ensure_igv_workflow_schema
        row=self._selected()
        if not row:return
        ensure_igv_workflow_schema(self.db)
        history=[dict(r) for r in self.db.cx.execute('SELECT * FROM igv_request_history_v0481 WHERE queue_id=? ORDER BY id DESC',(row['id'],)).fetchall()]
        if not history:
            messagebox.showinfo('IGV istek geçmişi','Bu varyant için önceki görüntü isteği kaydı yok. Klinik bilgi güncellemeleri yeni görüntü isteği oluşturmaz.')
            return
        win=tk.Toplevel(self);win.title('IGV istek geçmişi');win.geometry('860x480')
        outer=ttk.Frame(win,padding=12);outer.pack(fill='both',expand=True)
        ttk.Label(outer,text=f"{row.get('patient_id') or ''} · {row.get('gene') or ''} {row.get('change') or ''} · Güncel görüntü isteği revizyonu {row.get('request_revision') or '?'}").pack(anchor='w',pady=(0,8))
        tree=ttk.Treeview(outer,columns=('rev','state','date','manual'),show='headings',height=8)
        for key,label,width in (('rev','Önceki revizyon',110),('state','Çekim durumu',130),('date','Arşiv zamanı',190),('manual','O istekteki patolog kararı',270)):
            tree.heading(key,text=label);tree.column(key,width=width)
        tree.pack(fill='both',expand=True)
        values={}
        for item in history:
            snap=json.loads(item['queue_snapshot_json']);values[str(item['id'])]=snap
            tree.insert('', 'end',iid=str(item['id']),values=(item['request_revision'],snap.get('status') or '',item['archived_at'],snap.get('manual_decision') or 'Yok'))
        note=tk.StringVar(value='Önceki görüntüler ve kararlar kendi istekleriyle korunur; güncel isteğin yerine geçmez.')
        ttk.Label(outer,textvariable=note,wraplength=800).pack(anchor='w',pady=8)
        def open_evidence():
            chosen=tree.selection()
            if not chosen:return
            snapshot=values[chosen[0]]
            evidence_path=snapshot.get('capture_meta_path') or snapshot.get('screenshot_path')
            if not evidence_path or not Path(evidence_path).is_file():
                note.set('Önceki dosya kaydı korunuyor; bu bilgisayarda dosya bulunamadı.');return
            _reveal_file_in_explorer_v0437(Path(evidence_path))
        ttk.Button(outer,text='Seçili eski görüntü dosyasını göster',command=open_evidence).pack(anchor='w')

# BEGIN NGS_VARIANT_EVIDENCE_v1_1_install_ai_review
from variant_evidence_integration import install_ai_review as _install_ngs_variant_evidence
_install_ngs_variant_evidence(globals())
# END NGS_VARIANT_EVIDENCE_v1_1_install_ai_review
