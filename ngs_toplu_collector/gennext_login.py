"""Bounded, same-origin GenNext login recovery on the Playwright owner thread.

No credential value leaves the browser.  This module does not own a browser,
change the clipboard, store credentials, or change clinical/source validation.
"""
from __future__ import annotations

import math
import re
import time
from urllib.parse import urlsplit


class LoginRequired(RuntimeError):
    """The protected page is not ready; the existing source must be preserved."""


class LoginCancelled(RuntimeError):
    """The user stopped the bounded login wait."""


RUNS_TABLE = 'table[class*="RunsTableNew-module__runsTable__"]'
_LOGIN_NAME = re.compile(
    r'^\s*(?:sign[\s-]*in|log[\s-]*in|login|giriş(?:\s+yap)?|oturum\s+aç)'
    r'\s*(?:…|\.{3})?\s*$', re.I)

_PROBE_JS = r"""({runsPath, tableSelector}) => {
  const visible = e => {
    if (!e || e.closest('[hidden], [aria-hidden="true"]')) return false;
    const s = getComputedStyle(e), r = e.getBoundingClientRect();
    return s.display !== 'none' && s.visibility !== 'hidden' &&
           s.visibility !== 'collapse' && s.opacity !== '0' && r.width > 0 && r.height > 0;
  };
  const any = selector => Array.from(document.querySelectorAll(selector)).some(visible);
  const password = any('input[type="password"]');
  const loginForm = any('#kt_login_signin_form');
  const table = any(tableSelector);
  const logout = Array.from(document.querySelectorAll('a, button, [role="button"]'))
    .some(e => visible(e) && /^(?:log\s*out|logout|sign\s*out|cikis(?:\s*yap)?|oturumu\s*kapat)$/
      .test((e.innerText || e.textContent || '').trim().toLowerCase()
        .normalize('NFD').replace(/[\u0300-\u036f]/g, '').replace(/ı/g, 'i')));
  const runsNav = Array.from(document.querySelectorAll(
    'nav a[href], [role="navigation"] a[href], aside a[href], [class*="sidebar"] a[href], [class*="menu"] a[href]'))
    .some(e => {
      if (!visible(e)) return false;
      try {
        const u = new URL(e.getAttribute('href'), location.href);
        return u.origin === location.origin && u.pathname.replace(/\/+$/, '') === runsPath;
      } catch (_) { return false; }
    });
  let failure = '';
  const alerts = Array.from(document.querySelectorAll(
    '[role="alert"], .alert-danger, .alert-error, .text-danger, .invalid-feedback, '
    + '[aria-live="assertive"], .swal2-popup, .Toastify__toast--error, [class*="error"], [class*="Error"]'));
  for (const e of alerts) {
    if (!visible(e)) continue;
    const t = (e.innerText || e.textContent || '').slice(0, 3000).toLowerCase()
      .normalize('NFD').replace(/[\u0300-\u036f]/g, '').replace(/ı/g, 'i');
    if (/too many (?:requests|attempts)|rate[ -]?limit|\b429\b|cok fazla.{0,40}(?:istek|deneme)|fazla.{0,20}giris denemesi/.test(t)) {
      failure = 'rate_limit'; break;
    }
    if (/account.{0,40}(?:locked|disabled|blocked)|(?:locked|disabled|blocked).{0,30}account|hesab.{0,40}(?:kilit|engellen|devre disi)|hesap.{0,40}kilit/.test(t)) {
      failure = 'locked'; break;
    }
    if (/(?:invalid|incorrect|wrong|bad).{0,50}(?:credentials|password|username|user name|e-?mail)|(?:credentials|password|username|user name|e-?mail).{0,50}(?:invalid|incorrect|wrong)|authentication failed|login failed|(?:kullanici|sifre|parola|e-?posta).{0,70}(?:hatali|yanlis|gecersiz)|(?:hatali|yanlis|gecersiz).{0,70}(?:kullanici|sifre|parola|e-?posta)/.test(t)) {
      failure = 'invalid_credentials'; break;
    }
  }
  return {password, loginForm, table, authenticated: logout || runsNav, failure};
}"""

