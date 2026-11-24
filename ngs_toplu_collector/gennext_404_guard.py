"""Fail-closed GenNext navigation and bounded 404 holds.

Every URL must be tied to the exact RUN and SAMPLE UUID, never inferred from a
row index. A 404 is NOT an empty/negative assay result. The hold is a local
navigation safety checkpoint and never changes any molecular report or result.
"""
from __future__ import annotations

import hashlib
import re
import time
from urllib.parse import parse_qs, urlparse

UUID = r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
DETAIL = re.compile(r'/run-detail-somatic/(' + UUID + r')/?', re.I)
VARIANT = re.compile(r'/somatic-detail/(' + UUID + r')&(' + UUID + r')&(snv|cnv|fusion)/?', re.I)
CLINICAL = re.compile(r'/somatic-detail-clinic/(' + UUID + r')&(' + UUID + r')&snv/?', re.I)
HOLD_SECONDS = 6 * 60 * 60


class NavigationNotVerified(ValueError):
    pass


class UpstreamNotFound(NavigationNotVerified):
    pass


def same_origin(url, origin):
    target, base = urlparse(str(url)), urlparse(str(origin))
    return (target.scheme in ('https', 'http') and not target.username and not target.password
            and (target.scheme, target.hostname, target.port) ==
                (base.scheme, base.hostname, base.port))


def validate_target(url, sample, kind, origin):
    """Validate route and identity before navigation, including constructed links."""
    if not same_origin(url, origin):
        raise NavigationNotVerified('GenNext source origin mismatch')
    target = urlparse(url)
    run = str(sample.get('run_uuid') or '').lower()
    su = str(sample.get('sample_uuid') or '').lower()
    if not re.fullmatch(UUID, run, re.I) or not re.fullmatch(UUID, su, re.I):
        raise NavigationNotVerified('Missing technical UUID')
    if kind == 'run':
        m = DETAIL.fullmatch(target.path)
        query = parse_qs(target.query, keep_blank_values=True)
        if (not m or m.group(1).lower() != run or
                [v.lower() for v in query.get('sampleUuid', [])] != [su]):
            raise NavigationNotVerified('Run Detail identity mismatch')
    elif kind in ('snv', 'cnv', 'fusion'):
        m = VARIANT.fullmatch(target.path)
        if not m or (m.group(1).lower(), m.group(2).lower(), m.group(3).lower()) != (run, su, kind):
            raise NavigationNotVerified('Somatic source identity mismatch')
    elif kind == 'clinical':
        m = CLINICAL.fullmatch(target.path)
        if not m or (m.group(1).lower(), m.group(2).lower()) != (run, su):
            raise NavigationNotVerified('Clinical source identity mismatch')
    return True


def is_explicit_not_found(page):
    """Check visible 404 UI in the current tab without exporting page text."""
    if page is None:
        return False
    try:
        return bool(page.evaluate(r'''() => {
          const norm = s => String(s || '').replace(/\s+/g,' ').trim().toLowerCase();
          const els = document.querySelectorAll('h1,h2,h3,[role="alert"],[class*="error"],[class*="Error"]');
          const isError = e => {
            const text=norm(e.textContent);
            if (text.length>500) return false;
            return /^(?:404|404 not found|page not found|not found|404 page not found)$/.test(text)
                || /(?:request failed with status code|failed to load resource).*\b404\b/.test(text);
          };
          if ([...els].some(isError)) return true;
          // React/Axios errors may appear in a generic div instead of an alert.
          // Inspect only SHORT, directly rendered messages, never a whole page.
          return [...document.querySelectorAll('body div,body p,body span')]
            .filter(e => e.children.length===0).slice(0,1200).some(isError);
        }'''))
    except Exception:
        return False


def require_ok_response(response, page=None):
    code = getattr(response, 'status', None)
    if callable(code):
        code = code()
    if code is not None:
        code = int(code)
        if code in (404, 410):
            raise UpstreamNotFound('GenNext source HTTP 404/410; no data was inferred')
        if code in (401, 403):
            raise NavigationNotVerified('GenNext authentication/authorization not verified')
        if code == 429:
            raise NavigationNotVerified('GenNext rate limit HTTP 429')
        if code >= 500:
            raise NavigationNotVerified('GenNext upstream HTTP 5xx')
        if code >= 400:
            raise NavigationNotVerified('GenNext HTTP 4xx')
    if is_explicit_not_found(page):
        raise UpstreamNotFound('GenNext visible 404 page; no data was inferred')
    return True


def ensure_schema(db):
    db.cx.execute('''CREATE TABLE IF NOT EXISTS gennext_navigation_hold(
      record_key TEXT NOT NULL, kind TEXT NOT NULL, link_sha256 TEXT NOT NULL,
      blocked_until REAL NOT NULL, reason_code TEXT NOT NULL,
      PRIMARY KEY(record_key,kind))''')
    db.cx.commit()


def _url_digest(url):
    return hashlib.sha256(str(url).encode('utf-8')).hexdigest()


def is_held(db, record_key, kind, url, now=None):
    ensure_schema(db)
    row = db.cx.execute('''SELECT link_sha256,blocked_until FROM gennext_navigation_hold
        WHERE record_key=? AND kind=?''', (record_key, kind)).fetchone()
    if row is None:
        return False
    return (row['link_sha256'] == _url_digest(url)
            and float(row['blocked_until']) > (time.time() if now is None else now))


def hold_404(db, record_key, kind, url, now=None):
    ensure_schema(db)
    until = (time.time() if now is None else now) + HOLD_SECONDS
    with db.cx:
        db.cx.execute('''INSERT INTO gennext_navigation_hold
          (record_key,kind,link_sha256,blocked_until,reason_code)
          VALUES(?,?,?,?,?) ON CONFLICT(record_key,kind) DO UPDATE SET
          link_sha256=excluded.link_sha256,blocked_until=excluded.blocked_until,
          reason_code=excluded.reason_code''',
          (record_key, kind, _url_digest(url), until, 'upstream_404'))
    return until


def clear_hold(db, record_key, kind):
    ensure_schema(db)
    with db.cx:
        db.cx.execute('DELETE FROM gennext_navigation_hold WHERE record_key=? AND kind=?',
                      (record_key, kind))


def wait_run_detail_signal(page, timeout_ms=12000):
    """Wait for Analyzed control OR a visible HTTP 404 without page reload.

    No action is sent to GenNext. Return False when the page remains ambiguous;
    this is NOT equivalent to 'Analyzed=False' or a negative variant report.
    """
    timeout_ms = max(1000, min(30000, int(timeout_ms)))
    end = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < end:
        if is_explicit_not_found(page):
            raise UpstreamNotFound('GenNext Axios/SPA HTTP 404 visible; no Analyzed conclusion')
        try:
            switches = page.locator('input[value="runIsSolved"]')
            if switches.count() == 1:
                return True
        except Exception:
            pass
        try:
            page.wait_for_timeout(300)
        except Exception:
            break
    if is_explicit_not_found(page):
        raise UpstreamNotFound('GenNext Axios/SPA HTTP 404 visible; no Analyzed conclusion')
    return False
