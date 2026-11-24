"""Safe, explicit import of GenNext RNA sequencing QC ZIP archives.

Only a selected RNA run and exact MP+year pairs can be linked. This module
never changes a patient's identity, assay, existing report, or QC task status.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
import zipfile
from collections import defaultdict
from pathlib import Path

from qc_enlil import SampleMetrics, parse_report_file

SCHEMA = 'ngs_rna_raw_qc_v1'
MEMBER_RE = re.compile(
    r'^(MP0*(?P<number>\d{1,6})(?:--?|[/_])(?P<year>\d{2,4})'
    r'_L(?P<lane>\d{1,3})_(?P<index>\d+))'
    r'(?P<suffix>\.trimming\.report|\.report\.html|_[12]\.fq\.fqStat\.txt)$', re.I)
FIELD_NAMES = (
    'raw_pairs', 'total_reads_m', 'q30_pct', 'r1_q30_pct', 'r2_q30_pct',
    'gc_pct', 'r1_gc_pct', 'r2_gc_pct', 'clean_bases_gb', 'raw_bases_gb',
    'base_retention_pct', 'pass_pairs', 'pass_pct',
)
MAX_ARCHIVE_BYTES = 70 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
MAX_MEMBER_BYTES = 5 * 1024 * 1024
MAX_MEMBERS = 2000


def mp_key(label: str) -> str:
    """Strict whole-label MP/year comparison; 25 and 26 never alias."""
    match = re.fullmatch(r'MP0*(\d{1,6})(?:--?|[/_])(\d{2}|\d{4})', str(label or '').strip(), re.I)
    if not match:
        return ''
    return f'MP{int(match.group(1))}-{match.group(2)[-2:]}'


def _file_kind(suffix: str) -> str:
    lower = suffix.lower()
    if lower == '.trimming.report':
        return 'trim'
    if lower == '.report.html':
        return 'html'
    return 'r1' if lower.startswith('_1') else 'r2'


def _source_members(zf: zipfile.ZipFile) -> dict[str, list[dict]]:
    groups = defaultdict(lambda: defaultdict(list))
    for info in zf.infolist():
        if info.is_dir() or info.filename.endswith(('\\', '/')):
            continue
        # Never extract paths from the archive. Names are only keys for reading.
        name = info.filename.replace('\\', '/')
        parts = name.split('/')
        if any(part in ('.', '..', '') for part in parts) or ':' in name:
            continue
        if (info.external_attr >> 16) & 0o170000 == 0o120000:
            continue  # ZIP symlink, not an ordinary QC file.
        match = MEMBER_RE.fullmatch(parts[-1])
        if not match:
            continue
        mp = f"MP{int(match['number'])}-{match['year'][-2:]}"
        group = (mp, match.group(1).upper())
        groups[group][_file_kind(match['suffix'])].append(info)
    by_mp = defaultdict(list)
    for (mp, group_id), files in groups.items():
        by_mp[mp].append({'group': group_id, 'files': dict(files)})
    return dict(by_mp)


def _parse_one(zf: zipfile.ZipFile, mp: str, files: dict) -> tuple[SampleMetrics | None, list[str]]:
    values = {}
    problems = []
    if any(len(items) != 1 for items in files.values()):
        return None, ['Ayni grupta birden fazla ayni tip QC dosyasi var.']
    with tempfile.TemporaryDirectory(prefix='ngs_rna_qc_') as directory:
        for kind, items in files.items():
            info = items[0]
            if info.file_size > MAX_MEMBER_BYTES:
                return None, ['QC dosyasi boyut sinirini asiyor.']
            basename = info.filename.replace('\\', '/').split('/')[-1]
            path = Path(directory) / basename
            path.write_bytes(zf.read(info))
            values[kind] = parse_report_file(path)
    trim, r1, r2, html = (values.get(x) for x in ('trim', 'r1', 'r2', 'html'))
    metric = SampleMetrics(sample=mp, sample_type='RNA', patient_protocol=mp, report_protocol=mp)
    if trim:
        for name in ('raw_pairs', 'pass_pairs', 'raw_bases_gb', 'clean_bases_gb'):
            setattr(metric, name, getattr(trim, name))
    if r1 and r2 and r1.raw_pairs is not None and r2.raw_pairs is not None:
        if r1.raw_pairs != r2.raw_pairs:
            return None, ['R1 ve R2 ham okuma sayilari farkli; otomatik eslestirme durduruldu.']
    if trim:
        for one in (r1, r2):
            if one and one.raw_pairs is not None and trim.raw_pairs is not None and one.raw_pairs != trim.raw_pairs:
                return None, ['Cutadapt ve fqStat ham okuma sayilari uyusmuyor.']
    if metric.raw_pairs is None:
        metric.raw_pairs = next((one.raw_pairs for one in (r1, r2) if one and one.raw_pairs is not None), None)
    if r1:
        metric.r1_q30_pct, metric.r1_gc_pct = r1.r1_q30_pct, r1.r1_gc_pct
    if r2:
        metric.r2_q30_pct, metric.r2_gc_pct = r2.r2_q30_pct, r2.r2_gc_pct
    if (not r1 or not r2) or metric.r1_q30_pct is None or metric.r2_q30_pct is None:
        problems.append('R1/R2 fqStat Q30 cifti eksik; HTML ozeti varsa Q30 oradan alinir.')
        if html:
            metric.q30_pct = html.q30_pct
    if html:
        if metric.gc_pct is None and not (r1 and r2 and metric.r1_gc_pct is not None and metric.r2_gc_pct is not None):
            metric.gc_pct = html.gc_pct
    # The intended displayed read-pair count is POST-filtering, not raw FASTQ.
    if metric.pass_pairs is not None:
        metric.total_reads_m = metric.pass_pairs / 1_000_000
        problems.append('Okuma cifti Cutadapt filtre sonrasi; Q30/GC ham FASTQ bazlarindan.')
    elif metric.raw_pairs is not None:
        metric.total_reads_m = metric.raw_pairs / 1_000_000
        problems.append('Cutadapt yok: okuma cifti ham FASTQ kaynagindan.')
    elif html and html.total_reads_m is not None:
        metric.total_reads_m = html.total_reads_m
        problems.append('Okuma cifti yalniz HTML ozetinden.')
    metric.finalize()
    if metric.total_reads_m is None and metric.q30_pct is None:
        return None, ['Gecerli RNA QC metrikleri ayristirilamadi.']
    metric.source_files.update(
        info.filename.replace('\\', '/').split('/')[-1]
        for items in files.values() for info in items
    )
    return metric, problems


def analyze_bundle(path: Path | str, target_rows: list[dict]) -> dict:
    """Preview import for *one already selected run*, no writes."""
    path = Path(path)
    if path.suffix.lower() != '.zip' or not path.is_file():
        raise ValueError('RNA QC icin ZIP dosyasi secin (RAR degil).')
    if path.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ValueError('QC ZIP boyut sinirini asiyor.')
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    by_id = defaultdict(list)
    for row in target_rows:
        if str(row.get('sample_type') or '').upper() != 'RNA':
            continue
        key = mp_key(row.get('patient_id') or '')
        if key:
            by_id[key].append(dict(row))
    items = []
    with zipfile.ZipFile(path) as zf:
        all_info = zf.infolist()
        if len(all_info) > MAX_MEMBERS or sum(x.file_size for x in all_info) > MAX_UNCOMPRESSED_BYTES:
            raise ValueError('ZIP dosyasi guvenli QC boyut/uye sinirini asiyor.')
        invalid = zf.testzip()
        if invalid:
            raise ValueError('ZIP CRC hatasi: ' + invalid.split('/')[-1])
        groups = _source_members(zf)
        if not groups:
            raise ValueError('ZIP icinde MP..._L... trimming/fqStat kaynaklari bulunamadi.')
        for mp in sorted(groups, key=lambda k: (int(re.search(r'\d+', k).group()), k)):
            group = groups[mp]
            entry = {'mp': mp, 'group': '', 'row': None, 'metric': None, 'status': '', 'notes': []}
            if len(group) != 1:
                entry['status'] = 'KARISIK';entry['notes'] = ['Ayni MP/yil icin birden fazla lane/grup var.']
            elif len(by_id.get(mp, [])) > 1:
                entry['status'] = 'KARISIK';entry['notes'] = ['Secili run icinde ayni MP/yil birden fazla teknik kayitta var.']
            elif not by_id.get(mp):
                entry['status'] = 'ESLESMEDI'
                same_num = [x for x in by_id if x.split('-')[0] == mp.split('-')[0]]
                if same_num:
                    entry['notes'] = ['Yil uyusmazligi: ' + mp + ' / programda ' + ', '.join(same_num)]
                else:
                    entry['notes'] = ['Secili run icinde bu MP/yil bulunamadi.']
            else:
                entry['row'] = by_id[mp][0]
                entry['group'] = group[0]['group']
                try:
                    metric, problems = _parse_one(zf, mp, group[0]['files'])
                except (OSError, UnicodeError, ValueError, RuntimeError) as exc:
                    metric, problems = None, ['QC ayristirma hatasi: ' + str(exc)]
                entry['metric'],entry['notes'] = metric,problems
                entry['status'] = 'HAZIR' if metric else 'KONTROL'
            items.append(entry)
    matches = [i for i in items if i['status'] == 'HAZIR']
    found = {x['mp'] for x in items}
    not_found = sorted(set(by_id) - found)
    return {'archive_path': str(path), 'archive_sha256': sha, 'items': items,
            'ready': matches, 'target_count': sum(len(v) for v in by_id.values()),
            'not_found': not_found}


def _numeric_fields(metric: SampleMetrics) -> dict:
    return {name: getattr(metric, name) for name in FIELD_NAMES if getattr(metric, name) is not None}


def import_bundle(base: Path, db, plan: dict) -> dict:
    """Persist only exact, confirmed target record keys. Re-import is idempotent."""
    source = Path(plan['archive_path'])
    if hashlib.sha256(source.read_bytes()).hexdigest() != plan['archive_sha256']:
        raise ValueError('QC ZIP onizlemeden sonra degistirilmis; lutfen yeniden secin.')
    ready = plan['ready']
    if not ready:
        return {'imported': 0, 'skipped': len(plan['items'])}
    root = Path(base) / 'DATA' / 'RNA_QC_IMPORTED'
    archives = root / 'archives'
    records = root / 'records'
    archives.mkdir(parents=True, exist_ok=True)
    records.mkdir(parents=True, exist_ok=True)
    archive_dest = archives / (plan['archive_sha256'] + '.zip')
    if not archive_dest.is_file():
        temp = archive_dest.with_suffix('.tmp')
        shutil.copyfile(source, temp)
        if hashlib.sha256(temp.read_bytes()).hexdigest() != plan['archive_sha256']:
            temp.unlink(missing_ok=True)
            raise ValueError('Kaydedilen ZIP dogrulama hatasi.')
        temp.replace(archive_dest)
    count = 0
    skipped = 0
    for entry in ready:
        row = entry['row']; record_key = row['record_key']; mp = entry['mp']
        current = db.get_sample(record_key)
        if not current or str(current.get('sample_type') or '').upper() != 'RNA' or mp_key(current.get('patient_id') or '') != mp:
            skipped += 1
            continue
        if (current.get('run_uuid') or '') != (row.get('run_uuid') or ''):
            skipped += 1
            continue
        metric = entry['metric']
        payload = {
            'format': SCHEMA, 'record_key': record_key, 'sample_id': mp,
            'sample_type': 'RNA', 'run_uuid': row.get('run_uuid') or '',
            'pool_name': row.get('pool_name') or '',
            'source_archive_sha256': plan['archive_sha256'],
            'source_archive_name': source.name,
            'source_files': sorted(metric.source_files),
            'metrics': _numeric_fields(metric), 'notes': list(entry['notes']),
        }
        content = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode('utf-8')
        digest = hashlib.sha256(content).hexdigest()
        name = f'{hashlib.sha256(record_key.encode()).hexdigest()[:16]}_{digest[:16]}.json'
        dest = records / name
        if not dest.is_file():
            temp = dest.with_suffix('.tmp')
            temp.write_bytes(content)
            temp.replace(dest)
        db.add_file({'record_key': record_key, 'sample_uuid': current.get('sample_uuid'),
                     'pool_name': current.get('pool_name'), 'patient_id': current.get('patient_id'),
                     'file_type': 'qc', 'path': str(dest.relative_to(base)),
                     'sha256': digest, 'size': len(content)})
        count += 1
    return {'imported': count, 'skipped': skipped + len(plan['items']) - len(ready)}


def read_imported_metrics(path: Path, expected_id: str = '', expected_record_key: str = '') -> SampleMetrics | None:
    obj = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if not isinstance(obj, dict) or obj.get('format') != SCHEMA:
        return None
    sample_id = mp_key(obj.get('sample_id'))
    if not sample_id or obj.get('sample_type') != 'RNA':
        return None
    if expected_id and sample_id != mp_key(expected_id):
        return None
    if expected_record_key and obj.get('record_key') != expected_record_key:
        return None
    metric = SampleMetrics(sample=sample_id, sample_type='RNA',
                           patient_protocol=sample_id, report_protocol=sample_id)
    fields = obj.get('metrics')
    if not isinstance(fields, dict):
        return None
    for name in FIELD_NAMES:
        value = fields.get(name)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        # Numeric QC fields must be finite and nonnegative; no fabricated scores.
        import math
        if not math.isfinite(value) or value < 0:
            return None
        if name.endswith('_pct') and value > 100:
            return None
        setattr(metric, name, value)
    metric.source_files.add(str(path))
    metric.notes.extend(str(x) for x in obj.get('notes', []) if isinstance(x, str))
    metric.finalize()
    return metric if metric.total_reads_m is not None or metric.q30_pct is not None else None
