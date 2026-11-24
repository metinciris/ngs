from __future__ import annotations

import re
import webbrowser
from urllib.parse import quote

ONCOKB_ROOT = 'https://www.oncokb.org/'
FRANKLIN_ROOT = 'https://franklin.genoox.com/clinical-db'


def protein_alteration(change: str) -> str:
    """Return a compact protein alteration suitable for an OncoKB URL."""
    s = str(change or '').strip()
    if not s:
        return ''
    # Common NGS platformu form: NM_...:exon2:c.G34T:p.G12C
    m = re.search(r'p\.([A-Za-z0-9_*?=]+(?:_[A-Za-z0-9_*?=]+)?(?:delins[A-Za-z0-9_*?=]+|del|dup|ins[A-Za-z0-9_*?=]+)?)', s)
    if m:
        return m.group(1)
    # Accept an already compact alteration such as G12C / V600E.
    tail = s.split(':')[-1].strip()
    if tail.lower().startswith('p.'):
        tail = tail[2:]
    if re.fullmatch(r'[A-Za-z*][A-Za-z0-9_*?=.-]{1,80}', tail):
        return tail
    return ''


def normalize_variant_key(variant_key: str) -> tuple[str, str, str, str] | None:
    """Parse chr|pos|ref|alt. SNV-prefixed keys are also accepted."""
    parts = [str(x).strip() for x in str(variant_key or '').split('|')]
    if len(parts) >= 5 and parts[0].upper() == 'SNV':
        parts = parts[1:5]
    if len(parts) != 4:
        return None
    chrom, pos, ref, alt = parts
    if not chrom or not pos or not ref or not alt:
        return None
    if not chrom.lower().startswith('chr'):
        chrom = 'chr' + chrom
    return chrom, pos, ref, alt


def search_text(gene: str = '', change: str = '', variant_key: str = '') -> str:
    alt = protein_alteration(change)
    if gene and alt:
        return f'{gene} {alt}'
    if gene:
        return str(gene).strip()
    parsed = normalize_variant_key(variant_key)
    if parsed:
        chrom, pos, ref, alt = parsed
        return f'{chrom}:{pos} {ref}>{alt}'
    return str(change or variant_key or '').strip()


def oncokb_url(gene: str = '', change: str = '', variant_key: str = '') -> str:
    gene = str(gene or '').strip()
    alt = protein_alteration(change)
    if gene and alt:
        return f'{ONCOKB_ROOT.rstrip("/")}/gene/{quote(gene, safe="")}/{quote(alt, safe="")}'
    if gene:
        return f'{ONCOKB_ROOT.rstrip("/")}/gene/{quote(gene, safe="")}'
    return ONCOKB_ROOT


def franklin_url(variant_key: str = '', reference: str = 'auto') -> str:
    """Return a Franklin URL.

    We deliberately avoid constructing a genomic deep-link unless the user explicitly
    configured a reference build. A wrong build is worse than one extra paste.
    """
    parsed = normalize_variant_key(variant_key)
    ref = str(reference or 'auto').lower()
    if parsed and ref in ('hg19', 'grch37'):
        chrom, pos, r, a = parsed
        return f'{FRANKLIN_ROOT}/variant/snpTumor/{quote(chrom)}-{quote(pos)}-{quote(r)}-{quote(a)}?app=assessment-tools'
    # Franklin accepts a variant / gene on its search page. The caller copies the query
    # to clipboard before opening this page when build is unknown.
    return FRANKLIN_ROOT


def open_url(url: str) -> None:
    webbrowser.open_new_tab(url)
