"""Read-only GenNext API route discovery for v0.4.86.

Run ONLY while the NGS application is closed. Uses an account-specific existing
Chromium profile; never saves response bodies, request bodies, secrets, headers,
full URLs, patient IDs or variant data. Unknown JSON dictionary keys are hidden.
The output is deliberately a SCHEMA MAP, not a clinical data export.
"""
from __future__ import annotations

import argparse
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

UUID = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I)
KNOWN_SEGMENTS = frozenset(('api','v1','v2','v3','graphql','runs','run','run-detail-somatic',
    'somatic-detail','somatic-detail-clinic','samples','sample','analysis','analyzed',
    'variant','variants','snv','indel','cnv','fusion','fusions','clinical','qc','report',
    'selection','reports','detail','results','filter','filters','annotations','page',
    'list','search','fetch','index','status','somatic','germline','metadata','data',
    'patient','patients','projects','project','case','cases','workgroups','workgroup'))
KNOWN_KEYS = frozenset(('data','result','results','items','list','status','message','error',
    'errors','count','total','totalCount','page','perPage','hasNext','hasMore','next',
    'id','uuid','runUuid','runUUID','runId','sampleUuid','sampleUUID','sampleId',
    'analysisId','runStatus','sampleStatus','isAnalyzed','analyzed','selection','selected',
    'included','excluded','variants','snvs','indels','cnvs','fusions','fusion',
    'genes','gene','type','sampleType','processType','filter','filters','sort',
    'pagination','meta','metadata','quality','qc','tmb','msi','hrd','code',
    'success','createdAt','updatedAt','chromosome','position','reference',
    'alternate','transcript','effect','classification','tier','clinicalSignificance',
    'rows','columns','fields','accession','run','sample','workgroup','clinical'))
KNOWN_QUERY = frozenset(('page','perPage','pageSize','limit','offset','runUuid',
    'sampleUuid','runId','sampleId','sort','order','search','filter','type','kind',
    'status','workgroupId','selection','analyzed','variantType'))


def safe_path(url):
    path = urlparse(str(url)).path
    parts = path.split('/')
    out = []
    for p in parts:
        if not p:
            out.append(p)
        elif UUID.fullmatch(p):
            out.append('{uuid}')
        elif p.isdecimal():
            out.append('{number}')
        elif p.lower() in KNOWN_SEGMENTS:
            out.append(p.lower())
        elif re.fullmatch(r'v\d+',p,re.I):
            out.append('{version}')
        else:
            out.append('{opaque}')
    return '/'.join(out)[:240]


def safe_query_keys(url):
    return sorted({k if k in KNOWN_QUERY else '{other}'
                   for k,v in parse_qsl(urlparse(url).query, keep_blank_values=True)})


def shape(obj, depth=0):
    if depth >= 4:
        return 'truncated'
    if isinstance(obj, dict):
        # Avoid unknown keys: sometimes API data uses specimen IDs as dictionary keys.
        known = [(k,v) for k,v in obj.items() if isinstance(k,str) and k in KNOWN_KEYS]
        public = {k:shape(v,depth+1) for k,v in known[:24]}
        unknown = len(obj) - len(known)
        if unknown:
            public['{other_fields}'] = 'present'
        return public
    if isinstance(obj,list):
        return {'array':'empty' if not obj else 'present',
                'example':shape(obj[0],depth+1) if obj else None}
    if obj is None:
        return 'null'
    if isinstance(obj,bool):
        return 'bool'
    if isinstance(obj,(int,float)):
        return 'number'
    if isinstance(obj,str):
        return 'string'
    return 'unknown'


def allowed_host(host, configured_host):
    host = str(host or '').lower()
    configured_host = str(configured_host or '').lower()
    return bool(host and configured_host and
                (host==configured_host or
                 host.endswith('.gennext.bio') and configured_host.endswith('.gennext.bio')))


