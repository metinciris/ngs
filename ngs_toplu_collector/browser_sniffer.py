"""v0.4.60: Local-only, privacy-bounded diagnostics of the *active* Playwright Fusion page.

This module never opens private APIs, sends data to a remote endpoint, or exports
cookies, token, request URLs/bodies, names, identities, or page HTML. It records
Fusion table shape, React availability and limited network-JSON SHAPES only.
A manual snapshot or a failed React recovery can produce the same report.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse


SNIFF_JS = r"""() => {
  const text = el => String(el?.innerText||el?.textContent||'').replace(/\s+/g,' ').trim();
  const table=[...document.querySelectorAll('[role="table"]')].find(t =>
    [...t.querySelectorAll('[role="columnheader"]')].some(h=>text(h).startsWith('Fusion Gene')) && t.getClientRects().length);
  const rows=table?[...table.querySelectorAll('[role="row"].rdt_TableRow')]
                 .filter(el=>el.getClientRects().length):[];
  const pagination=text(document.querySelector('nav.rdt_Pagination'));
  const match=pagination.match(/(\d+)\s*[-–]\s*(\d+)\s+of\s+([\d,]+)/i);
  const allowed_headers=new Set(['Fusion Gene','Found in DB','Arriba','STAR-Fusion',
       'Fusion Indication Index (FII)','OncoKB','Tools']);
  const headers=table?[...table.querySelectorAll('[role="columnheader"]')]
    .map(text).filter(x=>allowed_headers.has(x)):[];
  const n=rows.length;
  let exact=0, alternate=0, noFiber=0, stale=0, anyFiber=0;
  const problem=[];
  for(let i=0;i<n;i++){
    const el=rows[i];
    const fusion=text(el.querySelector('[role="cell"][data-column-id="3"]'));
    const nodes=[el,...el.querySelectorAll('[role="cell"]')].slice(0,18);
    let got=false,hasFiber=false,hasAlternate=false,hasStale=false;
    for(const node of nodes){
      for(const key of Object.keys(node)){
        if(!key.startsWith('__reactFiber$')&&!key.startsWith('__reactProps$'))continue;
        hasFiber=true;
        const start=node[key],queue=[start,start?.alternate],seen=new Set();
        for(let k=0;queue.length&&k<110;k++){
          const f=queue.shift();if(!f||typeof f!=='object'||seen.has(f))continue;
          seen.add(f);
          for(const props of [f.memoizedProps,f.pendingProps,f]){
            if(!props||typeof props!=='object')continue;
            for(const rr of [props.row,props.row?.original,props.data,props.item]){
              if(!rr||typeof rr!=='object'||Array.isArray(rr))continue;
              const seenName=String(rr.Fusion||'').trim();
              if(!seenName)continue;
              if(seenName===fusion){got=true;if(f===start?.alternate)hasAlternate=true;break;}
              hasStale=true;
            }
            if(got)break;
          }
          if(got)break;
          if(f.return)queue.push(f.return);
          if(f.alternate)queue.push(f.alternate);
        }
        if(got)break;
      }
      if(got)break;
    }
    if(got){exact++;if(hasAlternate)alternate++;}
    else{
      if(!hasFiber)noFiber++;
      if(hasStale)stale++;
      if(problem.length<30)problem.push({row_index:i+1,has_react_fiber:hasFiber,
           stale_other_row:hasStale,can_expand:!!el.querySelector('button[aria-label="Expand Row"]')});
    }
    if(hasFiber)anyFiber++;
  }
  // GenNext's existing in-memory Fusion capture may include raw private data.
  // Inspect JSON KEYS AND TYPES in-memory; NEVER return text, URLs or bodies.
  const risky=/(?:token|secret|cookie|auth|password|patient|identity|birth|national|firstname|lastname|email|phone|url|uuid|name)/i;
  const shape=(obj,depth=0)=>{
    if(depth>2)return {kind:'depth_limit'};
    if(obj===null)return {kind:'null'};
    if(Array.isArray(obj))return {kind:'array',count:obj.length,element:obj.length?shape(obj[0],depth+1):null};
    if(typeof obj==='object'){
      const keys=Object.keys(obj).filter(k=>!risky.test(k) && /^[A-Za-z0-9_\- ]{1,46}$/.test(k)).slice(0,35);
      return {kind:'object',keys,children:depth<2?Object.fromEntries(keys.slice(0,14)
            .map(k=>[k,shape(obj[k],depth+1)])):undefined};
    }
    return {kind:typeof obj};
  };
  const traces=[];
  const traffic=Array.isArray(window.__ngsFusionTraffic)?window.__ngsFusionTraffic:[];
  for(const entry of traffic.slice(-8)){
    if(!entry||typeof entry!=='object')continue;
    let payload={kind:'unparseable_or_too_large'};
    try {
      const raw=String(entry.text||'');
      if(raw.length>0 && raw.length<=1300000)payload=shape(JSON.parse(raw));
    }catch(e){}
    const ctype=String(entry.contentType||'').split(';')[0].toLowerCase();
    traces.push({content_type:/^[a-z\d.+\/-]{1,60}$/.test(ctype)?ctype:'other',
      body_size_bucket:String(entry.text||'').length>1000000?'large':
        String(entry.text||'').length>10000?'medium':'small',
      shape:payload});
  }
  return {fusion_table_present:!!table,visible_rows:n,
    expected_total:match?Number(match[3].replace(/,/g,'')):null,
    page_start:match?Number(match[1]):null,page_end:match?Number(match[2]):null,
    rows_per_page:text(document.querySelector('nav.rdt_Pagination select[aria-label="Rows per page:"]')?.selectedOptions?.[0])||null,
    columns:headers,
    react:{rows_with_exact_props:exact,rows_with_any_fiber:anyFiber,
           rows_with_alternate:alternate,rows_without_fiber:noFiber,
           rows_with_stale_row_props:stale,missing_examples:problem},
    network_json_shapes:traces};
}"""


def _safe_sample(label):
    v=str(label or '').strip().upper()
    return v if re.fullmatch(r'MP[0-9]{1,5}-{1,2}[0-9]{2,4}',v) else ''


def sniff_fusion_page(page, folder: Path, sample_id: str = '', reason: str = 'manual',
                      expected_origin: str = '') -> Path:
    """Take a bounded privacy-safe snapshot without writing any raw browser data."""
    origin=urlparse(str(expected_origin or ''))
    current=urlparse(str(getattr(page,'url','') or ''))
    if origin.netloc and (origin.scheme,origin.netloc)!=(current.scheme,current.netloc):
        raise ValueError('Sniffer refused: active page is outside the configured platform origin.')
    if current.scheme not in ('https','http'):
        raise ValueError('Sniffer refused: page has no web origin.')
    pathname=current.path.casefold()
    page_type=('fusion' if 'somatic-detail' in pathname and
               (current.path.casefold().endswith('fusion') or 'fusion' in current.path.casefold())
               else 'runs' if pathname.startswith('/runs') else 'other')
    data=page.evaluate(SNIFF_JS)
    if not isinstance(data,dict):
        raise ValueError('Cannot read a browser DOM snapshot.')
    now=datetime.now()
    kind=str(reason or 'manual')[:32]
    kind=re.sub('[^a-z0-9_-]','_',kind.casefold())
    sample=_safe_sample(sample_id)
    output={
        'schema':'ngs_browser_fusion_sniff_v1',
        'notice':'Only DOM/network JSON STRUCTURE (not request URLs, payloads, cookies, tokens or patient names). Not a clinical report.',
        'created_at':now.isoformat(timespec='seconds'),
        'source':'current_playwright_chromium',
        'reason':kind,
        'sample_id':sample,
        'page_type':page_type,
        'counts_and_shapes':data,
    }
    folder=Path(folder)
    folder.mkdir(parents=True,exist_ok=True)
    outfile=folder/f'FUSION_SNIFF_{now.strftime("%Y%m%d_%H%M%S_%f")}.json'
    outfile.write_text(json.dumps(output,ensure_ascii=False,indent=2),encoding='utf-8')
    return outfile
