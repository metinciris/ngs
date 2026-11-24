"""Recover verified, SAMPLE-SPECIFIC filtered Fusion caller support from a live React grid.

The GenNext native TSV currently coerces nested caller objects to '[object Object]'.
That TSV remains unmodified. Only data rendered for the selected sample, including
React row props, are read; no guessed API endpoints or clinical conclusions.

A full recovered filtered table can count as an *acquired analytical source*
only if ALL expected React grid rows, with readable caller evidence, are captured.
This is NOT a statement of clinical oncogenicity or reportability.
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from source_validation import fusion_candidate_identity, validate_fusion_source


class ReactCaptureStopped(Exception):
    """Raised when the user clicks stop in the host Collector."""


# Restrict DOM capture to explicit Fusion result fields; no app state, cookies,
# tokens, clinical text, names, or arbitrary React object trees are exported.
PAGE_JS = r"""() => {
  const text=e=>String(e?.innerText || e?.textContent || '').replace(/\s+/g,' ').trim();
  const table=[...document.querySelectorAll('[role="table"]')].find(t=>
    [...t.querySelectorAll('[role="columnheader"]')].some(h=>text(h).startsWith('Fusion Gene')) && t.getClientRects().length);
  const nav=document.querySelector('nav.rdt_Pagination');
  const pageText=text(nav);
  const match=pageText.match(/(\d+)\s*[-–]\s*(\d+)\s+of\s+([\d,]+)/i);
  const meta={start:match?Number(match[1]):null,end:match?Number(match[2]):null,
     total:match?Number(match[3].replace(/,/g,'')):null,pagination:pageText.slice(0,100),
     selected_size:nav?.querySelector('select[aria-label="Rows per page:"]')?.value||'',
     patient_header:text(document.querySelector('h1.page-heading')),
     has_table:!!table};
  const small=(value) => {
    if(value===null || value===undefined) return null;
    if(typeof value==='string')return value.slice(0,300);
    if(typeof value==='number' && Number.isFinite(value))return value;
    if(typeof value==='boolean')return value;
    return null;
  };
  function shallow(obj,allow){
    if(!obj || typeof obj!=='object' || Array.isArray(obj))return null;
    const out={};
    for(const k of allow){
      if(Object.prototype.hasOwnProperty.call(obj,k)){
        const v=small(obj[k]); if(v!==null)out[k]=v;
      }
    }
    return Object.keys(out).length?out:null;
  }
  const ARRIBA=['confidence','coverage1','coverage2','discordant_mates','position',
      'reading-frame','split_reads1','split_reads2','type'];
  const STAR=['ffmp','ffpm','junction_reads','spanning_reads','position'];
  function getRow(el,gene){
    // React reuses row nodes after pagination. memoizedProps may still contain
    // the PREVIOUS page while pendingProps or alternate holds the new row.
    // Inspect all variants independently, but bind only an EXACT Fusion match.
    const candidates=[el,...el.querySelectorAll('[role="cell"]')].slice(0,18);
    for(const node of candidates){
      for(const k of Object.keys(node)){
        if(!k.startsWith('__reactFiber$')&&!k.startsWith('__reactProps$'))continue;
        const start=node[k];
        const queue=[start,start?.alternate];
        const seen=new Set();
        for(let steps=0;queue.length&&steps<110;steps++){
          const f=queue.shift();
          if(!f||typeof f!=='object'||seen.has(f))continue;
          seen.add(f);
          for(const props of [f.memoizedProps,f.pendingProps,f]){
            if(!props||typeof props!=='object')continue;
            for(const r of [props.row,props.row?.original,props.data,props.item]){
              if(!r||typeof r!=='object'||Array.isArray(r))continue;
              if(String(r.Fusion||'').trim()!==gene)continue;
              const databases=Array.isArray(r.Databases)?r.Databases.map(small)
                .filter(x=>typeof x==='string').slice(0,12)
                :(typeof r.Databases==='string'?[r.Databases.slice(0,300)]:[]);
              return {found:true,fusion:gene,db:databases,
                formula:small(r['Explained FII']),fii:small(r['Fusion Indication Index (FII)']),
                arriba:shallow(r.arriba,ARRIBA),starfusion:shallow(r.starfusion,STAR)};
            }
          }
          if(f.return)queue.push(f.return);
          if(f.alternate)queue.push(f.alternate);
        }
      }
    }
    return {found:false,fusion:gene,db:[],formula:null,fii:null,arriba:null,starfusion:null};
  }
  const trs=table?[...table.querySelectorAll('[role="row"].rdt_TableRow')]
    .filter(el=>el.getClientRects().length):[];
  return {...meta,rows:trs.map((el,i)=>{
    const gene=text(el.querySelector('[role="cell"][data-column-id="3"]'));
    return {...getRow(el,gene),page_row:i};
  })};
}"""

FIELDS = [
    'FusionName','Gene1','Gene2','Breakpoint1','Breakpoint2','Frame',
    'JunctionReadCount','SpanningFragCount','FFPM','Arriba Confidence',
    'Arriba Split Reads1','Arriba Split Reads2','Arriba Coverage1','Arriba Coverage2',
    'Arriba Type','Arriba Position','Arriba Discordant Mates',
    'STAR-Fusion Position','STAR-Fusion Raw ffmp',
    'Known/Recurrent','FII','STAR-Fusion Support',
    'CollectorProvenance',
]


def _str(x):
    return '' if x is None else str(x)


def normalise_row(row):
    """Return one candidate table row WITHOUT inferring missing read counts."""
    fusion, gene1, gene2=fusion_candidate_identity({'FusionName':row.get('fusion','')})
    if not gene1 or not gene2:
        return None
    a=row.get('arriba') if isinstance(row.get('arriba'),dict) else {}
    st=row.get('starfusion') if isinstance(row.get('starfusion'),dict) else {}
    # STAR-Fusion and Arriba use distinct breakpoint syntaxes, both kept as
    # exported by the platform; no genome-build conversion is inferred.
    position=_str(st.get('position') or a.get('position'))
    bp=position.split('#',1) if '#' in position else [position,'']
    known=row.get('db') or []
    if isinstance(known,list):known='; '.join(_str(x) for x in known if x is not None)
    has_st=bool(st)
    return {
      'FusionName':fusion,'Gene1':gene1,'Gene2':gene2,
      'Breakpoint1':bp[0],'Breakpoint2':bp[1],
      'Frame':_str(a.get('reading-frame')),
      'JunctionReadCount':_str(st.get('junction_reads')),
      'SpanningFragCount':_str(st.get('spanning_reads')),
      'FFPM':_str(st.get('ffpm',st.get('ffmp'))),
      'Arriba Confidence':_str(a.get('confidence')),
      'Arriba Split Reads1':_str(a.get('split_reads1')),
      'Arriba Split Reads2':_str(a.get('split_reads2')),
      'Arriba Coverage1':_str(a.get('coverage1')),
      'Arriba Coverage2':_str(a.get('coverage2')),
      'Arriba Type':_str(a.get('type')),
      'Arriba Position':_str(a.get('position')),
      'Arriba Discordant Mates':_str(a.get('discordant_mates')),
      'STAR-Fusion Position':_str(st.get('position')),
      'STAR-Fusion Raw ffmp':_str(st.get('ffmp')),
      'Known/Recurrent':_str(known),
      'FII':_str(row.get('fii')),
      'STAR-Fusion Support':_str(st.get('junction_reads')) if has_st else '',
      'CollectorProvenance':'sample_specific_react_filtered',
    }


def _row_has_caller_evidence(row):
    a=row.get('arriba') if isinstance(row.get('arriba'),dict) else {}
    st=row.get('starfusion') if isinstance(row.get('starfusion'),dict) else {}
    def valid_position(position):
        parts=str(position or '').split('#')
        return len(parts)==2 and all(
            re.match(r'^chr[A-Za-z0-9_.-]+:[0-9]+(?::[+-])?$', x) for x in parts
        )
    def valid_reads(value):
        return bool(re.fullmatch(r'[0-9]+(?:\.[0-9]+)?',str(value).strip())) if value is not None else False
    return bool(
        (valid_position(a.get('position')) and
         (valid_reads(a.get('split_reads1')) or valid_reads(a.get('split_reads2'))))
        or (valid_position(st.get('position')) and valid_reads(st.get('junction_reads')))
    )


# GenNext's "Expand Row" panel is a SECOND, independent on-screen source
# when virtualized/reused React nodes no longer expose a matching row object.
# Only caller fields from the page's explicitly labeled details are accepted.
# Never infer read counts or breakpoints from a gene name or another sample.
_EXPANDED_FIELDS = {
    'confidence': ('arriba', 'confidence'),
    'coverage1': ('arriba', 'coverage1'),
    'coverage2': ('arriba', 'coverage2'),
    'discordant mates': ('arriba', 'discordant_mates'),
    'position': (None, 'position'),
    'reading frame': ('arriba', 'reading-frame'),
    'split reads1': ('arriba', 'split_reads1'),
    'split reads2': ('arriba', 'split_reads2'),
    'type': ('arriba', 'type'),
    'ffpm': ('starfusion', 'ffpm'),
    'ffmp': ('starfusion', 'ffmp'),
    'junction reads': ('starfusion', 'junction_reads'),
    'spanning reads': ('starfusion', 'spanning_reads'),
}


def parse_expanded_caller_details(text):
    """Decode ONLY labeled Arriba/STAR-Fusion detail panel values.

    Empty or unrelated material returns no caller evidence. Raw labels must be
    explicitly present on the page. No patient/clinical text is retained.
    """
    parts=[str(x).strip() for x in str(text or '').splitlines() if str(x).strip()]
    result={'arriba':{}, 'starfusion':{}}
    section=None
    for i,line in enumerate(parts):
        low=line.casefold().strip().strip(':')
        if low == 'arriba details':
            section='arriba';continue
        if low in ('star-fusion details', 'starfusion details'):
            section='starfusion';continue
        if low not in _EXPANDED_FIELDS or section is None or i+1>=len(parts):
            continue
        target,key=_EXPANDED_FIELDS[low]
        if target is not None and target != section:
            continue
        value=parts[i+1]
        if (not value or len(value)>250 or value.casefold().strip(':') in _EXPANDED_FIELDS
                or value.casefold().endswith(' details')):
            continue
        result[section][key]=value
    return {k:v for k,v in result.items() if v}


def _dom_expanded_detail(page, index, expected_fusion):
    """Read expanded text ONLY from the identical visible row, never its neighbor."""
    return page.evaluate(r"""({index,gene}) => {
        const table=[...document.querySelectorAll('[role="table"]')].find(t=>
            [...t.querySelectorAll('[role="columnheader"]')].some(h=>
              String(h.innerText||h.textContent||'').trim().startsWith('Fusion Gene')) && t.getClientRects().length);
        if(!table)return '';
        const rows=[...table.querySelectorAll('[role="row"].rdt_TableRow')]
            .filter(el=>el.getClientRects().length);
        const row=rows[index];if(!row)return '';
        const cur=String(row.querySelector('[role="cell"][data-column-id="3"]')?.innerText||'').trim();
        if(cur!==gene)return '';
        const next=row.nextElementSibling;
        if(!next || !/Expander|expand/i.test(String(next.className||'')))return '';
        return String(next.innerText||next.textContent||'').slice(0,9000);
    }""", {'index':int(index),'gene':str(expected_fusion)})


def recover_expanded_missing_rows(page, captured, stop=lambda:False, log=lambda s:None,
                                  max_seconds=65):
    """Repair last-page holes via GenNext's own read-only Expand Row detail.

    Clicks ONLY expansion toggles. Results must match the visible gene and
    carry a verified breakpoint + read count, else remain incomplete.
    """
    needs=[i for i,r in enumerate(captured) if not r.get('found') or
           not normalise_row(r) or not _row_has_caller_evidence(r)]
    if not needs:return captured
    begin=time.monotonic();repaired=0
    log(f'FUSION: {len(needs)} satir React nesnesiz; Expand Row dogrulama basladi')
    for idx in needs:
        if stop():raise ReactCaptureStopped('User stopped expanded-row recovery')
        if time.monotonic()-begin>max_seconds:
            log(f'FUSION: Expand Row sure siniri ({max_seconds} sn); kalanlar KONTROL GEREKLI')
            break
        fusion=str(captured[idx].get('fusion') or '').strip()
        if not fusion:continue
        try:
            # The original row order is preserved. An expanded panel appears
            # BETWEEN rows and must not change the .rdt_TableRow index.
            row=page.locator('[role="table"] [role="row"].rdt_TableRow').nth(idx)
            displayed=row.locator('[role="cell"][data-column-id="3"]').inner_text(timeout=900).strip()
            if displayed!=fusion:continue
            detail=_dom_expanded_detail(page,idx,fusion)
            toggle=row.locator('button[aria-label="Expand Row"]')
            opened=False
            if not detail:
                if not toggle.count():continue
                toggle.first.click(timeout=1300)
                opened=True
                for _ in range(8):
                    if stop():raise ReactCaptureStopped('User stopped expanded-row recovery')
                    page.wait_for_timeout(55)
                    detail=_dom_expanded_detail(page,idx,fusion)
                    if detail:break
            value=parse_expanded_caller_details(detail)
            trial={**captured[idx], 'fusion':fusion}
            if value.get('arriba'):
                trial['arriba']=value['arriba']
            if value.get('starfusion'):
                trial['starfusion']=value['starfusion']
            # This is still a sample-specific, identity-checked caller row.
            if normalise_row(trial) and _row_has_caller_evidence(trial):
                trial['found']=True
                trial['recovered_via']='expanded_row_labeled_caller_fields'
                captured[idx]=trial
                repaired+=1
            if opened:
                try:toggle.first.click(timeout=950)
                except Exception:pass
        except ReactCaptureStopped:
            raise
        except Exception:
            # A failed read is never converted to a success.
            continue
        if repaired and repaired%20==0:
            log(f'FUSION: Expand Row dogrulandi {repaired}/{len(needs)}')
    log(f'FUSION: Expand Row sonucu {repaired}/{len(needs)} kurtarildi')
    return captured


@dataclass
class RecoveryResult:
    json_path: Path
    tsv_path: Path
    complete: bool
    captured: int
    expected: int | None
    reason: str


def _save_recovery(source_tsv, sample_id, rows, total, errors, complete):
    native=Path(source_tsv)
    suffix='_REACT_FILTERED' if complete else '_REACT_FILTERED_PARTIAL'
    out_json=native.with_name(native.stem+suffix+'.json')
    out_tsv=native.with_name(native.stem+suffix+'.tsv')
    rawsha=hashlib.sha256(native.read_bytes()).hexdigest()
    report={'schema':'ngs_fusion_react_recovery_v1',
       'notice':'Filtered analytical candidates; NOT a validated positive or negative clinical fusion report.',
       'patient_id':str(sample_id),'native_source':native.name,'native_sha256':rawsha,
       'native_source_unmodified':True,'source':'sample_specific_react_visible_row_props',
       'filtered_expected':total,'captured':len(rows),'complete':bool(complete),
       'errors':errors,'created_at':datetime.now().isoformat(timespec='seconds'),
       'rows':rows}
    out_json.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    with out_tsv.open('w',encoding='utf-8-sig',newline='') as fh:
        writer=csv.DictWriter(fh,fieldnames=FIELDS,delimiter='\t',extrasaction='ignore')
        writer.writeheader()
        for row in rows:
            norm=normalise_row(row)
            if norm:writer.writerow(norm)
    return out_json,out_tsv


def recover_filtered_fusions(page, sample_id, native_tsv, stop=lambda:False, log=lambda x:None,
                             max_pages=30,max_total=2000, page_wait_seconds=14,
                             expand_max_seconds=65):
    """Capture every row from the *filtered* current sample Fusion grid.

    Fail-closed: incomplete DOM pages, missing React data, mismatched pagination,
    an incorrect patient header, or missing caller evidence remain REVIEW only.
    """
    start_time=time.monotonic()
    errors=[];all_rows=[];expected=None
    def check_stop():
        if stop():raise ReactCaptureStopped('User stopped React Fusion recovery')
    def read():
        check_stop()
        val=page.evaluate(PAGE_JS)
        return val if isinstance(val,dict) else {}

    initial=read()
    head=str(initial.get('patient_header') or '')
    if not head or not str(sample_id).strip() or str(sample_id).upper() not in head.upper():
        errors.append(f'Patient page header missing or mismatch (expected {sample_id}; not using data).')
        jsonp,tsvp=_save_recovery(native_tsv,sample_id,[],None,errors,False)
        return RecoveryResult(jsonp,tsvp,False,0,None,errors[0])
    expected=initial.get('total') if isinstance(initial.get('total'),int) else None
    if expected is None or not 0<expected<=max_total:
        errors.append('Missing/invalid/out-of-range filtered pagination total')
        jsonp,tsvp=_save_recovery(native_tsv,sample_id,[],expected,errors,False)
        return RecoveryResult(jsonp,tsvp,False,0,expected,errors[0])
    # A 100-row view reduces per-sample SPA roundtrips from ~40 to ~4 for the
    # busiest Fusion cases. Fall back to the current size if selection fails.
    current_size=int(initial.get('selected_size') or 10)
    desired_size=100 if expected>10 else current_size
    if desired_size==100 and current_size!=100:
        try:
            sel=page.locator('nav.rdt_Pagination select[aria-label="Rows per page:"]')
            if sel.count() and sel.first.is_visible():
                sel.first.select_option('100',timeout=3500)
                log(f'{sample_id} FUSION: 100 satir/sayfa secildi')
            else:
                desired_size=current_size
        except Exception as ex:
            log(f'{sample_id} FUSION: sayfa boyutu degistirilemedi ({type(ex).__name__}); mevcut boyut korunuyor')
            desired_size=current_size

    def settled_page(want_start,want_size):
        sig=None;since=None;last={}
        deadline=time.monotonic()+page_wait_seconds
        while time.monotonic()<deadline:
            check_stop()
            last=read()
            start,end,total=last.get('start'),last.get('end'),last.get('total')
            rows=last.get('rows') or []
            good=(bool(last.get('has_table')) and total==expected and start==want_start
                  and isinstance(end,int) and isinstance(start,int)
                  and end==min(start+want_size-1,expected)
                  and len(rows)==end-start+1)
            if good:
                cur=(start,end,total,tuple(r.get('fusion') for r in rows))
                if cur!=sig:sig=cur;since=time.monotonic()
                elif since is not None and time.monotonic()-since>=(0.75 if
                     all(r.get('found') for r in rows) else 1.6):return last
            else:sig=None;since=None
            page.wait_for_timeout(220)
        errors.append(f'Page did not settle: start={want_start}, got={last.get("start")}-{last.get("end")}, rows={len(last.get("rows") or [])}')
        return None

    cursor=1
    for pn in range(1,max_pages+1):
        check_stop()
        if cursor>expected:break
        capture=settled_page(cursor,desired_size)
        if capture is None:break
        got=capture['rows']
        found=sum(bool(row.get('found')) for row in got)
        log(f'{sample_id} FUSION: React ayrinti sayfa {pn}, {cursor}-{capture.get("end")}/{expected}, React={found}/{len(got)}')
        if found<len(got) or any(not _row_has_caller_evidence(r) for r in got):
            got=recover_expanded_missing_rows(page,got,stop=stop,log=log,
                                             max_seconds=expand_max_seconds)
        all_rows.extend(got)
        cursor=int(capture['end'])+1
        if cursor>expected:break
        next_button=page.locator('nav.rdt_Pagination button[aria-label="Next Page"]')
        try:
            if not next_button.count() or next_button.first.is_disabled():
                errors.append(f'Next Page unavailable before row {cursor}');break
            next_button.first.click(timeout=3500)
        except Exception as ex:
            errors.append(f'Next Page failed: {type(ex).__name__}');break
    if cursor<=expected and not errors: errors.append(f'Exceeded max_pages={max_pages}')
    if len(all_rows)!=expected: errors.append(f'Pagination incomplete: got {len(all_rows)}/{expected}')
    for i,r in enumerate(all_rows):
        norm=normalise_row(r)
        if not r.get('found') or not norm:
            errors.append(f'Row {i+1} missing exact React fusion pair')
        elif not _row_has_caller_evidence(r):
            errors.append(f'Row {i+1} missing actual caller breakpoint+read evidence')
    complete=not errors and len(all_rows)==expected
    out_json,out_tsv=_save_recovery(native_tsv,sample_id,all_rows,expected,errors,complete)
    if complete:
        ok,reason,meta=validate_fusion_source(out_tsv)
        if not ok:
            errors.append(f'Normalized TSV validation failed: {reason}')
            complete=False
            # Do not leave a validated-looking filename or metadata after failure.
            partial_json,partial_tsv=_save_recovery(native_tsv,sample_id,all_rows,expected,errors,False)
            out_json.unlink(missing_ok=True);out_tsv.unlink(missing_ok=True)
            out_json,out_tsv=partial_json,partial_tsv
    result=RecoveryResult(out_json,out_tsv,complete,len(all_rows),expected,'OK' if complete else '; '.join(errors[:3]))
    log(f'{sample_id} FUSION: React kurtarma {result.captured}/{result.expected}, '+('eksiksiz' if complete else 'KONTROL GEREKLI'))
    return result