def discover(base, account='1', max_shape_bytes=400_000):
    from playwright.sync_api import sync_playwright
    from config_loader import load_config
    cfg = load_config(base)
    runs = str(cfg.get('runs_url') or '')
    origin = urlparse(runs)
    if origin.scheme!='https' or not origin.hostname or not origin.hostname.endswith('.gennext.bio'):
        raise RuntimeError('The configured GenNext runs_url must use HTTPS and gennext.bio')
    profile = base/'LOCAL_BROWSER_PROFILES'/('gennext_account_'+account)
    if not profile.exists():
        legacy = base/str(cfg.get('profile_dir') or 'PROFILE')
        if legacy.exists():
            profile = legacy
        else:
            profile = base/'LOCAL_BROWSER_PROFILES'/('gennext_account_'+account)
    diagnostic = base/'DIAGNOSTICS'/'API_HARITASI_V2.json'
    seen = {}
    starts = {}
    print('NGS app must be closed before launch. Browser profile:', profile.name)
    print('No request or response content will be saved.')
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(profile),headless=False,accept_downloads=False,
            args=['--disable-dev-shm-usage'])
        try:
            def on_req(request):
                if request.resource_type in ('fetch','xhr'):
                    starts[request]=time.perf_counter()

            def on_res(response):
                try:
                    req = response.request
                    if req.resource_type not in ('fetch','xhr'):
                        return
                    target = urlparse(response.url)
                    if not allowed_host(target.hostname,origin.hostname):
                        return
                    content_type = str(response.headers.get('content-type') or '').split(';')[0].lower()
                    if content_type not in ('application/json','application/problem+json') and not content_type.endswith('+json'):
                        return
                    # Request URLs are held in memory temporarily, never serialized.
                    masked = safe_path(response.url)
                    key = f'{req.method.upper()} {target.hostname}{masked}'
                    elapsed = round(1000*(time.perf_counter()-starts.pop(req,time.perf_counter())))
                    row = seen.setdefault(key,{'method':req.method.upper(),'host':target.hostname,
                        'path':masked,'resource_type':req.resource_type,
                        'status_codes':set(),'query_keys':set(),
                        'samples_seen':0,'max_duration_ms':0,'json_shape':None,
                        'authorization_header_present':False})
                    row['samples_seen']+=1
                    row['status_codes'].add(int(response.status))
                    row['max_duration_ms']=max(row['max_duration_ms'],elapsed)
                    row['query_keys'].update(safe_query_keys(response.url))
                    row['authorization_header_present'] |= ('authorization' in req.headers)
                    if (row['json_shape'] is None and response.status==200 and
                            int(response.headers.get('content-length') or 0) <= max_shape_bytes):
                        body = response.body()
                        if len(body) <= max_shape_bytes:
                            row['json_shape']=shape(json.loads(body))
                    print('[%d] %s %s' % (response.status, req.method.upper(), masked))
                except Exception:
                    return
                finally:
                    try:starts.pop(response.request,None)
                    except Exception:pass

            context.on('request',on_req)
            context.on('response',on_res)
            page=context.pages[0] if context.pages else context.new_page()
            page.goto(runs,wait_until='domcontentloaded',timeout=45000)
            print('Browse RUNS > POOL > sample > SNV/CNV/QC/RNA Fusions/Selection.')
            print('Close the first tab or press Ctrl+C to save sanitized route map.')
            try:
                page.wait_for_event('close',timeout=0)
            except KeyboardInterrupt:
                pass
        finally:
            # Must serialize before shutdown to survive a close event.
            output = {'schema':'ngs_api_route_discovery_v2',
                'generated_utc':datetime.now(timezone.utc).isoformat(timespec='seconds'),
                'privacy':'path IDs and unknown keys redacted; no values/headers/body/secrets',
                'note':'Metadata only. No source results or report decisions verified.',
                'routes':[]}
            for row in sorted(seen.values(),key=lambda d:(d['host'],d['path'],d['method'])):
                row['status_codes']=sorted(row['status_codes'])
                row['query_keys']=sorted(row['query_keys'])
                output['routes'].append(row)
            diagnostic.parent.mkdir(parents=True,exist_ok=True)
            diagnostic.write_text(json.dumps(output,ensure_ascii=True,indent=2),encoding='utf-8')
            print('Sanitized map saved:',diagnostic,'routes:',len(output['routes']))
            try:context.close()
            except Exception:pass
    return diagnostic


def main():
    parser=argparse.ArgumentParser(description='Local GenNext API route discovery (no PHI saved)')
    parser.add_argument('--account',choices=['1','2'],default='1')
    args=parser.parse_args()
    discover(Path(__file__).resolve().parent,account=args.account)


if __name__=='__main__':
    main()
