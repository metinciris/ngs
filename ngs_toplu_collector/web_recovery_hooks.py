"""Playwright-thread safe, additive recovery hooks for existing BulkCollector."""
from __future__ import annotations
import shutil
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse
from web_recovery import (RecoveryStore, manual_guide, validate_manual,
                          import_manual_source, classify_failure)


def _recovery_scan(self, record_keys=None):
    store=RecoveryStore(self.base)
    try:
        if record_keys is None:
            record_keys=self.db.latest_record_keys()
        return store.report(self.db,record_keys)
    finally:store.close()


def _retry_source(self,rk,kind,force=False):
    if kind=='selection_dna':
        x=self.scan_report_selections(max_samples=1,record_keys=[rk])
        return 'waiting' if x.get('incomplete') or x.get('run_waiting') else 'review' if x.get('review') or x.get('error') or not x.get('processed') else 'done'
    if kind=='selection_rna':
        x=self.scan_fusion_selections(max_samples=1,record_keys=[rk])
        return 'waiting' if x.get('incomplete') or x.get('run_waiting') else 'review' if x.get('review') or x.get('error') or not x.get('processed') else 'done'
    sample=self.db.get_sample(rk)
    if not sample:return 'review'
    st=str(sample.get('sample_type') or '').upper()
    outcome=self.run_queue(dna=st in ('DNA','DNA+RNA'),rna=st in ('RNA','DNA+RNA'),
        only_missing=False,record_keys=[rk],task_types=[kind],preserve_previous_done=True,force_navigation=force)
    if outcome.get('status')=='stopped':return 'stopped'
    if self.db.task_status(rk,kind)=='done':return 'done'
    if outcome.get('waiting') or outcome.get('blocked'):return 'waiting'
    return 'review'


def _recovery_retry(self,record_key=None,kind=None,limit=4,force=False):
    """One bounded pass; manual statuses aren't silently retried in the background."""
    keys=[record_key] if record_key else self.db.latest_record_keys()
    store=RecoveryStore(self.base)
    attempted=success=0
    try:
        store.synchronize(self.db,keys)
        if record_key and kind:
            work=[x for x in store.rows(keys) if x['record_key']==record_key and x['kind']==kind and x['state']!='verified']
            if not force:work=[x for x in work if x['state'] in ('retry','waiting') and x['next_attempt_epoch']<=time.time()]
        else:work=store.due(keys,limit=limit,force=force)
        # One site refresh may repair vanished/changed source links. Do not repeatedly
        # navigate RUNS for every row; never scan historic pages on each idle tick.
        if any('bağlantı' in (x['source_reason'] or '').lower() for x in work):
            try:
                self.log('WEB KURTARMA: eksik bağlantılar için RUNS sayfası yeniden taranıyor.')
                self.scan_runs(max_pages=2,selection_backfill=False)
                store.synchronize(self.db,keys)
            except Exception as exc:self.log('WEB KURTARMA: RUNS yenilemesi başarısız; eski kaynaklar korundu: '+str(type(exc).__name__))
        for job in work:
            if self._stop_now() or self._stop_after():break
            rk=job['record_key'];tp=job['kind']
            # Fusion Selection was an opt-in pilot in v0.4.83. Do not activate
            # it silently via the new background retry supervisor.
            if tp=='selection_rna' and not force and not self.cfg.get('web_recovery_auto_rna_selection',False):
                store.set_manual(rk,tp,'RNA Fusion Selection otomatik pilotu kapalı. Kurtarma ekranında açık kullanıcı onayıyla Yeniden Dene.')
                continue
            refreshed=next((x for x in store.rows([rk]) if x['kind']==tp),None)
            if refreshed is None or refreshed['state']=='verified':continue
            if not force and refreshed['state']=='manual':continue
            # Never bypass a known upstream FAILED run through retries.
            if refreshed['state']=='blocked':continue
            if not force and refreshed['next_attempt_epoch']>time.time():continue
            self.status_cb(f'WEB eksiklerini tamamlama: {job["mp"]} / {tp.upper()}')
            self.log(f'WEB KURTARMA {job["mp"]} / {tp.upper()}: güvenli yeniden deneme')
            try:
                outcome=_retry_source(self,rk,tp,force=force)
                # Re-read the actual collector result BEFORE interpreting its
                # high-level 'processed/done' flag. A completed website visit
                # is not a validated source file or Selection result.
                store.synchronize(self.db,[rk])
                observed=next((x for x in store.rows([rk]) if x['kind']==tp),{})
                if outcome=='done' and observed.get('source_state')!='verified':
                    outcome='review'
                # Use the *new* error (e.g., HTTP 429 / timeout), not a stale
                # pre-retry reason; this controls conservative backoff.
                reason=(observed.get('source_reason') or refreshed.get('source_reason')
                        or 'Kaynak henüz doğrulanmadı')
                if outcome!='stopped':store.mark_outcome(rk,tp,outcome,reason)
                store.synchronize(self.db,[rk]);attempted+=1
                final=next((x for x in store.rows([rk]) if x['kind']==tp),{})
                success+=int(final.get('state')=='verified')
                self.log(f'WEB KURTARMA {job["mp"]} / {tp.upper()}: {final.get("state",outcome).upper()}')
                if outcome=='stopped':break
            except Exception as exc:
                store.mark_outcome(rk,tp,'review',str(exc))
                self.log(f'WEB KURTARMA {job["mp"]}: kaynak tekrar alınamadı ({type(exc).__name__}); yerel veri korunuyor.')
                attempted+=1
            if self._stop_now() or self._stop_after():break
            # Server-friendly rate limit; nonblocking UI, but worker keeps serial ownership.
            time.sleep(max(0.3,min(2.5,float(self.cfg.get('web_recovery_step_delay_s',1.2)))))
        report=store.report(self.db,keys)
        return {'status':'stopped' if self._stop_now() or self._stop_after() else 'completed',
                'processed':attempted,'repaired':success,'remaining':report['missing'],
                'manual':report['manual'],'waiting':report['waiting'],
                'report_path':report['local_report_path']}
    finally:store.close()


