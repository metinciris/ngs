"""Attach a strict web-source-completeness report to the AI bundle in one ZIP pass.

No NGS result is promoted by this module. This wraps the existing packager
AFTER the optional variant-evidence integration has been installed.
"""
from __future__ import annotations
from contextvars import ContextVar
from functools import wraps
from pathlib import Path
import json

_ACTIVE=ContextVar('ngs_recovery_ai_active',default=None)


def install_recovery_bundle(ns):
    if ns.get('_ngs_web_recovery_bundle_v12_installed'):return
    old_write=ns['write_pool_ai_bundle']
    old_zip=ns['_write_bundle_archive']

    @wraps(old_write)
    def write_with_recovery(base,db,cfg,*args,**kwargs):
        pool=str(kwargs.get('pool_group') or (args[0] if args else '')).strip()
        scope=str(kwargs.get('sample_scope') or 'DNA').strip().upper()
        token=_ACTIVE.set((Path(base),db,pool,scope))
        try:return old_write(base,db,cfg,*args,**kwargs)
        finally:_ACTIVE.reset(token)

    @wraps(old_zip)
    def zip_with_recovery(outdir,zip_path):
        source=_ACTIVE.get()
        if source is not None:
            base,db,pool,scope=source
            out=Path(outdir)
            try:
                from web_recovery import RecoveryStore,manual_guide
                from variant_evidence_privacy import Masker
                privacy_reader=ns.get('_privacy_sensitive_values')
                try:
                    names,tcs=privacy_reader(db,[dict(x) for x in db.samples()]) if callable(privacy_reader) else (None,None)
                except Exception:
                    names,tcs=None,None
                masker=Masker(names or [],tcs or [])
                privacy_ok=names is not None
                store=RecoveryStore(base)
                try:
                    keys=[]
                    group=db.pool_group_name(pool)
                    for s in db.samples():
                        st=str(s['sample_type'] or '').upper()
                        if db.pool_group_name(s['pool_name'])!=group:continue
                        if scope=='DNA' and st not in ('DNA','DNA+RNA'):continue
                        if scope=='RNA' and st not in ('RNA','DNA+RNA'):continue
                        keys.append(s['record_key'])
                    report=store.report(db,keys)
                finally:store.close()
                report.pop('local_report_path',None)
                report['matching_sample_count']=len(keys)
                for item in report.get('entries') or []:
                    for field in ('pool','mp','last_reason','source_reason'):
                        if not privacy_ok and field.endswith('reason'):
                            item[field]='Kaynak durumu: ayrıntı yalnız yerel web kurtarma panelinde.'
                        else:item[field]=masker.text(item.get(field) or '')
                    if item['state']!='verified':item['guide']=manual_guide(item['kind'])
                # No patient names, TC or external URLs; MP and technical UUID
                # remain (as in other authorized NGS clinical bundle content).
                (out/'WEB_EKSIK_KAYNAK_RAPORU.json').write_text(
                    json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
                lines=['# WEB KAYNAK TAMLIK RAPORU',
                    f"POOL: {pool} / {scope} | doğrulanmış: {report['verified']} | eksik: {report['missing']}",
                    'Eksik kaynak klinik olarak NEGATİF değildir. Selection doğrulanmadan rapora seçili varyant kesinleştirilmez.',
                    'Platformun geçici hataları, eksik indirmeler ve manuel doğrulama ayrı kaydedilir.']
                for r in report.get('entries') or []:
                    if r['state']=='verified':continue
                    lines.append(f"- {r['mp']} [{r['kind']}]: {r['state']} · {r['source_reason']} · {r['guide']}")
                (out/'WEB_EKSIK_KAYNAK_RAPORU.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
                prompt=out/'POOL_AI_PROMPT.md'
                if prompt.is_file():
                    with prompt.open('a',encoding='utf-8') as f:
                        f.write('\n\n## WEB KAYNAK TAMLIK DENETİMİ\n')
                        f.write(f"WEB_EKSIK_KAYNAK_RAPORU.json: {report['missing']} açık / "
                                f"{report['total']} beklenen kaynak görevi.\n")
                        f.write('Eksik Selection/RNA/CNV/SNV/QC kaynakları negatif veya raporlanabilir sayılamaz. '
                                'Her eksik teknik örneği kaynak ve manuel tamamlama gereğiyle birlikte açıkça bildir.\n')
                man=out/'MANIFEST.json'
                if man.is_file():
                    manifest=json.loads(man.read_text(encoding='utf-8'))
                    manifest['web_recovery_schema']='ngs_web_recovery_v1'
                    manifest['web_missing_count']=report['missing']
                    manifest['web_manual_count']=report['manual']
                    manifest['web_sources_all_verified']=bool(keys) and report['missing']==0
                    man.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
            except Exception as exc:
                (out/'WEB_EKSIK_KAYNAK_RAPORU.json').write_text(json.dumps({
                    'schema':'ngs_web_recovery_v1','status':'audit_unavailable',
                    'all_sources_verified':False,'error_type':type(exc).__name__,
                    'instruction':'Kaynak tamlık kontrolü yapılamadı; eksiksiz rapor varsaymayın.'},
                    ensure_ascii=False,indent=2),encoding='utf-8')
                (out/'WEB_EKSIK_KAYNAK_RAPORU.md').write_text(
                    '# WEB KAYNAK TAMLIK DENETİMİ YAPILAMADI\n\n'
                    'Kaynak tamlığı bilinmiyor; Selection ve SNV/CNV/QC/Fusion eksiksiz sayılmamalı. '
                    'Yerel Web Eksikleri / Manuel sekmesini kontrol edin.\n',encoding='utf-8')
                prompt=out/'POOL_AI_PROMPT.md'
                if prompt.is_file():
                    with prompt.open('a',encoding='utf-8') as f:
                        f.write('\n\nWEB KAYNAK UYARISI: Otomatik kaynak tamlık denetimi başarısız; '
                                'kaynaklar doğrulanana kadar eksiksiz veri veya negatiflik varsaymayın.\n')
                man=out/'MANIFEST.json'
                if man.is_file():
                    manifest=json.loads(man.read_text(encoding='utf-8'))
                    manifest['web_sources_all_verified']=False
                    manifest['web_recovery_audit_failed']=True
                    man.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
            # The previously installed variant-evidence ZIP hook recomputes the
            # manifest after our sidecars, so one compression pass is preserved.
        return old_zip(outdir,zip_path)

    ns['write_pool_ai_bundle']=write_with_recovery
    ns['_write_bundle_archive']=zip_with_recovery
    ns['_ngs_web_recovery_bundle_v12_installed']=True
