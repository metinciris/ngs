from __future__ import annotations

import csv
import io
import json
import zipfile
import sys
from pathlib import Path

# Some platform exports may contain long annotation cells. Keep the parser from
# failing purely because of Python's conservative default CSV field size, but
# still classify legacy broad-network captures explicitly below.
try:
    csv.field_size_limit(min(sys.maxsize, 16 * 1024 * 1024))
except (OverflowError, ValueError):
    csv.field_size_limit(2 * 1024 * 1024)



def _basename_set(zf: zipfile.ZipFile) -> set[str]:
    return {Path(n).name.lower() for n in zf.namelist() if n and not n.endswith('/')}


def _norm_sample_id(value: str) -> str:
    return ''.join(ch for ch in str(value or '').upper() if ch.isalnum())


def _sample_match(expected: str, observed: str) -> bool:
    a=_norm_sample_id(expected); b=_norm_sample_id(observed)
    if not a or not b:return False
    if a==b:return True
    # MultiQC may append lane/read suffixes to the original sample label.
    return b.startswith(a) or a.startswith(b)


def _general_stats_samples_from_bytes(raw: bytes, suffix: str) -> list[str]:
    text=raw.decode('utf-8-sig',errors='replace')
    low=suffix.lower()
    if low.endswith('.json'):
        try:obj=json.loads(text)
        except Exception:return []
        found=[]
        def walk(v):
            if isinstance(v,dict):
                # MultiQC general_stats JSON commonly stores sample names as mapping keys.
                for k,val in v.items():
                    if isinstance(val,(dict,list)):walk(val)
                    if isinstance(k,str) and ('MP' in k.upper() or '-' in k):found.append(k)
            elif isinstance(v,list):
                for x in v:walk(x)
        walk(obj);return list(dict.fromkeys(found))
    delimiter='\t' if low.endswith('.tsv') else ','
    rows=list(csv.reader(io.StringIO(text),delimiter=delimiter))
    if not rows:return []
    header=[str(x or '').strip().lower() for x in rows[0]]
    idx=0
    for i,h in enumerate(header):
        if h in ('sample','sample name','sample_name') or h.startswith('sample'):
            idx=i;break
    out=[]
    for row in rows[1:]:
        if len(row)>idx and str(row[idx]).strip():out.append(str(row[idx]).strip())
    return out


