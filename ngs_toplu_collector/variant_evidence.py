"""Privacy-safe, bounded NCBI evidence enrichment for GenNext AI packages.

Research support only. This module NEVER promotes a variant to reportable,
changes Selection/Oncogenicity, or classifies technical read evidence.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import urlencode, quote
from urllib.request import Request, urlopen
from xml.etree import ElementTree

SCHEMA = 'ngs_variant_evidence_v1_1'
API_ROOT = 'https://eutils.ncbi.nlm.nih.gov/entrez/eutils/'
GENE_RE = re.compile(r'^[A-Z][A-Z0-9-]{1,24}$')
PROTEIN_RE = re.compile(r'(?<![A-Za-z0-9])p\.([A-Za-z*]{1,5}\d{1,6}(?:_[A-Za-z*]{1,5}\d{1,6})?(?:delins[A-Za-z*]+|del|ins[A-Za-z*]+|dup|fs\*?\d*|[A-Za-z*]{1,5})?)', re.I)
BARE_PROTEIN_RE = re.compile(r'^[A-Za-z*]{1,5}\d{1,6}(?:_[A-Za-z*]{1,5}\d{1,6})?(?:delins[A-Za-z*]+|del|dup|ins[A-Za-z*]+|fs\*?\d*|[A-Za-z*]{1,5})$')
FUSION_RE = re.compile(r'^([A-Z][A-Z0-9]{1,20})(?:--|::|/|-)([A-Z][A-Z0-9]{1,20})$', re.I)
_MAX_TEXT = 300


def _text(value, max_len=_MAX_TEXT):
    if isinstance(value, (dict, list, tuple)):
        return ''
    return re.sub(r'[\x00-\x1f]+', ' ', str(value or '')).strip()[:max_len]


def _gene(value):
    value = _text(value, 30).upper()
    return value if GENE_RE.fullmatch(value) else ''


def _protein(value):
    text = _text(value, 400)
    match = PROTEIN_RE.search(text)
    if match:
        return 'p.' + match.group(1)
    if BARE_PROTEIN_RE.fullmatch(text):
        return 'p.' + text
    return ''


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')).hexdigest()


# Research priority is NOT a reportable or actionable clinical determination.
PRIORITY_GENES = frozenset({
    'EGFR', 'KRAS', 'NRAS', 'BRAF', 'ERBB2', 'MET', 'ALK', 'ROS1',
    'RET', 'NTRK1', 'NTRK2', 'NTRK3', 'PIK3CA', 'FGFR1', 'FGFR2',
    'FGFR3', 'KIT', 'PDGFRA', 'IDH1', 'IDH2', 'BRCA1', 'BRCA2',
    'ESR1', 'AKT1', 'MTOR', 'MAP2K1', 'NF1', 'RAF1', 'HRAS',
})
CNV_PRIORITY_GENES = frozenset({'ERBB2','MET','EGFR','MYC','MDM2','CDK4','FGFR1',
    'FGFR2','FGFR3','CCND1','CCNE1','KRAS','ALK','RET','NTRK1','NTRK2','NTRK3',
    'BRCA1','BRCA2','PTEN','CDKN2A','RB1'})


def _variant_annotations(row):
    annotations = row.get('annotations')
    return annotations if isinstance(annotations, dict) else {}


def _kind_variant(row, kind):
    if not isinstance(row, dict):
        return None
    kind = str(kind).upper()
    if kind == 'SNV':
        annotations = _variant_annotations(row)
        gene = _gene(row.get('gene') or row.get('gene_symbol') or annotations.get('gene'))
        p = _protein(row.get('protein') or row.get('change') or annotations.get('protein'))
        if not gene or not p:
            return None
        cdna = _text(row.get('cdna') or row.get('cDNA') or annotations.get('cDNA') or annotations.get('cdna'), 90)
        transcript = _text(row.get('transcript') or annotations.get('transcript'), 80)
        cdna = cdna if re.fullmatch(r'c\.[A-Za-z0-9_+*?=><-]{1,75}', cdna) else ''
        transcript = transcript if re.fullmatch(r'(?:N[MR]_\d+\.\d+|ENST\d+(?:\.\d+)?)', transcript) else ''
        return {'kind': 'SNV', 'gene': gene, 'descriptor': p,
                'cdna': cdna, 'transcript': transcript,
                'specificity': 'protein_name_only_allele_unverified'}
    if kind == 'RNA':
        g5 = _gene(row.get('gene_5prime') or row.get('left_gene') or row.get('gene5') or row.get('gene1'))
        g3 = _gene(row.get('gene_3prime') or row.get('right_gene') or row.get('gene3') or row.get('gene2'))
        if not (g5 and g3):
            raw = _text(row.get('fusion') or row.get('fusion_name') or row.get('change') or '', 100)
            match = FUSION_RE.fullmatch(raw)
            if match:
                g5, g3 = _gene(match.group(1)), _gene(match.group(2))
        if g5 and g3 and g5 != g3:
            return {'kind': 'RNA', 'gene': g5 + '--' + g3, 'descriptor': g5 + '--' + g3,
                    'specificity': 'gene_pair_only_breakpoints_not_verified'}
    if kind == 'CNV':
        gene = _gene(row.get('gene'))
        event = _text(row.get('direction') or row.get('sv_type') or row.get('event_type'), 50).upper()
        if gene and any(word in event for word in ('GAIN', 'AMP', 'LOSS', 'DEL')):
            mode = 'GAIN' if 'GAIN' in event or 'AMP' in event else 'LOSS'
            return {'kind': 'CNV', 'gene': gene, 'descriptor': mode,
                    'specificity': 'gene_event_only_no_segment_verified'}
    return None


def _source_variant_key(row):
    return _text(row.get('variant_key') or row.get('fusion_key') or row.get('cnv_key') or row.get('event_key'), 220)


def _research_sort_key(item):
    '''Clinical-relevance search ordering, NOT reportability or drug eligibility.'''
    ident=item['identity'];gene=ident['gene'];change=ident['descriptor'].upper()
    primary=(
        (gene=='EGFR' and (('DEL' in change and '_' in change) or change in
           ('P.L858R','P.T790M','P.C797S','P.G719A','P.G719S','P.G719C','P.L861Q','P.S768I')))
        or (gene=='KRAS' and change in ('P.G12C','P.G12D','P.G12V'))
        or (gene=='BRAF' and change=='P.V600E')
        or (gene=='ERBB2' and ('INS' in change or 'DUP' in change))
        or (ident['kind']=='CNV' and gene in ('ERBB2','MET') and change=='GAIN')
        or (ident['kind']=='RNA' and any(gene.endswith('--'+g) for g in ('ALK','ROS1','RET','NTRK1','NTRK2','NTRK3')))
    )
    kind_order={'SNV':0,'RNA':1,'CNV':2}
    return (0 if primary else 1,kind_order.get(ident['kind'],3))


def _candidate_rows(pkg):
    """Balanced per-sample priority while keeping verified Selection distinct."""
    patients = [p for p in (pkg.get('patients') or []) if isinstance(p, dict)]
    per_sample = []
    for patient in patients:
        rk = _text((patient.get('technical_sample') or {}).get('record_key'), 220)
        if not rk:
            continue
        selection = patient.get('report_selection_by_assay') or {}
        buckets = [[] for _ in range(4)]
        seen = set()

        def offer(row, kind, provenance, tier):
            if not isinstance(row, dict):
                return
            identity = _kind_variant(row, kind)
            if not identity:
                return
            key = _source_variant_key(row)
            source_key = bool(key)
            if not key and kind == 'RNA':
                # Research-only synthetic key is explicitly NOT a caller identity.
                br = [_text(row.get(x), 70) for x in ('breakpoint1', 'breakpoint2')]
                key = 'RESEARCH_FUSION|' + identity['descriptor'] + '|' + _fingerprint(br)[:14]
            if not key:
                return
            unique = (rk, key, kind)
            if unique in seen:
                return
            seen.add(unique)
            buckets[tier].append({'record_key': rk, 'variant_key': key, 'kind': kind,
                'identity': identity, 'provenance': provenance, 'priority_tier': tier,
                'source_variant_key_present': source_key,
                'source_oncogenicity': _text(row.get('oncogenicity') or row.get('classification') or '', 55)})

        for assay, kind in (('SNV_INDEL','SNV'),('RNA_FUSION','RNA'),('CNV','CNV')):
            status = selection.get(assay) or {}
            if status.get('report_selection_verified') is True:
                for row in (status.get('selected_variants') or [])[:160]:
                    offer(row,kind,'verified_selection',0)
        # For unverified/empty Selection, retain research candidates but NEVER promote them.
        for row in (patient.get('oncogenic_and_likely_oncogenic_variants') or [])[:1200]:
            gene = _gene(row.get('gene'))
            offer(row,'SNV','research_oncogenic',1 if gene in PRIORITY_GENES else 2)
        for assay,kind in (('SNV_INDEL','SNV'),('RNA_FUSION','RNA')):
            status=selection.get(assay) or {}
            for row in (status.get('not_final_variants') or [])[:250]:
                gene=_gene(row.get('gene'))
                offer(row,kind,'research_not_final',1 if gene in PRIORITY_GENES else 2)
        for row in ((patient.get('fusion_summary') or {}).get('candidates') or [])[:50]:
            offer(row,'RNA','research_fusion',1)
        for row in ((patient.get('cnv') or {}).get('calls') or [])[:600]:
            gene = _gene(row.get('gene'))
            if gene in CNV_PRIORITY_GENES:
                offer(row,'CNV','research_cnv',1)
            elif len(buckets[3]) < 12:
                offer(row,'CNV','research_cnv',3)
        for tier in (1,2,3):
            buckets[tier].sort(key=_research_sort_key)
        per_sample.append((rk, buckets))
    # Phase 1 reserves one research slot for every sample with an eligible
    # candidate. Verified Selection gets the first slot when present, but a
    # patient with 100 selections cannot starve patients with no Selection.
    candidates=[]
    emitted=set()
    front=sorted(per_sample,key=lambda part:0 if part[1][0] else 1)
    for _,buckets in front:
        for tier in range(4):
            if buckets[tier]:
                item=buckets[tier][0]
                candidates.append(item)
                emitted.add((item['record_key'],item['variant_key'],item['kind']))
                break
    # Phase 2 fills the remaining slots by priority tier, round-robin within
    # each tier. Nothing from Phase 1 is duplicated.
    for tier in range(4):
        max_depth=max((len(buckets[tier]) for _,buckets in per_sample),default=0)
        for depth in range(max_depth):
            for _,buckets in per_sample:
                if depth < len(buckets[tier]):
                    item=buckets[tier][depth]
                    key=(item['record_key'],item['variant_key'],item['kind'])
                    if key not in emitted:
                        candidates.append(item)
                        emitted.add(key)
    return candidates


def _unresearched_selected(pkg, queued):
    found = {(r['record_key'],r['variant_key'],r['kind']) for r in queued}
    missing = []
    for patient in (pkg.get('patients') or []):
        rk = _text((patient.get('technical_sample') or {}).get('record_key'),220)
        for assay,kind in (('SNV_INDEL','SNV'),('RNA_FUSION','RNA'),('CNV','CNV')):
            status=(patient.get('report_selection_by_assay') or {}).get(assay) or {}
            if status.get('report_selection_verified') is not True: continue
            for row in (status.get('selected_variants') or []):
                if not isinstance(row,dict): continue
                key=_source_variant_key(row)
                if (rk,key,kind) not in found:
                    missing.append({'record_key':rk,'variant_key':key or None,'kind':kind,
                        'gene':_gene(row.get('gene')) or None,'status':'manual_literature_review_required',
                        'reason':'missing_reliable_protein_or_exact_source_key'})
    return missing[:150]


class LocalCache:
    """Only public molecular descriptors and NCBI results are cached; no PHI."""
    def __init__(self, path):
        self.cx = None
        try:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            self.cx = sqlite3.connect(str(p), timeout=2)
            self.cx.execute('CREATE TABLE IF NOT EXISTS responses (request_hash TEXT PRIMARY KEY, expires REAL, payload TEXT NOT NULL)')
            self.cx.commit()
            try:
                os.chmod(p, 0o600)
            except OSError:
                pass
        except (OSError, sqlite3.Error):
            self.cx = None

    def get(self, key):
        if self.cx is None:
            return None
        try:
            row = self.cx.execute('SELECT payload,expires FROM responses WHERE request_hash=?', (key,)).fetchone()
            return json.loads(row[0]) if row and row[1] > time.time() else None
        except (ValueError, sqlite3.Error):
            return None

    def put(self, key, value, ttl=86400 * 14):
        if self.cx is None:
            return
        try:
            self.cx.execute('INSERT OR REPLACE INTO responses VALUES(?,?,?)', (key, time.time()+ttl,
                            json.dumps(value, ensure_ascii=False, separators=(',', ':'))))
            self.cx.commit()
        except (sqlite3.Error, ValueError):
            pass

    def close(self):
        if self.cx is not None:
            self.cx.close()


class NCBI:
    def __init__(self, cache, max_seconds=35, timeout=2.5, opener=None):
        self.cache = cache
        self.deadline = time.monotonic() + max(0.1, min(float(max_seconds), 300))
        self.timeout = max(0.5, min(float(timeout), 8))
        self.opener = opener or urlopen
        self.last = 0.0
        self.errors = 0
        self.network_requests = 0
        self.cache_hits = 0
        self.disabled = os.environ.get('NGS_EVIDENCE_OFFLINE', '0').strip() in ('1','true','yes')

    def get(self, action, params):
        if action not in ('esearch.fcgi', 'esummary.fcgi', 'efetch.fcgi') or params.get('db') not in ('clinvar', 'pubmed') or (action == 'efetch.fcgi' and params.get('db') != 'pubmed'):
            return None, 'invalid_request'
        clean = {str(k): str(v) for k, v in params.items() if k in ('db', 'term', 'id', 'retmax', 'retmode')}
        clean['retmode'] = 'xml' if action == 'efetch.fcgi' else 'json'
        key = _fingerprint((action, clean))
        cache = self.cache.get(key)
        if cache is not None:
            self.cache_hits += 1
            return cache, 'cached'
        if self.disabled:
            return None, 'offline'
        if self.errors >= 3:
            return None, 'circuit_open'
        if time.monotonic() >= self.deadline:
            return None, 'time_budget_exceeded'
        try:
            delta = time.monotonic() - self.last
            if delta < 0.46:
                time.sleep(0.46 - delta)  # under 3/s without key, across requests in this worker
            self.last = time.monotonic()
            # No direct identifiers / disease free-text ever go to this endpoint.
            clean['tool'] = 'NGSVariantEvidence'
            url = API_ROOT + action + '?' + urlencode(clean)
            response = self.opener(Request(url, headers={'User-Agent': 'NGSVariantEvidence/1.0'}), timeout=self.timeout)
            with response as handle:
                raw = handle.read(1_000_001)
            if len(raw) > 1_000_000:
                raise ValueError('response_too_large')
            if action == 'efetch.fcgi':
                value = raw.decode('utf-8')
                if not value.lstrip().startswith('<'):
                    raise ValueError('invalid_xml_response')
            else:
                value = json.loads(raw.decode('utf-8'))
                if not isinstance(value, dict) or value.get('error'):
                    raise ValueError('invalid_response')
            self.cache.put(key, value)
            self.network_requests += 1
            self.errors = 0
            return value, 'online'
        except (OSError, ValueError, TypeError, TimeoutError) as exc:
            self.errors += 1
            return None, 'network_error_' + type(exc).__name__


def _esearch(api, db, term, limit):
    out, state = api.get('esearch.fcgi', {'db': db, 'term': term, 'retmax': min(limit, 6)})
    ids = []
    if isinstance(out, dict):
        raw = (out.get('esearchresult') or {}).get('idlist') or []
        ids = [str(x) for x in raw if str(x).isdigit()][:limit]
    return ids, state


def _esummary(api, db, ids):
    if not ids:
        return [], 'no_records'
    result, state = api.get('esummary.fcgi', {'db': db, 'id': ','.join(ids)})
    values = []
    content = (result or {}).get('result') or {}
    for uid in ids:
        row = content.get(uid)
        if isinstance(row, dict):
            values.append(row)
    return values, state


def _abstracts(api, ids):
    """Retrieve limited PubMed abstract excerpts, not full text or proven clinical evidence."""
    if not ids:
        return {}, 'no_records'
    value, status = api.get('efetch.fcgi', {'db': 'pubmed', 'id': ','.join(ids[:3])})
    if not isinstance(value, str):
        return {}, status
    try:
        root = ElementTree.fromstring(value)
        abstracts = {}
        for article in root.findall('.//PubmedArticle')[:3]:
            pmid = (article.findtext('./MedlineCitation/PMID') or '').strip()
            if not pmid.isdigit() or pmid not in ids:
                continue
            text_parts = []
            for section in article.findall('.//Abstract/AbstractText'):
                text = ' '.join(''.join(section.itertext()).split())
                if not text:
                    continue
                label = section.attrib.get('Label') or ''
                text_parts.append((label + ': ' if label else '') + text)
            abstracts[pmid] = ' '.join(text_parts)[:1200]
        return abstracts, status
    except (ElementTree.ParseError, ValueError, TypeError):
        return {}, 'xml_parse_error'


def _pubmed_term(identity):
    kind, gene, descriptor = identity['kind'], identity['gene'], identity['descriptor']
    if kind == 'SNV':
        # Conservative specific protein substitution, never gene-only.
        token = descriptor[2:]
        return f'"{gene}"[Title/Abstract] AND "{token}"[Title/Abstract]'
    if kind == 'RNA':
        a, b = gene.split('--')
        return f'"{a}"[Title/Abstract] AND "{b}"[Title/Abstract] AND fusion[Title/Abstract]'
    if kind == 'CNV':
        event = '(amplification[Title/Abstract] OR "copy number"[Title/Abstract])' if descriptor == 'GAIN' else '(deletion[Title/Abstract] OR "copy number loss"[Title/Abstract])'
        return f'"{gene}"[Title/Abstract] AND {event}'
    return ''


def _clinvar_term(identity):
    # ClinVar candidate search ONLY for small SNV/indels; never gene-only CNV/fusion.
    if identity['kind'] != 'SNV':
        return ''
    return f'{identity["gene"]}[gene] AND "{identity["descriptor"][2:]}"'


def _explicit_variant_mention(text, descriptor):
    """Does title/abstract actually name this protein variant? NOT clinical proof."""
    term = str(descriptor or '').removeprefix('p.')
    if not term:
        return False
    pattern = r'(?<![A-Za-z0-9_])(?:p\.)?' + re.escape(term) + r'(?![A-Za-z0-9_])'
    return bool(re.search(pattern, text or '', flags=re.I))


def query_evidence(api, identity):
    """PubMed/ClinVar leads. Exact allele and disease context require human review."""
    result = {'schema':SCHEMA, 'kind':identity['kind'], 'gene':identity['gene'],
        'descriptor':identity['descriptor'], 'specificity':identity['specificity'],
        'transcript':identity.get('transcript') or None, 'cdna':identity.get('cdna') or None,
        'somatic_oncogenicity':None,'somatic_clinical_impact':None,'germline_classification':None,
        'classification_verified':False,'auto_classification_permitted':False,
        'allele_match_verified':False,'tumor_context_verified':False,
        'requires_hgvs_and_tumor_review':True,
        'pubmed':{'status':'not_queried','papers':[]},
        'clinvar':{'status':'not_applicable','candidates':[]}}
    cterm=_clinvar_term(identity)
    if cterm:
        cids,csearch = _esearch(api,'clinvar',cterm,3)
        rows,csummary = _esummary(api,'clinvar',cids) if cids else ([],'not_needed')
        candidates=[]
        dropped=0
        for row in rows:
            uid=str(row.get('uid') or '')
            label=_text(row.get('title') or row.get('name') or row.get('variation_name'),350)
            if not uid.isdigit():continue
            exact_mention=_explicit_variant_mention(label, identity['descriptor'])
            cdna_mention=bool(identity.get('cdna') and identity['cdna'] in label)
            if not (exact_mention or cdna_mention):
                dropped+=1
                continue
            candidates.append({'variation_id':uid[:16], 'title':label,
                 'url':f'https://www.ncbi.nlm.nih.gov/clinvar/variation/{uid}/',
                 'match_status':'protein_or_cdna_text_mention_only_genomic_allele_unverified',
                 'allele_match_verified':False,'somatic_classification_not_read':True})
        result['clinvar']={'status': 'candidates_unverified' if candidates else
                    ('no_candidates_with_matching_variant_label' if dropped else
                     ('no_results' if csearch in ('online','cached') else csearch)),
                    'candidates':candidates,'excluded_offtarget_candidate_count':dropped}
        if cids and not rows and csummary not in ('online','cached','no_records'):
            result['clinvar']['status']=csummary
    ids,search_state=_esearch(api,'pubmed',_pubmed_term(identity),5)
    rows,summary_state=_esummary(api,'pubmed',ids) if ids else ([],'not_needed')
    abstracts,ab_state=_abstracts(api,ids) if ids else ({},'no_records')
    papers=[]
    off_target=0
    for row in rows:
        uid=str(row.get('uid') or '')
        if not uid.isdigit():continue
        title=_text(row.get('title'),350)
        abstract=abstracts.get(uid) or ''
        mention = (_explicit_variant_mention(title+' '+abstract,identity['descriptor'])
                   if identity['kind']=='SNV' else False)
        if identity['kind']=='SNV' and not mention:
            off_target+=1
            # Off-target references are kept out of evidence cards.
            continue
        papers.append({'pmid':uid,'title':title,
            'publication_date':_text(row.get('pubdate'),50),
            'source':_text(row.get('fulljournalname') or row.get('source'),100),
            'url':f'https://pubmed.ncbi.nlm.nih.gov/{uid}/',
            'variant_mention_in_metadata':mention if identity['kind']=='SNV' else None,
            'allele_match_verified':False,'tumor_context_verified':False,
            'relevance':'protein_mentioned_not_clinically_verified' if mention else 'gene_pair_or_cnv_search_only',
            'abstract_excerpt':abstract or None,
            'abstract_status':'retrieved_unreviewed' if abstract else ab_state})
    result['pubmed']={'status':'search_results_unverified' if papers else
        ('no_variant_specific_metadata_match' if off_target else
         ('no_results' if search_state in ('online','cached') else search_state)),
        'papers':papers,'offtarget_filtered':off_target,'abstracts_status':ab_state}
    if ids and not rows and summary_state not in ('online','cached','no_records'):
        result['pubmed']['status']=summary_state
    return result


def _selection_status(pkg):
    states=[]
    for p in pkg.get('patients') or []:
        rk=_text((p.get('technical_sample') or {}).get('record_key'),220)
        assay=p.get('report_selection_by_assay') or {}
        x=assay.get('SNV_INDEL') or {}
        if (p.get('technical_sample') or {}).get('sample_type') == 'RNA':
            x=assay.get('RNA_FUSION') or {}
        states.append({'record_key':rk,'verified':x.get('report_selection_verified') is True,
                       'status':str(x.get('result_status') or 'not_available'),
                       'selected_count':x.get('selected_count')})
    return states


def enrich_packages(compact, full, base, cfg=None, opener=None):
    """Automatic, fail-open research; evidence and report decisions remain separate."""
    cfg=cfg or {}
    max_items=max(1,min(int(cfg.get('variant_evidence_max_items',96)),250))
    max_seconds=max(1,min(float(cfg.get('variant_evidence_max_seconds',75)),180))
    cache=LocalCache(Path(base)/'DATA'/'variant_evidence_cache_v1.sqlite3')
    api=NCBI(cache,max_seconds=max_seconds,
             timeout=cfg.get('variant_evidence_http_timeout',2.5),opener=opener)
    candidates=_candidate_rows(compact)
    selected_missing=_unresearched_selected(compact,candidates)
    query_map={}
    output=[]
    for item in candidates[:max_items]:
        identity=item['identity']
        lookup=_fingerprint(identity)
        if lookup not in query_map:
            query_map[lookup]=query_evidence(api,identity)
        output.append({'record_key':item['record_key'],'variant_key':item['variant_key'],
            'kind':item['kind'],'provenance':item['provenance'],
            'priority_tier':item['priority_tier'],
            'source_variant_key_present':item['source_variant_key_present'],
            'source_oncogenicity':item['source_oncogenicity'],
            'evidence_key':lookup,'evidence':query_map[lookup]})
    cache.close()
    # E-utilities search processed != clinically/allele verified.
    selection_states=_selection_status(compact)
    evaluated=set(r['record_key'] for r in output)
    counts={rk:sum(1 for r in output if r['record_key']==rk) for rk in
            {p['record_key'] for p in selection_states}}
    complete_search_status={'search_results_unverified','no_results','no_variant_specific_metadata_match',
                            'candidates_unverified','no_candidates_with_matching_variant_label'}
    researched_count=sum(1 for r in output if (r['evidence'].get('pubmed') or {}).get('status') in complete_search_status)
    pending=[{'record_key':r['record_key'],'variant_key':r['variant_key'],
              'kind':r['kind'],'gene':r['identity']['gene'],
              'descriptor':r['identity']['descriptor'],
              'priority_tier':r['priority_tier']} for r in candidates[max_items:max_items+250]]
    summary={'candidate_total':len(candidates),'examined':len(output),
        'pending_research_queue':pending,'pending_research_queue_truncated':len(candidates)-len(output)>len(pending),
        'research_completed_or_searched_no_result_count':researched_count,
        'attempted_but_source_unavailable_count':len(output)-researched_count,
        'deferred':max(0,len(candidates)-len(output)),
        'per_sample_evidence_count':counts,
        'samples_without_evidence_attempt':[x['record_key'] for x in selection_states if x['record_key'] not in evaluated],
        'selection_status_by_sample':selection_states,
        'unverified_selection_sample_count':sum(not x['verified'] for x in selection_states),
        'verified_selection_without_specific_query':selected_missing,
        'verified_selection_without_specific_query_count':len(selected_missing),
        'distinct_queries':len(query_map),'network_requests':api.network_requests,
        'cache_hits':api.cache_hits,'offline_or_unavailable':api.disabled or api.errors>=3,
        'time_budget_exhausted':time.monotonic()>=api.deadline,
        'query_limited_by_budget':bool(api.errors>=3 or time.monotonic()>=api.deadline),
        'time_budget_seconds':max_seconds,'auto_research_enabled':True,
        'human_verification_required':True,'allele_level_verified_count':0,
        'note':'Evidence search completeness is independent from clinical report readiness.'}
    result_hash=_fingerprint(output)
    by_pair={(r['record_key'],r['variant_key'],r['kind']):r for r in output}
    for pkg in (compact,full):
        pkg['variant_evidence_schema']=SCHEMA
        pkg['variant_evidence_policy']=(
            'Research only. GenNext Selection, YZ recommendation, technical IGV and pathologist report decision '
            'stay independent. Protein mention is not exact genomic allele or clinical-actionability evidence. '
            'Unknown oncogenicity must be preserved. A missing online result is NOT a negative assay.')
        pkg['variant_evidence_records']=json.loads(json.dumps(output,ensure_ascii=False))
        pkg['variant_evidence_source_sha256']=result_hash
        pkg['variant_evidence_summary']=json.loads(json.dumps(summary,ensure_ascii=False))
        for p in pkg.get('patients') or []:
            rk=_text((p.get('technical_sample') or {}).get('record_key'),220)
            lists=[(p.get('oncogenic_and_likely_oncogenic_variants') or [],'SNV'),
                   ((p.get('fusion_summary') or {}).get('candidates') or [],'RNA'),
                   ((p.get('cnv') or {}).get('calls') or [],'CNV')]
            selection=p.get('report_selection_by_assay') or {}
            for assay,kind in (('SNV_INDEL','SNV'),('RNA_FUSION','RNA'),('CNV','CNV')):
                x=selection.get(assay) or {}
                lists += [(x.get('selected_variants') or [],kind),
                          (x.get('not_final_variants') or [],kind)]
            for rows,kind in lists:
                for row in rows:
                    if not isinstance(row,dict): continue
                    key=_source_variant_key(row)
                    hit=by_pair.get((rk,key,kind))
                    if hit:
                        row['literature_evidence']=json.loads(json.dumps(hit['evidence'],ensure_ascii=False))
                        row['literature_evidence_key']=hit['evidence_key']
    return compact,full