_FIELDS_JS = r"""password => {
  const visible = e => {
    const s = getComputedStyle(e), r = e.getBoundingClientRect();
    return !e.closest('[hidden], [aria-hidden="true"]') && s.display !== 'none' &&
           s.visibility !== 'hidden' && s.opacity !== '0' && r.width > 0 && r.height > 0;
  };
  const filled = e => {
    if (Boolean(e.value)) return true;
    try { return e.matches(':-webkit-autofill'); } catch (_) { return false; }
  };
  const root = password.closest('form') || document;
  const users = Array.from(root.querySelectorAll(
    'input[type="email"], input[type="text"], input:not([type]), input[autocomplete="username"]'))
    .filter(e => visible(e) && !e.disabled && e.type !== 'password' && e.type !== 'hidden');
  const complete = filled(password) && users.every(filled);
  const busy = Array.from(root.querySelectorAll(
    '[aria-busy="true"], [data-kt-indicator="on"], [role="progressbar"], .spinner-border, .spinner-grow'))
    .some(visible);
  return {complete: Boolean(complete), busy: Boolean(busy)};
}"""


def _origin(url):
    try:
        u = urlsplit(str(url or ''))
        if u.scheme.lower() not in ('http', 'https') or not u.hostname or u.username or u.password:
            return None
        return (u.scheme.lower(), u.hostname.lower(), u.port or (443 if u.scheme.lower() == 'https' else 80))
    except (ValueError, TypeError):
        return None


def _url(page):
    try:
        return str(page.url or '')
    except Exception:
        return ''


def _auth_route(url):
    try:
        u = urlsplit(url)
        paths = [u.path, u.fragment.split('?', 1)[0]]
        return any(
            (path.rstrip('/').lower() == base or path.lower().startswith(base + '/'))
            for path in paths for base in ('/auth', '/login', '/signin', '/sign-in'))
    except (ValueError, TypeError):
        return False


def _same_target(current, target):
    """The browser must return to the requested source path, query and fragment."""
    if _origin(current) != _origin(target):
        return False
    try:
        a, b = urlsplit(current), urlsplit(target)
        return (a.path or '/', a.query, a.fragment) == (b.path or '/', b.query, b.fragment)
    except (ValueError, TypeError):
        return False


def _scopes(page, origin):
    if _origin(_url(page)) != origin:
        return []
    result = [page]
    try:
        main_frame = page.main_frame
        for frame in page.frames:
            if frame is not main_frame and _origin(_url(frame)) == origin:
                result.append(frame)
    except Exception:
        pass
    return result


def _snapshot(page, runs_url):
    expected = _origin(runs_url)
    result = {'login': _auth_route(_url(page)), 'table': False,
              'authenticated': False, 'failure': '', 'readable': False,
              'form_present': False}
    runs_path = urlsplit(runs_url).path.rstrip('/') or '/runs'
    for scope in _scopes(page, expected):
        try:
            data = scope.evaluate(_PROBE_JS, {'runsPath': runs_path, 'tableSelector': RUNS_TABLE})
            if not isinstance(data, dict):
                continue
            result['readable'] = True
            has_form = bool(data.get('password') or data.get('loginForm'))
            result['form_present'] |= has_form
            result['login'] |= has_form
            result['table'] |= bool(data.get('table'))
            result['authenticated'] |= bool(data.get('authenticated'))
            if data.get('failure') in ('rate_limit', 'locked', 'invalid_credentials'):
                result['failure'] = data['failure']
        except Exception:
            # The frame may be navigating.  An unreadable DOM is never proof of login.
            continue
    # React sometimes keeps /auth in the address bar briefly after rendering
    # the authenticated app. An old route string cannot trump visible proof.
    if (result['table'] or result['authenticated']) and not result['form_present']:
        result['login'] = False
    return result


def needs_login(page, runs_url):
    """Inspect only the configured origin; never interact with a foreign frame."""
    if _origin(runs_url) is None or _origin(_url(page)) != _origin(runs_url):
        return False
    return bool(_snapshot(page, runs_url)['login'])


def _visible_enabled(locator):
    try:
        if not locator.is_visible():
            return False
        try:
            return locator.is_enabled(timeout=250)
        except TypeError:
            return locator.is_enabled()
    except Exception:
        return False


