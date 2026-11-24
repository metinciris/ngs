"""Passive GenNext v0.5 UI evidence captured during ordinary NGS jobs.

The recorder never navigates, clicks, sends HTTP, accesses clinical storage,
reads tokens, or exports DOM text, HTML, patient IDs, URLs, or variant values.
Only an allow-listed schema of control states, counts and HTTP error classes
is stored.  This evidence cannot validate a sample's clinical result.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from api_kesif_v2 import safe_path

ALLOWED_COMMANDS = frozenset({
    'scan_runs', 'run_queue', 'complete_latest', 'scan_report_selections',
    'scan_fusion_selections', 'scan_archive_selections_admin',
    'recovery_retry', 'recovery_open_watch',
})
ALLOWED_STAGES = frozenset({
    'runs_ready', 'dna_run_detail', 'dna_clinical', 'rna_run_detail',
    'rna_fusion', 'rna_run_detail_open', 'rna_run_detail_ready',
    'rna_fusion_popup', 'rna_fusion_enter', 'rna_fusion_identity',
    'rna_selection_button', 'rna_selection_modal', 'rna_selection_stable',
    'rna_fusion_tab_attempt', 'rna_fusion_table_ready',
    'rna_selection_button_missing',
    'rna_selection_exit', 'rna_terminal', 'dna_clinical_open',
    'dna_selection_loaded', 'dna_terminal', 'source_run', 'source_clinical', 'source_snv',
    'source_cnv', 'source_fusion', 'source_qc',
})
MAX_CAPTURE = 90
MAX_STAGE_CAPTURE = 8
MAX_HTTP_ERRORS = 150
MAX_EVALUATE_MS = 800

# Examine the page without exporting any page text or any element attribute
# containing identities, UUIDs, URLs, variant calls, or browser credentials.
# Whitelisted labels are matched IN MEMORY and exported as enum names only.
SNAP_JS = r'''() => {
 const d=document;
 const allow=/^(?:SomaticDetailTable-module__[A-Za-z0-9_-]{1,64}|ReportSelection-module__[A-Za-z0-9_-]{1,64}|RunsTableNew-module__[A-Za-z0-9_-]{1,64}|rdt_[A-Za-z0-9_-]{1,56}|variant-filtering-button|active|selected|disabled|btn|btn-primary|btn-light-primary|nav-link|tab|tab-active|show|fade)$/;
 const classNames=e=>(e&&e.classList)?[...e.classList].filter(c=>allow.test(c)).slice(0,7):[];
 const isActive=e=>!!(e&&([...(e.classList||[])].some(c=>/^(?:active|selected|Mui-selected|ant-tabs-tab-active)$/.test(c)||/module__active__/.test(c))||e.getAttribute('aria-selected')==='true'));
 const tag=e=>{const t=e?.localName||'';return ['button','a','label','div','span','section','nav','input','table','ul','li'].includes(t)?t:'other'};
 const role=e=>{const r=e?.getAttribute('role');return ['tab','tabpanel','button','dialog','table','row','columnheader','switch','checkbox'].includes(r)?r:null};
 const mark=e=>{
  const sel=e.getAttribute('aria-selected'), pressed=e.getAttribute('aria-pressed');
  let a=e.parentElement;const ancestors=[];
  for(let i=0;i<3&&a;i++,a=a.parentElement){
   ancestors.push({tag:tag(a),role:role(a),classes:classNames(a),active:isActive(a)});
  }
  const r=e.getBoundingClientRect();
  return {
    tag:tag(e),role:role(e),classes:classNames(e),
    parent_classes:classNames(e.parentElement),ancestors,
    has_active_class:isActive(e),
    aria_selected:sel==='true'?true:sel==='false'?false:null,
    aria_pressed:pressed==='true'?true:pressed==='false'?false:null,
    state:['active','inactive','open','closed','selected','unselected'].includes(e.getAttribute('data-state'))?e.getAttribute('data-state'):null,
    disabled:!!e.disabled || e.getAttribute('aria-disabled')==='true',
    hidden:!!e.hidden || e.getAttribute('aria-hidden')==='true',
    visible_box:!!(r.width>0&&r.height>0),
    child_badge_present:!!e.querySelector('[class*="badge"],[class*="Badge"]'),
  };
 };
 const labelKey=s=>{
  s=String(s||'').replace(/\s+/g,' ').trim().toLowerCase();
  if(/^fusions?(?:\s*\(?\d+\)?)?$/.test(s))return 'FUSIONS';
  if(/^fusion genes(?:\s*\(?\d+\)?)?$/.test(s))return 'FUSION_GENES';
  if(/^selection(?:\s*\(?\d+\)?)?$/.test(s))return 'SELECTION';
  if(/^analyzed$/.test(s))return 'ANALYZED';
  if(/^clinical snvs(?:\/short indel)?(?:\s*\(?\d+\)?)?$/.test(s))return 'CLINICAL_SNVS';
  if(/^snvs?(?:\s*\(?\d+\)?)?$/.test(s))return 'SNV';
  if(/^cnvs?(?:\s*\(?\d+\)?)?$/.test(s))return 'CNV';
  if(/^qc$/.test(s))return 'QC';
  if(/^sample id$/.test(s))return 'SAMPLE_ID';
  return null;
 };
 const controls={};
 const elements=[...d.querySelectorAll('button,a,[role="tab"],label')].slice(0,850);
 for(const el of elements){
   const key=labelKey(el.textContent)||labelKey(el.getAttribute('aria-label'));
   if(!key)continue;
   if(!controls[key])controls[key]=[];
   if(controls[key].length<7)controls[key].push(mark(el));
 }
 const count=s=>{try{return d.querySelectorAll(s).length}catch(e){return 0}};
 const headers=[...d.querySelectorAll('[role="columnheader"], th')].slice(0,45);
 const headerKinds=[];
 for(const el of headers){
   const s=(el.textContent||'').trim().toLowerCase();
   let code='OTHER';
   if(/^fusion gene(s)?$/.test(s))code='FUSION_GENE';
   else if(/^arriba$/.test(s))code='ARRIBA';
   else if(/^star[-\s]?fusion$/.test(s))code='STAR_FUSION';
   else if(/^oncokb$/.test(s))code='ONCOKB';
   else if(/^tools?$/.test(s))code='TOOLS';
   else if(/^gene$/.test(s))code='GENE';
   else if(/^variant$/.test(s))code='VARIANT';
   if(!headerKinds.includes(code))headerKinds.push(code);
 }
 const table={
   runs_table:count('table[class*="RunsTableNew-module__runsTable__"]'),
   table_count:count('table,[role="table"]'),
   fusion_table:count('[role="table"].rdt_Table,[role="table"]'),
   fusion_header:headerKinds.includes('FUSION_GENE'),
   header_kinds:headerKinds,
   row_count:Math.min(count('[role="row"]'),1000),
   column_count:Math.min(count('[role="columnheader"]'),50),
   expandable_rows:Math.min(count('button[aria-label="Expand Row"]'),1000),
   pagination:count('nav.rdt_Pagination'),
   next_page:count('button[aria-label="Next Page"],button[title="Next Page"]'),
   selection_modal:count('[class*="ReportSelection-module__overlay__"]'),
   selection_groups:count('[class*="ReportSelection-module__group__"]'),
   selection_rows:count('[class*="ReportSelection-module__optionRow__"]'),
   switches:count('[role="switch"]'),
   checkbox_count:count('input[type="checkbox"]'),
   analyzed_switches:count('input[value="runIsSolved"]'),
   busy:count('[role="progressbar"],.spinner,.loading,.spinner-grow'),
   react_root:count('#root,#__next'),
   tab_buttons:count('[role="tab"]'),
   tab_panels:count('[role="tabpanel"]'),
 };
 const roots=[...d.querySelectorAll('.rdt_Table,[role="table"],[class*="ReportSelection-module__overlay__"]')].slice(0,5);
 const focus_structure=roots.map(el=>({tag:tag(el),role:role(el),classes:classNames(el),
   ancestors:(()=>{const r=[];let p=el.parentElement;for(let i=0;i<3&&p;i++,p=p.parentElement)
     r.push({tag:tag(p),role:role(p),classes:classNames(p)});return r})()}));
 const errors=[...d.querySelectorAll('h1,h2,[role="alert"],.toast,.error')].slice(0,35);
 const ui404=errors.some(el=>/^(?:404|not found|page not found|request failed with status code 404)$/i
  .test(String(el.textContent||'').replace(/\s+/g,' ').trim()));
 return {ready_state:['loading','interactive','complete'].includes(d.readyState)?d.readyState:'loading',
    controls,table,focus_structure,ui_404:ui404};
}'''


def route_family(url):
    p=urlparse(str(url or '')).path.lower()
    if p.startswith('/runs'): return 'runs'
    if p.startswith('/run-detail-somatic/'): return 'run_detail'
    if p.startswith('/somatic-detail-clinic/'): return 'clinical'
    if p.startswith('/somatic-detail/'): return 'fusion' if p.endswith('&fusion') else 'somatic_detail'
    if 'igv' in p: return 'igv'
    if 'qc' in p: return 'qc'
    return 'other'


def trusted_origin(url, expected):
    a=urlparse(str(url or ''))
    b=urlparse(str(expected or ''))
    return a.scheme==b.scheme=='https' and a.netloc.lower()==b.netloc.lower()


def status_host_allowed(url, expected):
    a=urlparse(str(url or ''))
    b=urlparse(str(expected or ''))
    return (a.scheme=='https' and bool(b.hostname) and
            a.hostname and a.hostname.lower().endswith('.gennext.bio') and
            b.hostname.lower().endswith('.gennext.bio'))


class AutoV05Structure:
    def __init__(self, base, runs_url, *, enabled=True, log=None):
        self.base=Path(base)
        self.runs_url=str(runs_url or '')
        self.enabled=bool(enabled)
        self.log=log or (lambda message:None)
        self.command=''
        self.records=[]
        self.errors=[]
        self.counts=Counter()
        self.keys=set()
        self.context=None
        self.started=None
        self.suspended=False
        self.attempts=Counter()
        self.skip_reasons=Counter()
        self.diagnostic_dir=self.base/'DIAGNOSTICS'/'V05_OTOMATIK'

    def begin(self,command,context=None):
        self.detach()
        self.command=command if command in ALLOWED_COMMANDS and self.enabled else ''
        self.records=[];self.errors=[];self.counts=Counter();self.keys=set()
        self.attempts=Counter();self.skip_reasons=Counter()
        self.started=datetime.now(timezone.utc)
        self.suspended=False
        if self.command and context is not None:
            self.attach(context)

    def attach(self,context):
        if not self.command or self.context is context:return
        self.detach()
        self.context=context
        try:context.on('response',self._on_response)
        except Exception:self.context=None

    def detach(self):
        if self.context is not None:
            try:self.context.remove_listener('response',self._on_response)
            except Exception:pass
        self.context=None

    def _on_response(self,response):
        try:
            status=int(response.status)
            if status<400 or not status_host_allowed(response.url,self.runs_url):return
            if len(self.errors)>=MAX_HTTP_ERRORS:return
            resource=str(response.request.resource_type).lower()
            if resource not in ('xhr','fetch','document','script','stylesheet','other'):
                resource='other'
            try:
                origin_page=response.request.frame.page.url
            except Exception:
                origin_page=''
            self.errors.append({
                'status':status,'resource':resource,
                'method':str(getattr(response.request,'method','')).upper() if
                    str(getattr(response.request,'method','')).upper() in ('GET','POST','PUT','PATCH','DELETE','OPTIONS')
                    else 'OTHER',
                'route_family':route_family(response.url) if
                    '/api/' not in urlparse(response.url).path else 'api',
                'masked_path':safe_path(response.url),
                'page_type':route_family(origin_page),
            })
        except Exception:pass

    def capture(self,page,stage):
        """Best-effort; never throws into a clinical collection task."""
        if not self.command or stage not in ALLOWED_STAGES:
            return False
        self.attempts[stage]+=1
        if self.suspended or len(self.records)>=MAX_CAPTURE or self.counts[stage]>=MAX_STAGE_CAPTURE:
            self.skip_reasons['limit_or_suspended']+=1
            return False
        try:
            if page is None or page.is_closed():
                self.skip_reasons['missing_page']+=1
                return False
            if not trusted_origin(page.url,self.runs_url):
                self.skip_reasons['different_origin']+=1
                return False
            start=time.monotonic()
            result=page.evaluate(SNAP_JS)
            elapsed=int((time.monotonic()-start)*1000)
            if elapsed>MAX_EVALUATE_MS:
                self.suspended=True
                self.log('GENNEXT V05: Web yapi yoklamasi yavas; geri kalan yoklamalar durduruldu.')
            if not isinstance(result,dict):return False
            # Defense in depth: export only expected fields, not anything else.
            table=result.get('table') if isinstance(result.get('table'),dict) else {}
            table_keys=('runs_table','table_count','fusion_table','fusion_header','row_count',
                        'column_count','expandable_rows','pagination','next_page','selection_modal',
                        'selection_groups','selection_rows','switches','checkbox_count',
                        'analyzed_switches','busy','react_root','tab_buttons','tab_panels')
            clean_table={k:(v if type(v) is bool else min(max(v,0),1000))
                         for k in table_keys for v in [table.get(k)]
                         if type(v) in (bool,int)}
            # The only stored header names are fixed technical ENUM values.
            allowed_headers={'FUSION_GENE','ARRIBA','STAR_FUSION','ONCOKB','TOOLS',
                             'GENE','VARIANT','OTHER'}
            headers=table.get('header_kinds')
            if isinstance(headers,list):
                clean_table['header_kinds']=[k for k in headers[:16] if k in allowed_headers]
            def safe_classes(seq,lim=7):
                if not isinstance(seq,list): return []
                return [c for c in seq[:lim*2] if isinstance(c,str) and len(c)<=88
                    and re.fullmatch(r'[A-Za-z0-9_-]+',c)
                    and not re.search(r'patient|personal|name|token|secret|auth|uuid|cookie|email|phone|user',c,re.I)
                    and (c.startswith(('SomaticDetailTable-module__','ReportSelection-module__',
                         'RunsTableNew-module__','rdt_')) or c in
                         ('variant-filtering-button','active','selected','disabled',
                          'btn','btn-primary','btn-light-primary','nav-link','tab','tab-active',
                          'show','fade'))][:lim]
            allowed_tags={'button','a','label','div','span','section','nav','input','table','ul','li','other'}
            allowed_roles={'tab','tabpanel','button','dialog','table','row','columnheader','switch','checkbox'}
            def safe_ancestors(ancs):
                if not isinstance(ancs,list):return []
                return [{'tag':a.get('tag') if a.get('tag') in allowed_tags else 'other',
                        'role':a.get('role') if a.get('role') in allowed_roles else None,
                        'classes':safe_classes(a.get('classes'),6),
                        'active':a.get('active') is True}
                       for a in ancs[:3] if isinstance(a,dict)]
            controls={}
            for key in ('FUSIONS','FUSION_GENES','SELECTION','ANALYZED','CLINICAL_SNVS',
                        'SNV','CNV','QC','SAMPLE_ID'):
                ls=(result.get('controls') or {}).get(key)
                if not isinstance(ls,list):continue
                filtered=[]
                for raw in ls[:7]:
                    if not isinstance(raw,dict):continue
                    classes=safe_classes(raw.get('classes'))
                    parent=safe_classes(raw.get('parent_classes'),5)
                    filtered.append({
                        'tag':raw.get('tag') if raw.get('tag') in allowed_tags else 'other',
                        'role':raw.get('role') if raw.get('role') in allowed_roles else None,
                        'classes':classes, 'parent_classes':parent,
                        'ancestors':safe_ancestors(raw.get('ancestors')),
                        'has_active_class':raw.get('has_active_class') is True,
                        'aria_selected':raw.get('aria_selected') if type(raw.get('aria_selected')) is bool else None,
                        'aria_pressed':raw.get('aria_pressed') if type(raw.get('aria_pressed')) is bool else None,
                        'state':raw.get('state') if raw.get('state') in
                                 ('active','inactive','open','closed','selected','unselected') else None,
                        'disabled':raw.get('disabled') is True,
                        'hidden':raw.get('hidden') is True,
                        'visible_box':raw.get('visible_box') is True,
                        'child_badge_present':raw.get('child_badge_present') is True,
                    })
                if filtered:controls[key]=filtered
            focus=[]
            for raw in (result.get('focus_structure') or [])[:5]:
                if not isinstance(raw,dict):continue
                focus.append({'tag':raw.get('tag') if raw.get('tag') in allowed_tags else 'other',
                              'role':raw.get('role') if raw.get('role') in allowed_roles else None,
                              'classes':safe_classes(raw.get('classes')),
                              'ancestors':safe_ancestors(raw.get('ancestors'))})
            entry={'stage':stage,'page_type':route_family(page.url),
                   'ready_state':result.get('ready_state') if result.get('ready_state') in
                   ('loading','interactive','complete') else 'unknown',
                   'ui_404':result.get('ui_404') is True,
                   'table':clean_table,'controls':controls,
                   'focus_structure':focus}
            digest=hashlib.sha256(json.dumps(entry,sort_keys=True,separators=(',',':')).encode()).hexdigest()
            if digest in self.keys:
                self.skip_reasons['duplicate_structure']+=1
                return False
            self.keys.add(digest)
            self.counts[stage]+=1
            entry['seen_utc']=datetime.now(timezone.utc).isoformat(timespec='seconds')
            self.records.append(entry)
            return True
        except Exception:
            self.counts['capture_error']+=1
            self.skip_reasons['evaluation_error']+=1
            return False

    def finish(self,api_map=None,outcome='unknown',failure_type=''):
        """Local zip only; never uploads. Calling this twice has no side effects."""
        if not self.command:return None
        try:
            self.diagnostic_dir.mkdir(parents=True,exist_ok=True)
            stamp=self.started.strftime('%Y%m%d_%H%M%S_%f')
            path=self.diagnostic_dir/f'NGS_V05_OTOMATIK_{stamp}.zip'
            summary={
                'schema':'ngs_v05_auto_capture_v3','command':self.command,
                'worker_outcome':outcome if outcome in ('completed','stopped','waiting','error') else 'unknown',
                'worker_error_class':failure_type if isinstance(failure_type,str) and
                    re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,79}',failure_type) else None,
                'generated_utc':datetime.now(timezone.utc).isoformat(timespec='seconds'),
                'structure_snapshots':len(self.records),'http_error_events':len(self.errors),
                'suspended':self.suspended,'capture_errors':self.counts['capture_error'],
                'clinical_data_verified':False,
                'capture_attempts_by_stage':dict(sorted(self.attempts.items())),
                'snapshots_by_stage':dict(sorted((k,v) for k,v in self.counts.items()
                    if k in ALLOWED_STAGES)),
                'skipped_by_reason':dict(sorted(self.skip_reasons.items())),
                'rna_fusion_table_snapshots':sum(1 for x in self.records if
                    x.get('page_type')=='fusion' and x.get('table',{}).get('fusion_header')),
                'rna_selection_modal_snapshots':sum(1 for x in self.records if
                    x.get('page_type')=='fusion' and x.get('table',{}).get('selection_modal')),
                'note':'Metadata only: a loaded tab, HTTP 200 or empty Selection never proves a clinical result.',
                'privacy':'No page text, identifiers, variant values, URLs, credentials, headers or bodies.',
            }
            if api_map is None:
                api_map=self.base/'DIAGNOSTICS'/'API_HARITASI_OTOMATIK_V3.json'
            api_map=Path(api_map)
            with zipfile.ZipFile(path,'w',compression=zipfile.ZIP_DEFLATED) as out:
                out.writestr('SUMMARY.json',json.dumps(summary,ensure_ascii=False,indent=2))
                out.writestr('UI_YAPI.json',json.dumps(self.records,ensure_ascii=False,indent=2))
                # Keep the path already anonymized by the same strict API allow-list.
                # Aggregate repeated 500s/404s rather than exporting a long duplicate log.
                errors=Counter((row['status'],row['resource'],row['method'],
                    row['route_family'],row['masked_path'],row['page_type'])
                    for row in self.errors)
                out.writestr('HTTP_HATALARI.json',json.dumps([
                    {'status':x['status'],'resource':x['resource'],
                     'route_family':x['route_family']} for x in self.errors],
                    ensure_ascii=False,indent=2))
                out.writestr('HTTP_YOL_HATALARI.json',json.dumps([
                    {'status':t[0], 'resource':t[1], 'method':t[2],
                     'route_family':t[3], 'masked_path':t[4],
                     'page_type':t[5], 'count':count}
                    for t,count in sorted(errors.items())],ensure_ascii=False,indent=2))
                if api_map.is_file() and api_map.stat().st_size<=2*1024*1024:
                    try:
                        shadow=json.loads(api_map.read_text(encoding='utf-8'))
                        if shadow.get('schema')=='ngs_api_shadow_v1':
                            out.writestr('API_HARITASI_OTOMATIK_V3.json',
                                         json.dumps(shadow,ensure_ascii=False,indent=2))
                    except Exception:pass
                out.writestr('README.txt',
                    'No clinical data. No automatic upload. Use for debugging the GenNext UI and API structure only.\n'
                    'Manually review this ZIP before sharing. All records have masked paths and fixed structural keys.\n')
            return path
        finally:
            self.detach()
            self.command=''
