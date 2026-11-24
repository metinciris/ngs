from __future__ import annotations
import queue
import threading
import traceback
from pathlib import Path

from db import StateDB
from collector import BulkCollector
from web_recovery_hooks import install as install_web_recovery


class BrowserWorker:
    """Owns the entire Playwright lifecycle on one dedicated thread.

    The UI may enqueue only one browser job at a time. Stop requests are global for the
    active job; queued starts are not allowed to stack behind each other.
    """
    def __init__(self, base: Path, cfg: dict, event_cb):
        self.base = base
        self.cfg = cfg
        self.event_cb = event_cb
        self.commands = queue.Queue()
        self.stop_after = threading.Event()
        self.stop_now = threading.Event()
        self._state_lock = threading.Lock()
        self._busy = False
        self._reserved = False
        self._running_command = ''
        self._closed = False
        self.auto_inspector_enabled = threading.Event()
        self.auto_inspector_snapshot_requested = threading.Event()
        self._auto_settings_lock = threading.Lock()
        self._auto_inspector_mode = 'errors'
        self.thread = threading.Thread(target=self._loop, name='NGSPlatformBrowserWorker', daemon=True)
        self.thread.start()

    @property
    def busy(self):
        with self._state_lock:
            return self._busy or self._reserved

    def set_auto_inspector(self, enabled: bool, mode='errors'):
        # Safe from Tk at any time; no Playwright calls or worker job created.
        with self._auto_settings_lock:
            self._auto_inspector_mode = 'sample' if mode == 'sample' else 'errors'
            if enabled:self.auto_inspector_enabled.set()
            else:
                self.auto_inspector_enabled.clear()
                self.auto_inspector_snapshot_requested.clear()

    def request_auto_snapshot(self):
        # Captures on the NEXT collection-task boundary, not on Tk thread.
        self.auto_inspector_snapshot_requested.set()

    def auto_inspector_mode(self):
        with self._auto_settings_lock:return self._auto_inspector_mode

    @property
    def running_command(self):
        with self._state_lock:
            return self._running_command if self._busy else ''

    def _set_running(self, running: bool):
        with self._state_lock:
            self._busy = running
            if running:
                self._reserved = False
        self.event_cb('busy', running)

    def submit(self, command: str, **kwargs) -> bool:
        if self._closed:
            return False
        with self._state_lock:
            if self._busy or self._reserved:
                self.event_cb('warning', 'Bir tarayıcı işi zaten çalışıyor veya başlamak üzere. Yeni kuyruk eklenmedi.')
                return False
            self._reserved = True
        # Stop flags are cleared when a new job is intentionally submitted, before the
        # worker can start it. A stop pressed after this point will therefore be preserved.
        self.stop_after.clear()
        self.stop_now.clear()
        self.event_cb('busy', True)
        self.commands.put((command, kwargs))
        return True

    def _drain_pending(self):
        drained = 0
        while True:
            try:
                cmd, kw = self.commands.get_nowait()
            except queue.Empty:
                break
            if cmd == '__close__':
                self.commands.put((cmd, kw))
                break
            drained += 1
        if drained:
            self.event_cb('log', f'{drained} bekleyen tarayıcı işi iptal edildi.')

    def request_stop(self, immediate=False):
        if immediate:
            self.stop_now.set()
            self.stop_after.set()
            self.event_cb('status', 'HEMEN DURDURULUYOR…')
            self.event_cb('log', 'HEMEN DURDUR istendi. Bekleyen işler iptal edildi.')
        else:
            self.stop_after.set()
            self.event_cb('status', 'Durdurma istendi · mevcut adım bitince duracak')
            self.event_cb('log', 'Mevcut adım sonrası durdurma istendi. Yeni hasta/adım başlamayacak.')
        self._drain_pending()

    def shutdown(self):
        self.stop_now.set()
        self.stop_after.set()
        self._closed = True
        self._drain_pending()
        self.commands.put(('__close__', {}))

    def _loop(self):
        db = StateDB(self.base / 'DATA' / 'collector_state.sqlite3')
        install_web_recovery(BulkCollector)  # NGS_WEB_RECOVERY_v1_2
        from ngs_account_integration import install as install_ngs_accounts
        install_ngs_accounts(BulkCollector)  # NGS_ACCOUNTS_WORKER_V1
        collector = BulkCollector(
            self.base, self.cfg, db,
            log_cb=lambda m: self.event_cb('log', m),
            status_cb=lambda m: self.event_cb('status', m),
            stop_after_event=self.stop_after,
            stop_now_event=self.stop_now,
        )
        from web_auto_inspector import AutoCollectionInspector
        auto_web=AutoCollectionInspector(collector,self.auto_inspector_enabled,
                                         self.auto_inspector_mode,
                                         self.auto_inspector_snapshot_requested,
                                         log=lambda m:self.event_cb('log',m))
        collector.auto_web_task_callback=auto_web.task_completed
        from gennext_v05_auto_capture import AutoV05Structure
        v05=AutoV05Structure(self.base, self.cfg.get('runs_url',''),
            enabled=self.cfg.get('v05_auto_capture_enabled',True),
            log=lambda message:self.event_cb('log',message))
        collector.v05_capture_callback=v05.capture
        collector.v05_capture_attach=v05.attach
        try:
            while True:
                try:
                    command, kwargs = self.commands.get(timeout=1.0)
                except queue.Empty:
                    try:
                        collector._login_idle_tick_v6(self)
                    except Exception:
                        collector._ngs_login_idle_paused_v6 = True
                        self.event_cb('log', 'LOGIN V6: otomatik oturum kontrolü durdu; Tarayıcıyı Aç / Login ile yeniden deneyin.')
                    continue
                if command == '__close__':
                    break
                with self._state_lock:self._running_command = command
                self._set_running(True)
                collector._ngs_login_idle_paused_v6 = False  # NGS_LOGIN_V6
                auto_web.begin(command)
                v05.begin(command,collector.context)
                outcome = 'completed'
                failure_type = ''
                detail = ''
                result_info = {}
                try:
                    if command == 'open_browser':
                        browser_result = collector.open_browser(**kwargs) or {}
                        detail = 'Tarayıcı hazır.'
                        result_info = {'browser_ready': True}
                        if isinstance(browser_result, dict):
                            result_info.update(browser_result)
                    elif command == 'scan_runs':
                        n = collector.scan_runs(**kwargs)
                        detail = f'Runs taraması tamamlandı · {n} teknik kayıt.'
                        result_info = {'technical_records': n}
                    elif command == 'scan_archive_selections_admin':
                        from ngs_accounts import AccountStore
                        key = str(kwargs.get('account_id', ''))
                        accounts = AccountStore(self.base).read()
                        if key not in ('1', '2') or key != str(accounts.get('admin', '1')):
                            raise PermissionError('Yonetici olarak isaretlenen hesap secilmedi.')
                        collector.open_browser(account_id=key)
                        # Fixed snapshot: every technical DNA row is attempted at
                        # most once in this job; failures remain eligible later.
                        from gennext_selection_store import ensure_schema, run_phase
                        ensure_schema(collector.db)
                        eligible = [s['record_key'] for s in collector.db.samples()
                                    if str(s['sample_type'] or '').upper() in ('DNA', 'DNA+RNA')
                                    and s['run_uuid'] and s['sample_uuid']]
                        aggregate = {'requested': len(eligible), 'processed': 0,
                            'positive': 0, 'negative_snv': 0, 'incomplete': 0,
                            'review': 0, 'error': 0, 'run_waiting': 0, 'changed': 0}
                        for start in range(0, len(eligible), 100):
                            if self.stop_after.is_set() or self.stop_now.is_set():
                                break
                            result = collector.scan_report_selections(
                                max_samples=100, record_keys=eligible[start:start+100]) or {}
                            for k in aggregate:
                                if k != 'requested':
                                    aggregate[k] += int(result.get(k, 0) or 0)
                            self.event_cb('log',
                                f"YONETICI SELECTION ARSIVI: {aggregate['processed']}/{len(eligible)} teknik kayit incelendi")
                        aggregate['status'] = ('stopped' if self.stop_after.is_set() or self.stop_now.is_set()
                            else 'completed')
                        outcome = aggregate['status']
                        detail = (f"Yoneticinin Selection arsivi: {aggregate['processed']}/{len(eligible)} incelendi; "
                                  f"secimli {aggregate['positive']}, secim yok {aggregate['negative_snv']}, "
                                  f"inceleme {aggregate['review']}, hata {aggregate['error']}")
                        result_info = aggregate
                    elif command == 'scan_report_selections':
                        res = collector.scan_report_selections(**kwargs) or {}
                        outcome = res.get('status','completed')
                        detail = (f"GenNext Selection · {res.get('processed',0)} incelendi · "
                                  f"pozitif {res.get('positive',0)} · SNV/indel secim yok {res.get('negative_snv',0)} · "
                                  f"analiz suruyor {res.get('incomplete',0)} · kontrol {res.get('review',0)} · hata {res.get('error',0)}")
                        result_info=dict(res)
                    elif command == 'scan_fusion_selections':
                        res = collector.scan_fusion_selections(**kwargs) or {}
                        outcome = res.get('status','completed')
                        detail = (f"GenNext RNA Fusion Selection · {res.get('processed',0)} incelendi · "
                                  f"rapora secili {res.get('positive_fusion',0)} · "
                                  f"secim yok {res.get('no_fusion_selection',0)} · "
                                  f"kontrol {res.get('review',0)} · hata {res.get('error',0)}")
                        result_info=dict(res)
                    elif command == 'run_queue':
                        res = collector.run_queue(**kwargs) or {}
                        outcome = res.get('status', 'completed')
                        detail = (f"Kuyruk {'durduruldu' if outcome=='stopped' else 'tamamlandı'} · "
                                  f"{res.get('processed',0)} adım · ✓ {res.get('done',0)} · kontrol {res.get('review',0)} · bekleyen {res.get('waiting',0)} · hata {res.get('error',0)}")
                        result_info = dict(res)
                        result_info['all_complete'] = outcome == 'completed' and int(res.get('error',0) or 0) == 0 and int(res.get('review',0) or 0) == 0 and int(res.get('waiting',0) or 0) == 0 and int(res.get('blocked',0) or 0) == 0
                    elif command == 'complete_latest':
                        res = collector.complete_latest_pool() or {}
                        outcome = res.get('status','completed')
                        pool=res.get('pool_name') or 'Son POOL'
                        if outcome=='stopped':
                            detail=f"{pool} durduruldu · {res.get('processed',0)} adım"
                        elif res.get('all_complete'):
                            detail=f"{pool} kaynak ve Selection kontrolleri tamam"
                        elif int(res.get('selection_unresolved',0) or 0)>0:
                            detail=f"{pool} · {res.get('selection_unresolved',0)} Selection kontrol/bekleme acik; sonuc negatif sayilmaz"
                        elif int(res.get('waiting_remaining',0) or 0)>0 and int(res.get('error_remaining',0) or 0)==0 and int(res.get('blocked_remaining',0) or 0)==0:
                            detail=f"{pool} · {res.get('waiting_remaining',0)} sayfa/çıktı henüz hazır değil; hazır olanlar alındı"
                        else:
                            detail=f"{pool} işlendi · kalan {res.get('remaining',0)} · kontrol {res.get('review_remaining',res.get('review',0))} · hata {res.get('error_remaining',res.get('error',0))}"
                        result_info = dict(res)
                    elif command in ('web_inspect_all','web_record_session'):
                        res=collector.inspect_web_interface(
                           seconds=int(kwargs.get('seconds',90)) if command=='web_record_session' else 0) or {}
                        detail=(f"WEB yapisal koklayici: {res.get('pages',0)} goruntu · "
                                f"{res.get('issues',0)} uyari · {res.get('events',0)} olay")
                        result_info=dict(res)
                        if res.get('stopped'):outcome='stopped'
                    elif command == 'sniff_browser':
                        res=collector.sniff_browser(**kwargs) or {}
                        detail='Tarayici koklayici: '+('Fusion sayfasi' if res.get('page_type')=='fusion' else 'RUNS/other sayfasi')+' yapisi kaydedildi.'
                        result_info=dict(res)
                    elif command == 'recovery_retry':
                        res=collector.recovery_retry(**kwargs) or {}
                        outcome=res.get('status','completed')
                        detail=(f"WEB eksikleri: {res.get('processed',0)} deneme · "
                                f"{res.get('repaired',0)} doğrulandı · "
                                f"{res.get('remaining',0)} açık · manuel {res.get('manual',0)}")
                        result_info=dict(res)
                    elif command == 'recovery_open_watch':
                        res=collector.recovery_open_watch(**kwargs) or {}
                        outcome=res.get('status','completed')
                        detail=("WEB manuel: kaynak sayfası açık; "
                                +("dosya indirildi, kimlik kontrolü gerekli" if res.get('manual')
                                  else "işlem tamamlandı / takip kaydedildi"))
                        result_info=dict(res)
                    elif command == 'igv_open':
                        res=collector.open_igv(**kwargs) or {}
                        detail=f"IGV açıldı · {res.get('igv_region') or ''}"
                        result_info=dict(res)
                    elif command == 'igv_capture':
                        res=collector.capture_igv(**kwargs) or {}
                        detail=f"IGV görüntüleri kaydedildi · {res.get('region') or res.get('igv_region') or ''} · {'4/4 READY' if res.get('ai_ready') else res.get('capture_status','PARTIAL').upper()}"
                        result_info=dict(res)
                    elif command == 'igv_batch_capture':
                        res=collector.capture_igv_batch(**kwargs) or {}
                        # v0.4.35: propagate collector stopped status for Web IGV batch
                        outcome=res.get('status','completed')
                        detail=(f"IGV 4-görüntü hazırlama · {res.get('processed',0)}/{res.get('requested',0)} varyant · "
                                f"READY {res.get('ai_ready',0)} · PARTIAL {res.get('review',0)} · hata {res.get('error',0)}")
                        result_info=dict(res)
                    else:
                        raise ValueError(f'Bilinmeyen worker komutu: {command}')
                    auto_web.record_job_outcome(outcome,result_info)
                    self.event_cb('refresh', None)
                    payload={'status': outcome, 'message': detail, 'command': command};payload.update(result_info or {})
                    self.event_cb('job_done', payload)
                except Exception as e:
                    failure_type = type(e).__name__
                    if getattr(e, 'ngs_login_required', False):
                        outcome = 'waiting'
                        auto_web.record_job_outcome('waiting', {})
                        self.event_cb('log', str(e))
                        self.event_cb('status', str(e))
                        self.event_cb('refresh', None)
                        self.event_cb('job_done', {'status': 'waiting', 'message': str(e), 'command': command, 'browser_ready': collector.context is not None, 'auth_verified': False, 'login_required': True})
                    elif getattr(e, 'ngs_login_cancelled', False):
                        outcome = 'stopped'
                        auto_web.record_job_outcome('stopped', {})
                        self.event_cb('job_done', {'status': 'stopped', 'message': str(e), 'command': command})
                    else:
                        # v0.4.84: late GenNext auth handoff is recoverable and should not
                        # create a blocking popup after the browser has actually logged in.
                        if command == 'open_browser' and 'GenNext Sign In' in str(e):
                            outcome = 'waiting'
                            auto_web.record_job_outcome('waiting',{})
                            self.event_cb('log', 'GenNext giriş yönlendirmesi gecikti; tarayıcı açık tutuldu. Login tamamlanınca RUNS taraması doğrulayacak.')
                            self.event_cb('refresh', None)
                            self.event_cb('job_done', {'status': 'waiting', 'message': 'GenNext giriş yönlendirmesi bekleniyor; bloklayıcı popup gösterilmedi.', 'command': command, 'browser_ready': True})
                        else:
                            outcome = 'error'
                            auto_web.record_job_outcome('error',{})
                            self.event_cb('error', f'{e}\n\n{traceback.format_exc()}')
                            self.event_cb('refresh', None)
                            self.event_cb('job_done', {'status': 'error', 'message': str(e),
                                'error_type': failure_type, 'command': command})
                finally:
                    # Also close scanner-owned tabs on unexpected errors, e.g.
                    # a TimeoutError before scan_fusion_selections can return.
                    # This touches only pages explicitly registered by the
                    # Fusion scanner, NEVER the user's manual browser tabs.
                    try:
                        collector.cleanup_auto_fusion_tabs()
                    except Exception:
                        pass
                    api_map=None
                    try:
                        api_map=collector.flush_api_shadow()
                        if api_map and command in ('scan_runs','complete_latest_pool',
                                'scan_report_selections','scan_fusion_selections','run_queue',
                                'open_browser'):
                            self.event_cb('log','GENNEXT API V3: otomatik guvenli harita kaydedildi: '+api_map.name)
                    except Exception:
                        pass
                    try:
                        captured=v05.finish(api_map=api_map,outcome=outcome,failure_type=failure_type)
                        if captured:
                            self.event_cb('log','GENNEXT V05: otomatik yapi paketi: '+captured.name)
                    except Exception:
                        self.event_cb('log','GENNEXT V05: yapi paketi olusturulamadi; hasta verileri korunuyor.')
                    try:
                        diag=auto_web.finish()
                        if diag:
                            self.event_cb('auto_web_result',diag)
                    except Exception:
                        self.event_cb('log','WEB AUTO: Tanı oturumu kapatılamadı; NGS toplama sonuçları korunur.')
                    with self._state_lock:self._running_command = ''
                    self._set_running(False)
        finally:
            try:
                collector.close()
            except Exception:
                pass
            try:
                db.cx.close()
            except Exception:
                pass

# === NGS_WEB_IGV_STOP_CONTROL_V0435 ===
_ngs_browser_submit_v0435_prev = BrowserWorker.submit


def _ngs_browser_submit_v0435(self, command: str, **kwargs) -> bool:
    if command == 'igv_stop_now':
        self.request_stop(True); return True
    if command == 'igv_stop_after':
        self.request_stop(False); return True
    return _ngs_browser_submit_v0435_prev(self, command, **kwargs)

BrowserWorker.submit = _ngs_browser_submit_v0435
