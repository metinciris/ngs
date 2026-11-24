"""Safe navigation and privacy-bounded diagnostics for GenNext Selection.

Use ONLY URLs captured from live RUNS hyperlinks: a missing Run Detail URL
must not be guessed from a UUID.  A guessed route can return HTTP 404 or,
worse, refer to an unrelated patient's React state.

No operating-system clipboard, keyboard hooks, global hotkeys or focus control.
No request/response bodies, cookies, patient IDs, full URLs or genomic values
are exported in a diagnostic report.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit

from gennext_report_selection import SelectionReadError, SelectionNavigationUnavailable

_UUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', re.I)
_ALLOWED_STAGES = {'run_link', 'run_detail', 'analyzed', 'clinical_link',
                   'clinical', 'selection', 'fusion_link', 'fusion_selection', 'other'}


def verified_run_link(raw_href, sample, origin):
    """Return the verified exact-origin Run Detail URL or raise; never fabricate.

    The source MUST be a real <a href> collected from the rendered RUNS row.
    The route's run and sample UUID must match the same technical record.
    """
    if not raw_href or not str(raw_href).strip():
        raise SelectionNavigationUnavailable('RUNS kaynak satirinda gercek Run Detail linki yok; URL tahmin edilmedi')
    target = str(raw_href).strip()
    base = urlsplit(str(origin or ''))
    if base.scheme not in ('https', 'http') or not base.netloc:
        raise SelectionReadError('GenNext origin dogrulanamadi')
    if target.startswith('//') or target.startswith('\\'):
        raise SelectionReadError('Guvenli olmayan Run Detail linki')
    dest = urlsplit(urljoin(f'{base.scheme}://{base.netloc}/', target))
    same_origin=(dest.scheme, dest.netloc.lower()) == (base.scheme, base.netloc.lower())
    # Some transferred RUNS records still contain the old beta links. Rebase
    # only the vendor's two *exact* known HTTPS origins, with no credentials,
    # port, fragment or off-site redirect. UUID and sample UUID are checked
    # below before any page opens. The original old site is never visited.
    migration=(base.scheme=='https' and dest.scheme=='https'
               and base.netloc.lower() in {'app.gennext.bio','beta.gennext.bio'}
               and dest.netloc.lower() in {'app.gennext.bio','beta.gennext.bio'}
               and base.netloc.lower()!=dest.netloc.lower()
               and not dest.username and not dest.password and not dest.port
               and not dest.fragment)
    if not (same_origin or migration):
        raise SelectionReadError('Run Detail linki baska sunucuya yoneliyor')
    match = re.fullmatch(r'/run-detail-somatic/(' + _UUID.pattern + r')/?', dest.path, re.I)
    if not match or match.group(1).lower() != str(sample.get('run_uuid') or '').lower():
        raise SelectionReadError('RUNS kaynak Run Detail linkinde RUN UUID eslesmedi')
    vals = parse_qs(dest.query).get('sampleUuid', [])
    if len(vals) != 1 or not _UUID.fullmatch(vals[0]) or vals[0].lower() != str(sample.get('sample_uuid') or '').lower():
        raise SelectionReadError('RUNS kaynak Run Detail linkinde SAMPLE UUID eslesmedi')
    if migration:
        # No extra query values from the legacy host are forwarded. Construct
        # only the source-proven path and the verified sampleUuid parameter.
        if set(parse_qs(dest.query))!={'sampleUuid'}:
            raise SelectionReadError('Eski Run Detail linkinde beklenmeyen parametre var')
        return urlunsplit((base.scheme,base.netloc,dest.path,
                           'sampleUuid='+vals[0],''))
    return dest.geturl()


def check_document_response(response, stage='run_detail'):
    """Catch a genuine HTTP failure *before* waiting on React selectors."""
    status = getattr(response, 'status', None) if response is not None else None
    if callable(status):
        status = status()
    if status is not None and int(status) >= 400:
        raise SelectionReadError(f'GenNext {stage}: HTTP {int(status)}; sayfa yok/erisim yok, sonuc kaydedilmedi')
    return status


def _page_kind(url):
    p = urlsplit(str(url or '')).path.lower()
    if p.startswith('/run-detail-somatic/'):
        return 'run_detail'
    if p.startswith('/somatic-detail-clinic/'):
        return 'clinical'
    if p.startswith('/somatic-detail/') and p.endswith('&fusion'):
        return 'fusion'
    if p.startswith('/runs'):
        return 'runs'
    return 'other'


def safe_selection_structure(page, *, origin):
    """Allow-listed, aggregate DOM booleans/counts only. No text or URLs."""
    if not page or page.is_closed():
        return {'available': False}
    current = urlsplit(str(getattr(page, 'url', '') or ''))
    base = urlsplit(str(origin or ''))
    if (current.scheme, current.netloc.lower()) != (base.scheme, base.netloc.lower()):
        return {'available': False, 'origin_match': False}
    try:
        d = page.evaluate(r'''() => {
          const count = s => document.querySelectorAll(s).length;
          const btns = [...document.querySelectorAll('button')];
          const text = el => (el?.textContent||'').replace(/\s+/g,' ').trim().toLowerCase();
          return {
            app_root:count('#root'), login_password:count('input[type="password"]'),
            run_switch:count('input[value="runIsSolved"]'),
            run_clinical_links:count('a.variant-filtering-button'),
            clinical_heading:count('h1.page-heading'),
            fusion_active_tabs:[...btns].filter(b=>text(b)==='fusions' && [...b.classList].some(c=>c.includes('active'))).length,
            selection_exact_buttons:btns.filter(b=>text(b)==='selection').length,
            selection_list_icons:count('button:has(> i.fa-list)'),
            report_selection_modals:count('[class*="ReportSelection-module__overlay__"]'),
            explicit_empty_views:count('[class*="ReportSelection-module__emptyState__"]'),
            loading_indicators:count('[role="progressbar"], .spinner, .spinner-border'),
            not_found_heading:!![...document.querySelectorAll('h1, h2')]
              .some(h => /^(404|page not found|not found)$/i.test(text(h)))
          };
        }''')
        allowed = {'app_root', 'login_password', 'run_switch', 'run_clinical_links',
                   'clinical_heading','fusion_active_tabs','selection_exact_buttons', 'selection_list_icons',
                   'report_selection_modals', 'explicit_empty_views',
                   'loading_indicators', 'not_found_heading'}
        return {'available': True, 'origin_match': True,
                'dom': {k: v for k, v in d.items() if k in allowed and isinstance(v, (bool, int))}}
    except Exception:
        return {'available': False, 'origin_match': True}


def monitor_http_failures(page, *, origin):
    """Record only status, broad resource type and broad route family.

    Handles a GenNext SPA with HTTP 200 HTML but an HTTP 404 API request.
    Never stores request URL, query parameters, headers or any response body.
    This observer is attached to the *private Playwright page*, not a user's
    other browser and never sends anything over the network.
    """
    found=[]
    expected=urlsplit(str(origin or ''))
    allowed_resources={'document', 'fetch', 'xhr', 'script', 'stylesheet', 'other'}

    def record(response):
        try:
            status=int(response.status)
            if status < 400:return
            target=urlsplit(str(response.url or ''))
            if (target.scheme,target.netloc.lower())!=(expected.scheme,expected.netloc.lower()):return
            path=target.path.lower()
            family=(_page_kind(response.url) if path.startswith(('/runs','/run-detail-somatic/',
                     '/somatic-detail-clinic/','/somatic-detail/')) else
                     'api' if path.startswith(('/api/','/v1/','/v2/','/graphql')) else
                     'other')
            resource=str(getattr(response.request,'resource_type','other')).lower()
            if resource not in allowed_resources:resource='other'
            if len(found)<16:
                found.append({'status':status,'resource':resource,'route_family':family})
        except Exception:
            return
    page.on('response',record)
    return found


def save_selection_diagnostic(folder, *, stage, page=None, origin='', http_status=None,
                              failure_kind='selection_unavailable', network_failures=None):
    """Write local, data-minimized proof for debugging 404 / stale React UI.

    No sample IDs, variants, exact paths, query strings, tokens or clipboard.
    """
    stage = stage if stage in _ALLOWED_STAGES else 'other'
    code = int(http_status) if isinstance(http_status, int) else None
    safe_kind = str(failure_kind or 'other').strip().lower()
    if not re.fullmatch(r'[a-z0-9_]{1,48}', safe_kind):
        safe_kind = 'other'
    dest = Path(folder)
    dest.mkdir(parents=True, exist_ok=True)
    created = datetime.now(timezone.utc)
    data = {
        'schema': 'gennext_selection_nav_diagnostic_v1',
        'created_at_utc': created.isoformat(timespec='seconds'),
        'privacy': 'only page type, HTTP status and allow-listed DOM counts; no PHI or browser secrets',
        'stage': stage,
        'failure_kind': safe_kind,
        'http_status': code,
        'page_kind': _page_kind(getattr(page, 'url', '') if page else ''),
        'structure': safe_selection_structure(page, origin=origin),
        'recent_http_failures': [
            {'status':int(e['status']), 'resource':str(e['resource']),
             'route_family':str(e['route_family'])}
            for e in (network_failures or [])[:16]
            if isinstance(e,dict) and isinstance(e.get('status'),int)
            and e.get('resource') in {'document','fetch','xhr','script','stylesheet','other'}
            and e.get('route_family') in {'runs','run_detail','clinical','fusion','api','other'}
        ],
    }
    path = dest / f'SELECTION_NAV_{created:%Y%m%d_%H%M%S_%f}.json'
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    return path
