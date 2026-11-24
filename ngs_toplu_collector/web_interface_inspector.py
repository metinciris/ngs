"""NGS GenNext web inspector v0.4.64 (low-impact browser tracing).

Read-only page / network-structure diagnostics for the existing Playwright context.
The exported material intentionally excludes DOM text, attribute values with user
content, URL/query strings, headers, response/request bodies, cookies and tokens.
Source patch proposals are human reviewed; this module NEVER modifies the app.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import shutil
import time
import zipfile
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

SCHEMA = 'ngs_web_interface_session_v1'
MAX_NODES = 8000
LIVE_POLL_MS = 1800
LIVE_FRAME_MIN_INTERVAL_SEC = 2.8
LIVE_BACKOFF_MS = 3400
MAX_PAGES = 28
MAX_SNAPSHOTS = 75
MAX_EVENTS = 300

# All strings emitted from HTML are produced from controlled vocabularies or
# narrowly checked *structural* tokens. No site-supplied innerText is copied.
SNAP_JS = r'''() => {
 const d = document;
 const allowed = ['Fusion Gene','Found in DB','Arriba','STAR-Fusion',
     'Fusion Indication Index (FII)','OncoKB','Tools','Gene','Name',
     'Log2 Fold Change','Fold Change','TMB','MSI','HRD'];
 const known = new Set(allowed);
 const safeId = x => typeof x==='string' && /^(?:root|__next|app|mqc[_-][a-z0-9_-]{1,45}|igv[_-][a-z0-9_-]{1,45}|rdt[_-][a-z0-9_-]{1,45}|kt[_-][a-z0-9_-]{1,45})$/i.test(x)
                     && !/(?:patient|token|secret|password|user|email|person|firstname|lastname|name|uuid|session|auth|cookie)/i.test(x);
 const staticClasses = /^(?:rdt_[\w-]+|RunsTableNew-module__[\w-]+|Mui[\w-]+|ant-[\w-]+|kt_[\w-]+|(?:btn|table|page|pagination|form|nav|modal|spinner|loading|select|row|column|grid|card|menu|tab|container|flex|d-flex|hidden|active|disabled|selected|chakra)[\w-]{0,35})$/;
 const knownTechnicalKeys=new Set([
  'fusion','Fusion','FusionName','FusionGene','Gene','Gene1','Gene2','genes','Genes',
  'gene1','gene2','chromosome','chrom','chr','position','start','end','strand',
  'arriba','Arriba','starfusion','STARFusion','junction_reads','junctionReads',
  'split_reads1','split_reads2','splitReads1','splitReads2','coverage1','coverage2',
  'confidence','reading-frame','readingFrame','type','annotation','annotations',
  'FII','fii','oncokb','db','databases','Databases','data','results','items',
  'rows','columns','headers','table','tableData','count','total','page',
  'pagination','pageIndex','pageSize','perPage','size','status','success',
  'error','errors','message','meta','metadata','metrics','qc','summary',
  'loading','isLoading','children','row','original','value','label',
  'id','key','sampleId','runId','sampleType','runStatus','name',
  'sort','filter','filters','search','offset','limit','links',
  'render','rendered','onClick','selected','disabled','checked',
  'hasNextPage','hasPreviousPage','totalRows','getRowId',
  'memoizedProps','pendingProps','state','props','rowData','rowIndex',
  'SplitReads1','SplitReads2','Coverage1','Coverage2','Position',
  'FusionIndicationIndex','FusionInspectorSupport','STARFusionSupport'
 ]);
 const safeToken = v => (knownTechnicalKeys.has(v) && typeof v==='string' && /^[a-z][a-z0-9_-]{0,55}$/i.test(v) &&
                  !/(?:password|patient|auth|token|cookie|secret|email|phone|name|birth|session)/i.test(v));
 const nodes=[];const frames=[];
 // Bound synchronous work on the GenNext rendering thread. Heavy DNA/CNV
 // pages must be able to respond to the user while recording is active.
 const max = 8000;
 const cpuBudgetMs = 95;
 const scanStart = performance.now();
 let clipped = false, budgetClipped = false, nodeCapClipped = false;
 const elements = d.documentElement ? [d.documentElement] : [];
 const stack=elements.map(e=>[e,-1,0]);
 const counted={button:0,input:0,table:0,dialog:0,iframe:0,select:0,link:0,script:0,style:0};
 while(stack.length){
  const [node,parent,depth]=stack.pop();
  if(!node || node.nodeType!==1)continue;
  if(nodes.length>=max){clipped=true;nodeCapClipped=true;break;}
  if((nodes.length&63)===0 && performance.now()-scanStart>cpuBudgetMs){
    clipped=true;budgetClipped=true;break;
  }
  const tag=String(node.localName||'').toLowerCase();
  if(!/^[a-z][a-z0-9-]{0,30}$/.test(tag))continue;
  const i=nodes.length;
  const a={};const role=node.getAttribute('role');
  if(role && safeToken(role)) a.role=role;
  const typ=node.getAttribute('type');
  if((tag==='input'||tag==='button') && typ && safeToken(typ))a.type=typ;
  const id=node.getAttribute('id');if(id && safeId(id))a.id=id;
  const dc=node.getAttribute('data-column-id');
  if(dc && /^\d{1,3}$/.test(dc))a['data-column-id']=dc;
  const aria=node.getAttribute('aria-label');
  if(['Expand Row','Rows per page:','Next Page','Previous Page',
      'First Page','Last Page'].includes(aria))a['aria-label']=aria;
  if(node.hasAttribute('disabled'))a.disabled=true;
  if(node.hasAttribute('required'))a.required=true;
  if(node.hasAttribute('hidden'))a.hidden=true;
  if(node.shadowRoot?.mode==='open')a.shadow_root=true;
  const cl=String(node.getAttribute('class')||'').split(/\s+/).filter(c=>staticClasses.test(c)).slice(0,6);
  if(cl.length)a.class=cl;
  // getClientRects()/getBoundingClientRect() for EVERY element forces
  // repeated style/layout calculations and can stall a busy DNA viewer.
  // This structural hint only uses cheap attributes (not geometric visibility).
  const visible=!(node.hidden || node.getAttribute('aria-hidden')==='true' ||
                  node.style?.display==='none');
  nodes.push({p:parent,t:tag,a,v:visible?1:0,d:Math.min(depth,200)});
  if(tag==='button')counted.button++;if(tag==='input')counted.input++;
  if(tag==='select')counted.select++;if(tag==='table'||role==='table')counted.table++;
  if(tag==='dialog'||role==='dialog')counted.dialog++;
  if(tag==='iframe')counted.iframe++;if(tag==='script')counted.script++;
  if(tag==='style')counted.style++;if(tag==='a')counted.link++;
  const kids=node.children;
  for(let j=kids.length-1;j>=0;j--)stack.push([kids[j],i,depth+1]);
  const sk=node.shadowRoot?.children;
  if(sk)for(let j=sk.length-1;j>=0;j--)stack.push([sk[j],i,depth+1]);
 }
 const selectorCount = sel => {try{return d.querySelectorAll(sel).length}catch(e){return 0}};
 const selectors = {
  react_root:selectorCount('#root')+selectorCount('#__next'),
  role_table:selectorCount('[role="table"]'),
  role_row:selectorCount('[role="row"]'),
  role_colheader:selectorCount('[role="columnheader"]'),
  fusion_gene_header:[...d.querySelectorAll('[role="columnheader"]')].some(x=>
    String(x.innerText||x.textContent||'').trim().startsWith('Fusion Gene'))?1:0,
  fusion_row:selectorCount('[role="row"].rdt_TableRow'),
  expand_row:selectorCount('button[aria-label="Expand Row"]'),
  pagination:selectorCount('nav.rdt_Pagination'),
  pagination_next:selectorCount('button[aria-label="Next Page"]'),
  qc_export:selectorCount('#mqc_exportplots'),
  qc_data_download:selectorCount('#mqc-dl-plot-btn'),
  login_password:selectorCount('input[type="password"]'),
  runs_table:selectorCount('table[class*="RunsTableNew-module__runsTable__"]'),
  runs_qc_buttons:selectorCount('a[class*="RunsTableNew-module__qcReportBtn__"]'),
  select_elements:selectorCount('select'),
  modals:selectorCount('[role="dialog"]'),
  spinners:selectorCount('[role="progressbar"],.spinner,.loading,.spinner-grow'),
  igv_canvas:selectorCount('canvas'),
  body_page_loading:(d.body?.classList.contains('page-loading')?1:0),
  iframe:selectorCount('iframe')
 };
 // Actual GenNext column headers can contain sorting icons or extra labels.
 // Export only allow-listed names, never the original header text.
 const headers=[...d.querySelectorAll('[role="columnheader"]')]
    .map(x=>String(x.textContent||'').replace(/\s+/g,' ').trim())
    .flatMap(raw=>{
      const match=allowed.find(label=>raw===label || raw.startsWith(label+' ') ||
          raw.startsWith(label+'\n') || raw.startsWith(label+'▲') || raw.startsWith(label+'▼'));
      return match?[match]:[];
    }).slice(0,24);
 const scanReact=()=>{
  let present=0, sampled=0;const props={};
  for(const el of d.querySelectorAll('[role="row"], [role="table"], #root')){
    if(sampled>=38)break;
    sampled++;
    const keys=Object.keys(el).filter(k=>k.startsWith('__reactFiber$') || k.startsWith('__reactProps$'));
    if(!keys.length)continue;
    present++;
    const fiber=el[keys[0]];
    for(const layer of [fiber?.memoizedProps,fiber?.pendingProps,fiber?.return?.memoizedProps]){
      if(!layer||typeof layer!=='object')continue;
      for(const [k,v] of Object.entries(layer).slice(0,80)){
        if(!safeToken(k))continue;
        props[k]=typeof v==='object'?(Array.isArray(v)?'array':'object'):typeof v;
      }
    }
  }
  return {sampled_elements:sampled,fiber_elements:present,prop_types:props};
 };
 const risky = /(?:token|secret|cookie|auth|password|patient|identity|birth|national|firstname|lastname|email|phone|url|uuid|name|personal|user|session|key|bearer)/i;
 const shape=(obj,depth=0)=>{
  if(depth>=7)return {kind:'depth_limit'};
  if(obj===null)return {kind:'null'};
  if(Array.isArray(obj))return {kind:'array', count:obj.length,
      element:obj.length?shape(obj[0],depth+1):null};
  if(typeof obj==='object'){
    const keys=Object.keys(obj).filter(k=>!risky.test(k) && safeToken(k)).slice(0,65);
    const children={};
    for(const k of keys.slice(0,22))children[k]=shape(obj[k],depth+1);
    return {kind:'object',keys,children};
  }
  return {kind:typeof obj};
 };
 const shapes=[];
 const traffic=Array.isArray(window.__ngsFusionTraffic)?window.__ngsFusionTraffic.slice(-22):[];
 for(const entry of traffic){
  if(!entry||typeof entry!=='object')continue;
  try {
    const raw=String(entry.text||'');if(!raw||raw.length>1500000)continue;
    const ctype=String(entry.contentType||'').split(';')[0].toLowerCase();
    if(!/^[a-z0-9.+/-]{1,60}$/.test(ctype))continue;
    shapes.push({content_type:ctype,byte_bucket:raw.length<10000?'small':raw.length<250000?'medium':'large',shape:shape(JSON.parse(raw))});
  }catch(e){}
 }
 return {node_count:nodes.length,clipped, nodes,counts:counted,selectors,
   allowed_column_headers:headers,react:scanReact(),network_json_shapes:shapes,
   scan_metrics:{dom_tree_ms:Math.round(performance.now()-scanStart),
                 budget_clipped:budgetClipped,node_cap_clipped:nodeCapClipped,
                 visibility_mode:'attributes_only',node_limit:max}};
}'''


def _json_schema_only(value,depth=0):
    """Whitelisted field-name syntax plus type only; never preserve JSON values."""
    if depth>=7:return {'type':'depth_limit'}
    if value is None:return {'type':'null'}
    if isinstance(value,bool):return {'type':'boolean'}
    if isinstance(value,(int,float)):return {'type':'number'}
    if isinstance(value,str):return {'type':'string'}
    if isinstance(value,list):
        return {'type':'array','count':len(value),
                'element':_json_schema_only(value[0],depth+1) if value else None}
    if isinstance(value,dict):
        suspicious=re.compile(r'(?:patient|person|identity|birth|national|firstname|lastname|'
             r'email|phone|token|secret|cookie|authorization|password|url|uri|'
             r'uuid|name|session|bearer|access|apikey|api_key|userid)',re.I)
        structural={'fusion','Fusion','FusionName','FusionGene','Gene','Gene1',
           'Gene2','genes','Genes','gene1','gene2','chromosome','chrom',
           'chr','position','start','end','strand','arriba','Arriba',
           'starfusion','STARFusion','junction_reads','junctionReads',
           'split_reads1','split_reads2','splitReads1','splitReads2',
           'coverage1','coverage2','confidence','reading-frame',
           'readingFrame','type','annotation','annotations','FII','fii',
           'oncokb','db','databases','Databases','data','results','items',
           'rows','columns','headers','table','tableData','count','total',
           'page','pagination','pageIndex','pageSize','perPage','size',
           'status','success','error','errors','message','meta','metadata',
           'metrics','qc','summary','loading','isLoading','children','row',
           'original','value','label','id','key','sort','filter','filters',
           'search','offset','limit','links','sampleId','runId','sampleType',
           'runStatus','SplitReads1','SplitReads2','Coverage1','Coverage2',
           'Position','FusionIndicationIndex','FusionInspectorSupport',
           'STARFusionSupport'}
        keys=[str(k) for k in value if isinstance(k,str) and str(k) in structural and
              re.fullmatch(r'[a-zA-Z][a-zA-Z0-9_-]{0,55}',k) and
              not re.match(r'MP\d',k,re.I) and not suspicious.search(k)]
        keys=keys[:55]
        return {'type':'object','keys':keys,
                'fields':{k:_json_schema_only(value[k],depth+1) for k in keys[:22]}}
    return {'type':'other'}


def validate_ai_review(doc:dict,session_id:str=''):
    """Validate AI advice as inert descriptive TEXT. Never accept runnable code."""
    if not isinstance(doc,dict) or doc.get('schema')!='ngs_web_ui_ai_review_v1':
        raise ValueError('This is not a Web UI AI review JSON (v1)')
    sid=str(doc.get('session_id') or '')
    if session_id and sid!=session_id:
        raise ValueError('AI review refers to a different web diagnostic session')
    if not re.fullmatch(r'WEB_\d{8}_\d{6}_\d{6}',sid):
        raise ValueError('Invalid session identifier')
    issues=doc.get('findings')
    if not isinstance(issues,list) or len(issues)>80:raise ValueError('Invalid findings list')
    clean=[]
    for obj in issues:
        if not isinstance(obj,dict):raise ValueError('Invalid finding')
        item={}
        for key in ('page_type','severity','issue','suggested_change','test_to_run'):
            val=obj.get(key,'')
            if not isinstance(val,str) or len(val)>700 or any(c in val for c in '\x00\r'):
                raise ValueError('Unexpected finding text')
            # Structured recommendations are display-only. Do not parse or execute.
            item[key]=val
        if item['severity'] not in ('info','warning','critical'):
            raise ValueError('Unknown severity')
        if not re.fullmatch('[a-z_]{2,25}',item['page_type']):
            raise ValueError('Invalid page category')
        clean.append(item)
    return clean


def page_type_from_url(url: str) -> str:
    """Classification only. NEVER store full URL, query, arbitrary route segments."""
    p=urlparse(str(url or ''))
    path=p.path.lower()
    if 'somatic-detail-clinic' in path:return 'clinical'
    if 'somatic-detail' in path:
        if path.endswith('fusion'):return 'fusion'
        if path.endswith('cnv'):return 'cnv'
        if path.endswith('snv'):return 'snv'
        return 'somatic_detail'
    if path.startswith('/runs'):return 'runs'
    if 'igv' in path:return 'igv'
    if 'multiqc' in path or 'qc-report' in path:return 'qc'
    if 'login' in path or 'signin' in path:return 'login'
    return 'other'


def _allowed_origin(url, expected, parent_allowed=False):
    current=urlparse(str(url or ''))
    origin=urlparse(str(expected or ''))
    if current.scheme=='about' and parent_allowed:return True
    return current.scheme in ('http','https') and (current.scheme,current.netloc)==(origin.scheme,origin.netloc)


def _stamp():return datetime.now().strftime('%Y%m%d_%H%M%S_%f')


def _safe_json(path, value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2,sort_keys=True),encoding='utf-8')


def _sha(obj):
    return hashlib.sha256(json.dumps(obj,ensure_ascii=True,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def _html_structure(nodes):
    """Nested HTML skeleton preserving parent-child layout without any DOM text."""
    lines=['<!doctype html>','<!-- NGS: METINSIZ DOM YAPISI, URL/VERI/COOKIE YOK -->']
    open_tags=[]
    for index,n in enumerate(nodes):
        tag=n['t']; a=n.get('a') or {}
        parent=n.get('p',-1)
        while open_tags and open_tags[-1][0]!=parent:
            _,old= open_tags.pop()
            lines.append('  '*min(len(open_tags),20)+f'</{old}>')
        attrs=''
        for k,v in a.items():
            if isinstance(v,list):v=' '.join(v)
            if v is True:v=k
            attrs+=f' {k}="{html.escape(str(v),quote=True)}"'
        lines.append('  '*min(len(open_tags),20)+f'<{tag}{attrs}>')
        open_tags.append((index,tag))
    while open_tags:
        _,old=open_tags.pop()
        lines.append('  '*min(len(open_tags),20)+f'</{old}>')
    return '\n'.join(lines)+'\n'


def _checks(page_type,selectors):
    # Only route-specific selectors with an independently meaningful check.
    required={
      'fusion': {'fusion_gene_header':1,'role_table':1,'pagination':1},
      'runs': {'react_root':1,'runs_table':1},
      'qc': {'qc_export':1},
      'clinical': {'react_root':1},
      'snv': {'react_root':1},
      'cnv': {'react_root':1},
      'login': {'login_password':1},
      'igv': {'igv_canvas':1},
    }.get(page_type,{})
    return [{'check':k,'expected_min':m,'actual':int(selectors.get(k) or 0),
             'ok':int(selectors.get(k) or 0)>=m} for k,m in required.items()]


def _fingerprint(frame_data):
    nodes=frame_data.get('nodes') or []
    # DOM tree hash includes PARENT and DEPTH. It never includes user text.
    words=[(n.get('t'),n.get('p'),n.get('d'),
        [(k,tuple(v) if isinstance(v,list) else v)
         for k,v in sorted((n.get('a') or {}).items())]) for n in nodes[:8000]]
    selectors=frame_data.get('selectors') or {}
    static={k:v for k,v in selectors.items() if k not in
          ('fusion_row','role_row','expand_row','spinners','modals')}
    return {'selector_presence':{k:bool(v) for k,v in static.items()},
            'known_columns':frame_data.get('allowed_column_headers') or [],
            'tree_sha256':_sha(words),
            'node_count':int(frame_data.get('node_count') or 0),
            'clipped':bool(frame_data.get('clipped'))}


def compare_to_baseline(page_type, fingerprint, checks, baseline_folder):
    baseline_path=Path(baseline_folder)/f'{page_type}.json'
    if not baseline_path.is_file():
        return {'baseline':'missing','changed':None,'severity':'needs_baseline',
                'missing_controls':[],'changed_controls':[],'missing_columns':[],
                'notes':['Once a healthy page is confirmed, approve its baseline manually.']}
    baseline=json.loads(baseline_path.read_text(encoding='utf-8'))
    old=baseline.get('fingerprint') or {}
    a=old.get('selector_presence') or {};b=fingerprint.get('selector_presence') or {}
    missing=sorted(k for k,v in a.items() if v and not b.get(k))
    changes=sorted(k for k in a.keys()|b.keys() if bool(a.get(k))!=bool(b.get(k)))
    missing_columns=sorted(set(old.get('known_columns') or [])-set(fingerprint.get('known_columns') or []))
    failed=[c['check'] for c in checks if not c['ok']]
    tree_changed=old.get('tree_sha256')!=fingerprint.get('tree_sha256')
    changed=bool(missing or changes or missing_columns or tree_changed)
    # Full DOM hashes vary with row count, expanded sections and loaded genomic tracks.
    # Report drift without equating it to a broken selector or a clinical data error.
    severity=('critical' if failed or missing or missing_columns else
              'changed_controls' if changes else
              'structure_drift' if tree_changed else 'unchanged')
    return {'baseline':'approved','changed':changed,'severity':severity,
            'missing_controls':missing,'changed_controls':changes,'missing_columns':missing_columns,
            'tree_changed':tree_changed,
            'notes':([] if not failed else ['Route-specific required controls absent: '+', '.join(failed)])}


def approve_baseline(root:Path, session_folder:Path, *, types=None):
    """Explicit user action: accept structure from an inspected successful session."""
    root=Path(root).resolve();session_folder=Path(session_folder).resolve()
    if session_folder.parent != (root/'sessions').resolve():raise ValueError('Session is not in WEB_ARAYUZ sessions folder')
    overview=json.loads((session_folder/'SESSION.json').read_text(encoding='utf-8'))
    baseline_dir=root/'baselines';baseline_dir.mkdir(parents=True,exist_ok=True)
    chosen=set(types or [])
    success=[]
    for ent in overview.get('pages',[]):
        tp=ent.get('page_type')
        if not re.fullmatch('[a-z_]{2,24}',str(tp or '')):continue
        # Never approve unidentified, login or transient/embedded pages as healthy.
        if tp not in ('runs','fusion','snv','cnv','qc','clinical','igv'):continue
        if chosen and tp not in chosen:continue
        src=session_folder/ent.get('file','')
        if src.parent != session_folder:continue
        page=json.loads(src.read_text(encoding='utf-8'))
        if not page.get('checks') or any(not c.get('ok') for c in page['checks']):continue
        if page.get('data',{}).get('clipped') or page.get('data',{}).get('scan_metrics',{}).get('automatic_lightweight'):continue
        # Approve one READY snapshot per page type; IGV needs actual canvas nodes.
        out={'schema':'ngs_browser_approved_baseline_v1','page_type':tp,
             'fingerprint':page['fingerprint'],'approved_on':datetime.now().isoformat(timespec='seconds')}
        _safe_json(baseline_dir/f'{tp}.json',out);success.append(tp)
    return list(dict.fromkeys(success))


def list_sessions(root:Path):
    folder=Path(root)/'sessions'
    if not folder.exists():return []
    results=[]
    for p in sorted(folder.glob('WEB_*'),reverse=True):
        f=p/'SESSION.json'
        if not f.exists() or not p.is_dir():continue
        try:
            j=json.loads(f.read_text(encoding='utf-8'))
            results.append({'folder':p,'stamp':j.get('created_at',''),
              'pages':len(j.get('pages',[])),'events':len(j.get('events',[])),
              'critical':sum(x.get('severity')=='critical' for x in j.get('pages',[])),
              'mode':j.get('mode',''), 'session_id':p.name})
        except Exception:continue
        if len(results)>=60:break
    return results


def create_ai_package(root:Path, session_folder:Path, target_dir:Path):
    """Package only the already privacy-filtered files; no browser or remote upload."""
    root=Path(root).resolve();session_folder=Path(session_folder).resolve()
    if session_folder.parent != (root/'sessions').resolve():raise ValueError('Unsafe session path')
    overview=json.loads((session_folder/'SESSION.json').read_text(encoding='utf-8'))
    if overview.get('schema')!=SCHEMA:raise ValueError('Not a NGS web inspector session')
    instructions='''# Gennext Web UI — YZ icin teknik inceleme\n\nBu pakette kullanici veya hasta kimlikleri, sayfa metinleri, ham genomik veriler,\nURL/query, kimlik dogrulama basliklari, cookie ve erisim anahtari BULUNMAZ.\n`.structure.html` dosyalari tum sayfalarin metni gizlenmis DOM yapisidir.\n\n1. SESSION.json: hangi sayfalar, kontroller, olaylar, karsilastirma.\n2. PAGE_*.json: tam structural DOM ve React/API semalari.\n3. PAGE_*.structure.html: orijinal HTML DEGIL, HTML benzeri metinsiz iskelet.\n4. BASELINE_DIFF.json: daha once kullanici tarafindan onaylanmis duzenle fark.\n5. NETWORK_SCHEMAS.json: veri DEGERLERI degil, HTTP/JSON alan TIPLERI.\n6. ISSUES.json: eksik selectorler, network/JS hata siniflari.\n7. YZ_DEGERLENDIRME_SABLONU.json: doldurulabilir ONERI formati; otomatik kod yamasi degildir.\n\nGorev: Sayfa turu bazinda degisimleri yorumla, olasi sebep ve selector duzeltmeleri\noner, dogrulanmamis hasta/mutasyon verisi uretme. Her sayfa icin korunacak\nselectorleri, bozulanlari, bekleme/timeout risklerini ve guvenli yama testlerini\nonceliklendir. Kendi kendine kod/deployment yapma. Kayit olmayan sayfalara\n'goruldu' deme.  Aktarilmayan DOM metninin eksikligini acikla.\n'''
    if overview.get('mode')=='auto_collection':
        instructions+='\nOTOMATIK MOD: Kayitlar yalniz secici varligi ve olay siniflari icerir. ' \
                      'Tam DOM ve ayrintili API semasi icin manuel Web Koklayici taramasi gerekir.\n'
    target_dir=Path(target_dir);target_dir.mkdir(parents=True,exist_ok=True)
    path=target_dir/f'NGS_WEB_YZ_TANI_{session_folder.name[4:]}.zip'
    allow_files={p.name:p for p in session_folder.iterdir() if p.is_file() and
       (p.name=='SESSION.json' or (re.fullmatch(r'PAGE_\d{3}(?:\.structure)?\.(?:json|html)',p.name)) or
         p.name in ('BASELINE_DIFF.json','ISSUES.json','NETWORK_SCHEMAS.json'))}
    ai_template={
        'schema':'ngs_web_ui_ai_review_v1',
        'session_id':session_folder.name,
        'findings':[],
        'instructions':'Display-only review. NEVER execute generated patch instructions or code.'
    }
    # Fixed local-only layout; never indiscriminately zip the profile or output.
    with zipfile.ZipFile(path,'w',zipfile.ZIP_DEFLATED,compresslevel=7) as z:
        z.writestr('YZ_INCELEME_TALIMATI.md',instructions)
        z.writestr('YZ_DEGERLENDIRME_SABLONU.json',json.dumps(ai_template,ensure_ascii=False,indent=2))
        for name,p in sorted(allow_files.items()):
            z.write(p,arcname=name)
    with zipfile.ZipFile(path) as z:
        if z.testzip():raise RuntimeError('ZIP CRC verification failed')
    return path


class WebInspectorSession:
    def __init__(self, context, root:Path, runs_url:str, *, log=lambda m:None):
        self.context=context
        self.root=Path(root)
        self.runs_url=str(runs_url or '')
        origin=urlparse(self.runs_url)
        if origin.scheme not in ('http','https') or not origin.netloc:
            raise ValueError('GenNext platform origin is not configured')
        self.log=log
        self.root.mkdir(parents=True,exist_ok=True)
        (self.root/'sessions').mkdir(exist_ok=True)
        self.folder=self.root/'sessions'/('WEB_'+_stamp())
        self.folder.mkdir(exist_ok=False)
        self.pages=[];self.events=[];self.reported=set();self.network_schemas=[]
        self.network_stats=Counter()
        self._page_callbacks=[];self._ctx_callbacks=[]
        self._previous={};self._page_instances={}
        self.excluded=0
        self._live_recording=False
        self._last_live_poll={}
        self._resource_pressure=False
        self._capture_timings=[]
        self._budget_clips=0
        self._node_clips=0

    def _event(self, kind, **extra):
        if len(self.events)>=MAX_EVENTS:return
        allowed={'console_error','page_exception','http_error','request_failed','new_page'}
        if kind not in allowed:return
        data={'kind':kind,'time':datetime.now().strftime('%H:%M:%S')}
        for k in ('page_type','status','error_class','resource_type'):
            if k in extra:data[k]=extra[k]
        self.events.append(data)

    def _attach(self,page):
        if id(page) in self.reported:return
        self.reported.add(id(page))
        def on_console(msg):
            try:
                if msg.type!='error':return
                # Export only a fixed error CLASS, never msg.text, location or stack.
                body=str(getattr(msg,'text','') or '').lower()
                category=('TypeError' if 'typeerror' in body else
                          'ReferenceError' if 'referenceerror' in body else
                          'SyntaxError' if 'syntaxerror' in body else
                          'NetworkError' if 'failed to load resource' in body or 'networkerror' in body else
                          'ConsoleError')
                self._event('console_error',page_type=page_type_from_url(page.url),error_class=category)
            except Exception:pass
        def on_pageerror(err):
            # No raw message, stack trace or URL: some sites log data/tokens!
            try:
                et=type(err).__name__
                if et not in ('Error','Exception','TypeError','ReferenceError','SyntaxError','RangeError'):
                    et='JavaScriptError'
                self._event('page_exception',page_type=page_type_from_url(page.url),error_class=et)
            except Exception:pass
        page.on('console',on_console);page.on('pageerror',on_pageerror)
        self._page_callbacks.extend([(page,'console',on_console),(page,'pageerror',on_pageerror)])

    def _wire(self, *, passive=False):
        for p in list(self.context.pages):
            if not p.is_closed() and _allowed_origin(p.url,self.runs_url):self._attach(p)
        def new_page(p):
            self._event('new_page',page_type=page_type_from_url(p.url))
            self._attach(p)
        def response(r):
            try:
                if int(r.status)<400:return
                if not _allowed_origin(r.url,self.runs_url):return
                typ=str(r.request.resource_type)
                if typ not in ('xhr','fetch','document','script','stylesheet','image','other'):
                    typ='other'
                self._event('http_error',page_type=page_type_from_url(r.url),status=int(r.status),resource_type=typ)
            except Exception:pass
        def failed(req):
            try:
                if not _allowed_origin(req.url,self.runs_url):return
                typ=str(req.resource_type)
                if typ not in ('xhr','fetch','document','script','stylesheet','image','other'):
                    typ='other'
                self._event('request_failed',page_type=page_type_from_url(req.url),resource_type=typ)
            except Exception:pass
        def finished(req):
            # We export schema/field TYPES only, never API payloads, URLs or tokens.
            # Chunked JSON often has NO Content-Length; v0.4.61 missed every one.
            try:
                if len(self.network_schemas)>=45:return
                if not _allowed_origin(req.url,self.runs_url):return
                if req.resource_type not in ('xhr','fetch'):return
                self.network_stats['xhr_fetch_finished']+=1
                res=req.response()
                if res is None:
                    self.network_stats['no_response']+=1;return
                headers=res.headers
                ct=str(headers.get('content-type') or '').lower()
                if 'json' not in ct:
                    self.network_stats['non_json_content_type']+=1;return
                self.network_stats['json_content_type']+=1
                try:size=int(headers.get('content-length') or '0')
                except (ValueError,TypeError):size=0
                if size>850000:
                    self.network_stats['size_header_too_large']+=1;return
                if size<=0:self.network_stats['missing_length_header']+=1
                # Playwright stores the response already. The buffer is used only
                # in memory, then discarded; no body is ever written to disk.
                raw=res.body()
                if len(raw)>850000:
                    self.network_stats['body_too_large']+=1;return
                if not raw:
                    self.network_stats['empty_body']+=1;return
                data=json.loads(raw)
                shape=_json_schema_only(data)
                spec={'page_type':page_type_from_url(req.url),
                    'resource_type':str(req.resource_type),
                    'http_status':int(res.status),
                    'body_size_bucket':'small' if len(raw)<10000 else 'medium' if len(raw)<250000 else 'large',
                    'json_schema':shape}
                sig=_sha(spec)
                if not any(x.get('shape_sha256')==sig for x in self.network_schemas):
                    spec['shape_sha256']=sig
                    self.network_schemas.append(spec)
                self.network_stats['json_schema_captured']+=1
            except Exception:
                # No Python exception/message contents are exported: URLs or
                # sensitive response fields might occur in third-party errors.
                self.network_stats['schema_unavailable']+=1
        self.context.on('page',new_page)
        self.context.on('response',response)
        self.context.on('requestfailed',failed)
        self._ctx_callbacks=[('page',new_page),('response',response),('requestfailed',failed)]
        if passive:
            # Never call response.body() while NGS is collecting. Manual full
            # inspection is required to discover detailed API field schemas.
            self.network_stats['passive_auto_no_body_reads']=1
        else:
            self.context.on('requestfinished',finished)
            self._ctx_callbacks.append(('requestfinished',finished))

    def _unwire(self):
        for obj,k,fn in self._page_callbacks:
            try:obj.remove_listener(k,fn)
            except Exception:pass
        for k,fn in self._ctx_callbacks:
            try:self.context.remove_listener(k,fn)
            except Exception:pass
        self._page_callbacks.clear();self._ctx_callbacks.clear()

    def _capture_page(self,page, frame, page_type, frame_no, force=False, snapshot_js=None):
        evaluated_at=time.monotonic()
        try:
            safe=frame.evaluate(snapshot_js if snapshot_js is not None else SNAP_JS)
            elapsed_ms=round((time.monotonic()-evaluated_at)*1000)
            self._capture_timings.append(elapsed_ms)
            if len(self._capture_timings)>500:self._capture_timings=self._capture_timings[-500:]
            if elapsed_ms>=650:self._resource_pressure=True
            if not isinstance(safe,dict):return False
            metrics=safe.get('scan_metrics') or {}
            if metrics.get('budget_clipped'):
                self._budget_clips+=1
                self._resource_pressure=True
            if metrics.get('node_cap_clipped'):
                self._node_clips+=1
                self._resource_pressure=True
        except Exception:
            # No exception messages: may contain private location or page values.
            self._event('page_exception',page_type=page_type,error_class='SnapshotUnavailable')
            return False
        fingerprint=_fingerprint(safe)
        if id(page) not in self._page_instances:
            self._page_instances[id(page)]=len(self._page_instances)+1
        instance=self._page_instances[id(page)]
        key=(instance,frame_no,page_type)
        # No duplicate snapshots for stable screens, even if monitor polls often.
        signature=(fingerprint['tree_sha256'],
              tuple(sorted((safe.get('selectors') or {}).items())),
              tuple(safe.get('allowed_column_headers') or []))
        if not force and self._previous.get(key)==signature:return False
        self._previous[key]=signature
        checks=_checks(page_type,safe.get('selectors') or {})
        if safe.get('scan_metrics',{}).get('automatic_lightweight'):
            diff={'baseline':'skipped_auto_light','changed':None,'severity':'automatic_light_snapshot',
                  'missing_controls':[],'changed_controls':[],'missing_columns':[],
                  'tree_changed':None}
        else:
            diff=compare_to_baseline(page_type,fingerprint,checks,self.root/'baselines')
            if any(not c['ok'] for c in checks):diff['severity']='critical'
            elif safe.get('clipped'):diff['severity']='partial_structure' 
        if len(self.pages)>=MAX_SNAPSHOTS:return False
        i=len(self.pages)+1
        file=f'PAGE_{i:03d}.json';struct=f'PAGE_{i:03d}.structure.html'
        doc={'schema':'ngs_web_ui_page_v1','page_type':page_type,'page_instance':instance,'frame_index':frame_no,
          'snapshot_number':i,'captured_at':datetime.now().isoformat(timespec='seconds'),
          'privacy':'DOM structure only; no page text or network bodies',
          'data':safe,'fingerprint':fingerprint,'checks':checks,'baseline_diff':diff}
        _safe_json(self.folder/file,doc)
        (self.folder/struct).write_text(_html_structure(safe.get('nodes') or []),encoding='utf-8')
        entry={'file':file,'structure_file':struct,'page_type':page_type,'page_instance':instance,'frame_index':frame_no,
           'nodes':int(safe.get('node_count') or 0),
           'severity':diff['severity'],
           'missing':sum(not c['ok'] for c in checks),
           'changed':diff.get('changed')}
        self.pages.append(entry)
        return True

    def capture_all(self, force=False):
        count=0
        for page in list(self.context.pages)[:MAX_PAGES]:
            try:
                if page.is_closed():continue
                if not _allowed_origin(page.url,self.runs_url):
                    self.excluded+=1;continue
                self._attach(page)
                tp=page_type_from_url(page.url)
                for i,frame in enumerate(page.frames[:12]):
                    if not _allowed_origin(frame.url,self.runs_url,parent_allowed=True):continue
                    if self._live_recording and not force:
                        # Poll a stable route only once every ~3s. New tabs/paths
                        # are sampled immediately without storing the URL on disk.
                        now=time.monotonic()
                        route=urlparse(str(frame.url or '')).path
                        key=(id(page),i,route)
                        if now-self._last_live_poll.get(key,0)<LIVE_FRAME_MIN_INTERVAL_SEC:
                            continue
                        self._last_live_poll[key]=now
                    elif self._live_recording and force:
                        # Seed the first-poll timestamp so the next loop does
                        # not immediately rescan the same page after startup.
                        key=(id(page),i,urlparse(str(frame.url or '')).path)
                        self._last_live_poll[key]=time.monotonic()
                    if self._capture_page(page,frame,tp if i==0 else 'embedded',i,force=force):count+=1
            except Exception:
                self._event('page_exception',page_type='other',error_class='SnapshotUnavailable')
        return count

    def _finish(self,mode,elapsed,stopped=False):
        # Individual snapshots are evidence, NOT independent clinical failures.
        # A new Fusion/IGV route often needs several seconds before its controls
        # exist. Judge an unrecovered failure from the FINAL snapshot of that
        # page instance, preserving all intermediate DOM skeletons for AI.
        issues=[];diffs=[]
        docs={}
        last_by_page={}
        groups={}
        for x in self.pages:
            doc=json.loads((self.folder/x['file']).read_text(encoding='utf-8'))
            docs[x['file']]=doc
            key=(x.get('page_instance',0),x['frame_index'],x['page_type'])
            groups.setdefault(key,[]).append(x)
            last_by_page[key]=x['file']
        recovered_count=0
        for key, entries in groups.items():
            last=docs[entries[-1]['file']]
            # In-flight snapshots deliberately inspect only cheap selector presence.
            # They are evidence, never a definitive failed/full page assessment.
            if last.get('data',{}).get('scan_metrics',{}).get('automatic_lightweight'):
                continue
            final_ok=bool(last['checks']) and all(c.get('ok') for c in last['checks'])
            if final_ok:
                for old in entries[:-1]:
                    old_doc=docs[old['file']]
                    if any(not c.get('ok') for c in old_doc['checks']):
                        old_doc['baseline_diff']['severity']='recovered_loading'
                        old['severity']='recovered_loading'
                        _safe_json(self.folder/old['file'],old_doc)
                        recovered_count+=1
            # Unconfirmed incomplete routes are recorded as warnings, not false
            # baseline regressions. Persisting >8s or failed approved baselines
            # remain critical if still unresolved when recording stops.
            elif last['checks']:
                span=0.
                try:
                    # Consider the current consecutive missing period, not a
                    # healthy earlier Fusion view in the same tab 60s ago.
                    current_failure=entries[-1]
                    for past in reversed(entries):
                        if docs[past['file']]['checks'] and all(c.get('ok') for c in docs[past['file']]['checks']):
                            break
                        current_failure=past
                    first_time=datetime.fromisoformat(docs[current_failure['file']]['captured_at'])
                    last_time=datetime.fromisoformat(last['captured_at'])
                    span=max(0.,(last_time-first_time).total_seconds())
                except (ValueError,KeyError):pass
                confirmed=bool(mode=='single' or span>=8)
                last['baseline_diff']['severity']='critical' if confirmed else 'not_yet_ready'
                entries[-1]['severity']=last['baseline_diff']['severity']
                _safe_json(self.folder/entries[-1]['file'],last)
                if not confirmed:
                    issues.append({'page':entries[-1]['file'],'page_type':key[2],
                                   'kind':'page_not_yet_ready','severity':'warning',
                                   'missing_selectors':[c['check'] for c in last['checks'] if not c['ok']]})
        for x in self.pages:
            page=docs[x['file']]
            d=page['baseline_diff']
            diffs.append({'file':x['file'],'page_type':x['page_type'],'severity':d['severity'],
                          'missing_controls':d.get('missing_controls') or [],
                          'changed_controls':d.get('changed_controls') or [],
                          'missing_columns':d.get('missing_columns') or [],
                          'tree_changed':d.get('tree_changed')})
            key=(x.get('page_instance',0),x['frame_index'],x['page_type'])
            if last_by_page[key]!=x['file']:continue
            if (page.get('data',{}).get('clipped') and
                not page.get('data',{}).get('scan_metrics',{}).get('automatic_lightweight')):
                issues.append({'page':x['file'],'page_type':x['page_type'],
                               'kind':'dom_scan_limited','severity':'warning'})
            if d['severity']=='not_yet_ready':continue
            if d['severity']=='critical':
                for c in page['checks']:
                    if not c.get('ok'):
                        issues.append({'page':x['file'],'page_type':x['page_type'],
                                      'kind':'missing_required_selector',
                                      'selector_key':c['check'],'severity':'critical'})
                if d.get('baseline')=='approved':
                    issues.append({'page':x['file'],'page_type':x['page_type'],
                                   'kind':'approved_baseline_regression','severity':'critical',
                                   'missing_controls':d.get('missing_controls') or [],
                                   'missing_columns':d.get('missing_columns') or []})
        for ev in self.events:
            if ev['kind'] in ('page_exception','http_error','request_failed','console_error'):
                issues.append({'kind':ev['kind'],'page_type':ev.get('page_type'),
                               'severity':'warning',
                               **({'http_status':ev['status']} if 'status' in ev else {}),
                               **({'error_class':ev['error_class']} if 'error_class' in ev else {}),
                               **({'resource_type':ev['resource_type']} if 'resource_type' in ev else {})})
        perf={'browser_evaluations':len(self._capture_timings),
              'max_evaluate_ms':max(self._capture_timings,default=0),
              'slow_evaluations_650ms':sum(x>=650 for x in self._capture_timings),
              'dom_budget_clipped':self._budget_clips,
              'dom_node_cap_clipped':self._node_clips,
              'record_poll_ms':LIVE_POLL_MS,
              'adaptive_backoff':bool(self._resource_pressure)}
        if self._resource_pressure:
            issues.append({'kind':'inspector_resource_pressure','severity':'warning',
                           'page_type':'other'})
        out={'schema':SCHEMA,'created_at':datetime.now().isoformat(timespec='seconds'),
          'mode':mode,'duration_sec':round(elapsed,1),'stopped_by_user':bool(stopped),
          'privacy':'No raw DOM text, URLs, request bodies/headers, cookies, API tokens, patient names or identifiers',
          'scope':'Only currently open and manually navigated GenNext pages; unvisited app routes are NOT inspected',
          'pages':self.pages,'events':self.events,'issues_count':len(issues),
          'recovered_loading_snapshots':recovered_count,
          'network_schema_count':len(self.network_schemas),
          'network_stats':dict(self.network_stats),
          'inspector_performance':perf,
          'baselines_present':bool(list((self.root/'baselines').glob('*.json'))) if (self.root/'baselines').exists() else False,
          'excluded_external_pages':self.excluded}
        _safe_json(self.folder/'NETWORK_SCHEMAS.json',{'schema':'ngs_network_json_types_v1',
                    'entries':self.network_schemas,'stats':dict(self.network_stats)})
        _safe_json(self.folder/'ISSUES.json',{'issues':issues})
        _safe_json(self.folder/'BASELINE_DIFF.json',{'comparisons':diffs})
        _safe_json(self.folder/'SESSION.json',out)
        self.log(f"WEB KOKLAYICI: {len(self.pages)} yapisal goruntu, {len(self.events)} olay, {len(issues)} uyari; klasor kaydedildi.")
        return {'session_dir':str(self.folder),'pages':len(self.pages),'issues':len(issues),
                'events':len(self.events),'network_schemas':len(self.network_schemas),'mode':mode,'stopped':bool(stopped),
                'excluded_external_pages':self.excluded}

    def run(self, *, monitor_seconds=0, stop_event=None):
        start=time.monotonic();mode='live' if monitor_seconds else 'single'
        self._live_recording=bool(monitor_seconds)
        self._wire()
        try:
            self.capture_all(force=True)
            if monitor_seconds:
                seconds=max(10,min(int(monitor_seconds),180))
                self.log(f'WEB KOKLAYICI: {seconds} sn canli izleme; Chromiumda GenNext sayfalarinda gezinebilirsiniz.')
                while time.monotonic()-start<seconds and len(self.pages)<MAX_SNAPSHOTS:
                    if stop_event is not None and stop_event.is_set():break
                    # page.wait_for_timeout pumps Playwright event callbacks on
                    # the owning BrowserWorker thread (which Queue.get won't).
                    active=next((p for p in self.context.pages if not p.is_closed()),None)
                    wait_ms=LIVE_BACKOFF_MS if self._resource_pressure else LIVE_POLL_MS
                    if active:active.wait_for_timeout(wait_ms)
                    else:time.sleep(wait_ms/1000.)
                    self.capture_all(force=False)
            stopped=bool(stop_event and stop_event.is_set())
            return self._finish(mode,time.monotonic()-start,stopped)
        finally:
            self._live_recording=False
            self._unwire()


# v0.4.65: minimal in-flight probe; does not walk the full DOM or read text.
# It is used only after a collection task has already returned, on the
# BrowserWorker thread. The full manual inspector is kept unchanged.
AUTO_SNAP_JS = r'''() => {
 const d=document;
 const has=(sel)=>{try{return d.querySelector(sel)?1:0}catch(e){return 0}};
 const selectors={
   react_root:has('#root,#__next'),
   role_table:has('[role="table"]'), role_row:has('[role="row"]'),
   role_colheader:has('[role="columnheader"]'),
   fusion_gene_header:0, fusion_row:0,
   expand_row:has('button[aria-label="Expand Row"]'),
   pagination:has('nav.rdt_Pagination'),
   pagination_next:has('button[aria-label="Next Page"]'),
   qc_export:has('#mqc_exportplots'),qc_data_download:has('#mqc-dl-plot-btn'),
   login_password:has('input[type="password"]'),
   runs_table:has('table[class*="RunsTableNew-module__runsTable__"]'),
   runs_qc_buttons:has('a[class*="RunsTableNew-module__qcReportBtn__"]'),
   select_elements:has('select'),modals:has('[role="dialog"]'),
   spinners:has('[role="progressbar"],.spinner,.loading,.spinner-grow'),
   igv_canvas:has('canvas'),body_page_loading:has('body.page-loading'),
   iframe:has('iframe')
 };
 // Header presence only: do NOT extract actual header/row text during an
 // automated workload. Detailed page examination remains manually available.
 const h=d.querySelector('[role="columnheader"]');
 if(h && String(h.textContent||'').trim().startsWith('Fusion Gene'))
    selectors.fusion_gene_header=1;
 const nodes=[{p:-1,t:'html',a:{},v:1,d:0}];
 if(d.body)nodes.push({p:0,t:'body',a:{},v:1,d:1});
 return {node_count:nodes.length,clipped:true,nodes,counts:{},selectors,
  allowed_column_headers:[],react:{sampled_elements:0,fiber_elements:0,prop_types:{}},
  network_json_shapes:[],scan_metrics:{dom_tree_ms:0,budget_clipped:false,
    node_cap_clipped:false,visibility_mode:'selector_only',node_limit:2,
    automatic_lightweight:true}};
}'''
