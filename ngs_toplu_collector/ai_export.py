from __future__ import annotations

import csv
import hashlib
import hmac
import secrets
import io
import json
import os
import re
import shutil
import time
import zipfile
from collections import defaultdict
from pathlib import Path
from hrd_utils import normalize_hrd
import tkinter as tk
from tkinter import ttk, messagebox
from ai_review import build_ai_review_return_template, ai_review_return_json_schema
from source_validation import validate_qc_source, validate_fusion_source
from export_privacy_v0482 import ExportPrivacyRedactor, has_email_address, redact_email_addresses
from export_runtime import report_progress
from ai_export_job import export_filesystem_path


AI_SCHEMA = 'ngs_pool_ai_v10'
BUNDLE_SCHEMA = 'ngs_pool_ai_bundle_v10'
EXPORT_COMPONENT_VERSION = '0.4.83'
SCOPE_VALUES = ('DNA', 'RNA', 'DNA+RNA')
SOURCE_TYPES = ('clinical', 'snv', 'cnv', 'fusion', 'fusion_evidence', 'qc')


def _now_stamp() -> str:
    return time.strftime('%Y%m%d_%H%M%S')


def _safe_name(text: str) -> str:
    s = re.sub(r'[^A-Za-z0-9._-]+', '_', str(text or '').strip())
    return s.strip('._') or 'item'


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()




_TITLE_TOKENS = {
    'prof', 'prof.', 'dr', 'dr.', 'doc', 'doc.', 'doç', 'doç.', 'ogr', 'ogr.', 'öğr', 'öğr.',
    'uyesi', 'üyesi', 'uz', 'uz.', 'ars', 'ars.', 'arş', 'arş.', 'gor', 'gor.', 'gör', 'gör.'
}

def _mask_word(word: str) -> str:
    w = str(word or '')
    letters = [i for i, ch in enumerate(w) if ch.isalpha()]
    if not letters:
        return w
    if len(letters) == 1:
        i = letters[0]
        return w[:i] + '*' + w[i+1:]
    if len(letters) == 2:
        # İki harfli adlarda ikinci harfi gizle; aksi halde ad tamamen açığa çıkar.
        i0, i1 = letters
        chars = list(w)
        chars[i1] = '*'
        return ''.join(chars)
    i0, i1 = letters[0], letters[-1]
    chars = list(w)
    for i in letters[1:-1]:
        chars[i] = '*'
    return ''.join(chars)

def _mask_person_name(name: str, keep_titles: bool = False) -> str:
    text = str(name or '').strip()
    if not text:
        return ''
    parts = re.split(r'(\s+)', text)
    out = []
    for part in parts:
        if not part or part.isspace():
            out.append(part); continue
        bare = part.casefold().strip(',;:()[]{}')
        if keep_titles and bare in _TITLE_TOKENS:
            out.append(part)
        else:
            out.append(_mask_word(part))
    return ''.join(out)

def _identity_secret(base: Path) -> bytes:
    """Persistent local secret used only to derive non-reversible patient linkage IDs."""
    data_dir = Path(base) / 'DATA'
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / 'ai_identity_link.key'
    if path.exists():
        raw = path.read_bytes()
        if len(raw) >= 32:
            return raw
    raw = secrets.token_bytes(32)
    path.write_bytes(raw)
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass
    return raw

def _patient_link_id(base: Path, db, cm: dict) -> tuple[str | None, str | None]:
    """Return stable local pseudonymous linkage id and evidence basis.

    A validated Turkish national ID is never exported; only HMAC-SHA256 derived token is.
    Name similarity alone never creates a patient link.
    """
    secret = _identity_secret(base)
    nid = ''.join(ch for ch in str(cm.get('national_id') or '') if ch.isdigit())
    if nid and db.validate_national_id(nid):
        msg = ('tc:' + nid).encode('utf-8')
        return 'PT-' + hmac.new(secret, msg, hashlib.sha256).hexdigest()[:20], 'validated_local_tc_hmac'
    return None, None

def _af_pct(v):
    if v is None or v == '':
        return None
    try:
        return round(float(v) * 100.0, 4)
    except Exception:
        return None


def _ratio(a, b):
    try:
        if a is None or b is None or float(b) <= 0:
            return None
        return float(a) / float(b)
    except Exception:
        return None


def _num(v):
    if v is None:
        return None
    s = str(v).strip().replace('%', '').replace('x', '').replace(',', '.')
    if not s or s == '.':
        return None
    try:
        return float(s)
    except Exception:
        return None


def _list_scope(total_count, exported_count, *, source_complete=None) -> dict:
    """State a bounded list's real scope; an unknown total is never a zero."""
    total = total_count if type(total_count) is int and total_count >= 0 else None
    exported = max(0, int(exported_count))
    return {'total_count': total, 'exported_count': exported,
            'truncated': (exported < total) if total is not None else None,
            'source_complete': source_complete}


def _application_version(base: Path) -> str:
    try:
        version = (Path(base) / 'VERSION').read_text(encoding='utf-8-sig').strip()
        return version if re.fullmatch(r'\d+\.\d+\.\d+(?:[A-Za-z0-9._-]*)?', version) else 'unknown'
    except OSError:
        return 'unknown'


def _read_json_file(path: Path):
    try:
        return json.loads(path.read_text(encoding='utf-8-sig', errors='replace'))
    except Exception:
        return None




def _privacy_sensitive_values(db, samples: list[dict]) -> tuple[list[str], list[str]]:
    names = set()
    tcs = set()
    metadata = [db.get_case_metadata(smp.get('case_family') or smp.get('patient_id') or '') for smp in samples]
    # Full exports can contain historical context from other local records.
    # Match those known names locally too; the identifier list itself is never exported.
    try:
        metadata.extend(dict(row) for row in db.cx.execute('SELECT * FROM case_metadata').fetchall())
    except Exception:
        pass
    for cm in metadata:
        for key in ('full_name', 'requesting_physician', 'reporting_physician'):
            v = str(cm.get(key) or '').strip()
            if len(v) >= 3:
                names.add(v)
        nid = ''.join(ch for ch in str(cm.get('national_id') or '') if ch.isdigit())
        if nid and db.validate_national_id(nid):
            tcs.add(nid)
    return sorted(names, key=len, reverse=True), sorted(tcs)

def _text_privacy_hits(text: str, names: list[str], tcs: list[str], *, _redactor=None) -> list[str]:
    text = str(text or '')
    redactor = _redactor or ExportPrivacyRedactor(names, tcs)
    hits = []
    for name in redactor.find_name_hits(text):
        hits.append('name:' + _mask_person_name(name, keep_titles=True))
    if redactor.find_national_id_hits(text):
        hits.append('national_id:[REDACTED]')
    if has_email_address(text):
        hits.append('email:[REDACTED]')
    if re.search(r'(?<!\d)\d{11}(?!\d)', str(text or '')):
        hits.append('possible_national_id:[REDACTED]')
    if re.search(r'"(?:full_?name|patient_?name|first_?name|last_?name|national_?id|tc_?kimlik|account_?email)"\s*:\s*"[^"\s][^"]*"', str(text or ''), re.I):
        hits.append('direct_identifier_field:[REDACTED]')
    # TSV/CSV identifiers can be absent from the local metadata table. Check
    # their explicit column labels without confusing genomic Name/Gene fields.
    first_line = str(text or '').splitlines()[:1]
    if first_line:
        for delimiter in ('\t', ','):
            headers = next(csv.reader(first_line, delimiter=delimiter), [])
            if any(_context_key(header) in _AI_PRIVATE_KEYS for header in headers):
                hits.append('direct_identifier_column:source_omitted')
                break
    if str(text or '').lstrip().startswith(('{', '[')):
        try:
            if _has_private_json_value(json.loads(text)):
                hits.append('direct_identifier_object:source_omitted')
        except (ValueError, TypeError, RecursionError):
            hits.append('privacy_scan_incomplete:unparsed_json')
    return hits


def _has_private_json_value(value, path=()):
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = _context_key(key)
            sensitive = (normalized in _AI_PRIVATE_KEYS or
                         (normalized == 'name' and any(part in ('patient', 'user', 'account', 'physician', 'analyst') for part in path)))
            if sensitive and child not in (None, '', [], {}):
                return True
            if _has_private_json_value(child, path + (normalized,)):
                return True
    elif isinstance(value, list):
        return any(_has_private_json_value(child, path) for child in value)
    return False

def _source_privacy_hits(path: Path, names: list[str], tcs: list[str], *, _redactor=None) -> list[str]:
    """Best-effort gate for known local names/TC in raw source attachments.

    If a known identifier is found, that raw source file is not copied into the AI bundle.
    Parsed/structured data remains available.
    """
    redactor = _redactor or ExportPrivacyRedactor(names, tcs)
    hits = _text_privacy_hits(path.name, names, tcs, _redactor=redactor)
    try:
        if path.suffix.lower() == '.zip':
            with zipfile.ZipFile(path, 'r') as zf:
                for zi in zf.infolist():
                    if zi.is_dir():
                        continue
                    if zi.file_size > 25 * 1024 * 1024:
                        hits.append('privacy_scan_incomplete:oversize_member')
                        continue
                    if Path(zi.filename).suffix.lower() not in ('.txt','.tsv','.csv','.json','.html','.htm','.xml','.yaml','.yml'):
                        hits.append('privacy_scan_incomplete:unsupported_member')
                        continue
                    txt = zf.read(zi).decode('utf-8', errors='ignore')
                    for h in _text_privacy_hits(txt, names, tcs, _redactor=redactor):
                        hits.append(f'{Path(zi.filename).name}:{h}')
        elif path.suffix.lower() in ('.txt','.tsv','.csv','.json','.html','.htm','.xml','.yaml','.yml') and path.stat().st_size <= 50 * 1024 * 1024:
            txt = path.read_text(encoding='utf-8', errors='ignore')
            hits.extend(_text_privacy_hits(txt, names, tcs, _redactor=redactor))
        else:
            hits.append('privacy_scan_incomplete:unsupported_or_oversize_source')
    except Exception:
        return ['privacy_scan_failed:source_omitted']
    return sorted(set(hits))

def _latest_file_path(base: Path, db, record_key: str, file_type: str):
    for row in db.files_for_record(record_key, file_type):
        p = Path(row['path'])
        if not p.is_absolute():
            p = base / p
        if p.exists() and p.is_file():
            return p, dict(row)
    return None, None


def _sample_alias_match(alias: str, patient_id: str) -> bool:
    a = str(alias or '').upper().replace(' ', '')
    p = str(patient_id or '').upper().replace(' ', '')
    if not a or not p:
        return False
    return a == p or a.startswith(p + '_') or a.startswith(p + '.') or p in a


def _scope_match(sample_type: str, scope: str) -> bool:
    st = str(sample_type or '').upper().replace(' ', '')
    sc = str(scope or 'DNA').upper().replace(' ', '')
    if sc == 'DNA+RNA':
        return st in ('DNA', 'RNA', 'DNA+RNA', 'RNA+DNA')
    if sc == 'DNA':
        return st in ('DNA', 'DNA+RNA', 'RNA+DNA')
    if sc == 'RNA':
        return st in ('RNA', 'DNA+RNA', 'RNA+DNA')
    return False


def _clean_cell(v):
    s = '' if v is None else str(v).strip()
    return '' if s == '.' else s


def _read_delimited_bytes(raw: bytes, delimiter='\t') -> dict:
    text = raw.decode('utf-8-sig', errors='replace')
    f = io.StringIO(text)
    reader = csv.DictReader(f, delimiter=delimiter)
    headers = [str(x or '').strip() for x in (reader.fieldnames or [])]
    rows = []
    for row in reader:
        rows.append({str(k or '').strip(): _clean_cell(v) for k, v in row.items() if k is not None})
    return {'headers': headers, 'rows': rows}


def _read_table_file(path: Path) -> dict | None:
    try:
        suf = path.suffix.lower()
        if suf == '.tsv':
            return _read_delimited_bytes(path.read_bytes(), '\t')
        if suf == '.csv':
            return _read_delimited_bytes(path.read_bytes(), ',')
    except Exception:
        pass
    return None


def _match_rows(rows: list[dict], patient_id: str) -> list[dict]:
    matched = []
    for row in rows or []:
        sample = row.get('Sample') or row.get('sample') or row.get('Name') or row.get('name') or ''
        if _sample_alias_match(sample, patient_id):
            matched.append(row)
    return matched


def _row_value(rows: list[dict], candidates: list[str], patient_id: str = ''):
    norm_candidates = [re.sub(r'[^a-z0-9]+', '', x.lower()) for x in candidates]
    ordered = list(rows or [])
    if patient_id:
        matched = _match_rows(ordered, patient_id)
        if matched:
            ordered = matched + [r for r in ordered if r not in matched]
    for row in ordered:
        for k, v in row.items():
            if _clean_cell(v) == '':
                continue
            nk = re.sub(r'[^a-z0-9]+', '', str(k).lower())
            if nk in norm_candidates:
                return _clean_cell(v)
    return None


def _normalize_pct(v):
    n = _num(v)
    if n is None:
        return None
    # MultiQC TSV percentage fields are normally already 0..100. Fractional values
    # such as 0.982 are kept as-is because blindly multiplying can corrupt real data.
    return n


def _extract_general_stats_tsv(table: dict, patient_id: str) -> dict:
    rows = table.get('rows') or []
    matched = _match_rows(rows, patient_id)
    metrics = {}
    for row in matched:
        sample = row.get('Sample') or row.get('Name') or ''
        for k, v in row.items():
            if k in ('Sample', 'Name') or _clean_cell(v) == '':
                continue
            metrics.setdefault(k, {})[sample] = _clean_cell(v)
    return {'headers': table.get('headers') or [], 'rows': matched, 'metrics': metrics}


def _extract_key_qc_metrics_from_tables(tables: dict, patient_id: str) -> dict:
    out = {}
    general = (tables.get('general_stats_table.tsv') or {}).get('rows') or []
    verify = (tables.get('verifybamid-results.tsv') or {}).get('rows') or []
    picard = (tables.get('picard_deduplication.tsv') or {}).get('rows') or []
    filtered = (tables.get('fastp_filtered_reads_plot.tsv') or {}).get('rows') or []

    def put(key, value, percent=False):
        if value in (None, '', '.'):
            return
        val = _normalize_pct(value) if percent else (_num(value) if _num(value) is not None else value)
        if val is not None:
            out[key] = val

    # General Stats: multiple rows may represent raw sample / .dupes / fastp suffix.
    put('mean_coverage_x', _row_value(general, ['Mean Cov.', 'Mean Coverage', 'mean_coverage'], patient_id))
    put('median_coverage_x', _row_value(general, ['Median', 'Median Coverage', 'median_coverage'], patient_id))
    put('target_ge_100x_pct', _row_value(general, ['≥ 100X', '>= 100X', '100X', '100 x pc', '100_x_pc'], patient_id), True)
    put('target_ge_50x_pct', _row_value(general, ['≥ 50X', '>= 50X', '50X', '50 x pc', '50_x_pc'], patient_id), True)
    put('target_ge_30x_pct', _row_value(general, ['≥ 30X', '>= 30X', '30X', '30 x pc', '30_x_pc'], patient_id), True)
    put('gc_pct', _row_value(general, ['GC content', 'GC', '% GC'], patient_id), True)
    put('q30_pct', _row_value(general, ['Q30', '% Q30', 'Q30 Rate', 'Q30 bases', 'Bases Q30'], patient_id), True)
    put('reads_after_filtering_m', _row_value(general, ['Reads After Filtering', 'Passed Filter Reads', 'Reads'], patient_id))
    put('adapter_pct', _row_value(general, ['% Adapter', 'Adapter', 'Adapter %'], patient_id), True)

    # Prefer the explicit .dupes row for Picard-like duplicate rate.
    dup_rows = [r for r in general if str(r.get('Sample') or '').strip().lower().endswith('.dupes')]
    dup_val = _row_value(dup_rows, ['Duplication', '% Duplication', 'Percent Duplication'])
    if dup_val is None:
        dup_val = _row_value(general, ['Duplication', '% Duplication', 'Percent Duplication'], patient_id)
    if dup_val is None and picard:
        pair = _num(_row_value(picard, ['Duplicate Pairs Nonoptical', 'Duplicate Pairs'], patient_id))
        unpaired = _num(_row_value(picard, ['Duplicate Unpaired'], patient_id))
        vals = [x for x in (pair, unpaired) if x is not None]
        dup_val = sum(vals) if vals else None
    put('duplication_pct', dup_val, True)

    # The platform MultiQC export labels VerifyBAMID as Contamination (Seq). Keep both
    # names so AI cannot mistake the original source label for a laboratory threshold.
    contam = _row_value(verify, ['Contamination (Seq)', 'FREEMIX', 'FREEMIX (Seq)', 'Contamination'], patient_id)
    if contam is None:
        contam = _row_value(general, ['Contamination (S)', 'Contamination (Seq)', 'FREEMIX'], patient_id)
    put('verifybamid_contamination_pct', contam, True)
    if contam not in (None, '', '.'):
        out['verifybamid_source_label'] = 'Contamination (Seq) / FREEMIX-compatible field from MultiQC export'

    if filtered:
        passed = _row_value(filtered, ['Passed Filter', 'Passed Filter Reads', 'Reads After Filtering'], patient_id)
        pnum = _num(passed)
        if pnum is not None:
            out['passed_filter_reads'] = pnum
            out['paired_end_read_pairs_m'] = round(pnum / 2_000_000.0, 6)
    return out


def _read_qc_package(path: Path, patient_id: str, include_full_qc: bool, sample_type: str = "") -> dict:
    source_ok, source_reason, source_validation = validate_qc_source(path, patient_id)
    result = {
        'source_file': path.name,
        'source_size_bytes': path.stat().st_size if path.exists() else None,
        'source_sha256': _sha256(path) if path.exists() else None,
        'source_verified': bool(source_ok),
        'source_validation_reason': source_reason,
        'source_validation': source_validation,
        'format': None,
        'parsed_table_count': 0,
        'table_inventory': [],
        'general_stats': {'headers': [], 'rows': [], 'metrics': {}},
        'key_metrics': {},
        'raw_multiqc_tables': {} if include_full_qc else None,
        'raw_multiqc_json': {} if include_full_qc else None,
        'fusion_evidence_tables': {},
        'read_errors': [],
    }
    tables = {}
    json_docs = {}
    imported_raw_rna = False
    try:
        if path.suffix.lower() == '.zip':
            result['format'] = 'multiqc_zip'
            with zipfile.ZipFile(path) as zf:
                for name in zf.namelist():
                    if name.endswith('/'):
                        continue
                    base_name = Path(name).name
                    low = base_name.lower()
                    try:
                        raw = zf.read(name)
                        if low.endswith('.tsv'):
                            table = _read_delimited_bytes(raw, '\t')
                            tables[base_name] = table
                            result['table_inventory'].append({'name': base_name, 'rows': len(table['rows']), 'columns': len(table['headers'])})
                        elif low.endswith('.csv'):
                            table = _read_delimited_bytes(raw, ',')
                            tables[base_name] = table
                            result['table_inventory'].append({'name': base_name, 'rows': len(table['rows']), 'columns': len(table['headers'])})
                        elif low.endswith('.json'):
                            doc = json.loads(raw.decode('utf-8-sig', errors='replace'))
                            json_docs[base_name] = doc
                    except Exception as e:
                        result['read_errors'].append(f'{base_name}: {e}')
        elif path.suffix.lower() in ('.tsv', '.csv'):
            result['format'] = path.suffix.lower().lstrip('.')
            table = _read_table_file(path)
            if table:
                tables[path.name] = table
                result['table_inventory'].append({'name': path.name, 'rows': len(table['rows']), 'columns': len(table['headers'])})
        elif path.suffix.lower() == '.json':
            result['format'] = 'json'
            data = _read_json_file(path)
            if isinstance(data, dict):
                json_docs[path.name] = data
                imported_raw_rna = data.get('format') == 'ngs_rna_raw_qc_v1'
        else:
            result['read_errors'].append('QC dosya tipi ZIP/TSV/CSV/JSON değil.')
    except Exception as e:
        result['read_errors'].append(str(e))

    result['parsed_table_count'] = len(tables)
    # The platform's current MultiQC v1.27 Data export is TSV; keep legacy JSON support too.
    gst = tables.get('general_stats_table.tsv') or tables.get('general_stats_table.csv')
    if gst:
        result['general_stats'] = _extract_general_stats_tsv(gst, patient_id)
    elif 'general_stats_table.json' in json_docs:
        result['general_stats'] = _extract_general_stats_json(json_docs['general_stats_table.json'], patient_id)

    result['key_metrics'] = _extract_key_qc_metrics_from_tables(tables, patient_id)
    # Reuse the embedded QC/Enlil parser for its stricter "required" semantics.
    # This prevents a merely downloaded ZIP from being treated as analytically complete.
    result['qc_reporter_required_missing'] = []
    result['qc_reporter_status'] = None
    result['qc_reporter_metrics'] = {}
    try:
        from qc_enlil import parse_multiqc_zip
        parsed = parse_multiqc_zip(path) if path.suffix.lower()=='.zip' else []
        wanted = str(sample_type or '').upper()
        candidates = [x for x in parsed if not wanted or str(getattr(x,'sample_type','')).upper()==wanted] or parsed
        metric = candidates[0] if candidates else None
        if metric is not None:
            metric.finalize()
            result['qc_reporter_required_missing'] = metric.missing_required()
            result['qc_reporter_status'] = metric.qc_status_label()
            result['qc_reporter_metrics'] = {
                'sample': metric.sample, 'sample_type': metric.sample_type,
                'paired_end_read_pairs_m': metric.total_reads_m, 'q30_pct': metric.q30_pct,
                'gc_pct': metric.gc_pct, 'clean_bases_gb': metric.clean_bases_gb,
                'mean_coverage_x': metric.mean_coverage_x, 'median_coverage_x': metric.median_coverage_x,
                'target_ge_50x_pct': metric.coverage_50x_pct, 'target_ge_100x_pct': metric.coverage_100x_pct,
                'duplication_pct': metric.duplication_pct, 'freemix_pct': metric.freemix_pct,
            }
            for k,v in result['qc_reporter_metrics'].items():
                if v not in (None,'') and k not in result['key_metrics']:
                    result['key_metrics'][k]=v
    except Exception as e:
        result['read_errors'].append('qc_enlil_parser: '+str(e))
    if imported_raw_rna:
        from rna_qc_bundle import read_imported_metrics
        metric = read_imported_metrics(path, patient_id)
        if metric is not None and str(sample_type or 'RNA').upper() == 'RNA':
            result['format'] = 'rna_raw_qc_json'
            result['qc_reporter_required_missing'] = metric.missing_required()
            result['qc_reporter_status'] = metric.qc_status_label()
            result['qc_reporter_metrics'] = {
                'sample': metric.patient_protocol, 'sample_type': 'RNA',
                'paired_end_read_pairs_m': metric.total_reads_m,
                'q30_pct': metric.q30_pct, 'gc_pct': metric.gc_pct,
                'clean_bases_gb': metric.clean_bases_gb,
                'mean_coverage_x': None, 'median_coverage_x': None,
                'target_ge_50x_pct': None, 'target_ge_100x_pct': None,
                'duplication_pct': None, 'freemix_pct': None,
            }
            result['key_metrics'] = {k:v for k,v in result['qc_reporter_metrics'].items() if v is not None}
            result['key_metrics']['reads_source'] = (
                'Cutadapt post-filter paired-end pairs' if metric.pass_pairs is not None
                else 'raw FASTQ paired-end pairs')
            result['key_metrics']['q30_source'] = 'raw FASTQ fqStat, R1/R2 (or HTML fallback)'
            result['key_metrics']['post_alignment_coverage_available'] = False
        else:
            result['read_errors'].append('RNA raw QC JSON: yanlis ornek/yil/tur')
    for name,table in tables.items():
        low=name.lower()
        if any(token in low for token in ('arriba','star-fusion','star_fusion','starfusion','fusioninspector','fusion_inspector','fusion_predictions','fusion')):
            result['fusion_evidence_tables'][name]=table
    if include_full_qc:
        result['raw_multiqc_tables'] = tables
        result['raw_multiqc_json'] = json_docs
    return result


