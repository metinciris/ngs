# -*- coding: utf-8 -*-
"""Optional, read-only, exact-record DNA coverage enrichment for the Boyama preview.

The base qc_enlil.SampleMetrics.report_sentence() is intentionally untouched.
The verified QC code already installed with Bilgi Atlasi v1.3+ is reused to
avoid choosing the first row in a multi-tool MultiQC general_stats_table.tsv.
No database writes, GenNext requests or automatic clinical validation.
"""
from __future__ import annotations

from functools import lru_cache
import math
from pathlib import Path
import re
from typing import Any

_MP = re.compile(r"(?i)MP\s*0*(\d{1,5})\s*[-_/]+\s*(?:20)?(\d{2})(?!\d)")


def _norm_mp(value: Any) -> str:
    match = _MP.search(str(value or ''))
    return ('MP%s-%s' % (int(match.group(1)), match.group(2))) if match else ''


def _positive(v: Any, low: float = 0, high: float = 100000, inclusive_low: bool = False) -> bool:
    return (isinstance(v, (int, float)) and math.isfinite(float(v)) and
            (low <= float(v) if inclusive_low else low < float(v)) and float(v) <= high)


def _display(v: float) -> str:
    # Turkish decimal separator; do not infer precision not in the source.
    s = ('%.2f' % float(v)).replace('.', ',')
    return s[:-3] if s.endswith(',00') else s


def _same(a: float, b: float, tolerance: float = 0.06) -> bool:
    return abs(float(a) - float(b)) <= tolerance


def _path(base: Path, raw: str) -> Path | None:
    try:
        path = Path(raw)
        if not path.is_absolute():
            path = Path(base) / path
            path.resolve().relative_to(Path(base).resolve())
        return path.resolve() if path.is_file() else None
    except (OSError, ValueError, RuntimeError, TypeError):
        return None


@lru_cache(maxsize=256)
def _atlas_qc_cached(path_string: str, file_size: int, mtime_ns: int, patient: str) -> dict:
    # No use of GenNext samples.depth or per-variant DP.
    from ngs_bilgi_atlasi_detay import qc_sources
    path = Path(path_string)
    # qc_sources checks the exact MP row in each MultiQC member independently.
    rows = qc_sources(path.parent, [{'file_type': 'qc', 'path': str(path)}],
                      {'sample_type': 'DNA', 'patient_id': patient})
    return dict(rows[0]) if len(rows) == 1 else {}


def _source_is_active(candidate: Path, selected_paths) -> bool:
    """Selected files *and folders* are accepted; no filename-only path guessing.

    The main NGS panel automatically loads QC ZIPs using collector paths or
    user-selected root folders. A selected directory is not itself a ZIP.
    """
    for raw in (selected_paths or ()):
        try:
            active = Path(raw).resolve()
            if active.is_file() and candidate == active:
                return True
            if active.is_dir() and active in candidate.parents:
                return True
        except (OSError, ValueError, RuntimeError, TypeError):
            continue
    return False


def _baseline_evidence(fields: dict, metrics, source_name_confirmed: bool):
    """Confirm distinct QC measurements from the live selected QC row.

    Multiple concordant baseline values prevent an autofilled MP-to-record
    association from borrowing another tumor's/rerun's coverage. The link from
    the Collector DB is still required, and conflicts are always blocking.
    """
    concordance = {
        'paired_end_pairs_m': 'total_reads_m',
        'gc_pct': 'gc_pct',
        'clean_bases_gb': 'clean_bases_gb',
        'duplication_pct': 'duplication_pct',
        'freemix_pct': 'freemix_pct',
        'q30_pct': 'q30_pct',
        'coverage_50x_pct': 'coverage_50x_pct',
        'coverage_100x_pct': 'coverage_100x_pct',
    }
    agreeing = []
    for source_key, old_key in concordance.items():
        value = fields.get(source_key)
        baseline = getattr(metrics, old_key, None)
        if value is None or baseline is None:
            continue
        try:
            good = _same(float(value), float(baseline))
        except (TypeError, ValueError, OverflowError):
            return False, [], 'BASE_QC_CONFLICT'
        if not good:
            return False, agreeing, 'BASE_QC_CONFLICT'
        agreeing.append(source_key)
    # Even an exact MP, exact technical record and matching filename should
    # not suffice to append coverage without a second independent QC signal.
    independent = {'paired_end_pairs_m', 'freemix_pct', 'duplication_pct', 'clean_bases_gb'}
    required = 2 if source_name_confirmed else 3
    if len(agreeing) < required or not (independent & set(agreeing)):
        return False, agreeing, 'QC_SIGNATURE_UNCONFIRMED'
    return True, agreeing, 'OK'