def _controls(page, runs_url):
    """Choose a visible password form and its visible, enabled submit control."""
    for scope in _scopes(page, _origin(runs_url)):
        try:
            passwords = scope.locator('input[type="password"]')
            for i in range(min(passwords.count(), 12)):
                password = passwords.nth(i)
                if not _visible_enabled(password):
                    continue
                forms = password.locator('xpath=ancestor::form[1]')
                root = forms.first if forms.count() else scope
                choices = [root.locator('#kt_sign_in_submit'),
                           root.get_by_role('button', name=_LOGIN_NAME)]
                for choice in choices:
                    for j in range(min(choice.count(), 12)):
                        button = choice.nth(j)
                        if _visible_enabled(button):
                            return scope, password, button
        except Exception:
            continue
    return None


def _number(cfg, name, default, minimum, maximum):
    try:
        value = float(cfg.get(name, default))
        if not math.isfinite(value):
            return default
        return max(minimum, min(maximum, value))
    except (ValueError, TypeError):
        return default


def _emit(collector, message, status=False):
    callback = getattr(collector, 'status_cb' if status else 'log', None)
    if callable(callback):
        try:
            callback(message)
        except Exception:
            pass


def _cancelled(collector):
    for name in ('_stop_now', '_stop_after'):
        check = getattr(collector, name, None)
        if callable(check) and check():
            raise LoginCancelled('NGS giriş beklemesi kullanıcı tarafından durduruldu.')