def _extract_general_stats_json(data, patient_id: str):
    out = {'metrics': {}, 'headers': {}, 'rows': []}
    if not isinstance(data, dict):
        return out
    datasets = data.get('datasets') or []
    if not datasets or not isinstance(datasets[0], dict):
        return out
    ds = datasets[0]
    values = ds.get('violin_value_by_sample_by_metric') or {}
    headers = ds.get('header_by_metric') or {}
    for metric, sample_map in values.items():
        if not isinstance(sample_map, dict):
            continue
        matched = {str(k): v for k, v in sample_map.items() if _sample_alias_match(k, patient_id)}
        if matched:
            out['metrics'][metric] = matched
            h = headers.get(metric) or {}
            if isinstance(h, dict):
                out['headers'][metric] = {k: h.get(k) for k in ('title', 'description', 'suffix', 'namespace')}
    return out


def _load_json_source(base: Path, db, record_key: str, file_type: str):
    path, _ = _latest_file_path(base, db, record_key, file_type)
    if not path:
        return None, None
    data = _read_json_file(path) if path.suffix.lower() == '.json' else None
    return data, path.name


# v0.4.40: clinical context is reporting context, not direct-identifying PII.
# Keep it in the AI bundle independently from the masked-name toggle.
_CONTEXT_EMPTY = {
    '', '-', '—', 'n/a', 'na', 'none', 'null', 'unknown', 'not specified',
    'bilinmiyor', 'bilinmeyen', 'seçilmedi', 'secilmedi', 'select',
}


def _context_text(value):
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return None
    text = re.sub(r'\s+', ' ', str(value)).strip()
    if not text or text.casefold() in _CONTEXT_EMPTY:
        return None
    return text


def _context_sex(value):
    """Normalize only explicit sex values; reject GenNext selector placeholders."""
    text = _context_text(value)
    if not text:
        return None
    folded = text.casefold().replace('ı', 'i')
    compact = re.sub(r'[^a-z0-9çğıöşü]+', '', folded)
    # Seen in GenNext when no real selection was made.
    if ('selectsex' in compact or 'selectgender' in compact or
            'malefemaleother' in compact or 'erkekkadindiger' in compact):
        return None
    if compact in ('e', 'erkek', 'm', 'male'):
        return 'E'
    if compact in ('k', 'kadin', 'f', 'female'):
        return 'K'
    if compact in ('d', 'diger', 'other', 'nonbinary', 'nonbinaryother'):
        return 'D'
    return None


def _context_age(value):
    text = _context_text(value)
    if not text:
        return None
    m = re.search(r'(?<!\d)(\d{1,3})(?!\d)', text)
    if not m:
        return None
    age = int(m.group(1))
    return age if 0 <= age <= 120 else None


def _context_percentage(value):
    text = _context_text(value)
    if not text:
        return None
    plain = text.replace('%', '').replace(',', '.').strip()
    try:
        number = float(plain)
        if 0 <= number <= 100:
            return int(number) if number.is_integer() else round(number, 2)
    except Exception:
        pass
    # Preserve meaningful laboratory wording such as "30-40" rather than
    # silently dropping it. The AI sees the exact local text.
    return text


def _context_key(key):
    return re.sub(r'[^a-z0-9]+', '', str(key or '').casefold().replace('ı', 'i'))


def _deep_context_value(obj, aliases, depth=0):
    """Find an explicitly named field in historical/new GenNext Clinical JSON."""
    if depth > 9:
        return None
    wanted = {_context_key(x) for x in aliases}
    if isinstance(obj, dict):
        # Exact keys at the current level win before recursive traversal.
        for key, value in obj.items():
            if _context_key(key) in wanted:
                v = _context_text(value)
                if v is not None:
                    return v
        # Prefer patient/context containers before unrelated nested evidence.
        priority = []
        rest = []
        for key, value in obj.items():
            target = priority if any(t in _context_key(key) for t in ('patient', 'clinicalcontext', 'patientcontext', 'samplecontext')) else rest
            target.append(value)
        for value in priority + rest:
            found = _deep_context_value(value, aliases, depth + 1)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for value in obj[:100]:
            found = _deep_context_value(value, aliases, depth + 1)
            if found is not None:
                return found
    return None


def _gennext_context_from_clinical(clinical):
    if not isinstance(clinical, dict):
        return {}
    diagnosis = _deep_context_value(clinical, (
        'clinicalDiagnosis', 'clinical_diagnosis', 'diagnosis', 'primaryDiagnosis',
    ))
    tumor_type = _deep_context_value(clinical, ('tumorType', 'tumor_type', 'cancerType', 'cancer_type'))
    return {
        'diagnosis': diagnosis or tumor_type,
        'tumor_type': tumor_type,
        'age_at_diagnosis': _context_age(_deep_context_value(clinical, (
            'ageAtDiagnosis', 'age_at_diagnosis', 'patientAge', 'patient_age', 'age',
        ))),
        'sex': _context_sex(_deep_context_value(clinical, ('sex', 'gender', 'patientSex', 'patientGender'))),
        'block_id': _deep_context_value(clinical, ('blockId', 'block_id', 'blockNo', 'block_no', 'specimenBlock')),
        'tumor_percentage': _context_percentage(_deep_context_value(clinical, (
            'tumorPercentage', 'tumor_percentage', 'tumorPercent', 'tumor_percent',
            'tumorPurity', 'tumor_purity', 'tumorContent', 'tumor_content',
        ))),
    }


def _resolve_clinical_context(db, sample: dict, case_meta: dict, sample_meta: dict, clinical) -> dict:
    """Use the record-scoped field store; do not inherit another block's values.

    The old arguments are kept for call compatibility. Family-wide metadata and
    arbitrary recursive Clinical JSON are not authorities for this export.
    """
    from reporting_context import build_record_context
    record = build_record_context(db, sample)
    values = record.get('clinical') or {}
    mapping = {'diagnosis': 'tumor_diagnosis', 'age_at_diagnosis': 'age',
               'sex': 'sex', 'block_id': 'block_no', 'tumor_percentage': 'tumor_percent',
               'specimen_type': 'specimen_type', 'specimen_site': 'specimen_site',
               'tumor_type': 'tumor_type', 'clinical_diagnosis': 'clinical_diagnosis',
               'age_reference_date': 'age_reference_date'}
    context = {out: values.get(source) for out, source in mapping.items()}
    context.update({'record_key': sample.get('record_key'),
                    'sample_type': str(sample.get('sample_type') or '').upper() or None,
                    'block_source': values.get('block_source'),
                    'revision': record.get('revision'),
                    'reporting_signer_selected': bool(values.get('reporting_signer'))})
    # Keep only non-identifying clinical/biomarker provenance. Physician names
    # do not belong in AI context even when stored in a generic value field.
    sources = record.get('field_sources') or {}
    context['field_sources'] = {key: value for key, value in sources.items()
                               if key not in ('reporting_signer', 'requesting_physician', 'source_analyst')}
    context['sources'] = {}
    aliases = {'diagnosis': ('tumor_diagnosis', 'reporting_diagnosis'),
               'age_at_diagnosis': ('age', 'age_at_reference'),
               'sex': ('sex',), 'block_id': ('block_no', 'block_id'),
               'tumor_percentage': ('tumor_percent', 'tumor_percentage'),
               'tumor_type': ('tumor_type', 'source_tumor_type'),
               'clinical_diagnosis': ('clinical_diagnosis', 'source_clinical_diagnosis'),
               'specimen_type': ('specimen_type',), 'specimen_site': ('specimen_site',)}
    for out, keys in aliases.items():
        source = next((sources[k] for k in keys if k in sources), None)
        if isinstance(source, dict):
            context['sources'][out] = (source.get('effective_origin') or source.get('effective_source')
                                       or source.get('source_kind') or 'record_field_store')
        elif source is not None:
            context['sources'][out] = source
    missing_aliases = {value: key for key, value in mapping.items()}
    missing_aliases.update({'reporting_diagnosis':'diagnosis', 'age_at_reference':'age_at_diagnosis',
                           'block_id':'block_id', 'tumor_percentage':'tumor_percentage'})
    context['missing_context'] = list(dict.fromkeys(missing_aliases.get(k, k)
                                               for k in (record.get('missing_fields') or [])))
    context['clinical_report_release'] = False
    context['export_purpose'] = 'technical_pre_review'
    diagnosis_field = sources.get('reporting_diagnosis') or {}
    diagnosis_manual = bool((diagnosis_field.get('manual_override') or {}).get('present'))
    source_proposal = (record.get('reporting_diagnosis_status') == 'source_proposal' or
                       diagnosis_field.get('effective_origin') == 'source_proposal')
    context['reporting_diagnosis_status'] = record.get('reporting_diagnosis_status')
    context['pathologist_tumor_diagnosis'] = (diagnosis_field.get('effective_value')
        if not source_proposal and (diagnosis_manual or diagnosis_field.get('pathologist_verified') is True) else None)
    if context.get('diagnosis') and not context['pathologist_tumor_diagnosis']:
        context['sources']['diagnosis'] = (diagnosis_field.get('proposal_from') or
                                           ('source_tumor_type' if values.get('tumor_type')
                                            else 'source_clinical_diagnosis'))
        context['diagnosis_role'] = 'web_context_not_pathologist_diagnosis'
        context['diagnosis_resolution'] = {'source_proposed': True,
                                           'selected_field': context['sources']['diagnosis'],
                                           'user_recorded_override': False}
    else:
        context['diagnosis_role'] = 'recorded_tumor_diagnosis' if context.get('diagnosis') else 'missing'
        context['diagnosis_resolution'] = {'source_proposed': False,
                                           'selected_field': 'reporting_diagnosis' if context.get('diagnosis') else None,
                                           'user_recorded_override': diagnosis_manual}
    return context


def _record_biomarkers(record: dict) -> dict:
    """Export effective values and raw provenance without summing HRD parts."""
    values = record.get('biomarkers') or {}
    source_fields = record.get('field_sources') or {}
    names = {'TMB': ('tmb_value', 'tmb_unit', 'tmb_status'),
             'MSI': ('msi_percent', 'msi_percentage', 'msi_status'),
             'HRD': ('hrd_score', 'hrd_loh', 'tai', 'lst', 'hrd_tai', 'hrd_lst')}
    tmb = values.get('tmb_value'); msi = values.get('msi_percent'); hrd = values.get('hrd_score')
    out = {
        'TMB': {'value_muts_per_mb': tmb, 'unit': values.get('tmb_unit'),
                'status': values.get('tmb_status'),
                'raw': (str(tmb) + (' ' + str(values['tmb_unit']) if values.get('tmb_unit') else '')) if tmb is not None else ''},
        'MSI': {'percentage': msi, 'percentage_scale': '0_to_100',
                'status': values.get('msi_status'), 'raw': str(msi) + '%' if msi is not None else ''},
        'HRD': {'score': hrd, 'raw': str(hrd) if hrd is not None else '',
                'components': {'LOH': values.get('hrd_loh'), 'TAI': values.get('tai'), 'LST': values.get('lst')}},
    }
    for kind, fields in names.items():
        out[kind]['field_sources'] = {key: source_fields[key] for key in fields if key in source_fields}
        out[kind]['record_key'] = record.get('record_key')
        out[kind]['revision'] = record.get('revision')
        main = source_fields.get({'TMB': 'tmb_value', 'MSI': 'msi_percentage', 'HRD': 'hrd_score'}[kind]) or {}
        effective = (main.get('manual_override') if (main.get('manual_override') or {}).get('present')
                     else main.get('source')) or {}
        out[kind]['raw_source_text'] = effective.get('raw')
        out[kind]['availability'] = effective.get('availability') or (
            'observed' if main.get('effective_value') is not None else 'missing')
        if out[kind]['availability'] == 'not_available':
            out[kind]['raw'] = 'N/A'
            if kind == 'HRD': out[kind]['status'] = 'not_available'
    return out


def _load_tsv_rows(path: Path) -> list[dict]:
    table = _read_table_file(path)
    return (table or {}).get('rows') or []


def _flatten_mapping(value,prefix='',out=None,depth=0):
    out={} if out is None else out
    if depth>8:return out
    if isinstance(value,dict):
        for k,v in value.items():_flatten_mapping(v,(prefix+'.'+str(k)) if prefix else str(k),out,depth+1)
    elif isinstance(value,list):
        for i,v in enumerate(value[:30]):_flatten_mapping(v,prefix+f'[{i}]',out,depth+1)
    elif value not in (None,''):out[prefix]=value
    return out


def _row_with_evidence(row: dict) -> dict:
    merged=dict(row or {})
    raw=merged.get('EvidenceJSON') or merged.get('evidence_json')
    if raw:
        try:obj=json.loads(raw) if isinstance(raw,str) else raw
        except Exception:obj=None
        if isinstance(obj,(dict,list)):
            for k,v in _flatten_mapping(obj).items():merged.setdefault(k,v)
    return merged


def _pick_ci(row: dict, *names):
    if not row:return ''
    row=_row_with_evidence(row)
    exact = {str(k).lower(): k for k in row.keys()}
    normalized = {re.sub(r'[^a-z0-9]+', '', str(k).lower()): k for k in row.keys()}
    for name in names:
        key = exact.get(str(name).lower())
        target=re.sub(r'[^a-z0-9]+','',str(name).lower())
        if key is None:key=normalized.get(target)
        if key is None:
            # Network evidence often nests caller-specific fields (e.g.
            # callers.arriba.junctionReadCount). A unique suffix match preserves it.
            matches=[k for nk,k in normalized.items() if nk.endswith(target)]
            if matches:key=matches[0]
        if key is not None and str(row.get(key) or '').strip() not in ('','[object Object]'):
            return str(row.get(key) or '').strip()
    return ''



def _fusion_caller_name(label: str) -> str:
    low=str(label or '').lower()
    if 'arriba' in low:return 'Arriba'
    if 'fusioninspector' in low or 'fusion_inspector' in low:return 'FusionInspector'
    if 'star-fusion' in low or 'star_fusion' in low or 'starfusion' in low:return 'STAR-Fusion'
    return 'platform'


def _normalise_fusion_candidate(row: dict, caller: str = 'platform') -> dict:
    from source_validation import fusion_candidate_identity
    expanded=_row_with_evidence(row)
    fusion_name,gene1,gene2=fusion_candidate_identity(expanded)
    if not (gene1 and gene2):return {}
    breakpoint1=_pick_ci(row,'Breakpoint1','breakpoint1','LeftBreakpoint','Left Breakpoint','Breakpoint A','breakpoint_1','left_breakpoint')
    breakpoint2=_pick_ci(row,'Breakpoint2','breakpoint2','RightBreakpoint','Right Breakpoint','Breakpoint B','breakpoint_2','right_breakpoint')
    combined_bp=_pick_ci(row,'Breakpoint','Breakpoints','Junction','Breakpoint(s)')
    frame=_pick_ci(row,'Frame','ReadingFrame','Reading Frame','InFrame','in-frame','Frame Type','frame_type')
    junction=_pick_ci(row,'JunctionReadCount','Junction Reads','JunctionReads','Junction Read Count','junction_read_count','split_reads','Split Reads')
    spanning=_pick_ci(row,'SpanningFragCount','Spanning Reads','SpanningFrags','Spanning Fragments','Discordant Reads','DiscordantMates','SpanningPairs','spanning_frag_count')
    ffpm=_pick_ci(row,'FFPM','FusionFragmentsPerMillion','fusion_fragments_per_million')
    confidence=_pick_ci(row,'Confidence','Arriba Confidence','confidence')
    star_support=_pick_ci(row,'STAR-Fusion Support','STARFusionSupport','star_fusion_support','est_J','JunctionReadCount','SpanningFragCount')
    inspector=_pick_ci(row,'FusionInspector Support','FusionInspector','fusioninspector_support','FI_support','LargeAnchorSupport')
    known=_pick_ci(row,'Known/Recurrent','Known Fusion','KnownFusion','Known','Recurrent','annots','Annotations','Tags')
    fii=_pick_ci(row,'FII','FusionInspectorIndex','Fusion Index','fusion_index')
    out={
      'caller':caller,'fusion_name':fusion_name,'gene1':gene1,'gene2':gene2,
      'breakpoint1':breakpoint1,'breakpoint2':breakpoint2,'breakpoint':combined_bp,
      'frame':frame,'junction_reads':junction,'spanning_or_discordant_reads':spanning,
      'ffpm':ffpm,'arriba_confidence':confidence if caller=='Arriba' or confidence else '',
      'star_fusion_support':star_support if caller=='STAR-Fusion' or star_support else '',
      'fusioninspector_support':inspector,'known_or_recurrent':known,'fii':fii,
    }
    return {k:v for k,v in out.items() if v not in ('',None)}


def _read_fusion_source(path: Path | None, include_raw: bool = False) -> dict:
    if not path:
        return {'available':False,'source_file':None,'source_verified':False,'source_kind':'missing','candidates':[],
                'candidates_scope': _list_scope(None, 0, source_complete=False)}
    ok,reason,validation=validate_fusion_source(path)
    result={'available':True,'source_file':path.name,'source_size_bytes':path.stat().st_size if path.exists() else None,
            'source_sha256':_sha256(path) if path.exists() else None,'source_verified':bool(ok),
            'source_validation_reason':reason,'source_kind':validation.get('kind'),'candidates':[],'headers':[],'row_count':validation.get('row_count'),
            'valid_candidate_count':validation.get('valid_candidate_count'),'metadata_rows_excluded':validation.get('metadata_rows_excluded',0)}
    if path.suffix.lower() in ('.tsv','.csv'):
        table=_read_table_file(path) or {'headers':[],'rows':[]};result['headers']=table['headers'];result['row_count']=len(table['rows'])
        parsed=[_normalise_fusion_candidate(r,'platform') for r in table['rows']]
        parsed=[x for x in parsed if x.get('gene1') and x.get('gene2')]
        if ok:
            result['candidates']=parsed
            result['candidate_review_status']='analytical_source_collected_not_clinical_positives'
            if include_raw:result['raw_rows']=table['rows']
        else:
            # GenNext native TSV may contain THOUSANDS of candidates but only
            # '[object Object]' in the support columns.  Do not promote these
            # raw records into a clinically reviewed candidate list or send
            # thousands of metadata-only rows into the compact AI payload.
            # Retain a bounded, clearly unverified triage sample; source bytes
            # stay available via SOURCE_FILES and path/hash metadata.
            result['candidates']=[]
            result['unverified_raw_candidate_count']=len(parsed)
            result['unverified_candidate_examples']=parsed[:20]
            result['candidate_review_status']='incomplete_source_do_not_interpret_as_positive_or_negative'
            if include_raw:
                result['raw_rows']=table['rows'][:20]
                result['raw_rows_truncated']=len(table['rows'])>20
                result['raw_total_rows']=len(table['rows'])
    elif path.suffix.lower()=='.txt':
        text=path.read_text(encoding='utf-8-sig',errors='replace')
        result['visible_text']=text[:120000] if include_raw else None
    elif path.suffix.lower()=='.json':
        obj=_read_json_file(path)
        if isinstance(obj,dict):
            result['manual_result']=obj.get('result') if obj.get('manual_review') else None
            result['collector_state']=obj.get('collector_state')
            result['export_error']=obj.get('export_error')
            if include_raw:result['raw_json']=obj
    total = len(parsed) if path.suffix.lower() in ('.tsv', '.csv') else result.get('valid_candidate_count')
    result['candidates_scope'] = _list_scope(total, len(result['candidates']), source_complete=bool(ok))
    if 'unverified_candidate_examples' in result:
        result['unverified_candidate_examples_scope'] = _list_scope(result.get('unverified_raw_candidate_count'),
                                        len(result['unverified_candidate_examples']), source_complete=False)
    if isinstance(result.get('raw_rows'), list):
        result['raw_rows_scope'] = _list_scope(result.get('row_count'), len(result['raw_rows']), source_complete=bool(ok))
    return result


def _partial_react_fusion_review(path: Path | None, expected_patient: str) -> dict | None:
    """Summarize PARTIAL React support without rebranding it as a validated call.

    GenNext's native TSV can lose nested caller objects. For incomplete
    React recoveries, keep actual split/junction and breakpoint fields in an
    explicitly REVIEW-ONLY digest; never replace source verification status.
    """
    if not path or not path.is_file() or path.suffix.lower()!='.json':return None
    if path.stat().st_size>10000000:return None
    try:data=json.loads(path.read_text(encoding='utf-8-sig'))
    except (ValueError,OSError):return None
    if not isinstance(data,dict) or data.get('schema')!='ngs_fusion_react_recovery_v1':return None
    if str(data.get('patient_id') or '').upper()!=str(expected_patient or '').upper():return None
    if data.get('complete') is True:return None
    from source_validation import fusion_candidate_identity
    from fusion_react_recovery import _row_has_caller_evidence
    original=data.get('rows') or []
    supported=[]
    for r in original[:2200]:
        if not isinstance(r,dict) or not bool(r.get('found')):continue
        fusion,g1,g2=fusion_candidate_identity({'FusionName':r.get('fusion')})
        if not g1 or not g2 or not _row_has_caller_evidence(r):continue
        a=r.get('arriba') if isinstance(r.get('arriba'),dict) else {}
        st=r.get('starfusion') if isinstance(r.get('starfusion'),dict) else {}
        def asint(value):
            try:return max(0,int(float(str(value))))
            except (ValueError,TypeError):return 0
        j=asint(st.get('junction_reads'))
        split1=asint(a.get('split_reads1'));split2=asint(a.get('split_reads2'))
        supported.append({
            'fusion_name':fusion,'gene1':g1,'gene2':g2,
            'junction_reads':j if st.get('junction_reads') is not None else None,
            'arriba_split_reads1':split1 if a.get('split_reads1') is not None else None,
            'arriba_split_reads2':split2 if a.get('split_reads2') is not None else None,
            'breakpoint':str(st.get('position') or a.get('position') or '')[:130],
            'arriba_confidence':str(a.get('confidence') or '')[:30],
            'review_status':'partial_source_caller_evidence_not_clinically_validated',
        })
    # Important: the strongest 20 raw caller rows are only TRIAGE hints.
    ranked=sorted(supported,key=lambda x:max(x.get('junction_reads') or 0,
        x.get('arriba_split_reads1') or 0,x.get('arriba_split_reads2') or 0),reverse=True)
    return {
        'source_file':path.name,'source_sha256':_sha256(path),
        'filtered_expected':data.get('filtered_expected'),
        'captured':data.get('captured'),
        'identified_read_supported_rows':len(supported),
        'error_count':len(data.get('errors') or []),
        'error_examples':list(data.get('errors') or [])[:8],
        'top_read_supported_review_only':ranked[:20],
        'top_read_supported_review_only_scope': _list_scope(len(ranked), min(len(ranked), 20), source_complete=False),
        'source_rows_examined': min(len(original), 2200), 'source_rows_available': len(original),
        'clinical_interpretation_prohibited_until_complete':True,
    }


