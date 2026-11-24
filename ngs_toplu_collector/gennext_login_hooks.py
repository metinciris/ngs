"""NGS login V6 integration. All Playwright calls stay on BrowserWorker's thread."""
from __future__ import annotations

import functools
import json
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

from gennext_login import LoginCancelled, LoginRequired, ensure_session, needs_login

TAG = "NGS_LOGIN_V6"
AUTH_PATHS = ("/auth", "/login", "/signin", "/sign-in")
RUNS_TABLE = 'table[class*="RunsTableNew-module__runsTable__"]'


def _same_origin(url, runs):
    try:
        a, b = urlparse(str(url)), urlparse(str(runs))
        a_port = a.port or (443 if a.scheme == "https" else 80)
        b_port = b.port or (443 if b.scheme == "https" else 80)
        return (a.scheme in ("http", "https") and not a.username and not a.password
                and (a.scheme, a.hostname, a_port) == (b.scheme, b.hostname, b_port))
    except (TypeError, ValueError):
        return False


def _auth_path(url):
    parsed = urlparse(str(url))
    paths = (parsed.path, parsed.fragment.split("?", 1)[0])
    return any(path.rstrip("/").lower() == p or path.lower().startswith(p + "/")
               for path in paths for p in AUTH_PATHS)


def _closed(page):
    try:
        return page is None or page.is_closed()
    except Exception:
        return True


def _watch_page(collector, page):
    if getattr(page, "_ngs_login_owner_v6", None) is collector:
        return
    page._ngs_login_owner_v6 = collector

    def remember(frame):
        # Event callback only records a URL in memory; never clicks or waits.
        if frame != page.main_frame or getattr(page, "_ngs_login_busy_v6", False):
            return
        url = str(page.url or "")
        if _same_origin(url, collector.cfg.get("runs_url", "")) and not _auth_path(url):
            page._ngs_login_target_v6 = url

    page.on("framenavigated", remember)
    remember(page.main_frame)


def _watch_context(collector):
    context = collector.context
    if context is None:
        return
    if getattr(collector, "_ngs_login_context_v6", None) is not context:
        context.on("page", lambda page: _watch_page(collector, page))
        collector._ngs_login_context_v6 = context
    for page in context.pages:
        if not _closed(page):
            _watch_page(collector, page)


def guarded_goto(page, url, **kwargs):
    """Adapter for existing IGV helpers which receive Page, not BulkCollector."""
    owner = getattr(page, "_ngs_login_owner_v6", None)
    if owner is None:
        return page.goto(url, **kwargs)
    return owner._login_goto_v6(page, url, **kwargs)


