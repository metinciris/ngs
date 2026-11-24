from __future__ import annotations

import csv
import hashlib
import re
from pathlib import Path


def _norm(s: str) -> str:
    return re.sub(r'\s+', '', str(s or '').strip()).upper()


def _norm_gene(s: str) -> str:
    return re.sub(r'[^A-Za-z0-9-]+', '', str(s or '')).upper()


def _norm_tx(s: str) -> str:
    m = re.search(r'(N[MR]_\d+(?:\.\d+)?)', str(s or ''), re.I)
    return m.group(1).upper() if m else ''


def _norm_cdna(s: str) -> str:
    x = str(s or '').strip()
    if not x:
        return ''
    x = x.replace('→', '>').replace('⇒', '>')
    x = re.sub(r'\s*-\s*>\s*', '>', x)
    x = re.sub(r'\s+', '', x)
    if x.lower().startswith('c.'):
        x = 'c.' + x[2:]
    return x.upper()


def _norm_protein(s: str) -> str:
    x = re.sub(r'\s+', '', str(s or '').strip())
    if not x or x in ('-', '(-)', 'N/A', 'NA'):
        return ''
    return x.upper()


def _pick(row, *names):
    for name in names:
        if name in row and str(row.get(name) or '').strip():
            return str(row.get(name) or '').strip()
    low = {str(k).casefold(): k for k in row.keys()}
    for name in names:
        k = low.get(str(name).casefold())
        if k is not None and str(row.get(k) or '').strip():
            return str(row.get(k) or '').strip()
    return ''


def _snv_key(row: dict) -> str:
    chrom = _pick(row, 'Chrom', 'Chr')
    start = _pick(row, 'Start', 'Pos')
    ref = _pick(row, 'Ref')
    alt = _pick(row, 'Alt')
    return '|'.join(['SNV', chrom, start, ref, alt])


def _cnv_key(row: dict) -> str:
    return '|'.join([
        'CNV', _pick(row, 'Chrom', 'Chr'), _pick(row, 'Start'), _pick(row, 'End'),
        _pick(row, 'SVType'), _pick(row, 'GeneName', 'Gene')
    ])


def report_text_hash(text: str) -> str:
    return hashlib.sha256(str(text or '').encode('utf-8')).hexdigest()


def extract_reported_items(text: str) -> dict:
    """Extract positive reported SNV/indel/CNV lines from a pasted signed-report style note.

    Deliberately conservative: only lines that explicitly say a finding was detected are
    considered positive. Negative-gene lines and biomarker summary lines are returned as
    context but are never marked as reported variants.
    """
    positives = []
    negative_genes = []
    raw_lines = [re.sub(r'\s+', ' ', ln).strip() for ln in str(text or '').splitlines()]
    for line in raw_lines:
        if not line:
            continue
        low = line.casefold().replace('\u0307','').translate(str.maketrans({'ı':'i','ş':'s','ğ':'g','ü':'u','ö':'o','ç':'c'}))
        gm = re.search(r'[-•]?\s*([A-Za-z0-9-]+)\s+gen(?:i|inde)\b', line, re.I)
        gene = _norm_gene(gm.group(1)) if gm else ''
        if gene and 'saptanmamistir' in low:
            negative_genes.append(gene)
            continue
        # Only explicit positive result statements are candidates.
        positive_phrase = 'saptanmistir' in low
        if not positive_phrase or not gene:
            continue
        if any(x in low for x in ('msi durumu', 'tümör mutasyon', 'tumor mutasyon', 'homolog rekombinasyon', 'hrd')):
            continue

        item = {
            'type': 'CNV' if ('kopya sayisi' in low or 'cnv' in low) else 'SNV',
            'gene': gene,
            'transcript': '', 'cdna': '', 'protein': '', 'exon': '', 'classification': '', 'feature': '',
            'raw_text': line,
        }
        txm = re.search(r'(N[MR]_\d+(?:\.\d+)?)', line, re.I)
        if txm:
            item['transcript'] = txm.group(1)
        # Accept standard HGVS and the historical "c.782+1 -> A" report form.
        cm = re.search(r'\bc\.\s*([A-Za-z0-9_+\-*?=]+(?:\s*(?:->|→|>)\s*[A-Za-z0-9_+\-*?=]+)?)', line, re.I)
        if cm:
            item['cdna'] = _norm_cdna('c.' + cm.group(1))
        pm = re.search(r'\bp\.\s*([A-Za-z0-9_*?=]+)', line, re.I)
        if pm:
            item['protein'] = _norm_protein('p.' + pm.group(1))
        exm = re.search(r'Ekzon\s*([0-9]+)', line, re.I)
        if exm:
            item['exon'] = exm.group(1)
        clm = re.search(r'Sınıflandırma\s*Aşama\s*([^,\)]+)', line, re.I)
        if clm:
            item['classification'] = clm.group(1).strip()
        if item['type'] == 'CNV':
            if 'artis' in low or 'gain' in low or 'dup' in low:
                item['cnv_event'] = 'GAIN'
            elif 'kayip' in low or 'loss' in low or 'del' in low:
                item['cnv_event'] = 'LOSS'
            fcm = re.search(r'Fold\s*Change\s*[:=]?\s*([0-9]+(?:[.,][0-9]+)?)', line, re.I)
            if fcm:
                item['fold_change'] = fcm.group(1).replace(',', '.')
            prm = re.search(r'Probe\s*(?:Say[ıi]s[ıi])?\s*[:=]?\s*([0-9]+)', line, re.I)
            if prm: item['probe_count'] = prm.group(1)
            parts=[item.get('cnv_event') or 'CNV']
            if item.get('fold_change'): parts.append('Fold Change '+item['fold_change'])
            if item.get('probe_count'): parts.append('Probe '+item['probe_count'])
            item['feature']=' · '.join(parts)
        positives.append(item)
    return {'positives': positives, 'negative_genes': sorted(set(negative_genes)), 'report_hash': report_text_hash(text)}


