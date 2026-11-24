"""Report-language extraction for READ-ONLY display of historical NGS.

Only explicit statements are parsed. No missing gene is inferred negative, and
nothing is silently added to current sample's reporting decisions.
"""
from __future__ import annotations
import re
import unicodedata
from report_parser import extract_reported_items

_ALLOWED_GENES = re.compile(r'^[A-Z][A-Z0-9-]{1,19}$')


def _ascii(text):
    x=str(text or '').casefold().replace('ı','i')
    x=''.join(c for c in unicodedata.normalize('NFKD',x) if not unicodedata.combining(c))
    return x


def _number(raw):
    if not raw:return None
    t=raw.strip().replace(',','.')
    try:
        value=float(t)
        return value if 0<=value<=100000 else None
    except (ValueError,TypeError):return None


def _strnum(x):
    return (str(int(x)) if x==int(x) else ('%g'%x)).replace('.',',')


def parse_previous_report(text):
    """Extract report-specific positive and negative findings with evidence.

    This is *text extraction*, not variant confirmation or clinical validation.
    The caller labels source status (confirmed local copy, draft or pasted).
    """
    src=str(text or '')
    parsed=extract_reported_items(src)
    positives=[]
    for item in parsed.get('positives') or []:
        # Historical notes can contain ': -ARID1A' on the same line.
        # The generic parser treats the bullet hyphen as part of the gene.
        gene=str(item.get('gene') or '').lstrip('-•').upper()
        if not _ALLOWED_GENES.fullmatch(gene):continue
        item=dict(item,gene=gene)
        if item.get('type')!='CNV' and not (item.get('cdna') or item.get('protein') or item.get('exon')):
            # Keep a gene-only positive, but do not fabricate a locus.
            pass
        positives.append({k:item.get(k) or '' for k in ('type','gene','transcript','cdna','protein','exon',
                'classification','feature','cnv_event','fold_change','probe_count')})
    lines=[line.strip() for line in src.splitlines() if line.strip()]
    negative=[]
    other_snv_negative=False;other_cnv_negative=False
    for line in lines:
        norm=_ascii(line)
        if not ('saptanmamistir' in norm or 'tespit edilmemistir' in norm):continue
        if 'analiz edilen diger genlerde' in norm:
            if 'cnv' in norm or 'kopya sayisi' in norm:other_cnv_negative=True
            elif 'snv' in norm or 'nokta mutasyonu' in norm:other_snv_negative=True
            continue
        found=re.search(r'\b([A-Za-z][A-Za-z0-9-]{1,18})\s+gen(?:i|inde)\b',line,re.IGNORECASE)
        if found:
            gene=found.group(1).upper()
            if _ALLOWED_GENES.fullmatch(gene) and gene not in negative:negative.append(gene)
    status=''
    pct=None
    tmb=None
    category=''
    hrd=None
    norm=_ascii(src)
    for line in lines:
        low=_ascii(line)
        if 'msi' in low or 'mikrosatellit' in low:
            if re.search(r'\bmss\b|ms.stable|mikrosatellit stabil',low):status='MSS'
            elif re.search(r'\bmsi.?h\b|msi.?high|mikrosatellit instabil',low):status='MSI-H'
            m=re.search(r'(?:instabilite\s+(?:tespit\s+edilen\s+)?(?:bolgelerin\s+)?yuzdesi\s*:?\s*|msi\s*[%:]\s*)(%?\s*\d+(?:[.,]\d+)?)',low)
            if m:
                v=_number(m.group(1).replace('%',''))
                if v is not None and v<=100:pct=v
        if 'tmb' in low or 'tumor mutasyon yuku' in low:
            m=re.search(r'(?:tmb\s+degeri\s*|tmb\s*[:=]\s*)(\d+(?:[.,]\d+)?)',low)
            if m:tmb=_number(m.group(1))
            if re.search(r'\btmb[- ]?l\b',low):category='TMB-L'
            elif re.search(r'\btmb[- ]?h\b',low):category='TMB-H'
        if 'hrd' in low or 'homolog rekombinasyon yetersizligi' in low:
            m=re.search(r'hrd\s+skor[uıi]?\s*[:=]?\s*(n\s*/\s*a|\d+(?:[.,]\d+)?)',low)
            if m:hrd= 'N/A' if '/' in m.group(1) else _number(m.group(1))
    return {'schema':'ngs_local_report_text_summary_v1','positives':positives[:75],
            'negative_genes':negative[:80],
            'other_snv_negative':other_snv_negative,'other_cnv_negative':other_cnv_negative,
            'msi_status':status,'msi_percent':pct,'tmb':tmb,'tmb_category':category,'hrd':hrd,
            'extraction_only':True}


