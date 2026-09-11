from __future__ import annotations
import queue
import threading
import traceback
from pathlib import Path

from db import StateDB
from collector import BulkCollector


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
        self._closed = False
        self.thread = threading.Thread(target=self._loop, name='NGSPlatformBrowserWorker', daemon=True)
        self.thread.start()

    @property
    def busy(self):
        with self._state_lock:
            return self._busy or self._reserved

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
        collector = BulkCollector(
            self.base, self.cfg, db,
            log_cb=lambda m: self.event_cb('log', m),
            status_cb=lambda m: self.event_cb('status', m),
            stop_after_event=self.stop_after,
            stop_now_event=self.stop_now,
        )
        try:
            while True:
                command, kwargs = self.commands.get()
                if command == '__close__':
                    break
                self._set_running(True)
                outcome = 'completed'
                detail = ''
                result_info = {}
                try:
                    if command == 'open_browser':
                        collector.open_browser()
                        detail = 'Tarayıcı hazır.'
                        result_info = {'browser_ready': True}
                    elif command == 'scan_runs':
                        n = collector.scan_runs(**kwargs)
                        detail = f'Runs taraması tamamlandı · {n} teknik kayıt.'
                        result_info = {'technical_records': n}
                    elif command == 'run_queue':
                        res = collector.run_queue(**kwargs) or {}
                        outcome = res.get('status', 'completed')
                        detail = (f"Kuyruk {'durduruldu' if outcome=='stopped' else 'tamamlandı'} · "
                                  f"{res.get('processed',0)} adım · ✓ {res.get('done',0)} · bekleyen {res.get('waiting',0)} · hata {res.get('error',0)}")
                        result_info = dict(res)
                        result_info['all_complete'] = outcome == 'completed' and int(res.get('error',0) or 0) == 0 and int(res.get('waiting',0) or 0) == 0 and int(res.get('blocked',0) or 0) == 0
                    elif command == 'complete_latest':
                        res = collector.complete_latest_pool() or {}
                        outcome = res.get('status','completed')
                        pool=res.get('pool_name') or 'Son POOL'
                        if outcome=='stopped':
                            detail=f"{pool} durduruldu · {res.get('processed',0)} adım"
                        elif res.get('all_complete'):
                            detail=f"{pool} TAMAM · tüm uygun adımlar ✓"
                        elif int(res.get('waiting_remaining',0) or 0)>0 and int(res.get('error_remaining',0) or 0)==0 and int(res.get('blocked_remaining',0) or 0)==0:
                            detail=f"{pool} NGS platformu bekleniyor · {res.get('waiting_remaining',0)} adım henüz hazır değil"
                        else:
                            detail=f"{pool} işlendi · kalan {res.get('remaining',0)} · hata {res.get('error_remaining',res.get('error',0))}"
                        result_info = dict(res)
                    else:
                        raise ValueError(f'Bilinmeyen worker komutu: {command}')
                    self.event_cb('refresh', None)
                    payload={'status': outcome, 'message': detail, 'command': command};payload.update(result_info or {})
                    self.event_cb('job_done', payload)
                except Exception as e:
                    outcome = 'error'
                    self.event_cb('error', f'{e}\n\n{traceback.format_exc()}')
                    self.event_cb('refresh', None)
                    self.event_cb('job_done', {'status': 'error', 'message': str(e), 'command': command})
                finally:
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