def validate_qc_source(path: Path | str, expected_patient_id: str = "") -> tuple[bool, str, dict]:
    """Validate that a local QC artifact is real and parseable, not just a task flag.

    This deliberately validates source presence/structure only. It does not decide
    whether a sample passes laboratory QC or whether RNA/DNA is clinically adequate.
    """
    p = Path(path)
    info = {'path': str(p), 'format': p.suffix.lower().lstrip('.'), 'size_bytes': None,
            'general_stats': None, 'member_count': 0, 'table_count': 0,
            'expected_patient_id': str(expected_patient_id or ''), 'sample_match': None}
    if not p.is_file():
        return False, 'QC kaynak dosyası diskte bulunamadı.', info
    try:
        info['size_bytes'] = p.stat().st_size
    except OSError as e:
        return False, f'QC kaynak dosyası okunamadı: {e}', info
    if not info['size_bytes']:
        return False, 'QC kaynak dosyası 0 byte.', info

    suffix = p.suffix.lower()
    try:
        if suffix == '.zip':
            if not zipfile.is_zipfile(p):
                return False, 'QC dosyası ZIP uzantılı ancak geçerli ZIP değil.', info
            with zipfile.ZipFile(p) as zf:
                bad = zf.testzip()
                if bad:
                    return False, f'QC ZIP CRC doğrulaması başarısız: {Path(bad).name}', info
                names = _basename_set(zf)
                info['member_count'] = len(names)
                info['table_count'] = sum(n.endswith(('.tsv', '.csv', '.json')) for n in names)
                member_by_base={Path(n).name.lower():n for n in zf.namelist() if n and not n.endswith('/')}
                for candidate in ('general_stats_table.tsv', 'general_stats_table.json', 'general_stats_table.csv'):
                    if candidate in names:
                        info['general_stats'] = candidate
                        break
                if not info['general_stats']:
                    return False, 'QC ZIP içinde general_stats_table.tsv/json/csv bulunamadı.', info
                if info['table_count'] < 1:
                    return False, 'QC ZIP içinde ayrıştırılabilir tablo/JSON bulunamadı.', info
                if expected_patient_id:
                    member=member_by_base.get(info['general_stats'])
                    observed=_general_stats_samples_from_bytes(zf.read(member), info['general_stats']) if member else []
                    info['general_stats_samples']=observed[:200]
                    info['sample_match']=any(_sample_match(expected_patient_id,x) for x in observed)
                    if not info['sample_match']:
                        return False, f'QC ZIP general_stats içinde beklenen örnek bulunamadı: {expected_patient_id}', info
            return True, 'Doğrulanmış MultiQC QC ZIP kaynağı.', info

        if suffix in ('.tsv', '.csv'):
            raw = p.read_bytes()
            text = raw.decode('utf-8-sig', errors='replace')
            delimiter = '\t' if suffix == '.tsv' else ','
            rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
            if not rows or not any(str(x).strip() for x in rows[0]):
                return False, 'QC tablo dosyasında başlık bulunamadı.', info
            info['table_count'] = 1
            info['general_stats'] = p.name
            if expected_patient_id:
                observed=[r[0] for r in rows[1:] if r and str(r[0]).strip()]
                info['general_stats_samples']=observed[:200]
                info['sample_match']=any(_sample_match(expected_patient_id,x) for x in observed)
                if not info['sample_match']:
                    return False, f'QC tabloda beklenen örnek bulunamadı: {expected_patient_id}', info
            return True, 'Doğrulanmış QC tablo kaynağı.', info

        if suffix == '.json':
            obj = json.loads(p.read_text(encoding='utf-8-sig', errors='replace'))
            if isinstance(obj, dict) and obj.get('format') == 'ngs_rna_raw_qc_v1':
                # Explicit, exact MP/year validation for the RNA raw-QC import.
                from rna_qc_bundle import read_imported_metrics
                metric = read_imported_metrics(p, expected_patient_id)
                info['table_count'] = 1
                info['general_stats'] = 'RNA raw QC v1'
                info['sample_match'] = metric is not None
                if metric is None:
                    return False, 'RNA QC JSON ornek/yil ya da metrik dogrulamasi basarisiz.', info
                return True, 'Dogrulanmis RNA ham QC kaynagi.', info
            if not isinstance(obj, (dict, list)):
                return False, 'QC JSON nesne/liste yapisinda degil.', info
            info['table_count'] = 1
            return True, 'Dogrulanmis QC JSON kaynagi.', info
    except (OSError, ValueError, json.JSONDecodeError, zipfile.BadZipFile) as e:
        return False, f'QC kaynak doğrulaması başarısız: {e}', info

    return False, 'QC için desteklenmeyen kaynak dosya tipi.', info


