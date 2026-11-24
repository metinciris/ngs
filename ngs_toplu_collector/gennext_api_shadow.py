"""In-session, passive, PHI-minimized API discovery for GenNext.

No requests are made by this module. No response values, request bodies,
headers, cookies, exact UUIDs, patient identifiers or URLs are persisted.
Successful JSON responses are inspected only when the declared body size is
small; schema keys are strictly allow-listed by api_kesif_v2.shape.

The output is structural evidence, NOT a verified clinical API contract.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from api_kesif_v2 import allowed_host, safe_path, safe_query_keys, shape

MAX_ROUTES = 240
MAX_SHAPE_BYTES = 96 * 1024
UUID_RE = __import__('re').compile(
    r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',
    __import__('re').I,
)


def page_category(url):
    path = urlparse(str(url or '')).path.lower()
    if path.startswith('/run-detail-somatic/'):
        return 'run_detail'
    if path.startswith('/somatic-detail-clinic/'):
        return 'clinical'
    if path.startswith('/somatic-detail/'):
        return 'fusion' if path.endswith('&fusion') else 'variant'
    if path.startswith('/runs'):
        return 'runs'
    return 'other'


def uuid_binding_hint(request_url, page_url):
    """Anonymized URL-only comparison, never evidence of specimen identity.

    'match' means URLs use matching UUID tokens, NOT that the response JSON
    was verified. An unknown state is always permitted and never implies error.
    """
    page = urlparse(str(page_url or ''))
    ids = [token.lower() for token in UUID_RE.findall(page.path)]
    sample = parse_qs(page.query).get('sampleUuid', [])
    if len(ids) != 1 or len(sample) != 1 or not UUID_RE.fullmatch(sample[0]):
        return 'unknown'
    expected = {ids[0], sample[0].lower()}
    got = {x.lower() for x in UUID_RE.findall(str(request_url or ''))}
    if not got:
        return 'absent'
    if expected.issubset(got):
        return 'url_tokens_match'
    if got.intersection(expected):
        return 'partial_tokens'
    return 'different_tokens'


class PassiveAPIShadow:
    """Attach to one Playwright persistent context on its worker thread."""

    def __init__(self, origin: str, diagnostic_dir: Path):
        parsed = urlparse(str(origin))
        if parsed.scheme != 'https' or not parsed.hostname or not parsed.hostname.endswith('.gennext.bio'):
            raise ValueError('Expected GenNext HTTPS origin')
        self.hostname = parsed.hostname.lower()
        self.diagnostic_dir = Path(diagnostic_dir)
        self.context = None
        self.routes: dict[str, dict] = {}
        self.starts: dict = {}
        self.events_since_save = 0
        self.cumulative = 0
        self.created = datetime.now(timezone.utc).isoformat(timespec='seconds')

    def attach(self, context):
        if self.context is context:
            return False
        # Never retain Playwright objects from an earlier or closed profile.
        self.context = context
        self.starts = {}
        self.routes = {}
        self.events_since_save = 0
        self.cumulative = 0
        self.created = datetime.now(timezone.utc).isoformat(timespec='seconds')
        context.on('request', self._on_request)
        context.on('response', self._on_response)
        context.on('requestfailed', self._on_requestfailed)
        context.on('requestfinished', self._on_requestfinished)
        return True

    def _on_request(self, request):
        try:
            if request.resource_type in ('xhr', 'fetch'):
                self.starts[request] = time.monotonic()
                if len(self.starts) > 600:
                    self.starts.clear()
        except Exception:
            pass

    def _on_requestfinished(self, request):
        self.starts.pop(request, None)

    def _on_requestfailed(self, request):
        # Network failures have no guaranteed HTTP status; log no network
        # exception string, raw URL, request object or personal information.
        try:
            if request.resource_type in ('xhr', 'fetch'):
                self._record(request, None, None, is_network_failure=True)
        except Exception:
            pass
        finally:
            self.starts.pop(request, None)

    def _on_response(self, response):
        try:
            req = response.request
            if req.resource_type not in ('xhr', 'fetch'):
                return
            status = int(response.status)
            self._record(req, response, status)
        except Exception:
            pass
        finally:
            try:
                self.starts.pop(response.request, None)
            except Exception:
                pass

    def _record(self, request, response, status, is_network_failure=False):
        parsed = urlparse(str(request.url or ''))
        if not allowed_host(parsed.hostname, self.hostname):
            return
        method = str(request.method or '').upper()
        if method not in ('GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'):
            return
        host = str(parsed.hostname or '').lower()
        masked = safe_path(request.url)
        key = f'{method} {host}{masked}'
        row = self.routes.get(key)
        if row is None:
            if len(self.routes) >= MAX_ROUTES:
                return
            row = {
                'method': method, 'host': host, 'path': masked,
                'query_keys': set(), 'status_codes': set(),
                'response_count': 0, 'network_failures': 0,
                'max_duration_ms': 0, 'source_pages': set(),
                'url_binding_hints': set(), 'json_shape': None,
                'shape_probe': 'not_observed',
            }
            self.routes[key] = row
        row['response_count'] += int(not is_network_failure)
        row['network_failures'] += int(is_network_failure)
        row['query_keys'].update(safe_query_keys(request.url))
        if status is not None:
            row['status_codes'].add(status)
        start = self.starts.get(request)
        if start is not None:
            row['max_duration_ms'] = max(row['max_duration_ms'],
                                         int(1000 * (time.monotonic() - start)))
        try:
            page_url = request.frame.page.url
        except Exception:
            page_url = ''
        row['source_pages'].add(page_category(page_url))
        row['url_binding_hints'].add(uuid_binding_hint(request.url, page_url))
        # Passive schema discovery only. Do not materialize unknown-size or
        # large payloads; explain which guard prevented schema inspection.
        if (response is not None and method == 'GET' and status == 200
                and row['json_shape'] is None):
            content_type = str(response.headers.get('content-type') or '').lower()
            length = str(response.headers.get('content-length') or '')
            if 'json' not in content_type:
                row['shape_probe'] = 'not_json'
            elif not length.isdecimal():
                row['shape_probe'] = 'missing_content_length'
            elif not (0 < int(length) <= MAX_SHAPE_BYTES):
                row['shape_probe'] = 'unknown_or_large_body'
            else:
                try:
                    body = response.body()
                    if len(body) <= MAX_SHAPE_BYTES:
                        row['json_shape'] = shape(json.loads(body))
                        row['shape_probe'] = 'schema_captured'
                    else:
                        row['shape_probe'] = 'actual_body_large'
                except Exception:
                    row['shape_probe'] = 'shape_read_failed'
        self.events_since_save += 1
        self.cumulative += 1

    def flush(self):
        if not self.events_since_save:
            return None
        output = {
            'schema': 'ngs_api_shadow_v1',
            'started_utc': self.created,
            'saved_utc': datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'privacy': ('Strict path/query/key allow-lists; no response values, specimen or '
                        'patient identifiers, full URLs, secrets, cookies or bodies.'),
            'clinical_data_verified': False,
            'notes': ('Passive browser metadata only. URL token hints are not '
                      'response identity proof. HTTP 404 is never a negative assay.'),
            'route_count': len(self.routes), 'observed_responses': self.cumulative,
            'routes': [],
        }
        for rec in sorted(self.routes.values(), key=lambda r:(r['host'],r['path'],r['method'])):
            output['routes'].append({
                **{k:v for k,v in rec.items() if k not in
                   ('status_codes','query_keys','source_pages','url_binding_hints')},
                'status_codes': sorted(rec['status_codes']),
                'query_keys': sorted(rec['query_keys']),
                'source_pages': sorted(rec['source_pages']),
                'url_binding_hints': sorted(rec['url_binding_hints']),
            })
        self.diagnostic_dir.mkdir(parents=True, exist_ok=True)
        destination = self.diagnostic_dir / 'API_HARITASI_OTOMATIK_V3.json'
        intermediate = destination.with_suffix('.json.part')
        intermediate.write_text(json.dumps(output, ensure_ascii=True, indent=2), encoding='utf-8')
        intermediate.replace(destination)
        self.events_since_save = 0
        return destination