def _fusion_caller_evidence_from_qc(qc: dict | None, include_raw: bool = False) -> dict:
    """Preserve MultiQC caller SUMMARY stages without inventing gene pairs.

    fusion-report-section-plot.tsv and fusion-inspector-section-plot.tsv
    contain one aggregate row per sample, not a per-fusion candidate table.
    Their numeric counts are still important: do not silently discard them.
    """
    out={'summary_metrics':{},'caller_tables':[],'structured_candidates':[],
         'has_detailed_candidate_table':False,'zero_summary_is_not_clinical_negative':True,
         'summary_stage_reconciliation_required':False,'candidate_identity_missing':True,
         'analytical_fusion_candidate_count':None}
    if not qc:return out
    metrics=((qc.get('general_stats') or {}).get('metrics') or {})
    for key,val in metrics.items():
        low=str(key).lower()
        if any(token in low for token in ('arriba','star-fusion','star_fusion','starfusion','fusioninspector','fusion_inspector','fusion')):
            out['summary_metrics'][key]=val
    tables=qc.get('fusion_evidence_tables') or qc.get('raw_multiqc_tables') or {}
    numeric=[]
    def add_numeric(value):
        if isinstance(value,dict):
            for item in value.values():add_numeric(item)
        elif isinstance(value,(int,float)):
            numeric.append(float(value))
        elif isinstance(value,str):
            try:numeric.append(float(value.replace(',','.')))
            except (ValueError,OverflowError):pass
    add_numeric(out['summary_metrics'])
    summary_stage_values={}
    for name,table in tables.items():
        low=str(name).lower()
        if not any(token in low for token in ('arriba','star-fusion','star_fusion','starfusion','fusioninspector','fusion_inspector','fusion_predictions','fusion-report-section-plot','fusion-inspector-section-plot')):
            continue
        caller=_fusion_caller_name(name)
        rows=(table or {}).get('rows') or []
        headers=(table or {}).get('headers') or []
        is_summary= ('fusion-report-section-plot' in low or 'fusion-inspector-section-plot' in low or
                     (rows and all(not _normalise_fusion_candidate(row,caller) for row in rows)))
        candidates=[] if is_summary else [c for row in rows if (c:=_normalise_fusion_candidate(row,caller))]
        entry={'name':name,'caller':caller,'rows':len(rows),'headers':headers,
               'candidate_rows':len(candidates),'stage':'summary_aggregate' if is_summary else 'per_candidate'}
        if is_summary:
            # Preserve the original column names and numeric values exactly.
            entry['summary_rows']=rows[:20]
            out['summary_metrics'][name]=rows[:20]
            for row in rows:
                for key,value in row.items():
                    if key not in ('Sample','sample'):
                        summary_stage_values[str(key)]=value
                        add_numeric(value)
        if include_raw:entry['raw_rows']=rows
        out['caller_tables'].append(entry)
        out['structured_candidates'].extend(candidates)
    out['has_detailed_candidate_table']=bool(out['structured_candidates'])
    out['candidate_identity_missing']=not out['has_detailed_candidate_table']
    out['summary_all_numeric_zero']=bool(numeric) and all(abs(v)<1e-12 for v in numeric)
    def as_num(key):
        try:return float(str(summary_stage_values[key]).replace(',','.'))
        except (KeyError,ValueError,TypeError):return None
    raw_total=as_num('Total_unique_fusions')
    arriba=as_num('Arriba_total');star=as_num('STARFusion_total')
    inspector=as_num('Realigned_fusions')
    out['summary_stage_reconciliation_required']=bool(
        (raw_total is not None and raw_total>0 and arriba==0 and star==0) or
        (inspector is not None and inspector>0 and arriba==0 and star==0))
    # Caller summary conflict is a DIFFERENT concept: a verified per-candidate
    # platform fusion with evidence vs an explicitly all-zero caller stage.
    out['caller_summary_conflict']=False
    return out


def _norm_chrom(v):
    s = str(v or '').strip()
    if not s:
        return ''
    return 'chr' + (s[3:] if s.lower().startswith('chr') else s).upper()


def _snv_key_from_row(row: dict) -> str:
    return '|'.join([
        _norm_chrom(_pick_ci(row, 'Chrom', 'Chr', 'Chromosome')),
        _pick_ci(row, 'Start', 'Pos', 'Position'),
        _pick_ci(row, 'Ref', 'Reference'),
        _pick_ci(row, 'Alt', 'Alternative'),
    ])


def _snv_annotation_rows(path: Path | None) -> tuple[dict[str, dict], dict, list[dict]]:
    if not path or path.suffix.lower() not in ('.tsv', '.csv'):
        return {}, {'source_file': path.name if path else None, 'row_count': 0, 'headers': []}, []
    table = _read_table_file(path) or {'headers': [], 'rows': []}
    idx = {}
    for row in table['rows']:
        key = _snv_key_from_row(row)
        if key.strip('|'):
            idx[key] = row
    return idx, {'source_file': path.name, 'row_count': len(table['rows']), 'headers': table['headers']}, table['rows']


def _selected_snv_annotations(row: dict) -> dict:
    if not row:
        return {}
    selected = {
        'transcript': _pick_ci(row, 'Transcript', 'Tx', 'tx', 'RefSeq', 'RefSeq Transcript', 'NM'),
        'cDNA': _pick_ci(row, 'cDNA', 'cDNA Change', 'Nucleotide Change', 'TxChange', 'txChange', 'HGVSc'),
        'protein': _pick_ci(row, 'Protein', 'Protein Change', 'AAChange', 'aaChange', 'HGVSp'),
        'variant_type': _pick_ci(row, 'Variant Type', 'VariantType', 'Variant_Type', 'variantType', 'ExonicFunc.refGene', 'ExonicFunc_refGene'),
        'function': _pick_ci(row, 'consequence', 'Func.refGene', 'Func_refGene', 'Function'),
        'cancer_var_tier': _pick_ci(row, 'CancerVar_Tier', 'CancerVar Tier', 'CancerVar'),
        'clinvar': _pick_ci(row, 'ClinVar', 'CLNSIG', 'ClinVar Clinical Significance'),
        'cosmic': _pick_ci(row, 'COSMIC', 'cosmic91', 'COSMIC ID'),
        'gnomad_af': _pick_ci(row, 'gnomAD_AF', 'gnomAD exome AF', 'gnomAD genome AF', 'gnomAD'),
        'onco_kb_oncogenic': _pick_ci(row, 'Oncogenic', 'Oncogenicity', 'OncoKB Oncogenicity'),
        'highest_level': _pick_ci(row, 'Highest Level', 'HighestLevel', 'highestLevel'),
    }
    return {k: v for k, v in selected.items() if v not in ('', None)}


def _safe_selection_variant(value: dict, snv_index=None) -> dict:
    keys = ('variant_key', 'gene', 'protein', 'cdna', 'exon', 'genomic', 'vaf', 'ad_dp',
            'filter', 'tier', 'consequence', 'group', 'origin', 'fusion', 'gene_5prime',
            'gene_3prime', 'callers', 'breakpoint1', 'breakpoint2')
    out = {key: value.get(key) for key in keys if key in value}
    annotations = _selected_snv_annotations((snv_index or {}).get(value.get('variant_key')) or {})
    # Gene name, PASS, VAF and IGV support never change this source classification.
    original = next((value[k] for k in ('oncogenicity', 'oncogenic', 'classification')
                     if value.get(k) not in (None, '')), annotations.get('onco_kb_oncogenic'))
    out['oncogenicity'] = original
    out['oncogenicity_display'] = ('Unknown / onkojenisitesi bilinmeyen'
                                   if str(original).casefold() == 'unknown' else original)
    out['classification_source'] = ('selection_source' if any(value.get(k) not in (None, '')
                                       for k in ('oncogenicity', 'oncogenic', 'classification'))
                                    else 'exact_variant_snv_annotation' if original is not None else None)
    out['annotations'] = annotations
    out['clinical_report_release'] = False
    return out


def _selection_export(raw: dict | None, sample: dict, scope: str, snv_index=None) -> dict:
    """Expose each assay's verified Selection independently from research calls."""
    from gennext_selection_store import run_phase
    raw = raw or {}
    state = raw.get('result_status') or 'not_scanned'
    selected = raw.get('selected') if isinstance(raw.get('selected'), list) else []
    candidates = raw.get('candidates') if isinstance(raw.get('candidates'), list) else []
    observed_count = raw.get('included_count')
    candidate_count = raw.get('candidate_count')
    sample_type = str(sample.get('sample_type') or '').upper()
    assay_applies = sample_type in (('RNA', 'DNA+RNA', 'RNA+DNA') if scope == 'RNA_FUSION_ONLY'
                                   else ('DNA', 'DNA+RNA', 'RNA+DNA'))
    observed_analyzed = raw.get('latest_analyzed') if 'latest_analyzed' in raw else raw.get('analyzed')
    analyzed = (bool(observed_analyzed) if isinstance(observed_analyzed, (bool, int))
                and observed_analyzed in (0, 1) else None)
    payload_ok = (assay_applies and raw.get('clinical_scope') == scope and
                  (not raw.get('record_key') or raw.get('record_key') == sample.get('record_key')) and
                  type(observed_count) is int and observed_count == len(selected) and
                  type(candidate_count) is int and candidate_count == len(candidates))
    verified = bool(raw.get('currently_verified')) and payload_ok and run_phase(sample) == 'ready'
    positive = 'fusion_selected_positive' if scope == 'RNA_FUSION_ONLY' else 'selected_positive'
    empty = 'no_fusion_selection' if scope == 'RNA_FUSION_ONLY' else 'no_snv_selection'
    final_selection = (verified and analyzed is True and state in (positive, empty) and
                       ((state == positive and observed_count > 0) or (state == empty and observed_count == 0)))
    safe_selected = [_safe_selection_variant(v, snv_index) for v in selected if isinstance(v, dict)]
    safe_candidates = [_safe_selection_variant(v, snv_index) for v in candidates if isinstance(v, dict)]
    if len(safe_selected) != len(selected) or len(safe_candidates) != len(candidates):
        final_selection = verified = False
    not_final = safe_candidates + ([] if final_selection else safe_selected)
    source = ('GenNext Run Detail > Analyzed + Fusion Genes > Fusions > Selection'
              if scope == 'RNA_FUSION_ONLY' else 'GenNext Run Detail > Analyzed + Clinical SNVs > Selection')
    return {
        'source': source, 'scope': scope, 'record_key': sample.get('record_key'),
        'analyzed': analyzed, 'currently_verified': verified, 'report_selection_verified': final_selection,
        'result_status': state, 'selected_count': observed_count if final_selection else None,
        'candidate_count': candidate_count if verified else None,
        'checked_at': raw.get('checked_at') or None, 'selection_hash': raw.get('selection_hash') or None,
        'selected_variants': safe_selected if final_selection else [],
        'selected_variants_scope': _list_scope(observed_count if final_selection else None,
                                               len(safe_selected) if final_selection else 0,
                                               source_complete=final_selection),
        'not_final_variants': not_final[:100],
        'not_final_variants_scope': _list_scope(len(not_final), min(len(not_final), 100),
                                               source_complete=verified),
        'historical_snapshot': ({'selected_count': observed_count, 'candidate_count': candidate_count,
                                 'analyzed': raw.get('analyzed'), 'checked_at': raw.get('checked_at'),
                                 'current_conclusion': False} if raw.get('checked_at') and not final_selection else None),
        'last_error': str(raw.get('last_error') or '')[:180],
        'clinical_report_release': False,
        'warning': 'Research/PASS/Oncogenic/IGV support is not report selection. '
                   'Unknown classifications remain unchanged, including TP53. '
                   'Zero selection covers only this assay; an unread/stale selection is null. '
                   'A verified Selection is not a signed Enlil report.',
    }


def _cnv_selection_unverified(sample: dict) -> dict:
    # v0.4.81 has no independently validated live CNV Selection reader. Neither
    # reportSelect columns nor a DNA SNV snapshot can provide that confirmation.
    return {'scope': 'CNV_ONLY', 'record_key': sample.get('record_key'),
            'source': None, 'currently_verified': False, 'report_selection_verified': False,
            'result_status': 'cnv_selection_not_verified', 'analyzed': None,
            'selected_count': None, 'candidate_count': None, 'selected_variants': [],
            'clinical_report_release': False,
            'warning': 'Raw CNV calls are research candidates. Independent CNV Selection was not verified.'}



def _fraction_af(v):
    n = _num(v)
    if n is None:
        return None
    # Platform SNV TSV exports AF as a 0..1 fraction. Protect against exports that
    # use percentages without silently changing values already in fraction form.
    if 1 < n <= 100:
        return n / 100.0
    return n


def _all_snv_observation(row: dict, sample: dict) -> dict | None:
    key = _snv_key_from_row(row)
    if not key.strip('|'):
        return None
    af = _fraction_af(_pick_ci(row, 'AF', 'VAF', 'Allele Fraction', 'Variant Allele Frequency'))
    ad = _num(_pick_ci(row, 'AD', 'Alt Depth', 'Alternative Depth'))
    dp = _num(_pick_ci(row, 'DP', 'Depth', 'Total Depth'))
    gene = _pick_ci(row, 'Gene', 'Gene_refGene', 'Gene.refGene', 'Gene Symbol', 'Symbol')
    protein = _pick_ci(row, 'aaChange', 'AAChange', 'Protein', 'Protein Change', 'HGVSp')
    cdna = _pick_ci(row, 'txChange', 'TxChange', 'cDNA', 'cDNA Change', 'HGVSc')
    return {
        'record_key': sample.get('record_key'),
        'patient_id': sample.get('patient_id'),
        'case_family': sample.get('case_family') or sample.get('patient_id'),
        'sample_type': sample.get('sample_type'),
        'is_rerun': bool(sample.get('is_rerun')),
        'variant_key': key,
        'gene': gene,
        'change': protein or cdna,
        'transcript': _pick_ci(row, 'tx', 'Tx', 'Transcript'),
        'consequence': _pick_ci(row, 'consequence', 'Function', 'Func.refGene'),
        'variant_type': _pick_ci(row, 'Variant_Type', 'VariantType', 'variantType', 'Variant Type'),
        'oncogenic': _pick_ci(row, 'Oncogenic'),
        'cancer_var_tier': _pick_ci(row, 'CancerVar_Tier', 'CancerVar Tier', 'CancerVar'),
        'clinvar': _pick_ci(row, 'Clinvar', 'ClinVar', 'CLNSIG'),
        'cosmic': _pick_ci(row, 'cosmic91', 'COSMIC', 'COSMIC ID'),
        'af': af,
        'ad': ad,
        'dp': dp,
        'filter': _pick_ci(row, 'Filter'),
    }


def _build_all_snv_fingerprint(observations: list[dict], cfg: dict) -> dict:
    """POOL-wide technical fingerprint from *all* SNV/indel calls.

    This intentionally includes benign/unknown/non-PASS calls. It is not a read-level
    BAM comparison; its purpose is to give the AI/laboratory reviewer a denser signal
    for cross-sample carry-over, recurrent artifacts and rerun concordance.
    """
    source_min = float(cfg.get('spread_source_min_af', 0.05))
    recipient_max = float(cfg.get('spread_recipient_max_af', 0.05))
    ratio_min = float(cfg.get('spread_ratio_min', 4.0))
    signal_limit = max(50, int(cfg.get('ai_fingerprint_signal_limit', 1500)))
    recurrent_limit = max(25, int(cfg.get('ai_recurrent_variant_limit', 250)))
    per_pair_limit = max(10, int(cfg.get('ai_pair_variant_limit', 80)))

    by_variant = defaultdict(list)
    sample_totals = defaultdict(int)
    sample_labels = {}
    sample_families = {}
    for o in observations:
        rk = o.get('record_key')
        if not rk:
            continue
        sample_totals[rk] += 1
        sample_labels[rk] = o.get('patient_id') or o.get('case_family') or rk
        sample_families[rk] = o.get('case_family') or o.get('patient_id') or ''
        by_variant[o.get('variant_key')].append(o)

    pair_stats = {}
    directional = []
    recurrent = []

    def pstate(a, b):
        key = tuple(sorted((a, b)))
        if key not in pair_stats:
            pair_stats[key] = {
                'record_key_a': key[0], 'record_key_b': key[1],
                'patient_a': sample_labels.get(key[0], key[0]), 'patient_b': sample_labels.get(key[1], key[1]),
                'same_case_family': bool(sample_families.get(key[0]) and sample_families.get(key[0]) == sample_families.get(key[1])),
                'all_snv_count_a': sample_totals.get(key[0], 0), 'all_snv_count_b': sample_totals.get(key[1], 0),
                'shared_exact_variant_count': 0, 'shared_pass_variant_count': 0,
                'shared_nonpass_variant_count': 0, 'shared_cancervar_tier_iv_count': 0,
                'high_to_low_directional_count': 0, 'high_to_low_nonpass_recipient_count': 0,
                '_signals': [],
            }
        return pair_stats[key]

    for variant_index, (vk, raw_rows) in enumerate(by_variant.items(), 1):
        if variant_index == 1 or variant_index % 250 == 0 or variant_index == len(by_variant):
            report_progress('pool_comparison', variant_index, len(by_variant), 'SNV/indel örnekler arası karşılaştırması yapılıyor.')
        uniq = {}
        for r in raw_rows:
            uniq[r.get('record_key')] = r
        rows = list(uniq.values())
        if len(rows) >= 3:
            afs = [r.get('af') for r in rows if r.get('af') is not None]
            recurrent.append({
                'variant_key': vk, 'gene': rows[0].get('gene'), 'change': rows[0].get('change'),
                'sample_count': len(rows), 'pass_count': sum(1 for r in rows if str(r.get('filter') or '').upper() == 'PASS'),
                'nonpass_count': sum(1 for r in rows if str(r.get('filter') or '').upper() != 'PASS'),
                'min_af_pct': _af_pct(min(afs)) if afs else None, 'max_af_pct': _af_pct(max(afs)) if afs else None,
                'cancer_var_tiers': sorted({r.get('cancer_var_tier') for r in rows if r.get('cancer_var_tier')}),
                'observations': [{
                    'patient_id': r.get('patient_id'), 'record_key': r.get('record_key'), 'af_pct': _af_pct(r.get('af')),
                    'ad': r.get('ad'), 'dp': r.get('dp'), 'filter': r.get('filter')
                } for r in rows],
            })
        if len(rows) < 2:
            continue
        for i in range(len(rows)):
            for j in range(i + 1, len(rows)):
                a, b = rows[i], rows[j]
                sa, sb = pstate(a.get('record_key'), b.get('record_key')), None
                sa['shared_exact_variant_count'] += 1
                fa, fb = str(a.get('filter') or '').upper(), str(b.get('filter') or '').upper()
                if fa == 'PASS' and fb == 'PASS':
                    sa['shared_pass_variant_count'] += 1
                else:
                    sa['shared_nonpass_variant_count'] += 1
                tiers = (str(a.get('cancer_var_tier') or '').lower(), str(b.get('cancer_var_tier') or '').lower())
                if any('tier iv' in x and 'benign' in x for x in tiers):
                    sa['shared_cancervar_tier_iv_count'] += 1

                for src, dst in ((a, b), (b, a)):
                    src_af, dst_af = src.get('af'), dst.get('af')
                    rat = _ratio(src_af, dst_af)
                    if src_af is None or dst_af is None or rat is None:
                        continue
                    if src_af >= source_min and dst_af <= recipient_max and rat >= ratio_min:
                        sig = {
                            'variant_key': vk, 'gene': src.get('gene') or dst.get('gene'), 'change': src.get('change') or dst.get('change'),
                            'consequence': src.get('consequence') or dst.get('consequence'), 'cancer_var_tier': src.get('cancer_var_tier') or dst.get('cancer_var_tier'),
                            'oncogenic': src.get('oncogenic') or dst.get('oncogenic'),
                            'source_record_key': src.get('record_key'), 'source_patient_id': src.get('patient_id'),
                            'source_af_pct': _af_pct(src_af), 'source_ad': src.get('ad'), 'source_dp': src.get('dp'), 'source_filter': src.get('filter'),
                            'recipient_record_key': dst.get('record_key'), 'recipient_patient_id': dst.get('patient_id'),
                            'recipient_af_pct': _af_pct(dst_af), 'recipient_ad': dst.get('ad'), 'recipient_dp': dst.get('dp'), 'recipient_filter': dst.get('filter'),
                            'ratio': round(rat, 3),
                        }
                        directional.append(sig)
                        sa['high_to_low_directional_count'] += 1
                        if str(dst.get('filter') or '').upper() != 'PASS':
                            sa['high_to_low_nonpass_recipient_count'] += 1
                        if len(sa['_signals']) < per_pair_limit:
                            sa['_signals'].append(sig)

    pair_summaries = []
    for st in pair_stats.values():
        denom = min(st.get('all_snv_count_a') or 0, st.get('all_snv_count_b') or 0)
        st['shared_exact_density_vs_smaller_sample'] = round(st['shared_exact_variant_count'] / denom, 6) if denom else None
        st['top_high_to_low_signals'] = sorted(st.pop('_signals'), key=lambda x: x.get('ratio') or 0, reverse=True)
        st['top_high_to_low_signals_scope'] = _list_scope(st['high_to_low_directional_count'],
                                                        len(st['top_high_to_low_signals']), source_complete=True)
        pair_summaries.append(st)
    pair_summaries.sort(key=lambda x: (x['high_to_low_directional_count'], x['shared_nonpass_variant_count'], x['shared_exact_variant_count']), reverse=True)
    directional.sort(key=lambda x: (x.get('ratio') or 0, x.get('source_af_pct') or 0), reverse=True)
    recurrent.sort(key=lambda x: (x.get('sample_count') or 0, x.get('nonpass_count') or 0), reverse=True)
    record_totals = {rk: {'outgoing_count': 0, 'incoming_count': 0, 'recurrent_count': 0} for rk in sample_totals}
    for signal in directional:
        record_totals[signal['source_record_key']]['outgoing_count'] += 1
        record_totals[signal['recipient_record_key']]['incoming_count'] += 1
    for event in recurrent:
        for observation in event['observations']:
            record_totals[observation['record_key']]['recurrent_count'] += 1

    return {
        'method': 'Exact chr/start/ref/alt comparison across every SNV/indel TSV row in the selected POOL scope.',
        'read_level_bam_comparison': False,
        'interpretation_note': 'Benign/unknown/non-PASS variants are intentionally included as a technical fingerprint. High shared counts or high→low patterns are review signals, not proof of contamination; common polymorphisms and recurrent platform artifacts can also recur.',
        'sample_all_call_counts': [{'record_key': rk, 'patient_id': sample_labels.get(rk, rk), 'row_count': n} for rk, n in sorted(sample_totals.items(), key=lambda kv: sample_labels.get(kv[0], kv[0]))],
        'pair_summaries': pair_summaries,
        'per_record_counts_before_truncation': record_totals,
        'high_to_low_signal_total': len(directional),
        'high_to_low_signals': directional[:signal_limit],
        'high_to_low_signals_truncated': len(directional) > signal_limit,
        'high_to_low_signals_scope': _list_scope(len(directional), min(len(directional), signal_limit), source_complete=True),
        'recurrent_pool_variant_total': len(recurrent),
        'recurrent_pool_variants': recurrent[:recurrent_limit],
        'recurrent_pool_variants_truncated': len(recurrent) > recurrent_limit,
        'recurrent_pool_variants_scope': _list_scope(len(recurrent), min(len(recurrent), recurrent_limit), source_complete=True),
        'limits': {'high_to_low_signals': signal_limit, 'recurrent_pool_variants': recurrent_limit, 'per_pair_signals': per_pair_limit},
    }