def _same_origin_url(self,sample,kind):
    src=(sample.get('links') or {}).get(kind)
    if kind=='selection_dna' or kind=='selection_rna':src=(sample.get('links') or {}).get('run')
    if not src:src=(sample.get('links') or {}).get('run')
    if not src and kind=='qc':src=(sample.get('links') or {}).get('qc')
    if not src:raise ValueError('Kaynağın bağlantısı RUNS taramasında bulunamadı. Önce RUNS tarayın.')
    if kind=='qc' and str(src).startswith('RUNS_QC::'):return 'RUNS_QC::'
    url=self._abs(str(src))
    origin=urlparse(str(self.cfg.get('runs_url') or ''))
    observed=urlparse(url)
    if origin.scheme!='https' and origin.scheme!='http':raise ValueError('NGS platformunun geçerli ana adresi yok.')
    if (origin.scheme,origin.netloc)!=(observed.scheme,observed.netloc):
        raise ValueError('Web kurtarma farklı alan adına yönlendirmeyi reddetti.')
    return url


def _recovery_open_watch(self,record_key,kind,watch_seconds=180):
    """Open target in collector Chromium and observe manual Download (no global hotkeys).

    Downloads are staged and validated; without exact sample match or user
    attestation they remain MANUAL, not DONE.  This call is cancellable.
    """
    if kind not in ('run','clinical','snv','cnv','qc','fusion','selection_dna','selection_rna'):
        raise ValueError('Desteklenmeyen kurtarma adımı.')
    sample=self.db.get_sample(record_key)
    if not sample:raise ValueError('Teknik örnek bulunamadı.')
    url=_same_origin_url(self,sample,kind)
    self.open_browser()
    page=None
    store=RecoveryStore(self.base)
    count=0
    try:
        if kind=='qc' and url=='RUNS_QC::':
            page=self._open_qc_from_runs(sample)
        else:
            page=self.context.new_page()
            response=self._login_goto_v6(page, url,wait_until='domcontentloaded',timeout=int(self.cfg.get('heavy_timeout_ms',90000)))
            status=getattr(response,'status',None)
            if status in (401,403,429) or (isinstance(status,int) and status>=500):
                store.set_manual(record_key,kind,f'WEB HTTP {status}; sayfada oturum veya sunucu durumunu kontrol edin.')
                raise RuntimeError(f'Kaynak HTTP {status}; manuel giriş veya sunucu yanıtı bekleniyor.')
        observed_url=str(page.url or '')
        if ('login' in urlparse(observed_url).path.lower() or
                'signin' in urlparse(observed_url).path.lower() or
                'sign-in' in urlparse(observed_url).path.lower() or
                urlparse(observed_url).path.lower().endswith('/auth')):
            self.log('WEB MANUEL: giriş ekranı açıldı. Bu tarayıcıda oturum açabilirsiniz; kaynak hâlâ tamamlanmış sayılmayacak.')
        self.log(f'WEB MANUEL: {sample.get("patient_id")} {kind.upper()} sayfası açıldı. '+manual_guide(kind))
        if kind.startswith('selection_'):
            self.log('WEB MANUEL: Selection için burada buton değiştirmeyin; sayfa hazır olunca Selection Yeniden Dene kullanın.')
            store.set_manual(record_key,kind,'Web sayfası kullanıcı tarafından açıldı. Selection otomatik doğrulaması ayrıca gereklidir.')
            return {'status':'completed','opened':True,'downloaded':0,'guide':manual_guide(kind)}
        holder=[]
        page.on('download',lambda dl:holder.append(dl))
        # Some GenNext controls open their exporter in a same-origin popup.
        # Register only on popups spawned by THIS recovery page, never on the
        # user's other Chromium tabs (let alone another desktop browser).
        def _watch_popup(p):
            try:p.on('download',lambda dl:holder.append(dl))
            except Exception:pass
        page.on('popup',_watch_popup)
        seconds=max(5,min(480,int(watch_seconds)))
        self.status_cb(f'WEB MANUEL İZLEME · {sample.get("patient_id")} {kind.upper()} · indirmeyi başlatın')
        start=time.time();last_notice=start;button_notified=False
        while time.time()-start<seconds:
            if self._stop_now() or self._stop_after():break
            page.wait_for_timeout(250)
            if holder:break
            if time.time()-last_notice>=8:
                last_notice=time.time()
                # DOM-only guidance; no page-body text, clipboard, shortcuts,
                # network payload or patient identity is recorded.
                try:
                    if kind in ('snv','cnv','fusion'):
                        ready=page.get_by_text('Download as TSV',exact=False).count()>0
                    elif kind=='qc':
                        ready=page.locator('#mqc-dl-plot-btn').count()>0
                    else:ready=False
                    if ready and not button_notified:
                        button_notified=True
                        self.log('WEB MANUEL: İndirme denetimi sayfada göründü; şimdi ilgili indirme düğmesine basabilirsiniz.')
                    elif not ready and not button_notified and time.time()-start>=24:
                        self.status_cb(f'WEB MANUEL: {kind.upper()} indirme denetimi henüz görünmüyor; sayfa yüklenmesini bekleyin.')
                except Exception:pass
        if not holder:
            store.set_manual(record_key,kind,'Kullanıcı sayfayı açtı; süre içinde indirme algılanmadı. Dosyayı elle ekleyin veya yeniden izleyin.')
            return {'status':'stopped' if self._stop_now() or self._stop_after() else 'completed',
                    'opened':True,'downloaded':0,'manual':True,'guide':manual_guide(kind)}
        dl=holder[0]
        name=Path(str(dl.suggested_filename or '')).name
        suffix=Path(name).suffix.lower()
        if suffix not in ('.zip','.tsv','.csv','.json'):
            raise ValueError('İndirilen dosya türü beklenen kaynak formatlarından değil.')
        stage=self.base/'OUTPUT'/'WEB_RECOVERY_DOWNLOADS';stage.mkdir(parents=True,exist_ok=True)
        staged=stage/f'{uuid.uuid4().hex}_{kind}{suffix}'
        dl.save_as(str(staged))
        ok,reason,matched=validate_manual(staged,kind,sample.get('patient_id') or '')
        if not ok:
            store.set_manual(record_key,kind,'İndirilen dosya doğrulanamadı: '+reason)
            return {'status':'completed','opened':True,'downloaded':1,'manual':True,
                    'file':str(staged),'reason':reason}
        if matched:
            info=import_manual_source(self.base,self.db,record_key,kind,staged)
            count+=1;staged.unlink(missing_ok=True)
            return {'status':'completed','opened':True,'downloaded':count,'repaired':1,'reason':info['message']}
        store.set_manual(record_key,kind,'Dosya yapı olarak uygun fakat örnek kimliği doğrulanmalı. Kurtarma sekmesinden Dosya Ekle ile eşleştirin: '+str(staged))
        self.log('WEB MANUEL: dosya kaydedildi; MP/Run eşleştirmesi için kullanıcı onayı gerekli. '+str(staged))
        return {'status':'completed','opened':True,'downloaded':1,'manual':True,'file':str(staged),
                'reason':'Dosya uygun ancak otomatik örnek kimliği kanıtlanamadı.'}
    except Exception as exc:
        store.set_manual(record_key,kind,str(exc))
        raise
    finally:
        # Keep page open so user can inspect it after the finite observer stops.
        store.close()