def load_variant_rows(snv_path: Path | None, cnv_path: Path | None) -> tuple[list[dict], list[dict]]:
    snv_rows, cnv_rows = [], []
    if snv_path and Path(snv_path).exists():
        with Path(snv_path).open('r', encoding='utf-8-sig', errors='replace', newline='') as f:
            for row in csv.DictReader(f, delimiter='\t'):
                row = dict(row); row['_variant_key'] = _snv_key(row); snv_rows.append(row)
    if cnv_path and Path(cnv_path).exists():
        with Path(cnv_path).open('r', encoding='utf-8-sig', errors='replace', newline='') as f:
            for row in csv.DictReader(f, delimiter='\t'):
                row = dict(row); row['_variant_key'] = _cnv_key(row); cnv_rows.append(row)
    return snv_rows, cnv_rows


def _cdna_compatible(report_cdna: str, row_cdna: str) -> bool:
    a,b=_norm_cdna(report_cdna),_norm_cdna(row_cdna)
    if not a or not b: return False
    if a==b: return True
    # Historical report form can omit the reference base, e.g. c.782+1 -> A.
    ma=re.fullmatch(r'C\.([0-9]+[+-][0-9]+)>([ACGT])',a)
    mb=re.fullmatch(r'C\.([0-9]+[+-][0-9]+)([ACGT])>([ACGT])',b)
    return bool(ma and mb and ma.group(1)==mb.group(1) and ma.group(2)==mb.group(3))


def _score_snv(item: dict, row: dict) -> tuple[int, list[str]]:
    gene = _norm_gene(_pick(row, 'Gene_refGene', 'Gene', 'Gene.refGene'))
    if gene != item.get('gene'):
        return 0, []
    score, why = 30, ['gene']
    tx_i, tx_r = _norm_tx(item.get('transcript')), _norm_tx(_pick(row, 'tx', 'Transcript', 'Tx'))
    cd_i, cd_r = _norm_cdna(item.get('cdna')), _norm_cdna(_pick(row, 'txChange', 'HGVSc', 'cDNA', 'cDNA Change'))
    pr_i, pr_r = _norm_protein(item.get('protein')), _norm_protein(_pick(row, 'aaChange', 'HGVSp', 'Protein', 'Protein Change'))
    if tx_i and tx_r and tx_i == tx_r:
        score += 20; why.append('transcript')
    if cd_i and cd_r and _cdna_compatible(cd_i, cd_r):
        score += 45; why.append('cDNA' if cd_i==cd_r else 'cDNA-fuzzy-splice')
    if pr_i and pr_r and pr_i == pr_r:
        score += 35; why.append('protein')
    # Exact cDNA should outweigh transcript version mismatches; protein-only matches stay reviewable.
    return min(score, 100), why


def _score_cnv(item: dict, row: dict) -> tuple[int, list[str]]:
    gene = _norm_gene(_pick(row, 'GeneName', 'Gene'))
    if gene != item.get('gene'):
        return 0, []
    score, why = 60, ['gene']
    sv = _pick(row, 'SVType').upper()
    ev = str(item.get('cnv_event') or '').upper()
    if ev == 'GAIN' and sv in ('DUP', 'GAIN', 'AMP', 'AMPLIFICATION'):
        score += 25; why.append('event')
    elif ev == 'LOSS' and sv in ('DEL', 'LOSS', 'DELETION'):
        score += 25; why.append('event')
    elif ev:
        score -= 20
    return max(0, min(score, 100)), why


def match_reported_items(items: list[dict], snv_rows: list[dict], cnv_rows: list[dict]) -> list[dict]:
    out = []
    for item in items:
        rows = cnv_rows if item.get('type') == 'CNV' else snv_rows
        candidates = []
        for row in rows:
            score, why = (_score_cnv(item, row) if item.get('type') == 'CNV' else _score_snv(item, row))
            if score:
                candidates.append((score, why, row))
        candidates.sort(key=lambda x: x[0], reverse=True)
        best = candidates[0] if candidates else None
        result = dict(item)
        if best:
            score, why, row = best
            result.update({
                'match_score': score,
                'match_basis': why,
                'variant_key': row.get('_variant_key') or '',
                'matched_gene': _pick(row, 'GeneName', 'Gene_refGene', 'Gene'),
                'matched_transcript': _pick(row, 'tx', 'Transcript', 'Tx'),
                'matched_cdna': _pick(row, 'txChange', 'HGVSc', 'cDNA', 'cDNA Change'),
                'matched_protein': _pick(row, 'aaChange', 'HGVSp', 'Protein', 'Protein Change'),
                'matched_filter': _pick(row, 'Filter'),
                'matched_af': _pick(row, 'AF', 'VAF'),
                'matched_dp': _pick(row, 'DP'),
                'matched_feature': (' '.join(x for x in [_pick(row,'SVType'), ('Fold Change '+_pick(row,'FoldChange','Fold Change')) if _pick(row,'FoldChange','Fold Change') else '', ('Probe '+_pick(row,'Probes','Probe Count')) if _pick(row,'Probes','Probe Count') else ''] if x) if item.get('type')=='CNV' else ''),
                'ambiguous_same_score': sum(1 for x in candidates if x[0] == score) > 1,
            })
            # Auto-suggest only strong, non-ambiguous molecular matches.
            result['suggest_confirm'] = bool(score >= 75 and not result['ambiguous_same_score'])
        else:
            result.update({'match_score': 0, 'variant_key': '', 'suggest_confirm': False, 'ambiguous_same_score': False})
        out.append(result)
    return out