def _cnv_value(row: dict, *names):
    # Never suffix-match id to parentId, or Start to an unrelated nested field.
    keys = {_context_key(k): k for k in row}
    for name in names:
        key = keys.get(_context_key(name))
        if key is not None and row[key] is not None and str(row[key]).strip() not in ('', '.'):
            return row[key]
    return None


def _cnv_direction(value):
    text = str(value or '').strip().upper()
    if text in ('DUP', 'GAIN', 'AMPLIFICATION', 'AMP', 'DUPLICATION', 'CNV GAIN'):
        return 'GAIN'
    if text in ('DEL', 'LOSS', 'DELETION', 'CNV LOSS'):
        return 'LOSS'
    return None  # Fold change alone is not a validated calling threshold.


def _cnv_overlap(value) -> dict:
    raw = str(value).strip() if value is not None else None
    percentage = None
    bases = None
    if raw:
        pct = re.search(r'(\d+(?:[.,]\d+)?)\s*%', raw)
        if pct:
            number = float(pct.group(1).replace(',', '.'))
            if 0 <= number <= 100:
                percentage = number
        size = re.match(r'^\s*(\d+)\s*(?:\(|$)', raw)
        if size:
            bases = int(size.group(1))
    return {'raw': raw, 'bases': bases, 'percentage': percentage,
            'scope': ('partial_cds' if percentage is not None and percentage < 100
                      else 'complete_cds' if percentage == 100 else 'unknown'),
            'whole_gene_event_established': False}


