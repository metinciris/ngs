"""Find reported HGVS in TSV by gene and actual molecular change.

No coordinate is invented; gene-only hits are never called an exact variant.
"""
from __future__ import annotations
import re
from report_parser import _pick


def _token(x):return str(x or '').upper().replace(' ','')


def _extract(x,prefix):
    if not x:return ''
    s=str(x)
    # Handles compound descriptions 'NM_...:exon12:c.3326delG:p.G1110Efs*51'
    pattern=(r'c\.\s*[A-Za-z0-9_+*>?=\-]+' if prefix=='c' else r'p\.\s*[A-Za-z0-9_+*>?=\-]+')
    match=re.search(pattern,s,re.I)
    return _token(match.group(0)) if match else ''


def _transcript(x):
    m=re.search(r'(N[MR]_\d+(?:\.\d+)?)',str(x or ''),re.I)
    return m.group(1).upper() if m else ''


def rank_variant(item, row, source='SNV'):
    """0:no gene, 1:gene only, 2:protein candidate, 3:cDNA candidate, 4:HGVS+transcript exact candidate.

    Different explicit cDNA/protein (or a different reference transcript)
    is a conflict, never an exact hit. A confident 4 requires exact genomic
    row coordinates as well, not only identical free text.
    """
    gene=_token(item.get('gene'))
    row_gene=_token(_pick(row,'Gene_refGene','GeneName','Gene'))
    if not gene or gene!=row_gene:return 0,'different_gene'
    if str(source).upper()=='CNV':
        event=_token(item.get('cnv_event'))
        sv=_token(_pick(row,'SVType'))
        if event and (('GAIN' in event and sv not in ('DUP','AMP','GAIN','AMPLIFICATION')) or
                      ('LOSS' in event and sv not in ('LOSS','DEL','DELETION'))):
            return 1,'same_gene_different_cnv_event'
        return (2,'cnv_gene_event_candidate') if event and sv else (1,'gene_only')
    tx=_transcript(item.get('transcript'))
    row_tx=_transcript(_pick(row,'tx','Transcript','AAChange.refGene','txChange'))
    c=_extract(item.get('cdna'),'c')
    rc=_extract(_pick(row,'txChange','HGVSc','cDNA','cDNA Change','AAChange.refGene'),'c')
    p=_extract(item.get('protein'),'p')
    rp=_extract(_pick(row,'aaChange','HGVSp','Protein','Protein Change','AAChange.refGene'),'p')
    if tx and row_tx and tx!=row_tx:return 1,'transcript_mismatch'
    if c and rc and c!=rc:return 1,'different_cdna'
    if p and rp and p!=rp:return 1,'different_protein'
    c_match=bool(c and rc and c==rc)
    p_match=bool(p and rp and p==rp)
    if c_match:
        if tx and row_tx and tx==row_tx and _pick(row,'Chrom','Chr') and _pick(row,'Start','Pos') and _pick(row,'Ref') and _pick(row,'Alt'):
            return 4,'same_cdna_transcript_and_genomic_coordinates'
        return 3,'same_cdna_transcript_not_verified'
    if p_match:return 2,'same_protein_genomic_position_unverified'
    return 1,'gene_only'


def best_candidates(item, rows, source='SNV'):
    matches=[]
    for row in rows:
        score,reason=rank_variant(item,row,source)
        if score:matches.append({'score':score,'reason':reason,'row':row})
    if not matches:return {'status':'gene_not_found','matches':[]}
    matches.sort(key=lambda x:x['score'],reverse=True)
    high=matches[0]['score']
    tied=[m for m in matches if m['score']==high]
    status=('exact_candidate' if high==4 and len(tied)==1 else
            'ambiguous_exact_candidate' if high==4 else
            'cdna_candidate' if high==3 and len(tied)==1 else
            'protein_candidate' if high==2 and len(tied)==1 else
            'gene_only')
    return {'status':status,'best':matches[0], 'tied_count':len(tied),'matches':matches}