def _complete_latest_recovery(self):
    old=_complete_latest_recovery.original
    res=old(self)
    if res.get('status')!='stopped':
        try:
            x=_recovery_scan(self,self.db.latest_record_keys())
            res['recovery_missing']=x['missing'];res['recovery_manual']=x['manual']
            res['recovery_report_path']=x['local_report_path']
            if x['missing']:
                res['all_complete']=False
                res['remaining']=max(int(res.get('remaining',0) or 0),x['missing'])
                self.log(f'WEB TAMLIK: {x["missing"]} kaynak/Selection açık; {x["manual"]} manuel, {x["waiting"]} bekleyen. Rapor: WEB_EKSIK_KAYNAK_RAPORU.json')
        except Exception as exc:self.log('WEB TAMLIK: rapor hazırlanamadı: '+type(exc).__name__)
    return res


def install(collector_class):
    if getattr(collector_class,'_recovery_v12_installed',False):return
    collector_class.recovery_scan=_recovery_scan
    collector_class.recovery_retry=_recovery_retry
    collector_class.recovery_open_watch=_recovery_open_watch
    _complete_latest_recovery.original=collector_class.complete_latest_pool
    collector_class.complete_latest_pool=_complete_latest_recovery
    collector_class._recovery_v12_installed=True

# NGS_LOGIN_V6: source navigation uses the common session gate.