def formatted_summary(details):
    rows=[]
    for item in details.get('positives') or []:
        loc=' · '.join(x for x in (item.get('transcript'),item.get('cdna'),item.get('protein'),
                               'Ekzon '+item['exon'] if item.get('exon') else '') if x)
        typ=item.get('type') or 'SNV'
        if typ=='CNV':
            # CNV is gene-level copy-number evidence, not a missing SNV HGVS.
            loc=item.get('feature') or item.get('cnv_event') or 'CNV durumu belirtilmiş'
        rows.append('+ '+item['gene']+' ('+typ+') '+(loc or '[tam varyant girilmemiş]'))
    neg=details.get('negative_genes') or []
    if neg:rows.append('− Raporda tek tek negatif belirtilen genler: '+', '.join(neg))
    bits=[]
    if details.get('msi_status'):bits.append('MSI '+details['msi_status'])
    if details.get('msi_percent') is not None:bits.append('MSI %'+_strnum(details['msi_percent']))
    if details.get('tmb') is not None:bits.append('TMB '+_strnum(details['tmb'])+' mut/Mb'+(' ('+details['tmb_category']+')' if details.get('tmb_category') else ''))
    if details.get('hrd') is not None:bits.append('HRD '+(str(details['hrd']) if isinstance(details['hrd'],str) else _strnum(details['hrd'])))
    if bits:rows.append(' | '.join(bits))
    if details.get('other_snv_negative'):rows.append('Diğer analiz edilen genlerde SNV negatif ifadesi var (yalnız rapor kapsamı).')
    if details.get('other_cnv_negative'):rows.append('Diğer analiz edilen genlerde CNV negatif ifadesi var (yalnız rapor kapsamı).')
    return '\n'.join(rows) or 'Bu rapor metninden yapılandırılmış bulgu çıkarılamadı; tam metni inceleyin.'


def safe_ai_summary(report):
    """Narrow AI allow-list; do not export raw free-form text or patient IDs."""
    parsed=report if isinstance(report,dict) else {}
    # Re-parse sanitized output: no free strings other than strictly validated markers.
    clean=[]
    for item in (parsed.get('positives') or [])[:30]:
        gene=str(item.get('gene') or '').upper()
        if not _ALLOWED_GENES.fullmatch(gene):continue
        def token(name):
            text=str(item.get(name) or '')[:80]
            return text if re.fullmatch(r'[A-Za-z0-9_.+*?>=/:-]{0,80}',text) else ''
        clean.append({'gene':gene,'variant_type':token('type'),'transcript':token('transcript'),
                      'cdna':token('cdna'),'protein':token('protein'),'exon':token('exon'),
                      'classification':token('classification'),'feature':token('feature')})
    neg=[x for x in (parsed.get('negative_genes') or [])[:80] if isinstance(x,str) and _ALLOWED_GENES.fullmatch(x)]
    return {'source':'historical_report_text_extraction_not_current_result','positive_variants':clean,
            'negative_genes':neg,'msi_status':parsed.get('msi_status') if parsed.get('msi_status') in ('MSS','MSI-H','MSI-L') else None,
            'msi_percent':parsed.get('msi_percent') if isinstance(parsed.get('msi_percent'),(int,float)) else None,
            'tmb':parsed.get('tmb') if isinstance(parsed.get('tmb'),(int,float)) else None,
            'tmb_category':parsed.get('tmb_category') if parsed.get('tmb_category') in ('TMB-H','TMB-L') else None,
            'hrd':parsed.get('hrd') if parsed.get('hrd')=='N/A' or isinstance(parsed.get('hrd'),(int,float)) else None,
            'other_snv_negative':bool(parsed.get('other_snv_negative')),
            'other_cnv_negative':bool(parsed.get('other_cnv_negative'))}
