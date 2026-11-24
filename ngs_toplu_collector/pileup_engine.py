# -*- coding: utf-8 -*-
"""Read-level pileup and BED coverage evidence for NGS AI/IGV workflows.

The module is deliberately clinical-conservative:
- it never guesses counts from screenshots;
- it never silently picks an ambiguous BAM/CRAM;
- SNV, indel and MNV are counted with different event logic;
- source identity is emitted with every result so reruns can be audited.

pysam is optional at application start.  Evidence generation reports a structured
``dependency_missing`` status when it is unavailable instead of crashing the UI.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

GENERATOR_VERSION = "0.4.33"
DEFAULT_BUILD = "hg38"
_SOURCE_CACHE: dict[tuple[str, tuple[str, ...], bool], list[Path]] = {}
_BED_CACHE: dict[tuple[str, int, int], dict[str, list[tuple[int, int]]]] = {}


def _safe_float(v, default=0.0):
    try:return float(v)
    except Exception:return float(default)


def _safe_int(v, default=0):
    try:return int(v)
    except Exception:return int(default)


def _sha256_file(path: Path, chunk=1024 * 1024) -> str:
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda:f.read(chunk), b''):h.update(block)
    return h.hexdigest()


def _fast_file_fingerprint(path: Path, chunk=1024 * 1024) -> str:
    """Stable-enough large-file fingerprint without hashing a multi-hundred-GB BAM.

    Includes size + first/last 1 MiB.  This is *not* advertised as a full SHA-256.
    """
    path=Path(path);size=path.stat().st_size;h=hashlib.sha256();h.update(str(size).encode())
    with path.open('rb') as f:
        h.update(f.read(chunk))
        if size>chunk:
            f.seek(max(0,size-chunk));h.update(f.read(chunk))
    return h.hexdigest()


def file_identity(path: Path | None, full_hash=False) -> dict:
    if not path:return {'path':'','exists':False}
    path=Path(path)
    if not path.exists():return {'path':str(path),'exists':False}
    st=path.stat();is_alignment=path.suffix.lower() in ('.bam','.cram')
    out={
        'path':str(path),'name':path.name,'exists':True,'size_bytes':int(st.st_size),
        'mtime_ns':int(st.st_mtime_ns),'identity_kind':'full_sha256' if full_hash or not is_alignment else 'fast_fingerprint_sha256',
    }
    try:
        out['sha256' if full_hash or not is_alignment else 'fingerprint_sha256']=_sha256_file(path) if (full_hash or not is_alignment) else _fast_file_fingerprint(path)
    except Exception as exc:out['identity_error']=str(exc)
    return out


def parse_region(region: str) -> tuple[str,int,int] | None:
    m=re.fullmatch(r'\s*((?:chr)?[A-Za-z0-9_.-]+):(\d[\d,]*)-(\d[\d,]*)\s*',str(region or ''))
    if not m:return None
    chrom=m.group(1);a=int(m.group(2).replace(',',''));b=int(m.group(3).replace(',',''))
    if a>b:a,b=b,a
    return chrom,a,b


def canonical_build(value) -> str:
    v=str(value or '').strip().lower().replace('grch','hg')
    if v in ('38','hg38','hg38.0'):return 'hg38'
    if v in ('37','hg37','hg19','19'):return 'hg19'
    return str(value or DEFAULT_BUILD)


def _name_token(value) -> str:
    return re.sub(r'[^A-Z0-9]+','',str(value or '').upper())


def _sample_tokens(sample: dict) -> list[str]:
    vals=[]
    for k in ('sample_uuid','patient_id','case_family','run_uuid'):
        raw=str(sample.get(k) or '').strip()
        if raw:vals.append(_name_token(raw))
    pid=str(sample.get('patient_id') or '')
    m=re.search(r'MP0*(\d+)[-/]{1,2}(\d{2,4})',pid,re.I)
    if m:
        n=str(int(m.group(1)));yy=m.group(2)[-2:]
        vals += [_name_token(f'MP{n}-{yy}'),_name_token(f'MP{n}/{yy}'),_name_token(f'{n}D'),_name_token(f'{n}R')]
    return [x for x in dict.fromkeys(vals) if len(x)>=3]


def _iter_files(root: Path, exts: tuple[str,...], recursive=True, limit=50000) -> list[Path]:
    root=Path(root)
    key=(str(root.resolve()) if root.exists() else str(root),tuple(sorted(exts)),bool(recursive))
    if key in _SOURCE_CACHE:return list(_SOURCE_CACHE[key])
    out=[]
    if root.is_file() and root.suffix.lower() in exts:out=[root]
    elif root.is_dir():
        iterator=root.rglob('*') if recursive else root.glob('*')
        for p in iterator:
            if p.is_file() and p.suffix.lower() in exts:
                out.append(p)
                if len(out)>=limit:break
    _SOURCE_CACHE[key]=out
    return list(out)


def _config_paths(value) -> list[Path]:
    if isinstance(value,(str,Path)):
        return [Path(value)] if str(value).strip() else []
    if isinstance(value,(list,tuple,set)):
        return [Path(x) for x in value if str(x).strip()]
    return []


def _db_source_candidates(base: Path, db, record_key: str, exts: tuple[str,...]) -> list[Path]:
    out=[]
    if db is None:return out
    try:rows=db.files_for_record(record_key)
    except Exception:return out
    for row in rows:
        d=dict(row);raw=Path(str(d.get('path') or ''))
        p=raw if raw.is_absolute() else Path(base)/raw
        ft=str(d.get('file_type') or '').lower()
        if p.suffix.lower() in exts or any(e.lstrip('.') in ft for e in exts):
            if p.exists():out.append(p)
    return list(dict.fromkeys(out))


def resolve_alignment_source(base: Path, db, cfg: dict, record_key: str) -> dict:
    sample=dict(db.get_sample(record_key) or {}) if db is not None else {}
    candidates=_db_source_candidates(base,db,record_key,('.bam','.cram'))
    roots=_config_paths(cfg.get('pileup_alignment_roots') or cfg.get('alignment_roots') or [])
    recursive=bool(cfg.get('pileup_scan_recursive',True))
    for root in roots:candidates.extend(_iter_files(root,('.bam','.cram'),recursive=recursive))
    candidates=list(dict.fromkeys(Path(p) for p in candidates if Path(p).exists()))
    if not candidates:return {'status':'source_not_found','path':'','candidates':[]}
    tokens=_sample_tokens(sample);scored=[]
    for p in candidates:
        name=_name_token(p.name);score=0;reasons=[]
        su=_name_token(sample.get('sample_uuid'))
        pid=_name_token(sample.get('patient_id'));fam=_name_token(sample.get('case_family'));run=_name_token(sample.get('run_uuid'))
        if su and su in name:score+=180;reasons.append('sample_uuid')
        if pid and pid in name:score+=120;reasons.append('patient_id')
        if fam and fam in name:score+=110;reasons.append('case_family')
        if run and run in name:score+=50;reasons.append('run_uuid')
        for t in tokens:
            if t and t in name:score=max(score,80);reasons.append('protocol_token');break
        m=re.search(r'MP0*(\d+)[-/]{1,2}\d{2,4}',str(sample.get('patient_id') or ''),re.I)
        if m:
            n=str(int(m.group(1)));stype=str(sample.get('sample_type') or '').upper()
            if stype=='DNA' and _name_token(n+'D') in name:score+=20;reasons.append('dna_token')
            if stype=='RNA' and _name_token(n+'R') in name:score+=20;reasons.append('rna_token')
        if p.suffix.lower()=='.bam':score+=2
        scored.append((score,p,','.join(dict.fromkeys(reasons))))
    scored.sort(key=lambda x:(-x[0],str(x[1]).lower()))
    top=scored[0]
    tied=[x for x in scored if x[0]==top[0]]
    if top[0]<=0 or len(tied)>1:
        return {'status':'ambiguous' if len(tied)>1 else 'no_matching_alignment','path':'','candidates':[{'path':str(p),'score':s,'reason':r} for s,p,r in scored[:12]]}
    return {'status':'ok','path':str(top[1]),'score':top[0],'reason':top[2],'candidates':[{'path':str(p),'score':s,'reason':r} for s,p,r in scored[:5]]}


def resolve_bed_source(base: Path, db, cfg: dict, record_key: str) -> dict:
    explicit=_config_paths(cfg.get('pileup_bed_path') or cfg.get('bed_path') or '')
    for p in explicit:
        if p.is_file():return {'status':'ok','path':str(p),'reason':'config'}
    cands=_db_source_candidates(base,db,record_key,('.bed',))
    roots=_config_paths(cfg.get('pileup_bed_roots') or [])
    for root in roots:cands.extend(_iter_files(root,('.bed',),recursive=bool(cfg.get('pileup_scan_recursive',True))))
    cands=list(dict.fromkeys(Path(p) for p in cands if Path(p).exists()))
    if len(cands)==1:return {'status':'ok','path':str(cands[0]),'reason':'single_candidate'}
    if not cands:return {'status':'source_not_found','path':'','candidates':[]}
    preferred=str(cfg.get('pileup_bed_name_contains') or '').casefold()
    if preferred:
        hits=[p for p in cands if preferred in p.name.casefold()]
        if len(hits)==1:return {'status':'ok','path':str(hits[0]),'reason':'name_filter'}
    return {'status':'ambiguous','path':'','candidates':[str(x) for x in cands[:20]]}


def _load_bed(path: Path) -> dict[str,list[tuple[int,int]]]:
    path=Path(path);st=path.stat();key=(str(path.resolve()),int(st.st_size),int(st.st_mtime_ns))
    if key in _BED_CACHE:return _BED_CACHE[key]
    out:dict[str,list[tuple[int,int]]]={}
    with path.open('r',encoding='utf-8-sig',errors='replace') as f:
        for line in f:
            if not line.strip() or line.startswith(('#','track','browser')):continue
            parts=line.rstrip('\n').split('\t')
            if len(parts)<3:continue
            try:a=int(parts[1]);b=int(parts[2])
            except Exception:continue
            if b<=a:continue
            out.setdefault(parts[0],[]).append((a,b))
    for chrom in out:out[chrom].sort()
    _BED_CACHE.clear();_BED_CACHE[key]=out
    return out


def _bed_chrom(intervals: dict, chrom: str) -> str | None:
    if chrom in intervals:return chrom
    alt=chrom[3:] if chrom.lower().startswith('chr') else 'chr'+chrom
    return alt if alt in intervals else None


def bed_target_status(path: Path | None, chrom: str, pos1: int, padding=0) -> dict:
    if not path:return {'target_status':'unknown','padding_only':None,'coding_region_covered':None}
    try:intervals=_load_bed(Path(path));c=_bed_chrom(intervals,chrom)
    except Exception as exc:return {'target_status':'error','padding_only':None,'coding_region_covered':None,'error':str(exc)}
    if not c:return {'target_status':'not_targeted','padding_only':False,'coding_region_covered':False}
    p0=pos1-1
    for a,b in intervals.get(c,[]):
        if a<=p0<b:return {'target_status':'targeted','padding_only':False,'coding_region_covered':True,'target_interval':f'{c}:{a+1}-{b}'}
        if padding and (a-padding)<=p0<(b+padding):return {'target_status':'padding_only','padding_only':True,'coding_region_covered':False,'nearest_target_interval':f'{c}:{a+1}-{b}'}
        if a>p0+padding:break
    return {'target_status':'not_targeted','padding_only':False,'coding_region_covered':False}


def _median(values):
    return round(float(statistics.median(values)),4) if values else None


def _read_ok(read,min_mapq):
    return not (getattr(read,'is_unmapped',False) or getattr(read,'is_secondary',False) or getattr(read,'is_supplementary',False) or getattr(read,'is_qcfail',False)) and int(getattr(read,'mapping_quality',0) or 0)>=min_mapq


def _read1read2(read):
    if getattr(read,'is_read1',False):return 'read1'
    if getattr(read,'is_read2',False):return 'read2'
    return 'unpaired'


def _alt_position_fraction(read,qpos):
    if qpos is None:return None
    qlen=getattr(read,'query_length',None) or len(getattr(read,'query_sequence','') or '')
    if not qlen:return None
    return (int(qpos)+1)/float(qlen)


def _summarize_support(total,ref_count,alt_count,alt_reads,duplicate_excluded_total,duplicate_excluded_alt,variant_type):
    fwd=sum(1 for r,_q,_bq in alt_reads if not getattr(r,'is_reverse',False));rev=len(alt_reads)-fwd
    r1=sum(1 for r,_q,_bq in alt_reads if _read1read2(r)=='read1');r2=sum(1 for r,_q,_bq in alt_reads if _read1read2(r)=='read2')
    bqs=[bq for _r,_q,bq in alt_reads if bq is not None];mqs=[int(getattr(r,'mapping_quality',0) or 0) for r,_q,_bq in alt_reads]
    pos=[_alt_position_fraction(r,q) for r,q,_bq in alt_reads];pos=[x for x in pos if x is not None]
    return {
        'status':'ok','variant_type':variant_type,'dp':total,'ad_ref':ref_count,'alt_ad':alt_count,
        'vaf':(alt_count/total if total else None),'forward_alt':fwd,'reverse_alt':rev,'alt_read1':r1,'alt_read2':r2,
        'duplicate_excluded_dp':duplicate_excluded_total,'duplicate_excluded_alt_ad':duplicate_excluded_alt,
        'duplicate_excluded_vaf':(duplicate_excluded_alt/duplicate_excluded_total if duplicate_excluded_total else None),
        'median_alt_baseq':_median(bqs),'median_alt_mapq':_median(mqs),
        'read_position':{'median_alt_fraction':_median(pos),'near_read_end_alt':sum(1 for x in pos if x<0.1 or x>0.9),'alt_count_with_position':len(pos)},
    }


def _pileup_column(aln,chrom,pos1,max_depth=1000000):
    for col in aln.pileup(chrom,pos1-1,pos1,truncate=True,stepper='all',max_depth=max_depth,min_base_quality=0):
        if int(getattr(col,'reference_pos',-2))==pos1-1:return col
    return None


def count_snv(aln,chrom,pos1,ref,alt,min_mapq=20,min_baseq=20,max_depth=1000000):
    col=_pileup_column(aln,chrom,pos1,max_depth);total=refc=altc=dedup_total=dedup_alt=0;alt_reads=[]
    if col is None:return _summarize_support(0,0,0,[],0,0,'SNV')
    for pr in col.pileups:
        read=pr.alignment
        if not _read_ok(read,min_mapq) or pr.is_del or pr.is_refskip or pr.query_position is None:continue
        q=int(pr.query_position);seq=getattr(read,'query_sequence','') or ''
        if q>=len(seq):continue
        quals=getattr(read,'query_qualities',None);bq=int(quals[q]) if quals is not None and q<len(quals) else 0
        if bq<min_baseq:continue
        base=seq[q].upper();total+=1
        if not getattr(read,'is_duplicate',False):dedup_total+=1
        if base==str(ref).upper():refc+=1
        if base==str(alt).upper():
            altc+=1;alt_reads.append((read,q,bq))
            if not getattr(read,'is_duplicate',False):dedup_alt+=1
    return _summarize_support(total,refc,altc,alt_reads,dedup_total,dedup_alt,'SNV')


def count_indel(aln,chrom,pos1,ref,alt,min_mapq=20,min_baseq=20,max_depth=1000000):
    ref=str(ref or '').upper();alt=str(alt or '').upper();delta=len(alt)-len(ref)
    col=_pileup_column(aln,chrom,pos1,max_depth);total=refc=altc=dedup_total=dedup_alt=0;alt_reads=[]
    if col is None:return _summarize_support(0,0,0,[],0,0,'INDEL')
    ins_seq=alt[len(ref):] if delta>0 and alt.startswith(ref) else (alt[1:] if delta>0 and len(alt)>1 else '')
    for pr in col.pileups:
        read=pr.alignment
        if not _read_ok(read,min_mapq) or pr.is_refskip:continue
        q=pr.query_position
        if q is None:continue
        quals=getattr(read,'query_qualities',None);bq=int(quals[q]) if quals is not None and q<len(quals) else 0
        if bq<min_baseq:continue
        event=int(getattr(pr,'indel',0) or 0);total+=1
        if not getattr(read,'is_duplicate',False):dedup_total+=1
        supported=False
        if delta>0 and event==delta:
            if ins_seq:
                seq=getattr(read,'query_sequence','') or '';got=seq[q+1:q+1+delta].upper();supported=(got==ins_seq.upper())
            else:supported=True
        elif delta<0 and event==delta:supported=True
        if supported:
            altc+=1;alt_reads.append((read,q,bq))
            if not getattr(read,'is_duplicate',False):dedup_alt+=1
        elif event==0:refc+=1
    return _summarize_support(total,refc,altc,alt_reads,dedup_total,dedup_alt,'INDEL')


def _read_haplotype(read,start1,length):
    seq=getattr(read,'query_sequence','') or ''
    mapping={r:q for q,r in read.get_aligned_pairs(matches_only=False) if r is not None and q is not None}
    qpos=[];bases=[]
    for pos0 in range(start1-1,start1-1+length):
        q=mapping.get(pos0)
        if q is None or q>=len(seq):return None,None
        qpos.append(q);bases.append(seq[q].upper())
    return ''.join(bases),qpos


def count_mnv(aln,chrom,pos1,ref,alt,min_mapq=20,min_baseq=20,max_depth=1000000):
    ref=str(ref or '').upper();alt=str(alt or '').upper();end1=pos1+len(ref)-1
    total=refc=altc=dedup_total=dedup_alt=0;alt_reads=[];seen=set()
    for read in aln.fetch(chrom,pos1-1,end1):
        key=(getattr(read,'query_name',''),getattr(read,'reference_start',-1),getattr(read,'is_read1',False),getattr(read,'is_read2',False))
        if key in seen:continue
        seen.add(key)
        if not _read_ok(read,min_mapq):continue
        hap,qpos=_read_haplotype(read,pos1,len(ref))
        if hap is None:continue
        quals=getattr(read,'query_qualities',None)
        bqs=[int(quals[q]) if quals is not None and q<len(quals) else 0 for q in qpos]
        if not bqs or min(bqs)<min_baseq:continue
        total+=1
        if not getattr(read,'is_duplicate',False):dedup_total+=1
        if hap==ref:refc+=1
        if hap==alt:
            altc+=1;alt_reads.append((read,qpos[len(qpos)//2],min(bqs)))
            if not getattr(read,'is_duplicate',False):dedup_alt+=1
    return _summarize_support(total,refc,altc,alt_reads,dedup_total,dedup_alt,'MNV')


def softclip_support(aln,chrom,pos1,window=10,min_mapq=20) -> dict:
    left=right=0;seen=set()
    try:reads=aln.fetch(chrom,max(0,pos1-window-1),pos1+window)
    except Exception:return {'left':None,'right':None}
    for read in reads:
        key=(getattr(read,'query_name',''),getattr(read,'reference_start',-1),getattr(read,'flag',0))
        if key in seen:continue
        seen.add(key)
        if not _read_ok(read,min_mapq):continue
        cig=getattr(read,'cigartuples',None) or []
        if cig and cig[0][0]==4 and abs((getattr(read,'reference_start',0)+1)-pos1)<=window:left+=1
        if cig and cig[-1][0]==4 and abs(int(getattr(read,'reference_end',0) or 0)-pos1)<=window:right+=1
    return {'left':left,'right':right,'window_bp':window}


def coverage_depths(aln,chrom,start1,end1,min_mapq=20,exclude_duplicates=True,max_depth=1000000):
    n=max(0,end1-start1+1);depth=[0]*n
    if n<=0:return depth
    for col in aln.pileup(chrom,start1-1,end1,truncate=True,stepper='all',max_depth=max_depth,min_base_quality=0):
        pos1=int(col.reference_pos)+1
        if pos1<start1 or pos1>end1:continue
        c=0
        for pr in col.pileups:
            read=pr.alignment
            if pr.is_refskip or not _read_ok(read,min_mapq):continue
            if exclude_duplicates and getattr(read,'is_duplicate',False):continue
            if pr.is_del:
                c+=1
            elif pr.query_position is not None:c+=1
        depth[pos1-start1]=c
    return depth


def summarize_coverage(depth: list[int], threshold=100, cliff_ratio=0.35) -> dict:
    if not depth:return {'mean_depth':None,'median_depth':None,'min_depth':None,'max_depth':None,'ge_100x_pct':None,'coverage_cliff':None}
    mean=sum(depth)/len(depth);med=statistics.median(depth);mn=min(depth);mx=max(depth);ge=sum(1 for x in depth if x>=threshold)/len(depth)*100
    cliff=bool(med and mn < med*cliff_ratio)
    return {'mean_depth':round(mean,2),'median_depth':round(float(med),2),'min_depth':int(mn),'max_depth':int(mx),'ge_100x_pct':round(ge,2),'coverage_cliff':cliff,'coverage_threshold_x':int(threshold),'coverage_cliff_ratio':float(cliff_ratio)}


def _choose_contig(aln,chrom):
    refs=set(getattr(aln,'references',()) or ())
    if chrom in refs:return chrom
    alt=chrom[3:] if chrom.lower().startswith('chr') else 'chr'+chrom
    return alt if alt in refs else chrom


def _coordinates_from_inputs(coordinates: dict | None,source_call: dict | None,region: str,variant_key='') -> dict:
    c=dict(coordinates or {});s=dict(source_call or {})
    chrom=str(c.get('chrom') or s.get('chrom') or '')
    pos=_safe_int(c.get('position') or s.get('start') or 0)
    ref=str(c.get('ref') or s.get('ref') or '')
    alt=str(c.get('alt') or s.get('alt') or '')
    reg=str(c.get('region') or region or '')
    parsed=parse_region(reg)
    if parsed:
        if not chrom:chrom=parsed[0]
        if not pos:pos=parsed[1]
    if (not chrom or not pos) and variant_key:
        m=re.search(r'((?:chr)?[A-Za-z0-9_.-]+):(\d+)(?::|\||-|$)',str(variant_key))
        if m:chrom=chrom or m.group(1);pos=pos or int(m.group(2))
    return {'genome_build':canonical_build(c.get('genome_build') or DEFAULT_BUILD),'chrom':chrom,'position':pos,'ref':ref,'alt':alt,'region':reg,'sort_anchor':_safe_int(c.get('sort_anchor') or pos)}


def _variant_type(ref,alt):
    if len(ref)==1 and len(alt)==1:return 'SNV'
    if ref and alt and len(ref)==len(alt) and len(ref)>1:return 'MNV'
    if ref and alt and len(ref)!=len(alt):return 'INDEL'
    return 'UNKNOWN'


def compute_pileup_and_coverage(base: Path,db,cfg: dict,record_key: str,coordinates: dict | None=None,region='',variant_key='',source_call: dict | None=None) -> dict:
    coords=_coordinates_from_inputs(coordinates,source_call,region,variant_key);build=canonical_build(coords.get('genome_build') or cfg.get('genome_build') or DEFAULT_BUILD);coords['genome_build']=build
    out={'schema':'ngs_read_evidence_v1','generator_version':GENERATOR_VERSION,'record_key':record_key,'genome_build':build,'coordinates':coords,'pileup':{},'coverage_audit':{},'sources':{}}
    align=resolve_alignment_source(Path(base),db,cfg,record_key);bed=resolve_bed_source(Path(base),db,cfg,record_key)
    out['sources']['alignment_resolution']=align;out['sources']['bed_resolution']=bed
    ap=Path(align.get('path')) if align.get('status')=='ok' and align.get('path') else None
    bp=Path(bed.get('path')) if bed.get('status')=='ok' and bed.get('path') else None
    out['sources']['alignment']=file_identity(ap,full_hash=bool(cfg.get('pileup_full_alignment_sha256',False))) if ap else {'exists':False,'status':align.get('status')}
    out['sources']['bed']=file_identity(bp,full_hash=True) if bp else {'exists':False,'status':bed.get('status')}
    chrom=str(coords.get('chrom') or '');pos=_safe_int(coords.get('position'));ref=str(coords.get('ref') or '');alt=str(coords.get('alt') or '')
    if bp and chrom and pos:
        out['coverage_audit'].update(bed_target_status(bp,chrom,pos,padding=_safe_int(cfg.get('pileup_bed_padding_bp',20),20)))
    else:out['coverage_audit'].update({'target_status':'unknown','padding_only':None,'coding_region_covered':None})
    if not ap:
        out['pileup']={'status':align.get('status') or 'source_not_found','dp':None,'alt_ad':None,'vaf':None};return out
    try:
        import pysam  # type: ignore
    except Exception as exc:
        out['pileup']={'status':'dependency_missing','dependency':'pysam','error':str(exc),'dp':None,'alt_ad':None,'vaf':None};return out
    if not chrom or not pos:
        out['pileup']={'status':'coordinates_missing','dp':None,'alt_ad':None,'vaf':None};return out
    min_mapq=_safe_int(cfg.get('pileup_min_mapq',20),20);min_baseq=_safe_int(cfg.get('pileup_min_baseq',20),20);max_depth=_safe_int(cfg.get('pileup_max_depth',1000000),1000000)
    ref_fasta=str(cfg.get('pileup_reference_fasta') or '').strip() or None
    try:
        kwargs={}
        if ap.suffix.lower()=='.cram' and ref_fasta:kwargs['reference_filename']=ref_fasta
        with pysam.AlignmentFile(str(ap),'rc' if ap.suffix.lower()=='.cram' else 'rb',**kwargs) as aln:
            cchrom=_choose_contig(aln,chrom);coords['resolved_contig']=cchrom;vt=_variant_type(ref,alt)
            if vt=='SNV':pile=count_snv(aln,cchrom,pos,ref,alt,min_mapq,min_baseq,max_depth)
            elif vt=='INDEL':pile=count_indel(aln,cchrom,pos,ref,alt,min_mapq,min_baseq,max_depth)
            elif vt=='MNV':pile=count_mnv(aln,cchrom,pos,ref,alt,min_mapq,min_baseq,max_depth)
            else:pile={'status':'variant_alleles_missing','variant_type':vt,'dp':None,'alt_ad':None,'vaf':None}
            if vt!='UNKNOWN':pile['softclip_support']=softclip_support(aln,cchrom,pos,window=_safe_int(cfg.get('pileup_softclip_window_bp',10),10),min_mapq=min_mapq)
            pile['filter_policy']={'min_mapq':min_mapq,'min_baseq':min_baseq,'exclude_secondary':True,'exclude_supplementary':True,'exclude_qcfail':True,'duplicates_in_primary_dp':True}
            out['pileup']=pile
            parsed=parse_region(coords.get('region') or '')
            if parsed:_,start1,end1=parsed
            else:
                half=_safe_int(cfg.get('pileup_coverage_window_bp',25),25);start1=max(1,pos-half);end1=pos+half
            max_window=max(1,_safe_int(cfg.get('pileup_coverage_max_window_bp',1000),1000))
            if end1-start1+1>max_window:
                mid=(start1+end1)//2;start1=max(1,mid-max_window//2);end1=start1+max_window-1
            dep=coverage_depths(aln,cchrom,start1,end1,min_mapq=min_mapq,exclude_duplicates=True,max_depth=max_depth)
            cov=summarize_coverage(dep,threshold=_safe_int(cfg.get('pileup_coverage_threshold_x',100),100),cliff_ratio=_safe_float(cfg.get('pileup_coverage_cliff_ratio',0.35),0.35))
            cov.update({'region':f'{cchrom}:{start1}-{end1}','duplicate_excluded':True})
            out['coverage_audit'].update(cov)
    except Exception as exc:
        out['pileup']={'status':'analysis_error','error':str(exc),'dp':None,'alt_ad':None,'vaf':None}
    return out


def source_identity_for_request(base: Path,db,cfg: dict,record_key: str) -> dict:
    a=resolve_alignment_source(Path(base),db,cfg,record_key);b=resolve_bed_source(Path(base),db,cfg,record_key)
    ap=Path(a.get('path')) if a.get('status')=='ok' and a.get('path') else None;bp=Path(b.get('path')) if b.get('status')=='ok' and b.get('path') else None
    return {'alignment':file_identity(ap,full_hash=bool(cfg.get('pileup_full_alignment_sha256',False))) if ap else {'exists':False,'status':a.get('status')},'bed':file_identity(bp,full_hash=True) if bp else {'exists':False,'status':b.get('status')}}

# === NGS_WEB_ONLY_DISABLE_LOCAL_PILEUP_V0434 ===
# v0.4.34 policy: do not inspect/download/open BAM/BAI/CRAM/BED locally.
def resolve_alignment_source(base,db,cfg,record_key):
    return {'status':'disabled_web_igv_only','path':'','candidates':[]}
def resolve_bed_source(base,db,cfg,record_key):
    return {'status':'disabled_web_igv_only','path':'','candidates':[]}
def source_identity_for_request(base,db,cfg,record_key):
    return {'mode':'web_igv_only','alignment':{'exists':False,'status':'disabled_web_igv_only'},'bed':{'exists':False,'status':'disabled_web_igv_only'}}
def compute_pileup_and_coverage(base,db,cfg,record_key,coordinates=None,region='',variant_key='',source_call=None):
    return {'schema':'ngs_web_only_read_evidence_v1','generator_version':'0.4.34','record_key':record_key,'coordinates':coordinates or {},
      'pileup':{'status':'disabled_web_igv_only','dp':None,'alt_ad':None,'vaf':None},
      'coverage_audit':{'target_status':'web_track_only','mean_depth':None,'min_depth':None},
      'sources':{'mode':'web_igv_only'},'note':'Yerel BAM/BAI/BED kullanılmaz; web IGV evidence ayrı toplanır.'}