def validated_dna_metrics(base, db, selected_metrics, selected_paths, *, allow_exact_source_fallback: bool = False) -> tuple[dict, str]:
    """Only return record-bound DNA coverage with a live QC signature match.

    Important: a read-only archive precheck can know more than the live UI.
    Here the actual UI sample, QC source selection (file OR folder), patient,
    DNA assay and current baseline measurements must agree. No extrapolation
    from GenNext depth or variant DP and no borrowing another RUN's QC.
    """
    if str(getattr(selected_metrics, 'sample_type', '') or '').upper() != 'DNA':
        return {}, 'RNA_UNCHANGED'
    if db is None:
        return {}, 'NO_COLLECTOR'
    rk = str(getattr(selected_metrics, 'collector_record_key', '') or '').strip()
    if not rk:
        return {}, 'NO_EXACT_RECORD'
    patient = _norm_mp(getattr(selected_metrics, 'patient_protocol', ''))
    if not patient:
        return {}, 'NO_PATIENT_PROTOCOL'
    try:
        records = [dict(r) for r in db.samples() if str(r['record_key']) == rk]
        if len(records) != 1:
            return {}, 'RECORD_NOT_UNIQUE'
        record = records[0]
        if str(record.get('sample_type') or '').upper() != 'DNA':
            return {}, 'ASSAY_MISMATCH'
        ids = {_norm_mp(record.get('patient_id')), _norm_mp(record.get('case_family'))}
        if patient not in ids:
            return {}, 'PATIENT_MISMATCH'
        files = [dict(f) for f in db.files_for_record(rk, 'qc')]
    except (AttributeError, KeyError, ValueError, TypeError, OSError):
        return {}, 'SOURCE_UNAVAILABLE'

    announced = {Path(str(x)).name.casefold() for x in
                 (getattr(selected_metrics, 'source_files', ()) or ())}
    if not selected_paths and not allow_exact_source_fallback:
        return {}, 'SOURCE_NOT_SELECTED'
    candidates = []
    seen = set()
    source_seen = False
    source_name_fallback_seen = False
    read_errors = 0
    disagree = False
    weak = False
    for row in files:
        if str(row.get('file_type') or 'qc').lower() != 'qc':
            continue
        candidate = _path(Path(base), str(row.get('path') or ''))
        if not candidate or candidate.suffix.lower() != '.zip' or candidate in seen:
            continue
        seen.add(candidate)
        is_active = _source_is_active(candidate, selected_paths)
        named_source = candidate.name.casefold() in announced
        # A strict alternative for the live NGS UI: an exact record-bound QC ZIP
        # can be read even when auto-sync tracked its extracted folder, rather
        # than the ZIP, *only* if the live row names this source and its numeric
        # QC signature independently confirms the same sample.
        fallback = bool(allow_exact_source_fallback and not is_active and named_source)
        if not is_active and not fallback:
            continue
        source_seen = source_seen or is_active
        source_name_fallback_seen = source_name_fallback_seen or fallback
        try:
            stat = candidate.stat()
            evidence = _atlas_qc_cached(str(candidate), stat.st_size, stat.st_mtime_ns, patient)
        except (OSError, ValueError, ImportError, RuntimeError):
            read_errors += 1
            continue
        if not evidence or not evidence.get('metrics'):
            continue
        fields = evidence['metrics']
        confirmed, matches, reason = _baseline_evidence(
            fields, selected_metrics, named_source)
        if fallback:
            # A filename and case/assay/record_key are not enough to prove
            # provenance. Require four different measurements including read
            # count and one independent post-alignment signature.
            strong = len(set(matches)) >= 4 and 'paired_end_pairs_m' in matches and bool(
                {'freemix_pct', 'duplication_pct'} & set(matches))
            if reason == 'OK' and not strong:
                confirmed, reason = False, 'QC_SIGNATURE_UNCONFIRMED'
        if reason == 'BASE_QC_CONFLICT':
            disagree = True
            continue
        if not confirmed:
            weak = True
            continue
        candidates.append(evidence)
    # Never accept a matched file while another active QC source bound to
    # this very record has contradictory baseline. Avoid mixing reruns.
    if disagree:
        return {}, 'BASE_QC_CONFLICT'
    if not candidates:
        if weak:
            return {}, 'QC_SIGNATURE_UNCONFIRMED'
        if read_errors:
            return {}, 'QC_READ_ERROR'
        return {}, 'NO_MATCHED_QC' if (source_seen or source_name_fallback_seen) else 'SOURCE_NOT_SELECTED'

    allowed = ('mean_depth_x', 'median_depth_x', 'coverage_50x_pct',
               'coverage_100x_pct', 'q30_pct')
    # Conflicting sources for one exact RUN are not a license to use a
    # convenient subset; withhold the whole optional enrichment.
    for key in allowed:
        observed = [float(e['metrics'][key]) for e in candidates
                    if e['metrics'].get(key) is not None]
        if len(observed) > 1 and any(abs(v-observed[0])>0.00001 for v in observed[1:]):
            return {}, 'QC_SOURCE_METRIC_CONFLICT'
    values: dict[str, float] = {}
    for key in allowed:
        measures = []
        for evidence in candidates:
            val = evidence['metrics'].get(key)
            if val is None:
                continue
            lo, hi = (0, 100) if key.endswith('_pct') else (0, 100000)
            if not _positive(val, lo, hi, inclusive_low=key.endswith('_pct')):
                measures = []
                break
            measures.append(float(val))
        if measures and all(abs(x - measures[0]) <= 0.00001 for x in measures[1:]):
            values[key] = measures[0]
    if ('coverage_50x_pct' in values and 'coverage_100x_pct' in values and
            values['coverage_100x_pct'] > values['coverage_50x_pct']):
        values.pop('coverage_50x_pct', None)
        values.pop('coverage_100x_pct', None)
    return values, 'VALIDATED' if values else 'NO_COVERAGE_METRIC'