def ensure_session(collector, page, target_url=None, require_runs=False):
    """Wait/recover login without navigating away during the authentication handoff.

    Call after navigating to a source.  A non-login source gets a short redirect
    check; a RUNS request must render the real table.  After a witnessed login,
    authenticated DOM is required before returning to the exact target URL.
    A target restore's response is exposed as page._ngs_login_navigation_response.
    """
    cfg = getattr(collector, 'cfg', {}) or {}
    runs_url = str(cfg.get('runs_url') or '')
    expected = _origin(runs_url)
    if expected is None:
        raise LoginRequired('NGS oturumu gerekli: yapılandırılmış RUNS adresi geçersiz.')
    origin_url = urlsplit(runs_url)
    auth_link = origin_url.scheme + '://' + origin_url.netloc + '/auth'

    def fail(reason):
        raise LoginRequired('NGS oturumu doğrulanamadı: ' + reason + ' Giriş sayfası: ' + auth_link)

    if target_url is not None:
        target_url = str(target_url)
        if _origin(target_url) != expected:
            fail('Hedef farklı alan adına ait; otomatik giriş yapılmadı.')
    current = _url(page)
    if _origin(current) != expected and current not in ('', 'about:blank'):
        fail('Sayfa farklı alan adına yönlendi; otomatik giriş yapılmadı.')

    total = _number(cfg, 'login_total_timeout_s', 75, .1, 180)
    manual = _number(cfg, 'login_manual_wait_s', 60, .05, 120)
    handoff = _number(cfg, 'login_handoff_wait_s', 20, .05, 45)
    form_settle = _number(cfg, 'login_form_settle_s', 1.5, 0, 5)
    stable = _number(cfg, 'login_authenticated_stable_s', 1.0, .05, 3)
    probe_settle = _number(cfg, 'login_probe_settle_s', .6, .05, 2)
    poll_ms = int(_number(cfg, 'login_poll_ms', 150, 20, 500))
    action_ms = int(_number(cfg, 'login_action_timeout_ms', 1500, 50, 5000))
    max_attempts = int(_number(cfg, 'login_max_attempts', 3, 1, 3))
    started = time.monotonic()
    deadline = started + total
    probe_since = started
    probe_signature = current
    auth_since = None
    auth_signature = None
    ready_since = None
    manual_deadline = None
    next_attempt = 0.0
    attempts = 0
    restores = 0
    seen_login = False
    verified = False
    manual_notice = False
    initial_navigation_done = False
    restore_wait_from = None
    target_path = urlsplit(target_url or '').path.rstrip('/')
    runs_path = urlsplit(runs_url).path.rstrip('/') or '/runs'
    need_table = bool(require_runs or (target_url and target_path == runs_path))

    while time.monotonic() < deadline:
        _cancelled(collector)
        current = _url(page)
        current_origin = _origin(current)
        if current_origin != expected and current not in ('', 'about:blank'):
            fail('Sayfa farklı alan adına yönlendi; otomatik giriş yapılmadı.')
        state = _snapshot(page, runs_url)
        now = time.monotonic()
        # Some GenNext SSO handoffs open /dashboard in a NEW Chromium tab,
        # leaving the original /auth tab unchanged. Only adopt an exact-origin
        # RUNS/dashboard page; do not treat the URL alone as authentication.
        # The adopted page must still render the real RUNS table to succeed.
        if need_table and page is getattr(collector, 'main', None):
            context = getattr(collector, 'context', None)
            for other in (list(getattr(context, 'pages', []) or []) if context else [])[:16]:
                if other is page:
                    continue
                try:
                    if other.is_closed() or _origin(_url(other)) != expected:
                        continue
                    other_path = urlsplit(_url(other)).path.rstrip('/').lower()
                    if other_path not in (runs_path.lower(), '/dashboard'):
                        continue
                    other_state = _snapshot(other, runs_url)
                    if not other_state['readable'] or other_state['login']:
                        continue
                    # A dashboard is a navigation candidate, NOT proof.
                    collector.main = other
                    page = other
                    current = _url(page)
                    state = other_state
                    now = time.monotonic()
                    seen_login = False
                    verified = False
                    probe_since = now
                    auth_signature = auth_since = None
                    _emit(collector, 'NGS LOGIN: Chromium icindeki acik GenNext sekmesine gecildi; RUNS kaniti beklenecek.')
                    break
                except Exception:
                    continue
        if current != probe_signature:
            probe_signature, probe_since = current, now
        if state['failure'] and (seen_login or state['login']):
            reasons = {
                'invalid_credentials': 'Giriş bilgileri site tarafından reddedildi; otomatik deneme durdu.',
                'locked': 'Hesap kilidi/engeli bildirildi; otomatik deneme durdu.',
                'rate_limit': 'Çok fazla istek/deneme uyarısı alındı; otomatik deneme durdu.',
            }
            fail(reasons[state['failure']])
        if state['login']:
            if not seen_login:
                _emit(collector, 'NGS LOGIN: giriş ekranı algılandı; kaynak adımı bekliyor.')
                _emit(collector, 'NGS oturumu yenileniyor; kaynak verisi bekliyor.', status=True)
                manual_deadline = min(deadline, now + manual)
            seen_login = True
            verified = False
            auth_since = auth_signature = None
            probe_since = now
            controls = _controls(page, runs_url)
            if controls:
                scope, password, button = controls
                try:
                    if _origin(_url(scope)) != expected:
                        fields = {}
                    else:
                        fields = password.evaluate(_FIELDS_JS, timeout=action_ms)
                except Exception:
                    fields = {}
                if not fields.get('complete'):
                    ready_since = None
                    if not manual_notice:
                        _emit(collector, 'NGS LOGIN: kayıtlı girişin veya elle tamamlanan formun hazır olması bekleniyor.')
                        _emit(collector, 'Giriş formunu NGS tarayıcısında tamamlayın; işlem otomatik devam edecek.', status=True)
                        manual_notice = True
                    if manual_deadline is not None and now >= manual_deadline:
                        fail('Giriş formu süre içinde tamamlanmadı; mevcut veriler korundu.')
                elif fields.get('busy'):
                    ready_since = None
                else:
                    if ready_since is None:
                        ready_since = now
                    if now - ready_since >= form_settle and now >= next_attempt and attempts < max_attempts:
                        _cancelled(collector)
                        # Recheck the origin immediately before the only form action.
                        if _origin(_url(page)) != expected or _origin(_url(scope)) != expected:
                            fail('Sayfa farklı alan adına yönlendi; otomatik giriş yapılmadı.')
                        attempts += 1
                        ready_since = None
                        try:
                            button.click(timeout=min(action_ms, max(50, int((deadline - now) * 1000))))
                            _emit(collector, f'NGS LOGIN: Login tıklandı ({attempts}/{max_attempts}); yönlendirme bekleniyor.')
                            next_attempt = time.monotonic() + handoff
                        except Exception:
                            # A covering overlay is never bypassed with force/JS clicks.
                            _emit(collector, f'NGS LOGIN: tıklamanın tamamlanması doğrulanamadı ({attempts}/{max_attempts}); sayfa bekleniyor.')
                            next_attempt = time.monotonic() + handoff
            else:
                ready_since = None
        else:
            ready_since = None
            # Neither a route string nor an unreadable, navigating DOM proves login.
            proof = state['table'] or state['authenticated']
            if current_origin == expected and state['readable']:
                if (not seen_login and need_table and target_url and not initial_navigation_done
                        and urlsplit(current).path.rstrip('/') != runs_path):
                    # The user may have navigated the main tab to a detail page.
                    # A RUNS preflight has an explicit, trusted navigation target.
                    initial_navigation_done = True
                    _cancelled(collector)
                    remaining_ms = max(50, int((deadline - time.monotonic()) * 1000))
                    try:
                        response = page.goto(target_url, wait_until='commit', timeout=min(5000, remaining_ms))
                        page._ngs_login_navigation_response = response
                    except Exception:
                        page._ngs_login_navigation_response = None
                    probe_since = time.monotonic()
                    auth_since = auth_signature = None
                elif not seen_login and not need_table:
                    if now - probe_since >= probe_settle:
                        return True
                elif (seen_login and need_table and target_url and
                      urlsplit(current).path.rstrip('/').lower() == '/dashboard' and
                      now - probe_since >= probe_settle and restores < max_attempts):
                    # Successful login may land on /dashboard without an
                    # observable logout or RUNS navigation button. Navigate
                    # once to the known protected target and require its table.
                    # A failed navigation cannot yield a positive session.
                    restores += 1
                    try:
                        remaining_ms = max(50, int((deadline - time.monotonic()) * 1000))
                        response = page.goto(target_url, wait_until='commit',
                                             timeout=min(7000, remaining_ms))
                        page._ngs_login_navigation_response = response
                        _emit(collector, 'NGS LOGIN: Dashboard goruldu; RUNS tablosu dogrulaniyor.')
                    except Exception:
                        page._ngs_login_navigation_response = None
                    probe_since = time.monotonic()
                    auth_signature = auth_since = None
                elif verified:
                    if seen_login and target_url and not _same_target(current, target_url):
                        auth_since = auth_signature = None
                    elif restore_wait_from is not None and current == restore_wait_from:
                        auth_since = auth_signature = None
                    elif not need_table and now - probe_since >= probe_settle:
                        return True
                    elif need_table and state['table']:
                        if auth_signature != current:
                            auth_signature, auth_since = current, now
                        if auth_since is not None and now - auth_since >= stable:
                            return True
                    else:
                        auth_since = auth_signature = None
                elif (need_table and not seen_login and state['table']) or (seen_login and proof):
                    if auth_signature != current:
                        auth_signature, auth_since = current, now
                    if auth_since is not None and now - auth_since >= stable:
                        verified = True
                        _emit(collector, 'NGS LOGIN: oturum görünür sayfa içeriğiyle doğrulandı.')
                        if seen_login and target_url and not _same_target(current, target_url):
                            if restores >= max_attempts:
                                fail('Oturum açıldı ancak hedef sayfaya dönüş doğrulanamadı.')
                            _cancelled(collector)
                            restores += 1
                            remaining_ms = max(50, int((deadline - time.monotonic()) * 1000))
                            try:
                                response = page.goto(target_url, wait_until='commit', timeout=min(5000, remaining_ms))
                                page._ngs_login_navigation_response = response
                                restore_wait_from = None
                            except Exception:
                                # A slow response may commit just after its timeout.
                                # Continue observing the actual page, never its old route.
                                page._ngs_login_navigation_response = None
                                restore_wait_from = current
                            probe_since = time.monotonic()
                            auth_since = auth_signature = None
                        elif not need_table or state['table']:
                            return True
                else:
                    auth_since = auth_signature = None
            else:
                probe_since = now
                auth_since = auth_signature = None
        _cancelled(collector)
        remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
        if not remaining_ms:
            break
        try:
            page.wait_for_timeout(min(poll_ms, remaining_ms))
        except Exception:
            fail('Tarayıcı sayfası kapandı veya erişilemez oldu.')
    _cancelled(collector)
    if seen_login:
        fail('Giriş/oturum geçişi süre içinde tamamlanmadı; ilgili adım eksik olarak korunuyor.')
    fail('RUNS tablosu veya oturumlu sayfa içeriği süre içinde hazır olmadı.')
