# -*- coding: utf-8 -*-
"""On-demand Boyama DNA coverage; source-checked, read-only, UI-visible.

Does NOT change collector records, patient/report drafts, or QC files. A live
report must never borrow coverage by MP alone or from a different RUN.
"""
from __future__ import annotations

from pathlib import Path
import sqlite3
from zipfile import BadZipFile
from typing import Any

from ngs_qc_boyama_coverage import (
    _atlas_qc_cached, _baseline_evidence, _norm_mp, _path,
    verified_clauses, _display, _positive,
)

class _ArchiveReadOnly:
    """Fallback only when NGS panel has no initialized collector DB object."""
    def __init__(self, base: Path):
        path = Path(base) / 'DATA' / 'collector_state.sqlite3'
        self.cx = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=3)
        self.cx.row_factory = sqlite3.Row
        self.cx.execute('PRAGMA query_only=ON')

    def samples(self):
        return self.cx.execute('SELECT record_key,patient_id,case_family,sample_type FROM samples').fetchall()

    def files_for_record(self, rk, kind):
        return self.cx.execute(
            'SELECT record_key,file_type,path FROM files WHERE record_key=? AND file_type=?', (rk, kind)
        ).fetchall()

    def close(self):
        self.cx.close()


def _status(code: str, metrics=None, matches: int = 0, source_count: int = 0, reason: str = '') -> dict:
    labels = {
        'VERIFIED': 'QC derinlik: doğrulandı',
        'RNA': 'QC derinlik: RNA raporunda DNA derinliği kullanılmaz',
        'NO_ID': 'QC derinlik: seçili örneğin MP/protokol kimliği bulunamadı',
        'NO_DB': 'QC derinlik: yerel Collector arşivi okunamadı',
        'NO_RECORD': 'QC derinlik: aynı MP + DNA teknik kaydı bulunamadı',
        'NO_QC': 'QC derinlik: teknik kayda bağlı QC ZIP bulunamadı',
        'NO_MATCH': 'QC derinlik: canlı QC ölçümleriyle eşleşen kaynak bulunamadı',
        'BASE_CONFLICT': 'QC derinlik: rapordaki temel ölçümler ile QC arşivi uyuşmuyor',
        'WEAK_SIGNATURE': 'QC derinlik: kesin eşleşme için yeterli bağımsız ölçüm yok',
        'QC_PARSE_ERROR': 'QC derinlik: QC arşivi okunamadı veya ayrıştırılamadı',
        'NO_FIELDS': 'QC derinlik: eşleşebilir QC sayısal alanları bulunamadı',
        'AMBIGUOUS': 'QC derinlik: birden fazla RUN/QC kaydı uyuyor; ekleme engellendi',
        'LINK_CONFLICT': 'QC derinlik: seçili teknik kayıt ile QC kaydı uyuşmuyor; ekleme engellendi',
        'SOURCE_CONFLICT': 'QC derinlik: aynı teknik kayıt için farklı derinlikler; ekleme engellendi',
        'NO_DEPTH': 'QC derinlik: kaynak eşleşti ancak ortalama/medyan ölçümü yok',
        'ERROR': 'QC derinlik: okuma veya eşleştirme hatası; değer eklenmedi',
    }
    description = labels.get(code, labels['ERROR'])
    if code == 'VERIFIED':
        parts = []
        if metrics.get('mean_depth_x') is not None:
            parts.append('ortalama ' + _display(metrics['mean_depth_x']) + '×')
        if metrics.get('median_depth_x') is not None:
            parts.append('medyan ' + _display(metrics['median_depth_x']) + '×')
        description += ' — ' + ', '.join(parts)
        description += ' (aynı teknik kayıt; ' + str(matches) + ' QC alanı eşleşti)'
    elif code in ('NO_MATCH','AMBIGUOUS','LINK_CONFLICT','SOURCE_CONFLICT','NO_DEPTH','BASE_CONFLICT','WEAK_SIGNATURE','QC_PARSE_ERROR','NO_FIELDS'):
        description += ' [teknik kontrol gerekli]'
    if code == 'ERROR' and reason:
        # Exception *type* only: no paths, file names, MP codes or private text.
        description += ' (' + reason[:50] + ')'
    return dict(code=code, label=description, matches=matches, source_count=source_count)