def enrich_dna_sentence(original: str, base, db, metrics, selected_paths) -> str:
    """Append verified new QC sentences while preserving original text byte-for-byte."""
    if not isinstance(original, str) or not original.startswith('DNA dizilemesinde'):
        return original
    fields, _ = validated_dna_metrics(base, db, metrics, selected_paths)
    if not fields:
        return original
    clauses = []
    mean = fields.get('mean_depth_x')
    median = fields.get('median_depth_x')
    if mean is not None and median is not None and 'Ortalama okuma derinliği' not in original:
        clauses.append('Ortalama okuma derinliği %s×, medyan okuma derinliği %s× olarak belirlenmiştir.' %
                       (_display(mean), _display(median)))
    else:
        if mean is not None and 'Ortalama okuma derinliği' not in original:
            clauses.append('Ortalama okuma derinliği %s× olarak belirlenmiştir.' % _display(mean))
        if median is not None and 'medyan okuma derinliği' not in original.lower():
            clauses.append('Medyan okuma derinliği %s× olarak belirlenmiştir.' % _display(median))
    if 'coverage_50x_pct' in fields and 'en az 50x' not in original:
        clauses.append('Hedef bölgelerde en az 50× kapsama oranı %%%s olarak belirlenmiştir.' %
                       _display(fields['coverage_50x_pct']))
    if 'coverage_100x_pct' in fields and 'en az 100x' not in original:
        clauses.append('Hedef bölgelerde en az 100× kapsama oranı %%%s olarak belirlenmiştir.' %
                       _display(fields['coverage_100x_pct']))
    if 'q30_pct' in fields and 'Q30 baz oranı' not in original:
        clauses.append('Q30 baz oranı %%%s olarak bulunmuştur.' % _display(fields['q30_pct']))
    if not clauses:
        return original
    # The original paired-end clause and all subsequent old metrics remain unchanged.
    pivot = original.find('. ')
    if pivot < 0:
        return original
    return original[:pivot+2] + ' '.join(clauses) + ' ' + original[pivot+2:]


# v1.7.3: apply verified, read-only coverage to the sample data model *before*
# any Boyama preview, group text, or copied QC sentence is composed.
def verified_clauses(fields: dict, metrics) -> tuple[str, ...]:
    clauses = []
    mean, median = fields.get('mean_depth_x'), fields.get('median_depth_x')
    if mean is not None and median is not None:
        clauses.append('Ortalama okuma derinliği %s×, medyan okuma derinliği %s× olarak belirlenmiştir.' %
                       (_display(mean), _display(median)))
    else:
        if mean is not None:
            clauses.append('Ortalama okuma derinliği %s× olarak belirlenmiştir.' % _display(mean))
        if median is not None:
            clauses.append('Medyan okuma derinliği %s× olarak belirlenmiştir.' % _display(median))
    for key, label in (('coverage_50x_pct', '50'), ('coverage_100x_pct', '100')):
        if fields.get(key) is not None and getattr(metrics, key, None) is None:
            clauses.append('Hedef bölgelerde en az %s× kapsama oranı %%%s olarak belirlenmiştir.' %
                           (label, _display(fields[key])))
    if fields.get('q30_pct') is not None and getattr(metrics, 'q30_pct', None) is None:
        clauses.append('Q30 baz oranı %%%s olarak bulunmuştur.' % _display(fields['q30_pct']))
    return tuple(clauses)