def _normalize_cnv_rows(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """Keep segment and split-gene rows separate; never sum segment probes."""
    ids = defaultdict(list)
    for row in rows:
        ident = _cnv_value(row, 'id', 'rowId', 'source_row_id')
        if ident is not None:
            ids[str(ident)].append(row)
    calls = []
    segments = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        ident = _cnv_value(row, 'id', 'rowId', 'source_row_id')
        parent_id = _cnv_value(row, 'parentId', 'parent_id', 'parent_segment_id')
        # Empty/missing parent is distinct from a real zero-valued identifier.
        parent = (ids[str(parent_id)][0] if parent_id is not None and
                  len(ids.get(str(parent_id), [])) == 1 else None)
        mode = _cnv_value(row, 'AnnotationMode', 'annotation_mode')
        is_segment = str(mode or '').lower() == 'full'
        segment_id = ident if is_segment else parent_id
        root = row if is_segment else parent
        gene = _cnv_value(row, 'Gene', 'Gene Symbol', 'Symbol', 'Genes', 'Gene Name', 'gene_name')
        gene_list = ([str(x).strip() for x in gene if str(x).strip()] if isinstance(gene, list)
                     else [x.strip() for x in re.split(r'[,;|]', str(gene or '')) if x.strip()])
        direction_raw = _cnv_value(row, 'SVType', 'SV Type', 'Event Type', 'eventType', 'Type')
        overlap = _cnv_overlap(_cnv_value(row, 'OverlappedCDS', 'Overlapped CDS', 'overlapped_cds'))
        probes = _cnv_value(row, 'Probes', 'Probe Count', 'Probe Sayısı')
        segment_probes = _cnv_value(root, 'Probes', 'Probe Count', 'Probe Sayısı') if root else None
        coordinates = {key: _cnv_value(row, *aliases) for key, aliases in
                       {'chromosome': ('Chromosome', 'Chrom', 'Chr'),
                        'start': ('Start',), 'end': ('End',)}.items()}
        call = {
            'id': ident, 'parent_id': parent_id, 'parentId': parent_id,
            'segment_id': segment_id, 'annotation_mode': mode, 'AnnotationMode': mode,
            'row_kind': 'segment' if is_segment else ('gene_annotation' if str(mode or '').lower() == 'split' else 'unknown'),
            'gene': gene, 'genes': gene_list, **coordinates,
            'event_key': 'CNV|' + '|'.join(str(v) if v is not None else '' for v in
                              (segment_id, ident if ident is not None else 'row:' + str(index),
                               gene, coordinates['chromosome'], coordinates['start'], coordinates['end'])),
            'cytoband': _cnv_value(row, 'CytoBand', 'Cytoband'),
            'sv_type': direction_raw, 'direction': _cnv_direction(direction_raw),
            'event_type': _cnv_value(row, 'Event Type', 'eventType'),
            'fold_change': _cnv_value(row, 'Fold Change', 'FoldChange'),
            'probes': probes,
            'probe_scope': {'scope': 'parent_segment' if parent else ('segment' if is_segment else 'unverified'),
                            'segment_id': segment_id, 'segment_probe_count': segment_probes,
                            'source_gene_row_probes': probes,
                            'parent_link_verified': bool(parent),
                            'do_not_sum_across_genes': True},
            'parent_link_verified': bool(parent),
            'parent_segment': ({'id': _cnv_value(root, 'id'),
                                'chromosome': _cnv_value(root, 'Chromosome', 'Chrom', 'Chr'),
                                'start': _cnv_value(root, 'Start'), 'end': _cnv_value(root, 'End'),
                                'sv_type': _cnv_value(root, 'SVType', 'SV Type', 'Type'),
                                'fold_change': _cnv_value(root, 'FoldChange', 'Fold Change'),
                                'probes': segment_probes} if parent else None),
            'overlapped_cds': overlap['raw'], 'OverlappedCDS': overlap['raw'], 'cds_overlap': overlap,
            'transcript': _cnv_value(row, 'Transcript', 'Transcripts', 'RefSeq', 'Transcript ID'),
            'sigma_level': _cnv_value(row, 'Sigma Level', 'SigmaLevel'),
            'oncogenic': _cnv_value(row, 'Oncogenic', 'Oncogenicity'),
            'highest_level': _cnv_value(row, 'Highest Level', 'HighestLevel'),
            'highest_sensitive_level': _cnv_value(row, 'Highest Sensitive Level', 'highestSensitiveLevel'),
            'highest_resistance_level': _cnv_value(row, 'Highest Resistance Level', 'highestResistanceLevel'),
            'source_report_select': _cnv_value(row, 'reportSelect', 'report_selected', 'Selected'),
            'usage': 'research_candidate_only', 'report_selected': None,
            'report_selection_status': 'independent_cnv_selection_not_verified',
        }
        calls.append(call)
        if is_segment:
            segments.append({key: call[key] for key in
                            ('id', 'event_key', 'chromosome', 'start', 'end', 'direction',
                             'fold_change', 'probes', 'annotation_mode')})
    return calls, segments


def _read_cnv(path: Path | None, include_raw: bool) -> dict:
    result = {
        'source_file': path.name if path else None,
        'source_size_bytes': path.stat().st_size if path and path.exists() else None,
        'source_sha256': _sha256(path) if path and path.exists() else None,
        'status': 'missing' if not path else 'unparsed',
        'row_count': None, 'calls': [], 'segments': [], 'segment_count': None,
        'raw_rows': None, 'read_errors': [], 'usage': 'research_candidate_only',
        **_list_scope(None, 0),
    }
    if not path:
        return result
    try:
        if path.suffix.lower() == '.json':
            data = _read_json_file(path)
            if isinstance(data, dict) and str(data.get('status') or '').lower() == 'no_data':
                result['status'] = 'no_data'
                result['raw_rows'] = data if include_raw else None
                return result
            rows = (data.get('raw_rows') if isinstance(data, dict) else data)
            if not isinstance(rows, list) and isinstance(data, dict):
                rows = data.get('rows')
            if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
                result['status'] = 'json_unparsed'
                result['raw_rows'] = data if include_raw else None
                result['read_errors'].append('CNV JSON içinde doğrulanabilir satır listesi bulunamadı.')
                return result
        else:
            table = _read_table_file(path)
            if not table:
                result['read_errors'].append('CNV dosyası tablo olarak okunamadı.')
                return result
            rows = table['rows']
        calls, segments = _normalize_cnv_rows(rows)
        result.update({'status': 'parsed', 'row_count': len(rows), 'calls': calls,
                       'segments': segments, 'segment_count': len(segments),
                       'raw_rows': rows if include_raw else None,
                       'count_note': 'Rows include segment and gene annotations; row count is not independent CNV event count.',
                       **_list_scope(len(rows), len(calls), source_complete=None)})
    except Exception as exc:
        result['read_errors'].append(str(exc))
    return result


def _source_inventory(base: Path, db, sample: dict, scope: str) -> tuple[list[dict], list[tuple[Path, str]]]:
    entries = []
    copies = []
    pid = sample.get('patient_id') or sample.get('case_family') or 'sample'
    st = sample.get('sample_type') or 'NA'
    for tp in SOURCE_TYPES:
        p, row = _latest_file_path(base, db, sample['record_key'], tp)
        if not p:
            continue
        arc = f"SOURCE_FILES/{_safe_name(st)}/{_safe_name(pid)}/{tp}__{_safe_name(p.name)}"
        entries.append({
            'file_type': tp,
            'file_name': p.name,
            'size_bytes': p.stat().st_size,
            'sha256': (row or {}).get('sha256') or _sha256(p),
            'bundle_path': arc,
        })
        copies.append((p, arc))
    return entries, copies


def _expected_components(sample_type: str, scope: str) -> list[str]:
    st = str(sample_type or '').upper()
    if st == 'RNA':
        return ['fusion', 'qc']
    if st in ('DNA+RNA', 'RNA+DNA'):
        return ['biomarkers', 'clinical', 'snv', 'cnv', 'fusion', 'qc']
    return ['biomarkers', 'clinical', 'snv', 'cnv', 'qc']


def _task_state(db, rk: str, component: str):
    if component == 'biomarkers':
        return None
    try:
        return db.task_status(rk, component)
    except Exception:
        return None


def _component_status(db, sample: dict, component: str, has_source: bool, parsed_ok: bool | None = None) -> dict:
    task = _task_state(db, sample['record_key'], component)
    run_status = str(sample.get('run_status') or '').upper()
    sample_status = str(sample.get('sample_status') or '').upper()
    upstream_done = (not run_status or run_status == 'COMPLETED') and (not sample_status or sample_status == 'COMPLETED')
    if component == 'biomarkers':
        present = (all(str(sample.get(k) or '').strip() not in ('', '—', '-') for k in ('tmb', 'msi'))
                   and bool(normalize_hrd(sample.get('hrd'), allow_bare=True)))
        return {'component': component, 'status': 'ok' if present else ('waiting' if not upstream_done else 'missing'), 'task_status': task}
    if task == 'not_available':
        return {'component': component, 'status': 'not_available', 'task_status': task}
    if task == 'review':
        return {'component': component, 'status': 'review', 'task_status': task}
    if not upstream_done or task in ('waiting', 'pending', 'blocked'):
        return {'component': component, 'status': 'waiting' if task != 'blocked' else 'blocked', 'task_status': task}
    if not has_source:
        return {'component': component, 'status': 'missing', 'task_status': task}
    if parsed_ok is False:
        return {'component': component, 'status': 'unparsed', 'task_status': task}
    return {'component': component, 'status': 'ok', 'task_status': task}


def _parse_biomarker_value(raw: str, kind: str) -> dict:
    s = '' if raw is None else str(raw).strip()
    out = {'raw': s}
    if not s:
        return out
    pieces = [x.strip() for x in re.split(r'\s*\|\s*', s) if x.strip()]
    if pieces:
        out['value_raw'] = pieces[0]
    if len(pieces) > 1:
        out['status'] = pieces[1]
    if kind == 'TMB':
        m = re.search(r'([0-9]+(?:[.,][0-9]+)?)\s*(?:Muts?|mut)\s*/\s*MB', s, re.I)
        if m: out['value_muts_per_mb'] = float(m.group(1).replace(',', '.'))
        sm = re.search(r'\b(TMB-[LH])\b', s, re.I)
        if sm: out['status'] = sm.group(1).upper()
    elif kind == 'MSI':
        m = re.search(r'([0-9]+(?:[.,][0-9]+)?)\s*%', s)
        if m: out['percentage'] = float(m.group(1).replace(',', '.'))
        sm = re.search(r'\b(MSS|MSI-H|MSI-L|MSI\s*HIGH|MSI\s*LOW|MS-STABLE)\b', s, re.I)
        if sm: out['status'] = sm.group(1).upper().replace(' ', '-')
    elif kind == 'HRD':
        score = normalize_hrd(s, allow_bare=True)
        if score == 'N/A':
            out['value_raw'] = 'N/A'
            out['status'] = 'not_available'
        elif score:
            out.pop('status', None)  # HRD-LOH/LST are components, not status.
            out['value_raw'] = score
            out['score'] = float(score.replace(',', '.'))
        else:
            # Other HRD-component numbers are NOT a total HRD score.
            out.pop('value_raw', None)
            out['status'] = 'unverified_score'
    return out


def _build_spread_signals(observations: list[dict], cfg: dict) -> dict:
    source_min = float(cfg.get('spread_source_min_af', 0.05))
    recipient_max = float(cfg.get('spread_recipient_max_af', 0.05))
    ratio_min = float(cfg.get('spread_ratio_min', 4.0))
    pair_min = int(cfg.get('spread_pair_min_variants', 2))

    by_variant = defaultdict(list)
    for o in observations:
        by_variant[o.get('variant_key')].append(o)

    shared = []
    high_low = []
    pair_counter = defaultdict(list)
    for vk, rows in by_variant.items():
        uniq = {r.get('record_key'): r for r in rows}
        rows = list(uniq.values())
        if len(rows) < 2:
            continue
        rows.sort(key=lambda r: (r.get('af') is not None, r.get('af') if r.get('af') is not None else -1), reverse=True)
        top = rows[0]
        shared.append({
            'variant_key': vk,
            'gene': top.get('gene'),
            'change': top.get('change'),
            'classification': top.get('oncogenic'),
            'observations': [{
                'record_key': r.get('record_key'),
                'patient_id': r.get('patient_id'),
                'case_family': r.get('case_family'),
                'sample_type': r.get('sample_type'),
                'is_rerun': bool(r.get('is_rerun')),
                'af': r.get('af'), 'af_pct': _af_pct(r.get('af')), 'ad': r.get('ad'), 'dp': r.get('dp'), 'filter': r.get('filter'),
            } for r in rows],
        })
        max_af = top.get('af')
        for recip in rows[1:]:
            recip_af = recip.get('af')
            rat = _ratio(max_af, recip_af)
            if max_af is not None and recip_af is not None and max_af >= source_min and recip_af <= recipient_max and rat is not None and rat >= ratio_min:
                sig = {
                    'variant_key': vk, 'gene': top.get('gene'), 'change': top.get('change'), 'classification': top.get('oncogenic'),
                    'source_record_key': top.get('record_key'), 'source_patient_id': top.get('patient_id'), 'source_af': max_af, 'source_af_pct': _af_pct(max_af), 'source_filter': top.get('filter'),
                    'recipient_record_key': recip.get('record_key'), 'recipient_patient_id': recip.get('patient_id'), 'recipient_af': recip_af, 'recipient_af_pct': _af_pct(recip_af),
                    'recipient_filter': recip.get('filter'), 'ratio': round(rat, 3), 'recipient_ad': recip.get('ad'), 'recipient_dp': recip.get('dp'),
                }
                high_low.append(sig)
                pair_counter[(top.get('record_key'), recip.get('record_key'))].append(sig)

    pair_signatures = []
    for (src, dst), sigs in pair_counter.items():
        if len(sigs) < pair_min:
            continue
        pair_signatures.append({
            'source_record_key': src, 'recipient_record_key': dst,
            'shared_high_to_low_variant_count': len(sigs),
            'variants': [{
                'variant_key': s['variant_key'], 'gene': s['gene'], 'change': s['change'],
                'source_af_pct': s['source_af_pct'], 'recipient_af_pct': s['recipient_af_pct'], 'ratio': s['ratio'],
            } for s in sigs],
        })
    pair_signatures.sort(key=lambda x: x['shared_high_to_low_variant_count'], reverse=True)
    high_low.sort(key=lambda x: x.get('ratio') or 0, reverse=True)
    shared.sort(key=lambda x: (-len(x['observations']), str(x.get('gene') or ''), str(x.get('change') or '')))
    return {
        'thresholds': {
            'source_min_af': source_min,
            'recipient_max_af': recipient_max,
            'minimum_source_recipient_ratio': ratio_min,
            'pair_signature_min_variants': pair_min,
        },
        'shared_exact_variants': shared,
        'high_to_low_signals': high_low,
        'multi_variant_pair_signatures': pair_signatures,
        'interpretation_note': 'Bunlar teknik inceleme sinyalleridir; otomatik kontaminasyon tanısı değildir. Hotspot biyolojisi, rerun, tümör oranı, AF/AD/DP, filtre ve QC/VerifyBAMID birlikte değerlendirilmelidir.',
    }




def _normalize_person_name(name: str) -> str:
    s = str(name or '').strip().casefold()
    s = re.sub(r'[^\wçğıöşü]+', ' ', s, flags=re.UNICODE)
    return re.sub(r'\s+', ' ', s).strip()


def _same_optional_value(a, b) -> bool:
    aa = str(a or '').strip().casefold()
    bb = str(b or '').strip().casefold()
    return (not aa) or (not bb) or aa == bb


def _linked_samples_by_name(base: Path, db, current_sample: dict, current_case_meta: dict,
                            pool_group: str, selected_run_uuids=()) -> dict:
    """Link DNA/RNA records locally without exporting national ID.

    A validated local TC match is strongest. If TC is unavailable, exact normalized full
    name is used with birth-date/sex guards when those fields are available. MP-number
    similarity is never used as identity evidence.
    """
    name = str(current_case_meta.get('full_name') or '').strip()
    norm = _normalize_person_name(name)
    current_nid = ''.join(ch for ch in str(current_case_meta.get('national_id') or '') if ch.isdigit())
    current_nid_valid = bool(current_nid and db.validate_national_id(current_nid))
    current_birth = str(current_case_meta.get('birth_date') or '').strip()
    current_sex = current_case_meta.get('sex')
    current_link_id, current_link_basis = _patient_link_id(base, db, current_case_meta)
    out = {
        'method': 'local_validated_tc_hmac_then_exact_name_guard',
        'name_available_locally': bool(norm),
        'validated_national_id_available_locally': current_nid_valid,
        'current_patient_link_id': current_link_id,
        'current_patient_link_id_basis': current_link_basis,
        'matched_samples': [],
        'candidate_matches': [],
        'conflicting_name_matches': [],
        'note': 'Doğrulanmış yerel kimlik eşleşmesi hasta bağlantısıdır; blok ayrıca doğrulanır. Ad/doğum tarihi/cinsiyet eşleşmeleri yalnız yerel kimlik kontrolü adaylarıdır; klinik veriler birleştirilmez.',
    }
    current_rk = current_sample.get('record_key')
    for row in db.samples():
        smp = dict(row)
        if smp.get('record_key') == current_rk:
            continue
        if (str(smp.get('run_uuid') or '') not in selected_run_uuids if selected_run_uuids
                else db.pool_group_name(smp.get('pool_name')) != pool_group):
            continue
        fam = smp.get('case_family') or smp.get('patient_id') or ''
        cm = db.get_case_metadata(fam)
        other_nid = ''.join(ch for ch in str(cm.get('national_id') or '') if ch.isdigit())
        other_nid_valid = bool(other_nid and db.validate_national_id(other_nid))
        basis = None
        if current_nid_valid and other_nid_valid and current_nid == other_nid:
            basis = 'validated_local_national_id'
        else:
            if current_nid_valid and other_nid_valid:
                continue  # A matching name never overrides conflicting validated IDs.
            if not norm or _normalize_person_name(cm.get('full_name')) != norm:
                continue
            same_birth = _same_optional_value(current_birth, cm.get('birth_date'))
            same_sex = _same_optional_value(current_sex, cm.get('sex'))
            if same_birth and same_sex:
                basis = 'exact_normalized_full_name_with_birth_date_sex_guard'
            else:
                out['conflicting_name_matches'].append({
                    'patient_id': smp.get('patient_id'), 'case_family': fam, 'sample_type': smp.get('sample_type'),
                    'pool_name': smp.get('pool_name'), 'run_status': smp.get('run_status'), 'sample_status': smp.get('sample_status'),
                    'age': cm.get('age'), 'sex': cm.get('sex'),
                    'reason': 'Aynı ad-soyad bulundu ancak doğum tarihi ve/veya cinsiyet bilgisi çelişiyor; otomatik eşleştirilmedi.'
                })
                continue
        other_link_id, other_link_basis = _patient_link_id(base, db, cm)
        current_block = db.get_sample_metadata(current_rk) or {}
        other_block = db.get_sample_metadata(smp.get('record_key')) or {}
        same_block = (basis == 'validated_local_national_id' and bool(current_block.get('block_id')) and
                      current_block.get('block_id') == other_block.get('block_id') and
                      current_block.get('block_source') == other_block.get('block_source'))
        summary = {
            'record_key': smp.get('record_key'),
            'patient_id': smp.get('patient_id'),
            'patient_link_id': other_link_id,
            'patient_link_id_basis': other_link_basis,
            'case_family': fam,
            'sample_type': smp.get('sample_type'),
            'pool_name': smp.get('pool_name'),
            'run_status': smp.get('run_status'),
            'sample_status': smp.get('sample_status'),
            'age': cm.get('age'),
            'sex': cm.get('sex'),
            'match_basis': basis,
            'patient_identity_verified': basis == 'validated_local_national_id',
            'block_id': other_block.get('block_id') or None,
            'same_block_verified': same_block,
            'fusion_source_available': bool(_latest_file_path(base, db, smp.get('record_key'), 'fusion')[0]),
            'qc_source_available': bool(_latest_file_path(base, db, smp.get('record_key'), 'qc')[0]),
        }
        out['matched_samples' if basis == 'validated_local_national_id' else 'candidate_matches'].append(summary)
    out['matched_samples'].sort(key=lambda x: (str(x.get('sample_type') or ''), str(x.get('patient_id') or '')))
    return out

def _sanitize_free_text_for_ai(text: str, names: list[str], tcs: list[str], *, _redactor=None) -> str:
    return (_redactor or ExportPrivacyRedactor(names, tcs)).redact(text)


_AI_PRIVATE_KEYS = {
    'fullname', 'maskedfullname', 'patientname', 'patientfullname', 'firstname', 'lastname',
    'namesurname', 'adsoyad', 'nationalid', 'tckn', 'tc', 'tckimlik', 'tckimlikno',
    'birthdate', 'dateofbirth', 'dob', 'email', 'emailaddress', 'accountemail', 'useremail',
    'requestingphysician', 'requestingphysicianmasked', 'reportingphysician',
    'reportingphysicianmasked', 'reportingsigner', 'sourceanalyst', 'referringphysicians',
    'referringphysician', 'analyst', 'analysts', 'username', 'password', 'accesstoken',
    'physician', 'physicians', 'doctor', 'doctors', 'displayname',
}


def _sanitize_ai_object(value, names: list[str], tcs: list[str], path=(), *, _redactor=None):
    """Sanitize structured FULL as well as COMPACT; safe clinical fields survive."""
    redactor = _redactor or ExportPrivacyRedactor(names, tcs)
    if isinstance(value, dict):
        out = {}
        for key, child in value.items():
            normalized = _context_key(key)
            if normalized in _AI_PRIVATE_KEYS:
                continue
            if normalized == 'name' and any(part in ('patient', 'user', 'account', 'physician', 'analyst')
                                             for part in path):
                continue
            safe_key = redactor.redact(str(key))
            out[safe_key] = _sanitize_ai_object(child, names, tcs, path + (normalized,), _redactor=redactor)
        return out
    if isinstance(value, (list, tuple)):
        return [_sanitize_ai_object(child, names, tcs, path, _redactor=redactor) for child in value]
    if isinstance(value, str):
        return redactor.redact(value)
    return value


def _fusion_pool_breakpoint_recurrence(patients: list[dict]) -> dict:
    """Exact partner+breakpoint recurrence across technically VERIFIED RNA tables.

    Repetition is a QC/background/artifact priority signal ONLY. It is not
    a contamination test and not a per-patient positive-fusion call.
    """
    grouped=defaultdict(dict)
    eligible=0
    for case in patients:
        fs=case.get('fusion_summary') or {}
        if not fs.get('source_verified') or fs.get('source_kind')!='structured_export':
            continue
        eligible+=1
        patient_id=str((case.get('identity') or {}).get('patient_id') or '')
        if not patient_id:continue
        for c in (fs.get('candidates') or [])[:2500]:
            pair=str(c.get('fusion_name') or '')
            left=str(c.get('breakpoint1') or '')
            right=str(c.get('breakpoint2') or '')
            if not (pair and left and right):continue
            key=(pair,left,right)
            if patient_id in grouped[key]:continue
            try:reads=float(c.get('junction_reads') or c.get('star_fusion_support') or 0)
            except (TypeError,ValueError):reads=0
            grouped[key][patient_id]=round(reads,2)
    events=[]
    for (pair,left,right),observations in grouped.items():
        if len(observations)<3:continue
        events.append({'fusion_name':pair,'breakpoint1':left,'breakpoint2':right,
            'sample_count':len(observations),
            'observed_patient_ids':sorted(observations),
            'max_star_junction_reads':max(observations.values()),
            'clinical_statement':'technical_recurrence_only_not_proof_of_contamination_or_true_fusion'})
    events.sort(key=lambda x:(-x['sample_count'],-x['max_star_junction_reads'],x['fusion_name']))
    return {'status':'technical_recurrence_review_only',
            'verified_source_samples':eligible,'recurrent_exact_events_ge3':len(events),
            'events':events[:100],
            'events_scope': _list_scope(len(events), min(len(events), 100), source_complete=True),
            'note':'Same partner + exact two breakpoints recurring across independent technical samples. Check reference/caller background, mapping artifacts, platform controls and cross-sample effects. Not proof of contamination.'}


def _safe_report_history_rows(rows) -> list[dict]:
    out=[]
    for r0 in rows or []:
        r=dict(r0)
        out.append({
            'created_at': r.get('created_at'), 'record_key': r.get('record_key'), 'patient_id': r.get('patient_id'),
            'pool_name': r.get('pool_name'), 'sample_type': r.get('sample_type'), 'variant_key': r.get('variant_key'),
            'variant_type': r.get('variant_type'), 'gene': r.get('gene'), 'transcript': r.get('transcript'),
            'cdna': r.get('cdna'), 'protein': r.get('protein'), 'source': r.get('source'),
            'match_score': r.get('match_score'), 'confirmed': bool(r.get('confirmed')),
        })
    return out


def _build_pool_contamination_context(patients: list[dict], fingerprint: dict, known_artifacts: list[dict]) -> dict:
    recurrent = fingerprint.get('recurrent_pool_variants') or []
    directional = fingerprint.get('high_to_low_signals') or []
    artifact_map = {str(x.get('variant_key') or ''): x for x in known_artifacts or []}
    recurrent_by_record=defaultdict(list)
    artifact_hits_by_record=defaultdict(list)
    for rv in recurrent:
        vk=str(rv.get('variant_key') or '')
        for ob in rv.get('observations') or []:
            rk=ob.get('record_key')
            if rk: recurrent_by_record[rk].append(vk)
            if rk and vk in artifact_map: artifact_hits_by_record[rk].append(vk)
    outgoing=defaultdict(int);incoming=defaultdict(int)
    for sig in directional:
        if sig.get('source_record_key'): outgoing[sig['source_record_key']]+=1
        if sig.get('recipient_record_key'): incoming[sig['recipient_record_key']]+=1
    sample_rows=[]
    full_counts = fingerprint.get('per_record_counts_before_truncation') or {}
    for p in patients:
        ts=p.get('technical_sample') or {}; ident=p.get('identity') or {}; rk=ts.get('record_key') or ''
        qc=(p.get('qc') or {}).get('summary') or {}
        sample_rows.append({
            'record_key':rk,'patient_id':ident.get('patient_id') or ident.get('case_family'),'patient_link_id':ident.get('patient_link_id'),
            'sample_type':ts.get('sample_type'),'is_rerun':bool(ts.get('is_rerun')),
            'verifybamid_contamination_pct':qc.get('verifybamid_contamination_pct'),
            'mean_coverage_x':qc.get('mean_coverage_x'),'duplication_pct':qc.get('duplication_pct'),
            'high_to_low_outgoing_count':full_counts.get(rk, {}).get('outgoing_count', outgoing.get(rk,0)),
            'high_to_low_incoming_count':full_counts.get(rk, {}).get('incoming_count', incoming.get(rk,0)),
            'recurrent_pool_variant_count':full_counts.get(rk, {}).get('recurrent_count', len(set(recurrent_by_record.get(rk,[])))),
            'known_artifact_hits':sorted(set(artifact_hits_by_record.get(rk,[]))),
            'technical_signal_state':'signal_present' if (full_counts.get(rk, {}).get('outgoing_count', outgoing.get(rk,0)) or
                                                         full_counts.get(rk, {}).get('incoming_count', incoming.get(rk,0)) or
                                                         artifact_hits_by_record.get(rk)) else 'no_predefined_signal',
        })
    pairs=[]
    for x in fingerprint.get('pair_summaries') or []:
        pairs.append({k:x.get(k) for k in ('record_key_a','record_key_b','patient_a','patient_b','same_case_family','shared_exact_variant_count','shared_pass_variant_count','shared_nonpass_variant_count','shared_cancervar_tier_iv_count','high_to_low_directional_count','high_to_low_nonpass_recipient_count','shared_exact_density_vs_smaller_sample','top_high_to_low_signals')})
    return {
        'automatic_contamination_diagnosis': False,
        'note':'Her teknik örnek ve bütün örnek çiftleri birlikte verilir. Sayılar teknik inceleme sinyalidir; laboratuvar validasyon eşiği yerine geçmez.',
        'samples': sample_rows, 'all_pair_summaries': pairs,
    }


def _compact_qc_for_markdown(qc: dict | None, sample: dict) -> dict:
    out = {'run_mean_depth': sample.get('depth')}
    if qc:
        out.update(qc.get('key_metrics') or {})
    return {k: v for k, v in out.items() if v not in (None, '', [], {})}


def ai_export_run_choices(db):
    """Return (UI label, canonical POOL, exact RUN UUIDs) for source-backed export."""
    from pool_spread import spread_run_choices
    by_group = {}
    for display, run_uuid in spread_run_choices(db):
        row = db.cx.execute('SELECT pool_name,run_type FROM pools WHERE run_uuid=? ORDER BY id DESC LIMIT 1',
                            (run_uuid,)).fetchone()
        if row is None:
            continue
        canonical = db.pool_group_name(db.display_pool_name(run_uuid, row['pool_name']))
        by_group.setdefault(canonical, []).append((display, run_uuid, str(row['run_type'] or '').upper()))
    out = []
    for group, runs in by_group.items():
        types = [kind for _,_,kind in runs]
        # Legitimate DNA and RNA runs belong to the same clinical pool group.
        # Duplicate DNA (or RNA) runs need separate choices to avoid mixing runs.
        if len(runs) == 1 or (len(runs) <= 2 and len(set(types)) == len(types)
                              and set(types) <= {'DNA', 'RNA'}):
            out.append((group, group, tuple(r[1] for r in runs)))
        else:
            for label, run_uuid, _ in runs:
                out.append((label, group, (run_uuid,)))
    return out


def _source_samples_for_export(db, group, scope, selected_run_uuids=()):
    """Keep exported records tied to the selected immutable RUNs."""
    runs = {str(ru) for ru in selected_run_uuids if str(ru)}
    if runs:
        return [dict(r) for r in db.samples() if str(r['run_uuid']) in runs
                and _scope_match(r['sample_type'], scope)]
    # Backwards-compatible direct API: historical callers still use source labels.
    return [dict(r) for r in db.samples() if db.pool_group_name(r['pool_name']) == group
            and _scope_match(r['sample_type'], scope)]


def build_pool_ai_package(base: Path, db, cfg: dict, pool_group: str, include_pii: bool = False,
                          include_full_qc: bool = True, sample_scope: str = 'DNA', include_source_files: bool = True):
    base = Path(base)
    group = db.pool_group_name(pool_group)
    scope = sample_scope if sample_scope in SCOPE_VALUES else 'DNA'
    selected_run_uuids = tuple(cfg.get('ai_selected_run_uuids') or ())
    samples = _source_samples_for_export(db,group,scope,selected_run_uuids)
    if not samples:
        raise ValueError(f'{group} / {scope} için teknik örnek bulunamadı.')
    selected_keys = {s['record_key'] for s in samples}
    report_progress('read_samples', 0, len(samples), 'Seçilen POOL kayıtları okunuyor.')
    privacy_names, privacy_tcs = _privacy_sensitive_values(db, samples)
    privacy_redactor = ExportPrivacyRedactor(privacy_names, privacy_tcs)
    known_artifacts = db.artifact_variant_summary() if hasattr(db, 'artifact_variant_summary') else []

    observations = []
    keys = sorted(selected_keys)
    for offset in range(0, len(keys), 500):
        chunk = keys[offset:offset + 500]
        placeholders = ','.join('?' for _ in chunk)
        observations.extend(dict(r) for r in db.cx.execute(
            f'SELECT * FROM oncogenic_observations WHERE record_key IN ({placeholders})', chunk))
    by_record = defaultdict(list)
    for r in observations:
        by_record[r.get('record_key')].append(r)

    patients = []
    full_patients = []
    source_copy_specs: list[tuple[Path, str]] = []
    bundle_warnings = []
    all_snv_observations = []

    for sample_index, s in enumerate(samples, 1):
        rk = s['record_key']
        report_progress('read_samples', sample_index, len(samples), 'Örnek kaynakları ve klinik bağlam okunuyor.')
        family = s.get('case_family') or s.get('patient_id') or ''
        cm = db.get_case_metadata(family)
        sm = db.technical_metadata_for_record(rk) if hasattr(db, 'technical_metadata_for_record') else db.get_sample_metadata(rk)

        inventory, copies = _source_inventory(base, db, s, scope)
        if include_source_files:
            source_copy_specs.extend(copies)
        inv_by_type = {x['file_type']: x for x in inventory}

        qc_path, _ = _latest_file_path(base, db, rk, 'qc')
        report_progress('read_qc', sample_index, len(samples), 'MultiQC tabloları okunuyor.')
        qc = _read_qc_package(qc_path, s.get('patient_id') or family, include_full_qc, s.get('sample_type') or '') if qc_path else None
        clinical, clinical_name = _load_json_source(base, db, rk, 'clinical')
        clinical_context = _resolve_clinical_context(db, s, cm, sm, clinical)
        snv_path, _ = _latest_file_path(base, db, rk, 'snv')
        report_progress('read_variants', sample_index, len(samples), 'SNV, CNV ve füzyon kaynakları okunuyor.')
        snv_idx, snv_meta, snv_rows = _snv_annotation_rows(snv_path)
        for raw_snv_row in snv_rows:
            all_call = _all_snv_observation(raw_snv_row, s)
            if all_call:
                all_snv_observations.append(all_call)
        cnv_path, _ = _latest_file_path(base, db, rk, 'cnv')
        cnv = _read_cnv(cnv_path, include_raw=include_full_qc)
        fusion_path, _fusion_row = _latest_file_path(base, db, rk, 'fusion')
        fusion = _read_fusion_source(fusion_path, include_raw=include_full_qc)
        if str(s.get('sample_type') or '').upper()=='RNA' and not fusion.get('source_verified'):
            partial_path,_ = _latest_file_path(base,db,rk,'fusion_evidence')
            partial_review=_partial_react_fusion_review(partial_path,s.get('patient_id') or '')
            if partial_review:
                fusion['partial_react_review_only']=partial_review
                bundle_warnings.append(f"{s.get('patient_id') or family}: kısmi React kanıtı {partial_review.get('identified_read_supported_rows',0)}/{partial_review.get('filtered_expected')} satırda; {partial_review.get('error_count',0)} hata devam ediyor. Teknik ve klinik doğrulama gerekli.")
        fusion_name = fusion.get('source_file')
        fusion_caller_evidence = _fusion_caller_evidence_from_qc(qc, include_raw=include_full_qc)
        if fusion_caller_evidence.get('summary_all_numeric_zero'):
            for cand in fusion.get('candidates') or []:
                support=' '.join(str(cand.get(k) or '') for k in ('fii','star_fusion_support','fusioninspector_support','junction_reads','spanning_or_discordant_reads'))
                if re.search(r'[1-9]',support):
                    fusion_caller_evidence['caller_summary_conflict']=True;break
        decisions = db.variant_decisions(rk)
        reported_history = _safe_report_history_rows(db.reported_variant_history(record_key=rk) if hasattr(db, 'reported_variant_history') else [])
        # GenNext's read-only Selection is an independent technical work state.
        # It NEVER creates/rewrites Enlil/manual reporting decisions.
        from gennext_selection_store import current_status
        from gennext_fusion_selection_store import current_fusion_status
        gen_context = _selection_export(current_status(db, rk), s, 'SNV_INDEL_ONLY', snv_idx)
        fusion_selection = _selection_export(current_fusion_status(db, rk), s, 'RNA_FUSION_ONLY')
        cnv_selection = _cnv_selection_unverified(s)

        variants = []
        full_variants = []
        for o in sorted(by_record.get(rk, []), key=lambda r: (0 if r.get('oncogenic') == 'Oncogenic' else 1, -(r.get('af') or -1))):
            raw_row = snv_idx.get(o.get('variant_key')) or {}
            annotations = _selected_snv_annotations(raw_row)
            chrom = _pick_ci(raw_row, 'Chrom', 'chrom') or o.get('chrom') or ''
            start = _pick_ci(raw_row, 'Start', 'start') or o.get('start') or ''
            end = _pick_ci(raw_row, 'End', 'end') or start
            ref = _pick_ci(raw_row, 'Ref', 'ref') or o.get('ref') or ''
            alt = _pick_ci(raw_row, 'Alt', 'alt') or o.get('alt') or ''
            igv_region = _pick_ci(raw_row, 'igv', 'IGV')
            if not igv_region and chrom and start:
                igv_region = f"{chrom}:{start}-{end or start}"
            v = {
                'variant_key': o.get('variant_key'), 'gene': o.get('gene'), 'change': o.get('change'),
                'classification': o.get('oncogenic'), 'classification_source': 'source_oncogenicity',
                'usage': 'research_candidate_only', 'report_selected': None,
                'af': o.get('af'), 'af_pct': _af_pct(o.get('af')),
                'ad': o.get('ad'), 'dp': o.get('dp'), 'filter': o.get('filter'), 'highest_level': o.get('highest_level'),
                'chrom': chrom, 'start': start, 'end': end, 'ref': ref, 'alt': alt, 'igv_region': igv_region,
                'manual_reporting_decision': decisions.get(o.get('variant_key')) or decisions.get('SNV|' + str(o.get('variant_key') or '')), 'annotations': annotations,
            }
            variants.append(v)
            fv = json.loads(json.dumps(v, ensure_ascii=False))
            if raw_row:
                fv['source_snv_row'] = raw_row
            full_variants.append(fv)

        link_id, link_basis = _patient_link_id(base, db, cm)
        ident = {'patient_id': s.get('patient_id'), 'case_family': family, 'patient_link_id': link_id, 'patient_link_id_basis': link_basis}
        if include_pii:
            ident.update({'masked_full_name': _mask_person_name(cm.get('full_name'))})
        linkage = _linked_samples_by_name(base, db, s, cm, group, selected_run_uuids)

        from reporting_context import build_record_context
        record_context = build_record_context(db, s)
        biomarkers = _record_biomarkers(record_context)

        components = []
        for comp in _expected_components(s.get('sample_type'), scope):
            if comp == 'biomarkers':
                bm_complete = all((biomarkers['TMB'].get('value_muts_per_mb') is not None,
                                   biomarkers['MSI'].get('percentage') is not None,
                                   biomarkers['HRD'].get('score') is not None))
                components.append({'component': 'biomarkers', 'status': 'ok' if bm_complete else 'missing',
                                   'task_status': None, 'source': 'record_field_store'})
            elif comp == 'qc':
                components.append(_component_status(db, s, comp, bool(qc_path), parsed_ok=(bool(qc and qc.get('source_verified') and (qc.get('parsed_table_count') or qc.get('raw_multiqc_json')) and not qc.get('qc_reporter_required_missing')))))
            elif comp == 'clinical':
                components.append(_component_status(db, s, comp, bool(clinical_name), parsed_ok=(clinical is not None)))
            elif comp == 'cnv':
                components.append(_component_status(db, s, comp, bool(cnv_path), parsed_ok=(cnv.get('status') in ('parsed', 'no_data'))))
            elif comp == 'snv':
                components.append(_component_status(db, s, comp, bool(snv_path), parsed_ok=(snv_path is not None)))
            elif comp == 'fusion':
                fp, _ = _latest_file_path(base, db, rk, 'fusion')
                components.append(_component_status(db, s, comp, bool(fp), parsed_ok=bool(fusion.get('source_verified'))))

        missing = [x['component'] for x in components if x['status'] in ('missing', 'unparsed', 'blocked')]
        review = [x['component'] for x in components if x['status'] == 'review']
        waiting = [x['component'] for x in components if x['status'] == 'waiting']
        if missing:
            bundle_warnings.append(f"{s.get('patient_id') or family}: eksik/okunamayan bileşen: {', '.join(missing)}")
        if review:
            bundle_warnings.append(f"{s.get('patient_id') or family}: manuel kontrol/yeniden toplama gereken bileşen: {', '.join(review)}")
        if waiting:
            bundle_warnings.append(f"{s.get('patient_id') or family}: henüz bekleyen bileşen: {', '.join(waiting)}")

        context_missing = list(clinical_context.get('missing_context') or [])
        if context_missing:
            context_labels = {
                'diagnosis':'tanı', 'age_at_diagnosis':'yaş', 'sex':'cinsiyet',
                'block_id':'blok', 'tumor_percentage':'tümör yüzdesi',
            }
            human = [context_labels.get(x, x) for x in context_missing]
            bundle_warnings.append(f"{s.get('patient_id') or family}: missing_context: {', '.join(human)}")
        if str(s.get('sample_type') or '').upper()=='RNA' and fusion_caller_evidence.get('summary_stage_reconciliation_required'):
            if fusion.get('source_verified') and fusion.get('candidates'):
                bundle_warnings.append(f"{s.get('patient_id') or family}: React ayrıntı adayı mevcut; MultiQC raw/realigned/Arriba/STAR-Fusion özetleri farklı aşamalardır. Gen çiftleri ve breakpoint teknik olarak alındı, klinik füzyon doğrulaması ayrıca gereklidir.")
            else:
                bundle_warnings.append(f"{s.get('patient_id') or family}: MultiQC raw/realigned aday toplamları ile caller özetleri farklı aşamalardır; bu örnekte eksiksiz doğrulanmış gen/partner/breakpoint kaynağı bulunmuyor.")
        if str(s.get('sample_type') or '').upper()=='RNA' and fusion_caller_evidence.get('summary_all_numeric_zero') and not fusion_caller_evidence.get('has_detailed_candidate_table'):
            bundle_warnings.append(f"{s.get('patient_id') or family}: Arriba/STAR-Fusion/FusionInspector özetleri sıfır görünse bile ayrıntılı aday tablosu yok; bu tek başına klinik negatiflik kabul edilmemelidir.")
        if str(s.get('sample_type') or '').upper()=='RNA' and fusion_caller_evidence.get('caller_summary_conflict'):
            bundle_warnings.append(f"{s.get('patient_id') or family}: MultiQC caller özeti 0 görünürken platform Fusion adayında FII/read/caller desteği var; bu kaynaklar çelişkili, manuel doğrulama gerekli.")
        if str(s.get('sample_type') or '').upper()=='RNA' and str(fusion.get('source_kind') or '').startswith('review'):
            bundle_warnings.append(f"{s.get('patient_id') or family}: Fusion kaynağı eksik/bozuk; review_only örnekler klinik pozitif veya negatif değildir. Kısmi React ayrıntısı varsa SOURCE_FILES içinde inceleyin.")

        case = {
            'identity': ident,
            # Backward-compatible top-level mirrors now use the resolved context.
            'diagnosis': clinical_context.get('diagnosis'), 'age': clinical_context.get('age_at_diagnosis'),
            'age_reference_date': clinical_context.get('age_reference_date'), 'sex': clinical_context.get('sex'),
            'clinical_context': clinical_context,
            'requesting_physician_masked': _mask_person_name(cm.get('requesting_physician'), keep_titles=True), 'reporting_physician_masked': _mask_person_name(cm.get('reporting_physician'), keep_titles=True), 'case_note': privacy_redactor.redact(cm.get('note')),
            'technical_sample': {
                'record_key': rk, 'pool_name': s.get('pool_name'), 'sample_type': s.get('sample_type'), 'is_rerun': bool(s.get('is_rerun')),
                'run_status': s.get('run_status'), 'sample_status': s.get('sample_status'), 'block_id': clinical_context.get('block_id'),
                'block_source': clinical_context.get('block_source'), 'tumor_percentage': clinical_context.get('tumor_percentage'),
                'run_id': s.get('run_uuid'), 'sample_id': s.get('sample_uuid'),
                'metadata_inherited_from': '', 'sample_note': sm.get('note'),
            },
            'biomarkers': biomarkers,
            'qc': {
                'summary': _compact_qc_for_markdown(qc, s),
                'general_stats': (qc or {}).get('general_stats') if qc else None,
                'table_inventory': (qc or {}).get('table_inventory') if qc else [],
                'source_file': (qc or {}).get('source_file') if qc else None,
                'source_sha256': (qc or {}).get('source_sha256') if qc else None,
                'read_errors': (qc or {}).get('read_errors') if qc else [],
                'source_verified': (qc or {}).get('source_verified') if qc else False,
                'source_validation_reason': (qc or {}).get('source_validation_reason') if qc else 'QC source missing',
                'fusion_caller_evidence': fusion_caller_evidence,
                'qc_reporter_required_missing': (qc or {}).get('qc_reporter_required_missing') if qc else [],
                'qc_reporter_status': (qc or {}).get('qc_reporter_status') if qc else None,
                'qc_reporter_metrics': (qc or {}).get('qc_reporter_metrics') if qc else {},
            },
            'clinical_summary': {
                'source_file': clinical_name,
                'total_genes': clinical.get('total_genes') if isinstance(clinical, dict) else None,
                'captured_gene_count': clinical.get('captured_gene_count') if isinstance(clinical, dict) else None,
                'page_count': clinical.get('page_count') if isinstance(clinical, dict) else None,
            },
            'snv_source_summary': snv_meta,
            'oncogenic_and_likely_oncogenic_variants': variants,
            'reported_variant_history': reported_history,
            'gennext_report_selection': gen_context,
            'gennext_fusion_selection': fusion_selection,
            'report_selection_by_assay': {'SNV_INDEL': gen_context, 'CNV': cnv_selection, 'RNA_FUSION': fusion_selection},
            'research_candidates_policy': 'Raw lists, Oncogenic/PASS calls and IGV evidence never become report selections automatically.',
            'research_snv_scope': _list_scope(len(variants), len(variants), source_complete=True),
            'export_purpose': 'technical_pre_review', 'clinical_report_release': False,
            'clinical_report_approval': {key: (record_context.get('approval') or {}).get(key)
                                         for key in ('state', 'approved_at', 'fingerprint', 'invalidation_reason')},
            'cnv': {k: v for k, v in cnv.items() if k != 'raw_rows'},
            'fusion_summary': {k:v for k,v in fusion.items() if k not in ('raw_rows','raw_json','visible_text')},
            'source_inventory': inventory,
            'dna_rna_identity_linkage': linkage,
            'clinical_context_completeness': {
                'status': 'complete' if not context_missing else 'warning',
                'missing_fields': context_missing,
                'missing_context': context_missing,
                'clinical_context_ready': not bool(context_missing),
                'export_purpose': 'technical_pre_review',
            },
            'completeness': {
                'components': components,
                'status': 'warning' if (missing or review or context_missing) else ('waiting' if waiting else 'complete'),
                'missing_or_unparsed': missing,
                'review_required': review,
                'waiting': waiting,
                'missing_context_fields': context_missing,
            },
        }
        patients.append(case)

        full_case = json.loads(json.dumps(case, ensure_ascii=False))
        full_case['oncogenic_and_likely_oncogenic_variants'] = full_variants
        if include_full_qc and qc:
            full_case['qc']['raw_multiqc_tables'] = qc.get('raw_multiqc_tables')
            full_case['qc']['raw_multiqc_json'] = qc.get('raw_multiqc_json')
        if clinical is not None:
            full_case['clinical_raw'] = clinical
        if cnv.get('raw_rows') is not None:
            full_case['cnv']['raw_rows'] = cnv.get('raw_rows')
        if fusion.get('available'):
            full_case['fusion_raw'] = fusion
        full_patients.append(full_case)

    source_privacy_blocked = []
    if include_source_files and source_copy_specs:
        safe_specs = []
        for source_index, (src, arc) in enumerate(source_copy_specs, 1):
            report_progress('source_privacy', source_index, len(source_copy_specs), 'Ham kaynakların kimlik denetimi yapılıyor.')
            hits = _source_privacy_hits(src, privacy_names, privacy_tcs, _redactor=privacy_redactor)
            if hits:
                source_privacy_blocked.append({'source_file': src.name, 'bundle_path': arc, 'hits': hits})
                bundle_warnings.append(f"{src.name}: ham kaynakta bilinen yerel kimlik bilgisi bulundu; SOURCE_FILES içine eklenmedi.")
            else:
                safe_specs.append((src, arc))
        source_copy_specs = safe_specs

    report_progress('pool_comparison', message='Seçilen örneklerin POOL teknik sinyalleri hesaplanıyor.')
    spread = _build_spread_signals(observations, cfg)
    all_snv_fingerprint = _build_all_snv_fingerprint(all_snv_observations, cfg)
    known_artifact_map = {str(x.get('variant_key') or ''): x for x in known_artifacts}
    for rv in all_snv_fingerprint.get('recurrent_pool_variants') or []:
        hist = known_artifact_map.get(str(rv.get('variant_key') or ''))
        rv['known_artifact_history'] = hist or None
    for sig in all_snv_fingerprint.get('high_to_low_signals') or []:
        hist = known_artifact_map.get(str(sig.get('variant_key') or ''))
        sig['known_artifact_history'] = hist or None
    pool_contamination_context = _build_pool_contamination_context(patients, all_snv_fingerprint, known_artifacts)
    sample_label = {s['record_key']: (s.get('patient_id') or s.get('case_family') or s['record_key']) for s in samples}
    for sig in spread['multi_variant_pair_signatures']:
        sig['source_patient_id'] = sample_label.get(sig['source_record_key'], sig['source_record_key'])
        sig['recipient_patient_id'] = sample_label.get(sig['recipient_record_key'], sig['recipient_record_key'])

    run_states = []
    for r in db.cx.execute('SELECT * FROM pools ORDER BY run_date,id').fetchall():
        d = dict(r)
        if (str(d.get('run_uuid') or '') not in selected_run_uuids if selected_run_uuids
                else db.pool_group_name(d.get('pool_name')) != group):
            continue
        if not _scope_match(d.get('run_type'), scope):
            continue
        run_states.append({k: d.get(k) for k in ('pool_name', 'run_type', 'run_date', 'run_status')})

    completeness = {
        'status': 'warning' if any(p['completeness']['status'] == 'warning' for p in patients) else ('waiting' if any(p['completeness']['status'] == 'waiting' for p in patients) else 'complete'),
        'warnings': bundle_warnings,
        'complete_samples': sum(1 for p in patients if p['completeness']['status'] == 'complete'),
        'warning_samples': sum(1 for p in patients if p['completeness']['status'] == 'warning'),
        'waiting_samples': sum(1 for p in patients if p['completeness']['status'] == 'waiting'),
    }

    common = {
        'schema': AI_SCHEMA, 'generated_at': time.strftime('%Y-%m-%dT%H:%M:%S'), 'pool_group': group, 'sample_scope': scope,
        'run_states': run_states, 'patient_count': len({(p['identity'].get('patient_link_id') or p['identity']['case_family']) for p in patients}), 'technical_sample_count': len(patients),
        'contains_direct_identifiers': False, 'direct_identifier_fields': [], 'contains_masked_names': bool(include_pii), 'national_id_included': False, 'source_files_in_bundle': bool(include_source_files),
        'clinical_context_pii_independent': True,
        'clinical_context_source_priority': ['record_manual_override_including_explicit_blank', 'record_verified_source', 'record_legacy_seed_with_provenance', 'null_with_missing_context'],
        'analysis_scope': {
            'variants': 'Seçilen örnek türündeki tüm Oncogenic + Likely Oncogenic çağrılar; non-PASS çağrılar teknik POOL yayılım incelemesi için korunur. Kompakt kayıtta CancerVar/ClinVar/COSMIC/transkript gibi mevcut SNV anotasyonları da taşınır.',
            'qc': 'MultiQC Data ZIP içindeki TSV/CSV/JSON tabloları okunur. General Stats, VerifyBAMID, Picard ve diğer tablolar FULL pakete gömülür; ana metrikler ayrıca normalize edilir.',
            'cnv': 'En son CNV çıktısı yapılandırılmış olarak eklenir; no_data sonucu pozitif/negatif yorum uydurulmadan açıkça korunur.',
            'shared_reads': 'Oncogenic/Likely Oncogenic exact varyant sinyallerine ek olarak tüm SNV/indel TSV satırları benign/unknown/non-PASS dahil teknik fingerprint için karşılaştırılır. Bu BAM read-level ortaklık testi değildir.',
            'biomarkers': 'Aynı teknik kaydın etkin TMB/MSI/HRD değerleri, ayrı LOH/TAI/LST ve alan kaynaklarıdır. Manuel düzeltme korunur; eksik skor bileşenlerden toplanmaz. Boş/N/A/0 ayrıdır.',
            'identity_linkage': 'Doğrulanmış yerel TC için HMAC hasta bağlantısı üretilir; TC çıkmaz. Ad/doğum tarihi benzerlikleri yalnız candidate_matches içinde kimlik kontrolü adayıdır. Aynı hasta ile aynı blok ayrı doğrulanır.',
            'clinical_context': 'MP, yaş, cinsiyet, tanı, blok ve tümör yüzdesi teknik record_key kapsamında korunur. Etkin değer, manuel düzeltme, ham kaynak, kaynak farkı ve kayıt sürümü birlikte aktarılır; başka bloktan otomatik değer kopyalanmaz.',
            'block_id': 'Blok bilgisi mevcutsa teknik record_key bağlamıyla taşınır; yoksa null kalır ve missing_context içinde belirtilir.',
            'reported_history': 'Vaka notundaki rapor metninden manuel onaylanan ve manuel eklenen raporlanmış varyantlar ayrı geçmiş olarak taşınır; geçmiş kayıt tek başına yeni raporlama kararı değildir.',
            'artifact_history': 'Patolog tarafından artefakt olarak işaretlenmiş önceki exact varyantlar known_artifact_history içinde verilir. Tekrarlayan POOL varyantları ayrıca all_snv fingerprint içinde gösterilir.',
            'privacy': 'Ad, TC, doğum tarihi ve hesap e-postası alanları COMPACT/FULL çıktısından çıkarılır. MP, yaş, cinsiyet, tanı, blok ve tümör yüzdesi korunur. Kimlik içeren veya taranamayan ham kaynaklar pakete eklenmez.',
        },
        'bundle_completeness': completeness,
        'source_privacy_blocked': source_privacy_blocked,
        'pool_spread_review': spread,
        'pool_contamination_review_context': pool_contamination_context,
        'known_artifact_history': known_artifacts[:500],
        'known_artifact_history_scope': _list_scope(len(known_artifacts), min(len(known_artifacts), 500), source_complete=True),
        'all_snv_technical_fingerprint_review': all_snv_fingerprint,
        'patients': patients,
        'fusion_pool_breakpoint_recurrence': _fusion_pool_breakpoint_recurrence(patients),
    }
    full = dict(common)
    full['patients'] = full_patients
    return common, full, source_copy_specs


def build_ai_markdown(pkg: dict) -> str:
    lines = []
    lines.append(f"# {pkg.get('pool_group')} · {pkg.get('sample_scope')} · YZ raporlama ön değerlendirmesi")
    lines.append('')
    lines.append('Bu paket karar desteği içindir. Nihai varyant sınıflaması, teknik yeterlilik, kontaminasyon yorumu ve klinik rapor patolog doğrulaması gerektirir.')
    lines.append('')
    comp = pkg.get('bundle_completeness') or {}
    lines.append(f"## Paket bütünlüğü · {str(comp.get('status') or '').upper()}")
    lines.append(f"Tam örnek: {comp.get('complete_samples', 0)} · Uyarılı: {comp.get('warning_samples', 0)} · Bekleyen: {comp.get('waiting_samples', 0)}")
    for w in comp.get('warnings') or []:
        lines.append(f"- {w}")
    lines.append('')
    lines.append('## YZ görevi')
    lines.extend([
        '1. Önce POOL düzeyinde teknik bulaş/ortak varyant sinyallerini değerlendir. Tek ortak hotspotu kontaminasyon kabul etme.',
        '2. Yüksek AF kaynak → düşük AF alıcı paternlerinde AD/DP, Filter, tümör %, rerun, QC ve VerifyBAMID değerlerini birlikte kullan.',
        '3. all_snv_technical_fingerprint_review bölümünde benign/unknown/non-PASS çağrıları da kullanarak örnek çiftleri arasındaki ortak varyant yoğunluğunu, yüksek→düşük izleri ve tekrarlayan POOL artefaktlarını değerlendir. Aynı case_family olan rerunları kontaminasyon gibi yorumlama.',
        '4. known_artifact_history ve recurrent_pool_variants bölümlerini birlikte incele. Daha önce patologca artefakt denmiş exact varyantları, yaygın germline/polimorfizm olabilecek tekrarlayanları ve olası cross-sample carry-over sinyallerini birbirinden ayır; gerekçeyi yaz.',
        '5. Aynı hasta çiftinde birden fazla exact Oncogenic/Likely Oncogenic varyantın birlikte taşınmasını daha güçlü teknik sinyal olarak ayrıca belirt.',
        '6. DNA ve RNA farklı MP numarası taşıyabilir. Doğrulanmış yerel kimlikten türetilmiş patient_link_id hasta bağlantısıdır; aynı blok ayrıca doğrulanır. candidate_matches kimlik kontrolü adaylarıdır, veriler birleştirilmez. TC isteme; MP/ad/tanı benzerliğiyle eşleştirme yapma.',
        '7. Sonra her hastayı ayrı değerlendir: clinical_context (yaş/cinsiyet/tanı/blok/tümör %/DNA-RNA), QC, TMB/MSI/HRD, SNV/indel, CNV ve varsa eşleşmiş RNA/fusion verisini birlikte özetle.',
        '8. OncoKB onkojenik sınıfını CancerVar/ClinVar/tedavi level alanlarıyla karıştırma; çelişkileri ayrı yaz.',
        '8a. report_selection_by_assay içindeki SNV_INDEL, CNV ve RNA_FUSION ayrı kaynaklardır. Pozitif rapor seçimi için ilgili kategorinin report_selection_verified=true olması gerekir; SNV seçimi CNV/RNA yerine geçmez. Enlil metni veya klinik rapor onayı otomatik üretilmez.',
        '8b. Doğrulanmış selected_count=0 yalnız o kategorinin rapor seçiminin boş olduğunu gösterir. Okunmamış, eski, kimliği belirsiz veya analiz incelemesi tamamlanmamış seçimde selected_count=null kalır; historical_snapshot tarihsel gözlemdir.',
        '8c. GenNext workgroup adayları, PASS, Oncogenic, yüksek VAF ve IGV desteği otomatik rapor seçimi değildir. Unknown seçimler Unknown / onkojenisitesi bilinmeyen olarak korunur. TP53 dahil hiçbir genin kaynak onkojenisitesini topluca değiştirme.',
        '8d. OncoKB onkojenisitesi, ClinVar germline sınıfı, CancerVar/Tier ve tedavi kanıt düzeyi ayrı alanlardır.',
        '9. Bu çıktı teknik ön incelemedir. Eksik bileşeni veya clinical_context içindeki null alanı negatif kabul etme. Eksik klinik alanlar görünür kalır; export üretimi klinik rapor onayı değildir.',
        '9b. VAF ile tümör yüzdesi yalnız bağlamsal teknik incelemedir. Tümör yüzdesi/2 kuralıyla ret, germline/artefakt kararı veya rapora yükseltme yapma; lokal CNV/LOH ve klonalite bilinmiyor olabilir.',
        '9c. CNV parent_id, annotation_mode, cds_overlap ve probe_scope alanlarını koru. Aynı segment prob sayısını genler için toplama; ayrı segmentteki aynı geni birleştirme. Kısmi CDS örtüşmesi tüm gen olayı değildir. GAIN otomatik yüksek düzey amplifikasyon/ilaç uygunluğu değildir.',
        '9d. Her kısaltılmış listenin total_count, exported_count ve truncated alanlarını belirt. Gösterilmeyen kayıtlar negatif veya incelenmiş değildir.',
        '9e. igv_scope_summary içindeki kanıt yalnız request_id/revision/record_key/variant_key ile bağlı isteğe uygulanır. Kısmi çekim ile görsel destek ayrıdır; diğer isteklere onay yayılmaz. Kaynak AD/DP/VAF bağımsız görüntü ölçümü yerine geçmez.',
        '9a. MP, yaş, cinsiyet, tanı, blok, tümör yüzdesi ve DNA/RNA bağlamını koru. Ad/TC/hesap e-postası isteme veya üretme.',
        '10. SOURCE_FILES içindeki ham dışa aktarımları gerektiğinde doğrulama için kullan.',
        '10a. RNA için Arriba/STAR-Fusion/FusionInspector özetindeki 0 değerini, ayrıntılı aday tablosu veya doğrulanmış Fusion exportu olmadan nihai negatif kabul etme. Fusion source_kind=review_fallback ise yeniden toplama/manüel kontrol iste.',
        '10b. Füzyon adaylarında mümkün olduğunda Gene1::Gene2, breakpoint, frame, junction/spanning reads, FFPM, Arriba confidence, STAR-Fusion/FusionInspector desteği, FII ve known/recurrent alanlarını ayrı değerlendir.',
        '10c. caller_summary_conflict=true ise MultiQC 0 özeti ile platform aday desteğini uzlaştırılmış kabul etme; ilgili örneği manuel doğrulama/yeniden export listesine koy.',
        '10d. ÖNEMLİ: fusion_summary.candidates yalnız source_verified=true ve gerçek kanıtı taşıyan React/TSV kaynağından gelir. Bu adaylar klinik pozitif değildir; önem sıralaması junction/split reads, confidence, breakpoint, biyolojik uyum ve QC ile yapılır.',
        '10e. fusion_summary.candidate_review_status=incomplete_source_do_not_interpret_as_positive_or_negative durumunda unverified_candidate_examples yalnız teknik ipucudur; klinik rapor önermeden önce SOURCE_FILES içindeki kısmi React JSON ve ham exportu karşılaştır, eksikliği belirt.',
        '10f. 20/24 gibi teknik tamamlanma istatistiklerini tümör füzyon negatiflik oranı olarak sunma. YAP1::NUTM1 gibi nadir çiftleri kesin tanı olarak kabul etmeden öncelikli IGV/ortogonal doğrulama talep et.',
        '10g. fusion_pool_breakpoint_recurrence bölümündeki birebir aynı iki breakpointin POOL içinde tekrarlanmasını teknik artefakt/arka plan açısından tartış; tekrarın bulaş ispatı olmadığını açık yaz.',
        '11. Çıktı sırası: POOL teknik riskleri → hasta bazında değerlendirme → raporlama öncesi kontrol listesi.',
        '12. En sonda ayrıca AI_REVIEW_RETURN.json üret. Paketteki AI_REVIEW_RETURN_TEMPLATE.json ve AI_REVIEW_RETURN_SCHEMA.json yapısını aynen kullan; geçerli JSON dışında yorum/Markdown ekleme. Yalnız anlamlı varyant kararlarını variant_reviews içine koy. IGV önerdiğin her varyantı igv_requests listesine record_key + variant_key + region ile ekle. Sürekli öne çıkan/artefakt değerlendirmelerini artifact_reviews içine koy.',
    ])
    lines.append('')
    spread = pkg.get('pool_spread_review') or {}
    fusion_repeat=pkg.get('fusion_pool_breakpoint_recurrence') or {}
    if fusion_repeat.get('verified_source_samples'):
        lines.append('## RNA füzyon teknik tekrarları (klinik pozitiflik/kanıtlanmış bulaş değildir)')
        lines.append(f"Doğrulanmış füzyon kaynağı olan teknik örnek: {fusion_repeat.get('verified_source_samples')} · en az 3 örnekte aynı partner+breakpoint: {fusion_repeat.get('recurrent_exact_events_ge3')}")
        for signal in (fusion_repeat.get('events') or [])[:15]:
            lines.append(f"- {signal.get('fusion_name')} · aynı iki breakpoint {signal.get('sample_count')} örnekte · {signal.get('breakpoint1')} / {signal.get('breakpoint2')}")
        lines.append('Not: Bu çoklu eşleşmeler PCR/haritalama/çağırıcı arka planı veya başka teknik nedenlerden kaynaklanabilir. Tek başına pozitiflik veya örnekler arası bulaş kanıtı değildir.')
        lines.append('')
    lines.append('## POOL teknik sinyalleri')
    pairs = spread.get('multi_variant_pair_signatures') or []
    highs = spread.get('high_to_low_signals') or []
    if not pairs and not highs:
        lines.append('Ön tanımlı eşiklerle yüksek→düşük veya çoklu-pair sinyali yok.')
    for p in pairs:
        lines.append(f"- **Çoklu imza:** {p.get('source_patient_id')} → {p.get('recipient_patient_id')} · {p.get('shared_high_to_low_variant_count')} varyant")
        for v in p.get('variants') or []:
            lines.append(f"  - {v.get('gene') or ''} {v.get('change') or v.get('variant_key')} · %{v.get('source_af_pct')} → %{v.get('recipient_af_pct')} · {v.get('ratio')}x")
    paired_keys = {(p.get('source_record_key'), p.get('recipient_record_key')) for p in pairs}
    for s in highs:
        if (s.get('source_record_key'), s.get('recipient_record_key')) in paired_keys:
            continue
        lines.append(f"- **Yüksek→düşük:** {s.get('source_patient_id')} → {s.get('recipient_patient_id')} · {s.get('gene') or ''} {s.get('change') or s.get('variant_key')} · %{s.get('source_af_pct')} → %{s.get('recipient_af_pct')} · {s.get('ratio')}x · alıcı filter={s.get('recipient_filter') or '-'}")
    lines.append('')
    fp = pkg.get('all_snv_technical_fingerprint_review') or {}
    lines.append('## Tüm SNV/indel teknik fingerprint')
    lines.append(f"Tüm çağrılarla yüksek→düşük sinyal: {fp.get('high_to_low_signal_total', 0)} · tekrarlayan POOL varyantı: {fp.get('recurrent_pool_variant_total', 0)}")
    for key, label in (('high_to_low_signals_scope', 'Yüksek→düşük liste'),
                       ('recurrent_pool_variants_scope', 'Tekrarlayan varyant listesi')):
        meta = fp.get(key) or {}
        lines.append(f"{label}: aktarılan {meta.get('exported_count', 0)} / toplam {meta.get('total_count')} · kısaltılmış={meta.get('truncated')}")
    for pair in (fp.get('pair_summaries') or [])[:20]:
        if pair.get('high_to_low_directional_count') or pair.get('shared_nonpass_variant_count'):
            rerun = ' · AYNI VAKA/RERUN' if pair.get('same_case_family') else ''
            lines.append(f"- {pair.get('patient_a')} ↔ {pair.get('patient_b')} · exact ortak={pair.get('shared_exact_variant_count')} · non-PASS ortak={pair.get('shared_nonpass_variant_count')} · yüksek→düşük={pair.get('high_to_low_directional_count')}{rerun}")
    lines.append('Not: Bu bölüm benign/unknown çağrıları da teknik imza için kullanır; BAM okuma paylaşımı testi değildir.')
    lines.append('')
    known = pkg.get('known_artifact_history') or []
    lines.append('## Önceki patolog artefakt bilgisi')
    if not known:
        lines.append('Yerel geçmişte patolog tarafından artefakt olarak işaretlenmiş exact varyant yok.')
    else:
        for a in known[:100]:
            lines.append(f"- {a.get('gene') or ''} {a.get('change') or a.get('variant_key')} · {a.get('sample_count')} teknik örnek / {a.get('pool_count')} POOL · hastalar={','.join(a.get('patients') or [])}")
    lines.append('')
    ctx=pkg.get('pool_contamination_review_context') or {}
    lines.append('## POOL hasta teknik / bulaş inceleme özeti')
    for x in ctx.get('samples') or []:
        lines.append(f"- {x.get('patient_id') or '?'} · VerifyBAMID={x.get('verifybamid_contamination_pct') if x.get('verifybamid_contamination_pct') is not None else '-'}% · mean={x.get('mean_coverage_x') or '-'}x · high→low dışa={x.get('high_to_low_outgoing_count',0)} / içe={x.get('high_to_low_incoming_count',0)} · tekrarlayan exact={x.get('recurrent_pool_variant_count',0)} · bilinen artefakt eşleşmesi={len(x.get('known_artifact_hits') or [])}")
    lines.append('Tüm örnek çiftleri JSON içindeki pool_contamination_review_context.all_pair_summaries bölümünde bulunur; yalnızca en dikkat çekici çiftlere değil bütün POOL’a bak.')
    lines.append('')

    lines.append('## Hasta bazında veri')
    for p in pkg.get('patients') or []:
        ident = p.get('identity') or {}
        ts = p.get('technical_sample') or {}
        cc = p.get('clinical_context') or {}
        lines.append(f"### {ident.get('patient_id') or ident.get('case_family') or 'Hasta'} · {ts.get('sample_type') or ''}")
        if ident.get('masked_full_name'):
            lines.append(f"Ad Soyad (maskeli): {ident.get('masked_full_name')}")
        if ident.get('patient_link_id'):
            lines.append(f"Hasta bağlantı kimliği: {ident.get('patient_link_id')} · temel={ident.get('patient_link_id_basis') or '-'}")
        lines.append(f"Tanı: {cc.get('diagnosis') or p.get('diagnosis') or '-'}")
        if cc.get('tumor_type') or cc.get('clinical_diagnosis'):
            lines.append(f"Kaynak tümör tipi: {cc.get('tumor_type') or '-'} · Kaynak klinik tanı: {cc.get('clinical_diagnosis') or '-'}")
        if cc.get('specimen_type') or cc.get('specimen_site'):
            lines.append(f"Numune: {cc.get('specimen_type') or '-'} · Yer: {cc.get('specimen_site') or '-'}")
        lines.append(f"Yaş (tanı/ilk NGS bağlamı): {cc.get('age_at_diagnosis') if cc.get('age_at_diagnosis') is not None else (p.get('age') or '-')} · Cinsiyet: {cc.get('sex') or p.get('sex') or '-'} · Blok: {cc.get('block_id') or ts.get('block_id') or '-'} · Tümör %: {cc.get('tumor_percentage') if cc.get('tumor_percentage') is not None else (ts.get('tumor_percentage') or '-')} · Rerun: {'Evet' if ts.get('is_rerun') else 'Hayır'}")
        if cc.get('missing_context'):
            lines.append('Eksik klinik bağlam (missing_context): ' + ', '.join(cc.get('missing_context') or []))
        if cc.get('sources'):
            lines.append('Klinik bağlam kaynakları: ' + ' · '.join(f'{k}={v}' for k,v in (cc.get('sources') or {}).items()))
        link = p.get('dna_rna_identity_linkage') or {}
        if link.get('matched_samples'):
            pairs = ', '.join(f"{x.get('patient_id') or '?'} ({x.get('sample_type') or '?'}, {x.get('sample_status') or x.get('run_status') or '?'})" for x in link.get('matched_samples') or [])
            lines.append(f"DNA↔RNA eşleşmesi: {pairs}")
        b = p.get('biomarkers') or {}
        lines.append('Biyobelirteçler: ' + ' · '.join(f"{k}={((v or {}).get('raw') or '-') if isinstance(v, dict) else (v or '-')}" for k, v in b.items()))
        hrd_parts = ((b.get('HRD') or {}).get('components') or {})
        if hrd_parts:
            lines.append('HRD alt bileşenleri (Run Detail): ' + ' · '.join(f'{k}={hrd_parts[k]}' for k in ('LOH','TAI','LST') if k in hrd_parts))
        qc = (p.get('qc') or {}).get('summary') or {}
        lines.append('QC: ' + (' · '.join(f'{k}={v}' for k, v in qc.items()) if qc else 'yok/okunamadı'))
        cnv = p.get('cnv') or {}
        lines.append(f"CNV: durum={cnv.get('status') or '-'} · satır={cnv.get('row_count') if cnv.get('row_count') is not None else 'bilinmiyor'} · aktarılan satır={len(cnv.get('calls') or [])}")
        lines.append(f"CNV segmentleri: {cnv.get('segment_count')} · satır kapsamı {cnv.get('exported_count')} / {cnv.get('total_count')} · kısaltılmış={cnv.get('truncated')}. Satır sayısı bağımsız CNV sayısı değildir.")
        gn=p.get('gennext_report_selection') or {}
        lines.append(f"GenNext Analyzed/Selection: {gn.get('result_status') or 'not_scanned'} · "
                     f"Analyzed={gn.get('analyzed')} · SNV/indel secili={gn.get('selected_count')} · "
                     f"ayri aday={gn.get('candidate_count')} · okunma={gn.get('checked_at') or '-'}")
        for v in (gn.get('selected_variants') or []):
            lines.append(f"- GenNext Selection: {v.get('gene')} {v.get('protein') or v.get('cdna')} · {v.get('variant_key')} · onkojenisite={v.get('oncogenicity_display') or 'kaynakta yok'}")
        if gn.get('not_final_variants'):
            lines.append(f"GenNext Selection Rapor disi/adayi (raporlanmis degildir): {len(gn.get('not_final_variants') or [])} varyant")
        if gn.get('last_error'):lines.append('GenNext Selection son kontrol: '+gn['last_error'])
        assay = p.get('report_selection_by_assay') or {}
        for key, label in (('CNV', 'CNV Selection'), ('RNA_FUSION', 'RNA Fusion Selection')):
            selection = assay.get(key) or {}
            if key == 'RNA_FUSION' and ts.get('sample_type') == 'DNA': continue
            lines.append(f"{label}: {selection.get('result_status') or 'okunmadı'} · seçili={selection.get('selected_count')} · doğrulanmış={selection.get('report_selection_verified', False)}")
            for variant in selection.get('selected_variants') or []:
                lines.append(f"- {label}: {variant.get('fusion') or variant.get('gene') or variant.get('variant_key')}")
        igv = p.get('igv_scope_summary') or {}
        if igv:
            lines.append(f"IGV: değerlendirilen {igv.get('reviewed_count')} / istek {igv.get('requested_count')} · tam çekim {igv.get('complete_count')} · kısmi {igv.get('partial_count')} · görüntü eksik {igv.get('missing_count')}")
        cs = p.get('clinical_summary') or {}
        if cs.get('source_file'):
            lines.append(f"Clinical: {cs.get('captured_gene_count') or '?'} / {cs.get('total_genes') or '?'} gen · {cs.get('page_count') or '?'} sayfa")
        vars_ = p.get('oncogenic_and_likely_oncogenic_variants') or []
        if not vars_:
            lines.append('Oncogenic/Likely Oncogenic araştırma indeksi boş veya yok; klinik negatiflik değildir.')
        else:
            lines.append(f'Araştırma için Oncogenic/Likely Oncogenic adayları ({len(vars_)}):')
            for v in vars_:
                ann = v.get('annotations') or {}
                extras = []
                for key, label in [('cDNA','cDNA'),('transcript','Tx'),('cancer_var_tier','CancerVar'),('clinvar','ClinVar'),('cosmic','COSMIC')]:
                    if ann.get(key): extras.append(f"{label}={ann[key]}")
                lines.append(f"- {v.get('classification')} · {v.get('gene') or ''} {v.get('change') or v.get('variant_key')} · AF %{v.get('af_pct') if v.get('af_pct') is not None else '-'} · AD {v.get('ad') or '-'} · DP {v.get('dp') or '-'} · Filter {v.get('filter') or '-'} · Level {v.get('highest_level') or '-'}" + (" · " + ' · '.join(extras) if extras else ''))
        rh=p.get('reported_variant_history') or []
        if rh:
            lines.append(f"Önceki/manuel onaylı raporlanan varyant geçmişi ({len(rh)}):")
            for h in rh[:50]:
                lines.append(f"- {h.get('gene') or ''} {h.get('protein') or h.get('cdna') or h.get('variant_key')} · kaynak={h.get('source')} · {h.get('created_at') or ''}")
        c = p.get('completeness') or {}
        if c.get('status') != 'complete':
            lines.append(f"Paket durumu: {c.get('status')} · eksik={','.join(c.get('missing_or_unparsed') or []) or '-'} · bekleyen={','.join(c.get('waiting') or []) or '-'}")
        lines.append('')
    return '\n'.join(lines).strip() + '\n'


def _write_bundle_archive(outdir: Path, zip_path: Path):
    """Compress final content once; a partial archive is never a ready ZIP."""
    files = sorted(p for p in outdir.rglob('*') if p.is_file())
    partial = zip_path.with_name(zip_path.name + '.partial')
    try:
        with zipfile.ZipFile(partial, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for index, path in enumerate(files, 1):
                report_progress('zip', index, len(files), 'ZIP dosyası hazırlanıyor.')
                zf.write(path, arcname=f'{outdir.name}/{path.relative_to(outdir).as_posix()}')
        os.replace(partial, zip_path)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise


def write_pool_ai_bundle(base: Path, db, cfg: dict, pool_group: str, include_pii=False, include_full_qc=True,
                         sample_scope='DNA', include_source_files=True, *, _defer_archive=False) -> dict:
    compact, full, copy_specs = build_pool_ai_package(
        base, db, cfg, pool_group, include_pii=include_pii, include_full_qc=include_full_qc,
        sample_scope=sample_scope, include_source_files=include_source_files,
    )
    root = Path(base) / str(cfg.get('ai_export_dir') or 'AI_EXPORT')
    root.mkdir(parents=True, exist_ok=True)
    stem = _safe_name(compact['pool_group']) + '_' + _safe_name(compact['sample_scope']) + '_AI_' + _now_stamp()
    if cfg.get('ai_export_job_id'):
        stem += '_' + _safe_name(cfg['ai_export_job_id'])
    outdir = root / stem
    outdir.mkdir(parents=True, exist_ok=False)

    compact_path = outdir / 'POOL_AI_COMPACT.json'
    full_path = outdir / 'POOL_AI_FULL.json'
    prompt_path = outdir / 'POOL_AI_PROMPT.md'
    inventory_path = outdir / 'SOURCE_INVENTORY.json'
    return_template_path = outdir / 'AI_REVIEW_RETURN_TEMPLATE.json'
    return_schema_path = outdir / 'AI_REVIEW_RETURN_SCHEMA.json'
    report_progress('write_json', 1, 2, 'COMPACT JSON yazılıyor.')
    compact_path.write_text(json.dumps(compact, ensure_ascii=False, indent=2), encoding='utf-8')
    report_progress('write_json', 2, 2, 'FULL JSON yazılıyor.')
    full_path.write_text(json.dumps(full, ensure_ascii=False, indent=2), encoding='utf-8')
    report_progress('templates', message='YZ promptu ve dönüş şablonları hazırlanıyor.')
    prompt_path.write_text(build_ai_markdown(compact), encoding='utf-8')
    return_template_path.write_text(json.dumps(build_ai_review_return_template(compact), ensure_ascii=False, indent=2), encoding='utf-8')
    return_schema_path.write_text(json.dumps(ai_review_return_json_schema(), ensure_ascii=False, indent=2), encoding='utf-8')

    inv = []
    for p in compact.get('patients') or []:
        ident = p.get('identity') or {}
        for src in p.get('source_inventory') or []:
            inv.append({'patient_id': ident.get('patient_id'), 'sample_type': (p.get('technical_sample') or {}).get('sample_type'), **src})
    inventory_path.write_text(json.dumps({'pool_group': compact['pool_group'], 'sample_scope': compact['sample_scope'], 'files': inv}, ensure_ascii=False, indent=2), encoding='utf-8')

    if include_source_files:
        for index, (src, arc) in enumerate(copy_specs, 1):
            report_progress('copy_sources', index, len(copy_specs), 'Denetlenen ham kaynaklar pakete ekleniyor.')
            dst = outdir / arc
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

    core_files = [compact_path, full_path, prompt_path, inventory_path, return_template_path, return_schema_path]
    manifest = {
        'schema': BUNDLE_SCHEMA,
        'generator_version': compact.get('generator_version'),
        'versions': compact.get('versions'),
        'pool_group': compact['pool_group'], 'sample_scope': compact['sample_scope'],
        'contains_direct_identifiers': False,
        'direct_identifier_fields': [],
        'contains_masked_names': False,
        'national_id_included': False,
        'patient_link_id_is_hmac': True,
        'clinical_context_pii_independent': True,
        'ai_review_return_schema': 'ngs_ai_review_return_v1',
        'ai_review_return_template_included': True,
        'full_qc_requested': bool(include_full_qc),
        'source_files_included': bool(include_source_files),
        'source_privacy_blocked_count': len(compact.get('source_privacy_blocked') or []),
        'bundle_completeness': compact.get('bundle_completeness'),
        'qc_tables_embedded_sample_count': sum(1 for p in full.get('patients') or [] if ((p.get('qc') or {}).get('raw_multiqc_tables'))),
        'files': [],
        'privacy_note': 'Names, national IDs, birth dates and account emails are removed. MP/protocol, age, sex, diagnosis, block and tumor percentage remain. Raw sources with detected identifiers or incomplete privacy scans are omitted; structured context remains. Patient linkage and block linkage are independent.',
    }
    if not _defer_archive:
        for p in core_files:
            manifest['files'].append({'name': p.name, 'size_bytes': p.stat().st_size, 'sha256': _sha256(p)})
    if include_source_files:
        manifest['source_file_count'] = len(copy_specs)
    manifest_path = outdir / 'MANIFEST.json'
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')

    zip_path = root / (stem + '.zip')
    if not _defer_archive:
        _write_bundle_archive(outdir, zip_path)
    return {'folder': str(outdir), 'zip': str(zip_path), 'prompt': str(prompt_path), 'compact': str(compact_path), 'full': str(full_path), 'package': compact}


class AIPackageExporter(ttk.Frame):
    def __init__(self, parent, base, db, cfg, reindex_callback=None):
        super().__init__(parent, padding=10)
        self.base = Path(base); self.db = db; self.cfg = cfg; self.reindex_callback = reindex_callback; self.last_result = None
        self._generation_busy = False
        self._export_job = None
        self._export_poll_id = None
        self._export_started = 0.0
        self._export_scope = 'DNA'
        self._destroyed = False
        self._follow_latest_pool = True
        self._export_pool_options = {}
        self._build(); self.refresh_pools()
        self.bind('<Destroy>', self._on_export_destroy, add='+')

    def _build(self):
        top = ttk.LabelFrame(self, text='POOL → YZ değerlendirme paketi', padding=10); top.pack(fill='x')
        ttk.Label(top, text='POOL:').grid(row=0, column=0, sticky='w')
        self.pool_var = tk.StringVar(); self.pool_box = ttk.Combobox(top, textvariable=self.pool_var, state='readonly', width=24)
        self.pool_box.grid(row=0, column=1, sticky='w', padx=(6, 14))
        self.pool_box.bind('<<ComboboxSelected>>', lambda e: setattr(self,'_follow_latest_pool',False))
        ttk.Label(top, text='Örnek türü:').grid(row=0, column=2, sticky='w')
        self.scope_var = tk.StringVar(value=str(self.cfg.get('ai_sample_scope_default') or 'DNA'))
        self.scope_box = ttk.Combobox(top, textvariable=self.scope_var, state='readonly', values=SCOPE_VALUES, width=10)
        self.scope_box.grid(row=0, column=3, sticky='w', padx=(6, 14))
        self.pii_var = tk.BooleanVar(value=False)
        self.full_qc_var = tk.BooleanVar(value=bool(self.cfg.get('ai_full_qc_default', True)))
        self.source_files_var = tk.BooleanVar(value=bool(self.cfg.get('ai_include_source_files_default', True)))
        ttk.Label(top, text='Ad / TC aktarılmaz').grid(row=1, column=0, columnspan=2, sticky='w', padx=6, pady=(10,0))
        ttk.Checkbutton(top, text='Tam MultiQC tabloları', variable=self.full_qc_var).grid(row=1, column=2, columnspan=2, sticky='w', padx=6, pady=(10,0))
        ttk.Checkbutton(top, text='Ham kaynak dosyaları ZIP’e ekle', variable=self.source_files_var).grid(row=1, column=4, columnspan=2, sticky='w', padx=6, pady=(10,0))
        self.generate_button = ttk.Button(top, text='YZ PAKETİ OLUŞTUR', command=self.generate)
        self.generate_button.grid(row=0, column=5, sticky='e', padx=(16, 4))
        top.columnconfigure(4, weight=1)

        self.export_tabs = ttk.Notebook(self)
        prompt_page = ttk.Frame(self.export_tabs, padding=8)
        note = ttk.Frame(self.export_tabs, padding=16)
        self.export_tabs.add(prompt_page,text='  Prompt Önizlemesi  ')
        self.export_tabs.add(note,text='  Paket Kapsamı / Güvenlik  ')
        txt = ('Klinik bağlam ve TMB/MSI/HRD, seçilen teknik çalışmanın kalıcı alan kaydından alınır; manuel düzeltme ve kaynak farkları korunur. '
               'QC ise MultiQC TSV/CSV/JSON Data paketinden okunur. SNV anotasyonları zenginleştirilir, CNV ayrı yapılandırılmış bölümde verilir, '
               'POOL teknik sinyalleri seçilen örneklerde hesaplanır. Doğrulanmış yerel kimlik DNA/RNA hasta bağlantısı sağlayabilir; blok ayrıca doğrulanır. Hasta/doktor adları, TC ve e-posta aktarılmaz. SOURCE_FILES seçeneği Clinical/SNV/CNV/QC/Fusion '
               'kaynaklarını kimlik denetiminden sonra pakete ekler. Yaş, cinsiyet, tanı, blok ve tümör yüzdesi eksikse tamamlama ekranı açılır; teknik ön inceleme olarak devam edilebilir. Selection kategorileri ve IGV kapsamı ayrı gösterilir. YZ değerlendirmesi AI_REVIEW_RETURN.json olarak içe alınır.')
        scope_note = ttk.Label(note, text=txt, wraplength=1050, justify='left')
        scope_note.pack(fill='x')
        note.bind('<Configure>',lambda e:scope_note.configure(wraplength=max(300,e.width-40)))
        ttk.Label(note, text='Paket oluşturulmadan önce bütünlük denetlenir; eksik TMB/MSI/HRD, okunamayan QC/CNV veya eksik kaynak “negatif” kabul edilmez.', foreground='#8a4b08').pack(anchor='w', pady=(5, 0))

        actions = ttk.Frame(self); actions.pack(fill='x', pady=(0, 6))
        ttk.Button(actions, text='Son Promptu Panoya Kopyala', command=self.copy_prompt).pack(side='left')
        ttk.Button(actions, text='AI_EXPORT Klasörünü Aç', command=self.open_folder).pack(side='left', padx=6)
        ttk.Button(actions, text='POOL Listesini Yenile', command=self.refresh_pools).pack(side='left', padx=6)
        ttk.Button(actions, text='POOL klinik bilgileri', command=self.open_clinical_completion).pack(side='left', padx=6)
        self.cancel_button = ttk.Button(actions, text='Üretimi İptal Et', command=self.cancel_generation, state='disabled')
        self.cancel_button.pack(side='left', padx=6)
        progress = ttk.Frame(self)
        progress.pack(fill='x', pady=(0, 6))
        self.status = tk.StringVar(value='POOL ve örnek türünü seçip paket oluşturun.')
        ttk.Label(progress, textvariable=self.status, foreground='#475569', wraplength=1350, justify='left').pack(fill='x')
        self.progress_bar = ttk.Progressbar(progress, mode='indeterminate')
        self.progress_bar.pack(fill='x', pady=(4, 0))

        self.preview = tk.Text(prompt_page, wrap='word', bg='#0f172a', fg='#e2e8f0', insertbackground='white', font=('Consolas', 9), relief='flat')
        self.export_tabs.pack(fill='both',expand=True,pady=(10,0))
        self.preview.pack(fill='both', expand=True); self.preview.insert('1.0', 'YZ prompt önizlemesi burada görünecek.\n')

    def refresh_pools(self,prefer_latest=False):
        choices = ai_export_run_choices(self.db)
        self._export_pool_options={label:(group,runs) for label,group,runs in choices}
        labels=[label for label,_,_ in choices]
        latest_uuids = {str(r.get('run_uuid') or '') for r in (self.db.latest_scan_group() or [])}
        latest_label = next((label for label,_,runs in choices if latest_uuids.intersection(runs)),None)
        if prefer_latest:
            self._follow_latest_pool=True
        self.pool_box['values'] = labels
        selected=self.pool_var.get()
        if self._follow_latest_pool or selected not in labels:
            selected=latest_label or (labels[0] if labels else '')
        if selected!=self.pool_var.get():
            self.pool_var.set(selected)

    def generate(self):
        if self._generation_busy or self._destroyed:
            return
        selected_label = self.pool_var.get().strip(); scope = self.scope_var.get().strip() or 'DNA'
        choice = self._export_pool_options.get(selected_label)
        if not choice:
            messagebox.showwarning('YZ Paketi', 'Geçerli bir POOL/RUN seçin.', parent=self); return
        pool, selected_run_uuids = choice
        selected_keys=[s['record_key'] for ru in selected_run_uuids for s in self.db.samples_for_run(ru)
                       if _scope_match(s['sample_type'],scope)]
        if not selected_keys:
            messagebox.showwarning('YZ Paketi', 'Bu RUN için seçilen DNA/RNA türünde örnek yok.', parent=self); return
        self._generation_busy = True
        self._set_generation_controls(True)
        try:
            # Clinical edits are completed once on Tk, before taking a snapshot.
            # The busy flag also guards re-entry while this modal is open.
            from clinical_completion import complete_before_export
            self.status.set('Klinik bilgiler denetleniyor…')
            if not complete_before_export(self, self.db, pool, scope, record_keys=selected_keys):
                self.status.set('YZ paketi oluşturulmadı; klinik bilgi girişi iptal edildi.')
                self._generation_busy = False
                self._set_generation_controls(False)
                return
            if self._destroyed:
                return
            from ai_export_job import ExportJob
            self._export_scope = scope
            self._export_started = time.monotonic()
            self._export_job = ExportJob(self.base, self.db.path, self.cfg, {
                'pool_group': pool, 'sample_scope': scope,
                'selected_run_uuids': list(selected_run_uuids),
                'include_full_qc': self.full_qc_var.get(),
                'include_source_files': self.source_files_var.get(),
            })
            self._export_job.start()
            self._set_generation_controls(True)
            self.status.set(f'{pool} / {scope} YZ paketi hazırlanıyor… · Geçen süre 00:00')
            self._show_last_package_link(generating=True)
            self._export_poll_id = self.after(100, self._poll_export_job)
        except Exception as exc:
            if self._export_job is not None:
                self._export_job.cancel()
                self._export_job = None
            self._generation_busy = False
            if not self._destroyed:
                self._set_generation_controls(False)
                self.status.set('Paket başlatılamadı: ' + str(exc))
                messagebox.showerror('YZ Paketi', str(exc), parent=self)

    def _set_generation_controls(self, busy):
        self.generate_button.configure(state='disabled' if busy else 'normal')
        self.cancel_button.configure(state='normal' if busy and self._export_job is not None else 'disabled')
        if busy and self._export_job is not None:
            self.progress_bar.configure(mode='indeterminate', maximum=100)
            self.progress_bar.start(15)
        elif not busy:
            self.progress_bar.stop()
            self.progress_bar.configure(mode='determinate', value=0)

    def _show_last_package_link(self, generating=False):
        if not hasattr(self, 'output_link_var'):
            return
        if self.last_result and self.last_result.get('zip'):
            prefix = 'Son tamamlanan ZIP (yeni paket hazırlanıyor): ' if generating else 'YZ’ye gönderilecek ZIP: '
            self.output_link_var.set(prefix + str(self.last_result['zip']))
        else:
            self.output_link_var.set('YZ’ye gönderilecek ZIP: hazırlanıyor…' if generating else 'YZ’ye gönderilecek ZIP: henüz oluşturulmadı')

    def _poll_export_job(self):
        self._export_poll_id = None
        if self._destroyed or self._export_job is None:
            return
        try:
            status = self._export_job.poll()
        except Exception as exc:
            self._export_job.cancel()
            status = {'state': 'failed', 'message': 'Paket durumu okunamadı: ' + str(exc)}
        state = status.get('state')
        if state == 'running':
            elapsed = max(0, int(time.monotonic() - self._export_started))
            count = ''
            current, total = status.get('current'), status.get('total')
            if isinstance(current, (int, float)) and isinstance(total, (int, float)) and total > 0:
                count = f' · {int(current)}/{int(total)}'
                self.progress_bar.stop()
                self.progress_bar.configure(mode='determinate', maximum=total, value=current)
            elif str(self.progress_bar['mode']) != 'indeterminate':
                self.progress_bar.configure(mode='indeterminate', maximum=100)
                self.progress_bar.start(15)
            self.status.set((status.get('message') or 'Paket hazırlanıyor…') + count
                            + f' · Geçen süre {elapsed // 60:02d}:{elapsed % 60:02d}'
                            + (' · 10 dakika aşıldı; üretim sürüyor, otomatik iptal yok.' if elapsed >= 600 and not self._export_job.timeout_seconds else ''))
            self._export_poll_id = self.after(150, self._poll_export_job)
            return
        self._export_job = None
        self._generation_busy = False
        self._set_generation_controls(False)
        if state == 'succeeded':
            result = status.get('result') or {}
            try:
                outputs_exist = (bool(result.get('zip')) and bool(result.get('prompt'))
                                 and export_filesystem_path(result['zip']).is_file() and export_filesystem_path(result['zip']).stat().st_size > 0
                                 and export_filesystem_path(result['prompt']).is_file())
            except (OSError, TypeError, ValueError):
                outputs_exist = False
            if not outputs_exist:
                self.status.set('Paket sonuç dosyası eksik; yeni paket hazır kabul edilmedi.')
                self._show_last_package_link()
                messagebox.showerror('YZ Paketi', self.status.get(), parent=self)
                return
            self.last_result = result
            self._show_last_package_link()
            try:
                with export_filesystem_path(result['prompt']).open(encoding='utf-8') as stream:
                    prompt = stream.read(100001)
                if len(prompt) > 100000:
                    prompt = prompt[:100000] + '\n\n[Önizleme sınırı. Tam prompt ZIP içindeki POOL_AI_PROMPT.md dosyasındadır.]\n'
                self.preview.delete('1.0', 'end')
                self.preview.insert('1.0', prompt)
            except (OSError, UnicodeError) as exc:
                self.preview.delete('1.0', 'end')
                self.preview.insert('1.0', 'Paket hazır; prompt önizlemesi açılamadı: ' + str(exc))
            comp = (result.get('package') or {}).get('bundle_completeness') or {}
            elapsed = max(0, int(time.monotonic() - self._export_started))
            self.progress_bar.configure(maximum=1, value=1)
            self.status.set(f"Hazır · ZIP doğrulandı · {elapsed // 60:02d}:{elapsed % 60:02d} · bütünlük={comp.get('status') or 'bilinmiyor'}")
            messagebox.showinfo('YZ Paketi', f"Hazır:\n{result['zip']}\n\nKapsam: {self._export_scope}\nPaket bütünlüğü: {str(comp.get('status') or '').upper()}\n\nYZ'den AI_REVIEW_RETURN.json dosyasını isteyin; dönüşü YZ Dönüş / IGV sekmesinden içe alın.", parent=self)
        else:
            self.status.set(status.get('message') or 'Paket oluşturulamadı; önceki paket korundu.')
            self._show_last_package_link()
            if state != 'cancelled':
                messagebox.showerror('YZ Paketi', self.status.get(), parent=self)

    def cancel_generation(self):
        if self._export_job is not None:
            self._export_job.cancel()
            if self._export_poll_id is not None:
                self.after_cancel(self._export_poll_id)
                self._export_poll_id = None
            self._poll_export_job()

    def _on_export_destroy(self, event):
        if event.widget is not self:
            return
        self._destroyed = True
        if self._export_poll_id is not None:
            try:
                self.after_cancel(self._export_poll_id)
            except tk.TclError:
                pass
            self._export_poll_id = None
        if self._export_job is not None:
            self._export_job.cancel()
            self._export_job = None

    def copy_prompt(self):
        if not self.last_result:
            messagebox.showwarning('YZ Paketi', 'Önce paket oluşturun.'); return
        try:
            text = export_filesystem_path(self.last_result['prompt']).read_text(encoding='utf-8')
            self.clipboard_clear(); self.clipboard_append(text); self.status.set('Prompt panoya kopyalandı.')
        except Exception as e:
            messagebox.showerror('YZ Paketi', str(e))

    def open_folder(self):
        root = self.base / str(self.cfg.get('ai_export_dir') or 'AI_EXPORT'); root.mkdir(parents=True, exist_ok=True)
        try: os.startfile(str(root))
        except Exception: pass

    def open_clinical_completion(self):
        from clinical_completion import open_pool_completion
        try:
            choice=self._export_pool_options.get(self.pool_var.get().strip())
            if choice:
                record_keys=[s['record_key'] for ru in choice[1] for s in self.db.samples_for_run(ru)]
                open_pool_completion(self,self.db,record_keys=record_keys)
            else:
                open_pool_completion(self,self.db,pool_group=None,record_keys=[])
        except Exception as exc:
            messagebox.showerror('Klinik bilgiler', str(exc), parent=self)

# === NGS_AI_EXPORT_LINK_V0432 ===
_ngs_original_write_pool_ai_bundle_v0432 = write_pool_ai_bundle


def write_pool_ai_bundle(*args, **kwargs):
    # V2 templates and prompt changes must precede the only compression pass.
    kwargs['_defer_archive'] = True
    result = _ngs_original_write_pool_ai_bundle_v0432(*args, **kwargs)
    from ai_review import build_igv_request_template, igv_request_json_schema, validate_generated_templates, RETURN_SCHEMA_V2, IGV_REQUEST_SCHEMA_V2
    out=Path(result['folder']);pkg=result['package']
    report_progress('validate_templates', message='Dönüş ve IGV şablonları doğrulanıyor.')
    validation=validate_generated_templates(pkg)
    if validation.get('schema_validation')!='PASS' or validation.get('cross_reference_validation')!='PASS' or validation.get('duplicate_key_validation')!='PASS':
        raise ValueError('YZ JSON template doğrulaması başarısız: '+json.dumps(validation,ensure_ascii=False))
    igv_template=out/'IGV_REQUEST_V2_TEMPLATE.json';igv_schema=out/'IGV_REQUEST_V2_SCHEMA.json';validation_path=out/'JSON_GENERATOR_VALIDATION.json'
    igv_template.write_text(json.dumps(build_igv_request_template(pkg),ensure_ascii=False,indent=2),encoding='utf-8')
    igv_schema.write_text(json.dumps(igv_request_json_schema(),ensure_ascii=False,indent=2),encoding='utf-8')
    validation_path.write_text(json.dumps(validation,ensure_ascii=False,indent=2),encoding='utf-8')
    prompt=Path(result['prompt'])
    with prompt.open('a',encoding='utf-8') as f:
        f.write('\n\n## JSON V2 zorunlu kurallar\n')
        f.write('- AI_REVIEW_RETURN için document_type=AI_REVIEW_RETURN ve schema=ngs_ai_review_return_v2 kullan.\n')
        f.write('- Yeni IGV görevi gerekiyorsa benzersiz request_id + request_revision üret; variant_key boş bırakılamaz.\n')
        f.write('- Tekrar görüntü için execution.force_new_task=true kullan. Caller-missed taramada sentetik MANUAL_SCAN variant_key üret.\n')
        f.write('- IGV_REQUEST_V2_TEMPLATE.json yalnız yeni IGV görev belgesidir; AI_REVIEW_RETURN ile karıştırma.\n')
        f.write('- pileup/coverage alanlarında ölçülmeyen değeri uydurma; null bırak.\n')
    manifest_path=out/'MANIFEST.json'
    try: manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
    except Exception: manifest={}
    manifest['ai_review_return_schema']=RETURN_SCHEMA_V2;manifest['igv_request_schema']=IGV_REQUEST_SCHEMA_V2
    manifest['json_generator_validation']=validation
    # All content must be finalised BEFORE computing SHA-256/size; previous
    # manifest hashes were stale after the V2 prompt was appended.
    final_files = sorted(p for p in out.rglob('*') if p.is_file() and p != manifest_path)
    manifest['files'] = []
    for index, p in enumerate(final_files, 1):
        report_progress('manifest', index, len(final_files), 'Paket dosyalarının bütünlük kaydı hazırlanıyor.')
        manifest['files'].append({'name': p.relative_to(out).as_posix(), 'size_bytes': p.stat().st_size, 'sha256': _sha256(p)})
    manifest['manifest_scope']='all_final_files_except_manifest_itself'
    manifest_path.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    zpath=Path(result['zip'])
    _write_bundle_archive(out, zpath)
    root=zpath.parent
    try:(root/'SON_YZ_PAKETI.txt').write_text(str(zpath),encoding='utf-8')
    except Exception:pass
    result['igv_request_template']=str(igv_template);result['json_validation']=validation
    return result


_NGSOriginalAIPackageExporterV0432 = AIPackageExporter
class AIPackageExporter(_NGSOriginalAIPackageExporterV0432):
    def _build(self):
        super()._build()
        self.output_link_var=tk.StringVar(value='YZ’ye gönderilecek ZIP: henüz oluşturulmadı')
        bar=ttk.LabelFrame(self,text='YZ’ye gönderilecek dosya',padding=7)
        try: bar.pack(fill='x',pady=(0,6),before=self.export_tabs)
        except Exception: bar.pack(fill='x',pady=(0,6))
        bar.columnconfigure(0,weight=1)
        self.output_link=tk.Label(bar,textvariable=self.output_link_var,fg='#0563c1',cursor='hand2',anchor='w')
        self.output_link.grid(row=0,column=0,sticky='ew');self.output_link.bind('<Button-1>',lambda e:self.open_last_package())
        ttk.Button(bar,text='Dosyayı Aç',command=self.open_last_package).grid(row=0,column=1,padx=(8,3))
        ttk.Button(bar,text='Klasörü Aç',command=self.open_last_package_folder).grid(row=0,column=2,padx=3)
        ttk.Button(bar,text='Yolu Kopyala',command=self.copy_last_package_path).grid(row=0,column=3,padx=3)

    def generate(self):
        super().generate()

    def open_last_package(self):
        if not self.last_result or not self.last_result.get('zip'): messagebox.showwarning('YZ Paketi','Önce paket oluşturun.');return
        p=Path(self.last_result['zip'])
        try: os.startfile(str(p))
        except Exception:
            try: os.startfile(str(p.parent))
            except Exception: pass

    def open_last_package_folder(self):
        if not self.last_result or not self.last_result.get('zip'): messagebox.showwarning('YZ Paketi','Önce paket oluşturun.');return
        try: os.startfile(str(Path(self.last_result['zip']).parent))
        except Exception: pass

    def copy_last_package_path(self):
        if not self.last_result or not self.last_result.get('zip'): messagebox.showwarning('YZ Paketi','Önce paket oluşturun.');return
        self.clipboard_clear();self.clipboard_append(str(self.last_result['zip']));self.status.set('YZ ZIP yolu panoya kopyalandı.')

# === NGS_AI_SOURCE_IDENTITY_V0433 ===
_ngs_build_pool_ai_package_v0433_prev=build_pool_ai_package


def build_pool_ai_package(base: Path, db, cfg: dict, pool_group: str, include_pii: bool=False,
                          include_full_qc: bool=True, sample_scope: str='DNA', include_source_files: bool=True):
    compact,full,copy_specs=_ngs_build_pool_ai_package_v0433_prev(base,db,cfg,pool_group,include_pii=include_pii,include_full_qc=include_full_qc,sample_scope=sample_scope,include_source_files=include_source_files)
    from pileup_engine import canonical_build, source_identity_for_request, compute_pileup_and_coverage, GENERATOR_VERSION as READ_EVIDENCE_VERSION
    build=canonical_build(cfg.get('genome_build') or 'hg38');created=compact.get('generated_at') or time.strftime('%Y-%m-%dT%H:%M:%S')
    evidence_by_pair={}
    for doc in (compact,full):
        doc['schema_version']=10;doc['generator_version']=_application_version(Path(base));doc['genome_build']=build;doc['created_at']=created;doc['read_evidence_engine_version']=READ_EVIDENCE_VERSION
        for patient in doc.get('patients') or []:
            rk=str((patient.get('technical_sample') or {}).get('record_key') or '')
            if rk:
                try:patient['read_evidence_sources']=source_identity_for_request(Path(base),db,cfg,rk)
                except Exception as exc:patient['read_evidence_sources']={'alignment':{'exists':False,'status':'resolution_error','error':str(exc)},'bed':{'exists':False,'status':'resolution_error','error':str(exc)}}
            if not rk or not bool(cfg.get('pileup_enrich_ai_export',True)):continue
            for v in patient.get('oncogenic_and_likely_oncogenic_variants') or []:
                if not isinstance(v,dict):continue
                vk=str(v.get('variant_key') or '');pair=(rk,vk)
                if pair not in evidence_by_pair:
                    coords={'genome_build':build,'chrom':v.get('chrom') or '','position':v.get('start') or 0,'ref':v.get('ref') or '','alt':v.get('alt') or '',
                            'region':v.get('igv_region') or '','sort_anchor':v.get('start') or 0}
                    try:evidence_by_pair[pair]=compute_pileup_and_coverage(Path(base),db,cfg,rk,coordinates=coords,region=v.get('igv_region') or '',variant_key=vk,source_call=v)
                    except Exception as exc:evidence_by_pair[pair]={'pileup':{'status':'analysis_error','error':str(exc),'dp':None,'alt_ad':None,'vaf':None},'coverage_audit':{'target_status':'unknown'},'sources':patient.get('read_evidence_sources') or {}}
                ev=evidence_by_pair[pair];v['pileup']=ev.get('pileup') or {};v['coverage_audit']=ev.get('coverage_audit') or {};v['read_evidence_sources']=ev.get('sources') or patient.get('read_evidence_sources') or {}
    return compact,full,copy_specs


# === NGS_PREVIOUS_REPORT_HISTORY_V0467 ===
# Keep original v0.4.66 pipeline and all QC / fusion / IGV validation rules.
_NGS_PreviousBuildPoolV0467 = build_pool_ai_package
_NGS_PreviousBuildMarkdownV0467 = build_ai_markdown


def _finalize_ai_exports(compact, full, source_specs, base, db):
    from igv_workflow_v0481 import igv_scope_summary
    samples = [dict(row) for row in db.samples()]
    names, national_ids = _privacy_sensitive_values(db, samples)
    redactor = ExportPrivacyRedactor(names, national_ids)
    summaries = {}
    included = {arc for _, arc in source_specs}
    for doc in (compact, full):
        doc['versions'] = {'application': _application_version(base),
                           'ai_export': EXPORT_COMPONENT_VERSION,
                           'ai_export_schema': AI_SCHEMA,
                           'read_evidence_engine': doc.get('read_evidence_engine_version')}
        doc['generator_version'] = doc['versions']['application']
        doc['export_purpose'] = 'technical_pre_review'
        doc['clinical_report_release'] = False
        doc['contains_direct_identifiers'] = False
        doc['contains_masked_names'] = False
        doc['source_files_in_bundle'] = bool(source_specs)
        for patient in doc.get('patients') or []:
            rk = (patient.get('technical_sample') or {}).get('record_key')
            if rk not in summaries:
                try:
                    summaries[rk] = igv_scope_summary(db, record_key=rk)
                except Exception as exc:
                    summaries[rk] = {'requested_count': None, 'captured_count': None,
                                     'reviewed_count': None, 'status': 'summary_unavailable',
                                     'error': str(exc), 'details': [], 'clinical_report_release': False}
            patient['igv_scope_summary'] = summaries[rk]
            patient['interpretation_limits'] = {
                'vaf_vs_tumor_percentage': 'Contextual technical review only; no tumor_percent/2 rejection or selection rule.',
                'report_selection': 'SNV/indel, CNV and RNA Fusion are independent; unknown selection is null.',
                'cnv': 'Separate segment events, gene/CDS overlap and parent-segment probes; GAIN is not automatically high-level amplification.',
                'igv': 'Evidence applies only to the identified request and variant; a partial capture is not clinical release.',
            }
            for item in patient.get('source_inventory') or []:
                item['included_in_bundle'] = item.get('bundle_path') in included
    report_progress('masking', 1, 2, 'COMPACT kimlik alanları temizleniyor.')
    compact = _sanitize_ai_object(compact, names, national_ids, _redactor=redactor)
    report_progress('masking', 2, 2, 'FULL ve tam MultiQC kimlik alanları temizleniyor.')
    full = _sanitize_ai_object(full, names, national_ids, _redactor=redactor)
    return compact, full, source_specs


def build_pool_ai_package(base: Path, db, cfg: dict, pool_group: str, include_pii: bool=False,
                          include_full_qc: bool=True, sample_scope: str='DNA', include_source_files: bool=True):
    compact, full, sources = _NGS_PreviousBuildPoolV0467(
        base, db, cfg, pool_group, include_pii=include_pii, include_full_qc=include_full_qc,
        sample_scope=sample_scope, include_source_files=include_source_files)
    from patient_ngs_history import find_previous_ngs, ai_history_context
    # The patient-name list is purely local; NEVER put it into the JSON output.
    known_case_info = [dict(r) for r in db.cx.execute('SELECT full_name,national_id FROM case_metadata')]
    known_names = [str(r['full_name']).strip() for r in known_case_info if str(r.get('full_name') or '').strip()]
    known_ids = [str(r['national_id']).strip() for r in known_case_info if db.validate_national_id(str(r.get('national_id') or '').strip())]
    history_redactor = ExportPrivacyRedactor(known_names, known_ids)
    cases = {}
    for doc in (compact, full):
        doc['generator_version'] = _application_version(Path(base))
        doc['prior_ngs_history_schema'] = 'ngs_prior_ngs_history_v1'
        for patient in doc.get('patients') or []:
            rk = str((patient.get('technical_sample') or {}).get('record_key') or '')
            if rk not in cases:
                if rk:
                    report_progress('history', len(cases) + 1, len(compact.get('patients') or []), 'Önceki yerel NGS bağlamı hazırlanıyor.')
                    history = find_previous_ngs(db, rk, base=base)
                    safe = ai_history_context(history)
                    for old in safe.get('previous_reports') or []:
                        for field in ('diagnosis', 'block_id'):
                            value = history_redactor.redact(old.get(field) or '')
                            # Generic identifier defenses on free-form diagnosis fields.
                            value = re.sub(r'(?<!\d)\d{9,15}(?!\d)', '[KİMLİK GİZLENDİ]', value)
                            value = redact_email_addresses(value)
                            old[field] = value
                    cases[rk] = safe
                else:
                    cases[rk] = {'schema':'ngs_prior_ngs_history_v1','status':'record_key_missing',
                                 'previous_report_count':0,'previous_reports':[],'clinical_review_prompts':[]}
            patient['prior_ngs_history'] = json.loads(json.dumps(cases[rk],ensure_ascii=False))
    compact['historical_reporting_policy'] = (
      'Prior NGS reports are clinical HISTORY only. No prior variant automatically becomes a new positive call. '
      'Separate source statuses: locally confirmed reports, standard-text drafts, legacy shared forms, manually pasted unverified Enlil copies, and structured confirmed variants. Only machine-readable extracted markers are exported; free old report text, TC, file no and names never leave the local PC. '
      'File-number-only matches require identity review; local user-confirmed is not ENLIL electronic signature.')
    full['historical_reporting_policy'] = compact['historical_reporting_policy']
    return _finalize_ai_exports(compact, full, sources, Path(base), db)


def build_ai_markdown(pkg: dict) -> str:
    original = _NGS_PreviousBuildMarkdownV0467(pkg)
    lines = ['## ÖNCEKİ NGS RAPORLARI · TC / DOSYA NO İLE YEREL EŞLEŞME',
             'Önceki genotipler GÜNCEL POZİTİF DEĞİLDİR. Eski rapor e-imzası doğrulanmamıştır.']
    for patient in pkg.get('patients') or []:
        technical = patient.get('technical_sample') or {}
        ident = patient.get('identity') or {}
        h = patient.get('prior_ngs_history') or {}
        lines.append('### ' + str(ident.get('patient_id') or '?') + ' / ' + str(technical.get('sample_type') or ''))
        old = h.get('previous_reports') or []
        if not old:
            lines.append('Önceki yerel raporlanmış NGS kaydı bulunmadı / kimlik yetersiz. Bu, önceki NGS negatifliği anlamına gelmez.')
        for r in old[:25]:
            label = 'doğrulanmış TC' if r.get('identity_verified') else 'DOSYA NO — KİMLİK DOĞRULA'
            lines.append('- '+str(r.get('prior_mp') or '?')+' '+str(r.get('sample_type') or '')+' | '+label
                         +' | '+str(r.get('report_status') or '')+' | '+str(r.get('diagnosis') or '-'))
            for v in (r.get('structured_reported_variants') or [])[:30]:
                lines.append('  - Geçmiş yapılandırılmış bulgu (YENİ SONUÇ DEĞİL): '+str(v.get('gene') or '')+' '+
                    str(v.get('protein') or v.get('cdna') or v.get('feature') or ''))
            extraction=r.get('historical_text_extraction') or {}
            for v in (extraction.get('positive_variants') or [])[:25]:
                lines.append('  - RAPOR METNİNDEN OKUNAN (kaynak statüsünü kontrol et): '+str(v.get('gene') or '')+' '+
                    str(v.get('transcript') or '')+' '+str(v.get('cdna') or '')+' '+str(v.get('protein') or ''))
            if extraction.get('negative_genes'):
                lines.append('  - RAPORDA AÇIKÇA NEGATİF: '+', '.join(extraction['negative_genes'][:40]))
            markers=[(k,extraction.get(k)) for k in ('msi_status','msi_percent','tmb','tmb_category','hrd')
                     if extraction.get(k) is not None]
            if markers:lines.append('  - Eski rapordan biyobelirteçler: '+', '.join(str(k)+'='+str(v) for k,v in markers))
        for prompt in (h.get('clinical_review_prompts') or [])[:7]:
            lines.append('- Klinik karşılaştırma: '+prompt)
    return original.rstrip() + '\n\n' + '\n'.join(lines) + '\n'

# BEGIN NGS_VARIANT_EVIDENCE_v1_1_install_ai_export
from variant_evidence_integration import install_ai_export as _install_ngs_variant_evidence
_install_ngs_variant_evidence(globals())
# END NGS_VARIANT_EVIDENCE_v1_1_install_ai_export
# NGS_WEB_RECOVERY_v1_2: manifest öncesi eksik kaynak yan dosyası
from web_recovery_ai import install_recovery_bundle as _install_recovery_bundle
_install_recovery_bundle(globals())