def install(collector_class, waiting_class, stopped_class):
    if getattr(collector_class, "_ngs_login_installed_v6", False):
        return

    def waiting(message):
        exc = waiting_class(message)
        exc.ngs_login_required = True
        return exc

    def checked(self, page, target_url=None, require_runs=False):
        if _closed(page):
            raise waiting("OTURUM BEKLENİYOR: GenNext sekmesi kapandı. Tarayıcıyı Aç / Login düğmesini kullanın.")
        _watch_page(self, page)
        page._ngs_login_busy_v6 = True
        try:
            return ensure_session(self, page, target_url=target_url, require_runs=require_runs)
        except LoginRequired as exc:
            self._ngs_login_idle_paused_v6 = True
            raise waiting(str(exc)) from None
        except LoginCancelled as exc:
            cancelled = stopped_class(str(exc))
            cancelled.ngs_login_cancelled = True
            raise cancelled from None
        finally:
            page._ngs_login_busy_v6 = False

    def navigate(self, page, url, **kwargs):
        runs = str(self.cfg.get("runs_url") or "")
        target = urljoin(runs, str(url))
        _watch_page(self, page)
        if not _same_origin(target, runs):
            # Preserve the existing source-navigation policy; never submit login
            # controls on an unrelated origin.
            return page.goto(url, **kwargs)
        page._ngs_login_target_v6 = target
        page._ngs_login_navigation_response = None
        error = None
        try:
            response = page.goto(url, **kwargs)
        except Exception as exc:
            error, response = exc, None
            if not needs_login(page, runs):
                raise
        required = urlparse(target).path.rstrip("/") == urlparse(runs).path.rstrip("/")
        checked(self, page, target, require_runs=required)
        recovered = getattr(page, "_ngs_login_navigation_response", None)
        if recovered is not None:
            return recovered
        if error is not None:
            raise error
        return response

    previous_open = collector_class.open_browser

    @functools.wraps(previous_open)
    def open_browser(self, auto_login=True):
        # Old V3/V5 patches may already call _remote_auto_login_if_needed. That
        # method is replaced below, so every path uses the same implementation.
        if self.context is not None and _closed(self.main):
            runs = self.cfg.get("runs_url", "")
            pages = [p for p in self.context.pages if not _closed(p)
                     and _same_origin(p.url, runs)
                     and (_auth_path(p.url) or urlparse(p.url).path.rstrip("/") in
                          (urlparse(runs).path.rstrip("/"), "/dashboard"))]
            self.main = pages[0] if pages else self.context.new_page()
            if str(self.main.url or "") in ("", "about:blank"):
                self.main.goto(runs, wait_until="domcontentloaded")
        previous_skip = getattr(self, "_ngs_login_skip_auth_v6", False)
        self._ngs_login_skip_auth_v6 = not auto_login
        try:
            result = previous_open(self, auto_login=False) or {}
        finally:
            self._ngs_login_skip_auth_v6 = previous_skip
        _watch_context(self)
        if not getattr(self, "_ngs_login_announced_v6", False):
            self.log("LOGIN V6: gerçek Chromium oturum denetimi etkin; giriş ve kaynak hedefi doğrulanacak.")
            self._ngs_login_announced_v6 = True
        if auto_login and not result.get("auth_verified"):
            result["auth_verified"] = checked(self, self.main, self.cfg.get("runs_url"), require_runs=True)
        result["browser_ready"] = True
        return result

    def remote(self):
        if getattr(self, "_ngs_login_skip_auth_v6", False):
            return False
        return checked(self, self.main, self.cfg.get("runs_url"), require_runs=True)

    def wait_runs(self, page):
        current = str(page.url or "")
        target = current if (_same_origin(current, self.cfg.get("runs_url", ""))
                             and not _auth_path(current)) else getattr(page, "_ngs_login_target_v6", "")
        return checked(self, page, target or self.cfg.get("runs_url"), require_runs=True)

    def guard_popup(self, page):
        _watch_page(self, page)
        if not needs_login(page, self.cfg.get("runs_url", "")):
            return page
        target = getattr(page, "_ngs_login_target_v6", "")
        if not target:
            checked(self, page, self.cfg.get("runs_url"), require_runs=True)
            raise waiting("OTURUM AÇILDI: yeni sekmenin özgün kaynak bağlantısı doğrulanamadı. İlgili eksik adımı yeniden deneyin.")
        checked(self, page, target)
        return page

    # Source processing can be reached after a delayed SPA redirect, after the
    # initial goto has returned. Re-run a failed read only after observed login.
    def wrap_source(previous):
        @functools.wraps(previous)
        def process(self, page, *args, **kwargs):
            for attempt in range(2):
                guard_popup(self, page)
                try:
                    result = previous(self, page, *args, **kwargs)
                except Exception as exc:
                    if (getattr(exc, "ngs_login_required", False)
                            or isinstance(exc, stopped_class)
                            or not needs_login(page, self.cfg.get("runs_url", ""))):
                        raise
                    if attempt:
                        raise waiting("OTURUM BEKLENİYOR: kaynak okunurken oturum yeniden düştü; adım tamamlandı sayılmadı.") from None
                    continue
                if not needs_login(page, self.cfg.get("runs_url", "")):
                    return result
                if attempt:
                    raise waiting("OTURUM BEKLENİYOR: kaynak okuma sonunda giriş ekranı görüldü; adım tamamlandı sayılmadı.")
            raise waiting("OTURUM BEKLENİYOR: kaynak doğrulanamadı.")
        return process

    previous_qc = getattr(collector_class, "_open_qc_from_runs", None)
    if previous_qc is not None:
        @functools.wraps(previous_qc)
        def open_qc(self, *args, **kwargs):
            page = previous_qc(self, *args, **kwargs)
            _watch_page(self, page)
            if needs_login(page, self.cfg.get("runs_url", "")):
                checked(self, page, self.cfg.get("runs_url"), require_runs=True)
                try:
                    page.close()
                except Exception:
                    pass
                page = previous_qc(self, *args, **kwargs)
                guard_popup(self, page)
            return page
        collector_class._open_qc_from_runs = open_qc

    previous_diag = getattr(collector_class, "_diag", None)
    if previous_diag is not None:
        @functools.wraps(previous_diag)
        def diagnostic(self, page, step, context, exc=None):
            # The legacy diagnostic inventory serializes input.value. Never
            # invoke it on an authentication form, including a foreign iframe.
            sensitive = _auth_path(str(getattr(page, "url", "")))
            try:
                for frame in page.frames:
                    if _auth_path(frame.url) or frame.locator('input[type="password"]').count():
                        sensitive = True
                        break
            except Exception:
                sensitive = sensitive or needs_login(page, self.cfg.get("runs_url", ""))
            if not sensitive:
                return previous_diag(self, page, step, context, exc)
            root = Path(self.base) / str(self.cfg.get("diagnostics_dir") or "DIAGNOSTICS")
            folder = root / ("LOGIN_V6_" + time.strftime("%Y%m%d_%H%M%S") + "_" + str(time.monotonic_ns()))
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "login_state.json").write_text(json.dumps({
                "generator": TAG, "login_required": True,
                "capture": "structural_only", "credential_values": "not_collected"
            }, indent=2), encoding="utf-8")
            self.log("LOGIN V6: giriş ekranı tanısı yalnız oturum durumu içerir; form değerleri kaydedilmedi.")
            return folder
        collector_class._diag = diagnostic

    def idle_tick(self, worker):
        if (self.context is None or getattr(self, "_ngs_login_idle_paused_v6", False)
                or self._stop_now() or self._stop_after()):
            return
        now = time.monotonic()
        if now < getattr(self, "_ngs_login_next_probe_v6", 0):
            return
        self._ngs_login_next_probe_v6 = now + 1.0
        _watch_context(self)
        pages = [p for p in self.context.pages if not _closed(p)]
        candidate = None
        for page in pages[:12]:
            try:
                if needs_login(page, self.cfg.get("runs_url", "")):
                    candidate = page
                    break
            except Exception:
                continue
        if candidate is None:
            return
        # Reserve atomically: an idle login cannot overlap or jump ahead of a
        # command the UI has already reserved.
        with worker._state_lock:
            if worker._busy or worker._reserved or worker._closed:
                return
            worker._busy = True
            worker._running_command = "session_recovery"
        worker.event_cb("busy", True)
        try:
            target = getattr(candidate, "_ngs_login_target_v6", None) or self.cfg.get("runs_url")
            checked(self, candidate, target, require_runs=(candidate is self.main))
            worker.event_cb("status", "GenNext oturumu doğrulandı; tarayıcı hazır.")
        except stopped_class:
            self._ngs_login_idle_paused_v6 = True
            worker.event_cb("status", "Otomatik giriş durduruldu. Gerekirse Tarayıcıyı Aç / Login ile yeniden deneyin.")
        except Exception as exc:
            self._ngs_login_idle_paused_v6 = True
            if getattr(exc, "ngs_login_required", False):
                self.log(str(exc))
                worker.event_cb("status", str(exc))
            else:
                self.log("LOGIN V6: oturum kontrolü tamamlanamadı; manuel giriş bekleniyor.")
        finally:
            with worker._state_lock:
                worker._running_command = ""
            worker._set_running(False)

    collector_class._login_ensure_v6 = checked
    collector_class._login_goto_v6 = navigate
    collector_class._login_guard_popup_v6 = guard_popup
    collector_class._login_idle_tick_v6 = idle_tick
    collector_class._remote_auto_login_if_needed = remote
    collector_class._wait_runs_rendered = wait_runs
    collector_class.open_browser = open_browser
    for name in ("_process_run", "_process_clinical", "_process_snv", "_process_cnv", "_process_qc", "_process_fusion"):
        previous = getattr(collector_class, name, None)
        if previous is not None:
            setattr(collector_class, name, wrap_source(previous))
    collector_class._ngs_login_installed_v6 = True