def _unique_verified_record_from_source(base, db, metrics):
    """Resolve missing UI record links only from a unique QC ZIP + numeric fingerprint.

    MP alone is NEVER a record identity. Two matching reruns cause abstention.
    Does not alter collector records or the stored UI sample association.
    """
    from types import SimpleNamespace
    target_mp = _norm_mp(getattr(metrics, 'patient_protocol', '')) or _norm_mp(
        getattr(metrics, 'multiqc_analysis_id', ''))
    sources = {Path(str(x)).name.casefold() for x in (getattr(metrics, 'source_files', ()) or ())}
    if not target_mp or not sources or db is None:
        return None, 'NO_UNIQUE_SOURCE_ID'
    hits = {}
    try:
        records = [dict(r) for r in db.samples()]
        for record in records:
            if str(record.get('sample_type') or '').upper() != 'DNA':
                continue
            if target_mp not in {_norm_mp(record.get('patient_id')), _norm_mp(record.get('case_family'))}:
                continue
            rk = str(record.get('record_key') or '')
            if not rk:
                continue
            for item in db.files_for_record(rk, 'qc'):
                row = dict(item)
                candidate = _path(Path(base), str(row.get('path') or ''))
                if not candidate or candidate.suffix.lower() != '.zip' or candidate.name.casefold() not in sources:
                    continue
                st = candidate.stat()
                fields = _atlas_qc_cached(str(candidate), st.st_size, st.st_mtime_ns, target_mp).get('metrics', {})
                if not fields:
                    continue
                confirmed, matches, reason = _baseline_evidence(fields, metrics, True)
                strong = (confirmed and len(set(matches)) >= 4 and 'paired_end_pairs_m' in matches and
                          bool({'freemix_pct', 'duplication_pct'} & set(matches)))
                if strong:
                    hits[rk] = (target_mp, rk)
    except (AttributeError, KeyError, ValueError, TypeError, OSError, ImportError, RuntimeError):
        return None, 'SOURCE_UNAVAILABLE'
    if len(hits) != 1:
        return None, 'AMBIGUOUS_SOURCE' if len(hits) > 1 else 'NO_UNIQUE_SOURCE_ID'
    mp, rk = next(iter(hits.values()))
    attributes = dict(vars(metrics)) if hasattr(metrics, '__dict__') else {}
    attributes.update(collector_record_key=rk,patient_protocol=mp,sample_type='DNA')
    return SimpleNamespace(**attributes), 'UNIQUE_QC_SOURCE_LINK'


def attach_verified_dna_qc(base, db, samples, selected_paths) -> dict:
    """Attach only validated technical fields to in-memory SampleMetrics.

    No clinical DB mutation, no persistence, no identity borrowing across runs.
    No network calls. Existing QC fields remain unchanged. All paths through
    SampleMetrics.report_sentence now show the identical verified addition.
    """
    summary = {'dna_rows': 0, 'enriched': 0, 'reasons': {}}
    for metrics in (samples or {}).values():
        try:
            if str(getattr(metrics, 'sample_type', '')).upper() != 'DNA':
                continue
            summary['dna_rows'] += 1
            current = metrics
            # Some imported QC rows have no durable Collector link even though
            # their QC ZIP is already stored under one exact technical record.
            # Only a UNIQUE basename+MP+4-metric fingerprint can bridge this.
            if not str(getattr(metrics, 'collector_record_key', '') or '').strip():
                current, link_reason = _unique_verified_record_from_source(base, db, metrics)
                if current is None:
                    metrics._verified_qc_clauses_v173 = ()
                    summary['reasons'][link_reason] = summary['reasons'].get(link_reason, 0) + 1
                    continue
            values, reason = validated_dna_metrics(base, db, current, selected_paths,
                            allow_exact_source_fallback=True)
            metrics._verified_qc_clauses_v173 = verified_clauses(values, metrics) if values else ()
            if metrics._verified_qc_clauses_v173:
                summary['enriched'] += 1
            summary['reasons'][reason] = summary['reasons'].get(reason, 0) + 1
        except (OSError, ValueError, KeyError, TypeError, AttributeError, ImportError, RuntimeError):
            summary['reasons']['ERROR'] = summary['reasons'].get('ERROR', 0) + 1
    return summary