def fusion_candidate_identity(row: dict) -> tuple[str, str, str]:
    """Return (fusion, 5prime gene, 3prime gene) only for a real gene pair.

    Metadata such as workgroup.name=Default, a sample name, or a graph title
    must never become a fusion candidate. No clinical positivity is inferred.
    """
    import re
    if not isinstance(row, dict):
        return '', '', ''
    cells = {re.sub(r'[^a-z0-9]+', '', str(k).lower()): v for k, v in row.items()}
    def pick(*keys):
        for key in keys:
            value = cells.get(re.sub(r'[^a-z0-9]+', '', key.lower()))
            if isinstance(value, (str, int, float)) and str(value).strip():
                return str(value).strip()
        return ''
    left = pick('Gene1','LeftGene','Left_Gene','gene_1','5p_gene','5prime_gene','geneA','Gene A','upstream_gene','gene_5prime')
    right = pick('Gene2','RightGene','Right_Gene','gene_2','3p_gene','3prime_gene','geneB','Gene B','downstream_gene','gene_3prime')
    fusion = pick('FusionName','Fusion Name','#FusionName','Fusion Gene','FusionGene','fusion_name','fusionName','fusion_pair','fusion_pair_name','fusion')
    # Numeric-locus GenNext labels such as EGFR--TPTE(2306105) are not HGNC
    # assertions. Keep each source token unchanged (never collapse to TPTE).
    # The annotation suffix must be bounded digits only; reject malformed
    # names, arbitrary parentheses, and concatenated/multiple fusion pairs.
    # GenNext uses IGH-@-ext / IGL-@-ext for immune-locus extension
    # annotations. Accept ONLY these explicitly named noncanonical tokens;
    # they are not assertions of a clinically validated fusion partner.
    gene_token = r'(?:[A-Za-z](?:[A-Za-z0-9_.]|-(?!-)){1,39}(?:\([0-9]{3,12}\))?|(?:IGH|IGK|IGL)-@-ext)'
    gene_rx = re.compile(rf'^{gene_token}$')
    def valid(value):
        base = str(value).split('(', 1)[0].casefold()
        return bool(gene_rx.fullmatch(value)) and base not in ('default','none','null','nan','unknown','loading')
    if not (left and right) and fusion:
        match = re.fullmatch(rf'\s*({gene_token})\s*(?:::|--|/)\s*({gene_token})\s*', fusion)
        if not match:
            match = re.fullmatch(r'\s*([A-Za-z][A-Za-z0-9_]{1,39})-([A-Za-z][A-Za-z0-9_]{1,39})\s*',fusion)
        if match:
            left=left or match.group(1)
            right=right or match.group(2)
    if not (valid(left) and valid(right)):
        return '', '', ''
    return fusion if fusion and fusion.lower()!='default' else f'{left}::{right}', left, right


