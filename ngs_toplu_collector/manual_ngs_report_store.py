"""Manual imports of already-entered Enlil NGS report TEXT (local only).

This does not query Enlil, verify e-signatures, or replace a current case's
analysis. Documents are strictly linked to the currently selected case and
marked as user-pasted/unverified until independently checked.
"""
from __future__ import annotations
import hashlib,json,re,uuid
from datetime import datetime
from pathlib import Path
from enlil_lookup import normalize_biopsy_query

SCHEMA='ngs_user_pasted_historical_report_v1'


def path_for(base,case_family):
    digest=hashlib.sha256(str(case_family).encode('utf-8')).hexdigest()[:30]
    return Path(base)/'DATA'/'NGS_PRIOR_REPORTS'/(digest+'.json')


def load_reports(base,case_family):
    f=path_for(base,case_family)
    if not f.exists():return []
    try:
        doc=json.loads(f.read_text(encoding='utf-8'))
        if doc.get('schema')!=SCHEMA or doc.get('case_family')!=case_family or not isinstance(doc.get('reports'),list):
            raise ValueError('Prior report file schema mismatch')
        if any(not isinstance(x,dict) or not all(isinstance(x.get(k),str) for k in ('report_id','prior_mp','assay','text','imported_at')) for x in doc['reports']):
            raise ValueError('Invalid imported report')
        return doc['reports']
    except (OSError,ValueError,UnicodeError,TypeError) as exc:
        raise ValueError('Önceki rapor dosyası hasarlı; eski kayıt silinmedi.') from exc


def save_manual_report(base,case_family,prior_mp,assay,text,*,source='manual_enlil_paste_unverified',report_id_on_screen='',tumor_diagnosis=''):
    family=str(case_family or '').strip()
    if not family or len(family)>120:raise ValueError('Seçili vaka bulunamadı.')
    protocol=normalize_biopsy_query(prior_mp)
    if assay not in ('DNA','RNA'):raise ValueError('DNA veya RNA seçilmeli.')
    content=str(text or '').strip()
    if not (30<=len(content)<=60000):raise ValueError('Rapor metni 30-60000 karakter arasında olmalı.')
    if source not in ('manual_enlil_paste_unverified','enlil_open_screen_jab_read_only'):
        raise ValueError('Onaylanmamış rapor kaynak türü.')
    tumor_diagnosis=' '.join(str(tumor_diagnosis or '').split())[:160]
    report_id_on_screen=str(report_id_on_screen or '').strip()
    if report_id_on_screen and not (report_id_on_screen.isascii() and report_id_on_screen.isdigit() and 4<=len(report_id_on_screen)<=12):
        raise ValueError('Ekrandaki rapor ID biçimi geçersiz.')
    reports=load_reports(base,family)
    digest=hashlib.sha256(content.encode('utf-8')).hexdigest()
    for item in reports:
        if item['prior_mp']==protocol and item['assay']==assay and item.get('sha256')==digest:
            return item['report_id'],False
    if len(reports)>=100:raise ValueError('Bu vaka için kayıt üst sınırına ulaşıldı; otomatik silme yapılmadı.')
    report_id=uuid.uuid4().hex
    item={'report_id':report_id,'prior_mp':protocol,'assay':assay,'text':content,
          'imported_at':datetime.now().astimezone().isoformat(timespec='seconds'),
          'sha256':digest, 'source':source, 'tumor_diagnosis':tumor_diagnosis,
          'report_id_on_screen':report_id_on_screen,
          'e_signature_independently_verified':False}
    reports.insert(0,item)
    file=path_for(base,family);file.parent.mkdir(parents=True,exist_ok=True)
    tmp=file.with_name(file.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        tmp.write_text(json.dumps({'schema':SCHEMA,'case_family':family,'reports':reports},ensure_ascii=False,indent=2),encoding='utf-8')
        tmp.replace(file)
    finally:
        if tmp.exists():tmp.unlink()
    return report_id,True
