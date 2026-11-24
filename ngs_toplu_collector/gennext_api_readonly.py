"""Read-only API transport contract for a future verified GenNext endpoint map.

Not connected to production collection until endpoint templates and response
schema are learned from a real authorized session and independently tested.
Do not guess endpoints or scrape access tokens from the browser.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from urllib.parse import quote, urlparse

UUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',re.I)


class APIContractError(RuntimeError):
    pass


@dataclass(frozen=True)
class VerifiedRecord:
    run_uuid: str
    sample_uuid: str
    sample_type: str

    def __post_init__(self):
        if not UUID.fullmatch(self.run_uuid) or not UUID.fullmatch(self.sample_uuid):
            raise APIContractError('Both UUIDs are required for source binding')
        if self.sample_type not in ('DNA','RNA','DNA+RNA'):
            raise APIContractError('Technical assay type is required')


def _walk_identity(value, depth=0, limit=150):
    """Find explicit runUuid+sampleUuid pairs, never rely on list positions."""
    if depth>4 or limit<1:
        return []
    if isinstance(value,dict):
        run = next((value[k] for k in ('runUuid','run_uuid','runUUID') if k in value),None)
        sample = next((value[k] for k in ('sampleUuid','sample_uuid','sampleUUID') if k in value),None)
        pairs=[]
        if run is not None and sample is not None:
            pairs.append((str(run).lower(),str(sample).lower()))
        for v in list(value.values())[:min(limit,20)]:
            if isinstance(v,(dict,list)):
                pairs.extend(_walk_identity(v,depth+1,limit-1))
        return pairs
    if isinstance(value,list):
        out=[]
        for v in value[:min(limit,20)]:
            out.extend(_walk_identity(v,depth+1,limit-1))
        return out
    return []


def verify_api_result_identity(payload, record:VerifiedRecord):
    pairs=_walk_identity(payload)
    if not pairs:
        raise APIContractError('API JSON contains no authoritative RUN+SAMPLE identity pair')
    expected=(record.run_uuid.lower(),record.sample_uuid.lower())
    if any(pair != expected for pair in pairs):
        raise APIContractError('API data includes a different technical sample identity')
    return True


class ReadOnlyGenNextAPI:
    """Transport for endpoints that have been documented/verified externally.

    Templates must explicitly contain {run_uuid} and {sample_uuid}; this module
    does not determine routes. Playwright context.request shares cookies but not
    necessarily the SPA's JS bearer tokens; HTTP 401 is not auto-bypassed.
    """
    def __init__(self,context,origin,verified_templates,allowed_hosts=None):
        self.context=context
        parsed=urlparse(str(origin))
        if parsed.scheme!='https' or not parsed.hostname:
            raise APIContractError('Only HTTPS GenNext API endpoints are supported')
        self.origin=f'{parsed.scheme}://{parsed.netloc}'
        self.allowed_hosts=set(allowed_hosts or [parsed.hostname])
        self.templates=dict(verified_templates)
        self._last_request_at=0.0

    def read(self,kind,record:VerifiedRecord,timeout_ms=30000,min_gap_s=0.8):
        template=self.templates.get(kind)
        if not template or '{run_uuid}' not in template or '{sample_uuid}' not in template:
            raise APIContractError('Verified endpoint template required; URL cannot be guessed')
        url=template.format(run_uuid=quote(record.run_uuid,safe=''),
                            sample_uuid=quote(record.sample_uuid,safe=''))
        parsed=urlparse(url)
        if parsed.scheme!='https' or parsed.hostname not in self.allowed_hosts:
            raise APIContractError('API endpoint is outside approved GenNext hosts')
        # Check both UUIDs are present in URL, including optional query string.
        all_ids={s.lower() for s in UUID.findall(url)}
        if record.run_uuid.lower() not in all_ids or record.sample_uuid.lower() not in all_ids:
            raise APIContractError('Endpoint URL not bound to both technical UUIDs')
        gap=max(0.25,min(5.0,float(min_gap_s)))
        delay=self._last_request_at+gap-time.monotonic()
        if delay>0:time.sleep(delay)
        self._last_request_at=time.monotonic()
        response=self.context.request.get(url,timeout=int(timeout_ms))
        if int(response.status)!=200:
            # No credentials, response body or full URL in exception strings.
            raise APIContractError(f'API response HTTP {int(response.status)}; source not verified')
        ctype=str(response.headers.get('content-type') or '').lower()
        if 'json' not in ctype:
            raise APIContractError('API response is not JSON')
        raw=response.body()
        if len(raw)>12*1024*1024:
            raise APIContractError('API JSON too large for bounded sample read')
        try:
            payload=json.loads(raw)
        except (ValueError,UnicodeDecodeError):
            raise APIContractError('API JSON cannot be parsed') from None
        verify_api_result_identity(payload,record)
        return payload