def validate_fusion_source(path: Path | str) -> tuple[bool, str, dict]:
    """Classify a Fusion artifact as complete, incomplete or review-only.

    A visible ``Loading...`` placeholder, a React ``[object Object]`` serialization,
    or a candidate table that has no read/breakpoint/frame evidence is NOT considered
    a complete Fusion source. Such files remain useful evidence and can still be sent
    to AI, but the task must stay REVIEW until a richer source or manual decision exists.
    """
    p = Path(path)
    info = {'path': str(p), 'format': p.suffix.lower().lstrip('.'), 'size_bytes': None,
            'kind': 'missing', 'row_count': None, 'manual_result': None,
            'loading_placeholder': False, 'object_object_cells': 0,
            'detail_complete': None, 'detail_columns': [],
            'object_object_columns':{}, 'critical_object_columns':[],
            'noncritical_object_columns':[], 'populated_detail_columns':[]}
    if not p.is_file():
        return False, 'Fusion kaynak dosyası diskte bulunamadı.', info
    info['size_bytes'] = p.stat().st_size
    if not info['size_bytes']:
        return False, 'Fusion kaynak dosyası 0 byte.', info
    suffix = p.suffix.lower()
    try:
        if suffix in ('.tsv', '.csv', '.txt'):
            text = p.read_text(encoding='utf-8-sig', errors='replace')
            if not text.strip():
                return False, 'Fusion kaynak tablosu boş.', info
            info['loading_placeholder']=bool(__import__('re').search(r'\bLoading(?:\.{1,3}|…)?\b|loading\s+fusion|please\s+wait',text,__import__('re').I))
            info['object_object_cells']=text.count('[object Object]')+text.count('[object object]')
            if info['loading_placeholder']:
                info['kind']='review_loading'
                return False, 'Fusion sayfası/exportu Loading durumunda kalmış; sonuç tamamlanmış kabul edilemez.', info
            delimiter = '\t' if suffix in ('.tsv', '.txt') else ','
            rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
            if not rows or not any(str(x).strip() for x in rows[0]):
                return False, 'Fusion kaynak tablosunda başlık bulunamadı.', info
            headers=[str(x or '').strip() for x in rows[0]]
            info['headers']=headers
            info['kind'] = 'structured_export'
            info['row_count'] = max(0, len(rows) - 1)
            # A successfully downloaded table is not a verified fusion result.
            # Validate content independently of transport/source verification.
            candidate_rows = [dict(zip(headers, row)) for row in rows[1:] if any(str(x).strip() for x in row)]
            valid_candidates = [row for row in candidate_rows if all(fusion_candidate_identity(row)[1:])]
            info['valid_candidate_count'] = len(valid_candidates)
            info['metadata_rows_excluded'] = len(candidate_rows)-len(valid_candidates)
            if not valid_candidates:
                info['detail_complete'] = False
                info['kind'] = 'review_metadata_not_fusion' if candidate_rows else 'review_empty_candidate_table'
                return False, ('Fusion tablosunda gen çifti bulunmadı; metadata/Default gerçek füzyon değildir.'
                               if candidate_rows else 'Fusion tablosu boş; doğrulanmış negatif analitik sonuç değildir.'), info
            # v0.4.5 briefly embedded whole nested API objects into EvidenceJSON.
            # Those files can exceed Python's CSV field limit and, more
            # importantly, may originate from a page-wide/static fusion catalogue
            # rather than the current sample. Preserve them as evidence only and
            # force a sample-specific recollection.
            if any(str(h).strip().lower()=='evidencejson' for h in headers):
                info['kind']='review_legacy_broad_network_capture'
                return False, 'Eski geniş network Fusion yakalaması (EvidenceJSON) örnek-spesifik kabul edilemez; v0.4.6 ile yeniden toplayın.', info
            # v0.4.54 could prematurely accept an endpoint response with ~1000
            # fusion-like rows simply because it included this sample UUID.
            # That is network evidence, not an authenticated native TSV export.
            # Recheck these DONE records at app startup; keep original files.
            provenance_columns=[i for i,h in enumerate(headers) if ''.join(c for c in h.lower() if c.isalnum())=='collectorprovenance']
            if provenance_columns and any(
                'sample_specific_network' in (r[provenance_columns[0]].lower() if len(r)>provenance_columns[0] else '')
                for r in rows[1:] if r
            ):
                info['kind']='review_uncorroborated_network'
                info['detail_complete']=False
                return False, 'Network tablosu dogrudan TSV indirmesiyle dogrulanmadi; katalog/analitik sonuc ayrimi icin yeniden toplanmali.', info
            # Genuine native TSVs can contain [object Object] in annotation-only
            # fields because the vendor's exporter serializes JS objects as text.
            # Do not discard *all* candidate identities for a lost annotation;
            # importantly, do NOT accept unreadable junction/breakpoint/frame data.
            # Preserve every original TSV byte; classify loss by field instead.
            norm=lambda x:''.join(c for c in str(x or '').lower() if c.isalnum())
            opaque_by_col={}
            for row in candidate_rows:
                for h, value in row.items():
                    if '[object object]' in str(value or '').casefold():
                        opaque_by_col[h]=opaque_by_col.get(h,0)+1
            info['object_object_columns']=opaque_by_col
            detail_tokens=('breakpoint','junction','spanning','discordant',
                           'ffpm','frame','splitread','supportread','supportingread',
                           'fusioninspector','callersupport','readcount','readsupport')
            detail=[h for h in headers if any(token in norm(h) for token in detail_tokens)]
            info['detail_columns']=detail
            critical_tokens=('arriba','starfusion','caller','breakpoint','junction',
                             'spanning','discordant','ffpm','frame','splitread',
                             'support','readcount','fusioninspector')
            critical_opaque=[h for h in opaque_by_col if any(t in norm(h) for t in critical_tokens)]
            # Gene identity, the other biologically essential component, must
            # also be parseable for each genuine candidate row; candidate_identity
            # above already excludes invalid or metadata-only rows.
            for h in headers:
                if norm(h) in ('fusiongene','fusionname','gene1','gene2',
                               'leftgene','rightgene','5primegene','3primegene') and h in opaque_by_col:
                    critical_opaque.append(h)
            info['critical_object_columns']=sorted(set(critical_opaque))
            info['noncritical_object_columns']=sorted(set(opaque_by_col)-set(critical_opaque))
            if critical_opaque:
                info['kind']='review_nested_object'
                info['detail_complete']=False
                return False, ('Fusion TSV kritik sütunlarında okunamayan [object Object] verisi var: '
                               + ', '.join(sorted(set(critical_opaque))[:12])
                               + '; ham dosya korundu, gerçek okuma desteği doğrulanamadı.'),info
            # A mere header is not evidence. At least one candidate needs an
            # interpretable value for a concrete breakpoint/read-related field.
            useful_detail=[h for h in detail if any(
                str(r.get(h) or '').strip().lower() not in ('','none','null','nan','-','n/a','[object object]')
                for r in valid_candidates)]
            info['populated_detail_columns']=useful_detail
            if not useful_detail:
                info['detail_complete']=False
                info['kind']='review_candidate_summary_only'
                msg='Fusion adayları var ancak kullanılabilir breakpoint/junction/read/frame/FFPM kanıtı yok.'
                if opaque_by_col:
                    msg+=' [object Object] içeren sütunlar: '+', '.join(sorted(opaque_by_col)[:12])+'.'
                return False,msg,info
            # A dataset with entirely absent read/breakpoint evidence remains a
            # review even if an auxiliary column such as Frame exists.
            info['detail_complete']=True
            info['kind']='structured_export_optional_annotation_loss' if opaque_by_col else 'structured_export'
            return True, ('Yapılandırılmış Fusion export; yalnız yardımcı açıklama sütunları okunamadı.'
                          if opaque_by_col else 'Yapılandırılmış Fusion export kaynağı.'),info
        if suffix == '.json':
            obj = json.loads(p.read_text(encoding='utf-8-sig', errors='replace'))
            if not isinstance(obj, dict):
                return False, 'Fusion JSON nesne yapısında değil.', info
            if obj.get('manual_review') and str(obj.get('result') or '').lower() in ('negative', 'positive', 'indeterminate'):
                info['kind'] = 'manual_review'
                info['manual_result'] = str(obj.get('result')).lower()
                return True, 'Manuel Fusion/RNA sonuç denetim kaydı.', info
            visible=str(obj.get('visible_text') or '')
            if __import__('re').search(r'\bLoading(?:\.{1,3}|…)?\b|loading\s+fusion|please\s+wait',visible,__import__('re').I):
                info['loading_placeholder']=True;info['kind']='review_loading'
                return False, 'Fusion görünür metni Loading durumunda; klinik negatif/tamam sonucu değildir.', info
            if obj.get('export_error') or obj.get('collector_state') == 'review_required':
                info['kind'] = 'review_fallback'
                return False, 'Fusion export alınamamış; yalnız görünür sayfa JSON yedeği mevcut.', info
            if obj.get('status') == 'no_data':
                info['kind'] = 'review_no_data'
                return False, 'Kaynak sistem no_data bildiriyor; analiz yeterliliği ve negatiflik doğrulanamadı.', info
            info['kind'] = 'json_source'
            return True, 'Fusion JSON kaynağı.', info
    except (OSError, ValueError, json.JSONDecodeError) as e:
        return False, f'Fusion kaynak doğrulaması başarısız: {e}', info
    return False, 'Fusion için desteklenmeyen kaynak dosya tipi.', info