def _live_values(base: Path, db: Any, row: Any) -> tuple[dict, dict]:
    """Return unique matched exact record QC, or an explicit failure status.

    Requires matching MP, DNA, >=4 independent numeric signals including read
    pairs and duplication or FREEMIX. If a record is explicitly linked, do not
    silently substitute a different RUN. No reliance on source_files display
    names or selected folder path (those can be absent for auto-loaded QC).
    """
    if str(getattr(row, 'sample_type', '') or '').upper() != 'DNA':
        return {}, _status('RNA')
    mp = _norm_mp(getattr(row,'patient_protocol',''))
    if not mp:
        # Analysis ID can ground the patient only when assigned protocol is empty.
        mp = _norm_mp(getattr(row,'multiqc_analysis_id',''))
    if not mp:
        return {}, _status('NO_ID')
    if db is None:
        return {}, _status('NO_DB')
    try:
        records = [dict(r) for r in db.samples()
                   if str(r['sample_type'] or '').upper() == 'DNA'
                   and mp in {_norm_mp(r['patient_id']),_norm_mp(r['case_family'])}]
    except (AttributeError, KeyError, sqlite3.Error, OSError, ValueError, TypeError):
        return {}, _status('NO_DB')
    if not records:
        return {}, _status('NO_RECORD')

    linked = str(getattr(row,'collector_record_key','') or '').strip()
    matched = []
    qc_files = 0
    baseline_conflicts = 0
    weak_signatures = 0
    parse_errors = 0
    empty_qc = 0
    seen = set()
    # Do NOT write or amend the panel's selected technical record binding.
    for record in records:
        rk = str(record['record_key'] or '')
        if not rk:continue
        try:
            files = list(db.files_for_record(rk,'qc'))
        except (AttributeError, sqlite3.Error, OSError, ValueError, TypeError):
            continue
        for entry in files:
            try:
                candidate = _path(base, str(dict(entry).get('path') or ''))
                if not candidate or candidate.suffix.lower() != '.zip': continue
                unique = (rk, str(candidate))
                if unique in seen: continue
                seen.add(unique)
                qc_files += 1
                stat = candidate.stat()
                evidence = _atlas_qc_cached(str(candidate), stat.st_size, stat.st_mtime_ns, mp)
                fields = evidence.get('metrics', {}) if evidence else {}
                if not fields:
                    empty_qc += 1
                    continue
                ok, agreeing, reason = _baseline_evidence(fields,row,False)
                signature = set(agreeing)
                # High-confidence numeric fingerprint of this live UI sample,
                # independent of file selection and source filename.
                strong = (ok and len(signature) >= 4
                          and 'paired_end_pairs_m' in signature
                          and bool({'duplication_pct','freemix_pct'} & signature))
                if strong:
                    matched.append((rk, str(candidate), fields, len(signature)))
                elif reason == 'BASE_QC_CONFLICT':
                    baseline_conflicts += 1
                else:
                    weak_signatures += 1
            except (OSError, TypeError, ValueError, AttributeError, KeyError,
                    ImportError, RuntimeError, sqlite3.Error, BadZipFile):
                parse_errors += 1
                continue
    if not qc_files: return {}, _status('NO_QC')
    if not matched:
        code = ('BASE_CONFLICT' if baseline_conflicts else
                'WEAK_SIGNATURE' if weak_signatures else
                'QC_PARSE_ERROR' if parse_errors else
                'NO_FIELDS' if empty_qc else 'NO_MATCH')
        return {}, _status(code,source_count=qc_files)
    linked_keys = {m[0] for m in matched}
    if len(linked_keys) != 1:
        return {}, _status('AMBIGUOUS', source_count=len(matched))
    matched_rk = next(iter(linked_keys))
    if linked and linked != matched_rk:
        return {}, _status('LINK_CONFLICT', source_count=len(matched))
    allowed = ('mean_depth_x','median_depth_x','coverage_50x_pct','coverage_100x_pct','q30_pct')
    values = {}
    for key in allowed:
        known = []
        for _,_,fields,_ in matched:
            value = fields.get(key)
            if value is None: continue
            limits = (0.0, 100.0) if key.endswith('_pct') else (0.0,100000.0)
            if not _positive(value,*limits,inclusive_low=key.endswith('_pct')):
                return {}, _status('SOURCE_CONFLICT', source_count=len(matched))
            known.append(float(value))
        if len(known) > 1 and any(abs(x-known[0]) > .00001 for x in known[1:]):
            return {}, _status('SOURCE_CONFLICT',source_count=len(matched))
        if known:values[key] = known[0]
    if (values.get('coverage_50x_pct') is not None and values.get('coverage_100x_pct') is not None
        and values['coverage_100x_pct'] > values['coverage_50x_pct']):
        values.pop('coverage_50x_pct',None); values.pop('coverage_100x_pct',None)
    if values.get('mean_depth_x') is None and values.get('median_depth_x') is None:
        return {}, _status('NO_DEPTH',source_count=len(matched))
    return values, _status('VERIFIED',values,
                           matches=max(m[3] for m in matched), source_count=len(matched))


def refresh_for_preview(base, db, selected_metrics, selected_paths=()) -> dict:
    """Called when the Boyama text is composed, NOT just on Analyze click."""
    if selected_metrics is None:
        return _status('NO_ID')
    if str(getattr(selected_metrics,'sample_type','') or '').upper() != 'DNA':
        return _status('RNA')
    # Never reuse a stale depth from another selection or previous session.
    selected_metrics._verified_qc_clauses_v173 = ()
    connection = None
    try:
        if db is None:
            connection = _ArchiveReadOnly(Path(base))
            db = connection
        values, status = _live_values(Path(base), db, selected_metrics)
        if status['code'] == 'VERIFIED':
            selected_metrics._verified_qc_clauses_v173 = verified_clauses(values, selected_metrics)
        return status
    except Exception as exc:
        # UI displays the exception TYPE, not its message (could contain PHI/path).
        return _status('ERROR',reason=type(exc).__name__)
    finally:
        if connection is not None:
            connection.close()
