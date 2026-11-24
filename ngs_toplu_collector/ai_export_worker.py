"""Private subprocess entry point: snapshot, selected-pool index, verify, publish."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import traceback
import zipfile

from ai_export_job import atomic_json, cleanup_publication, export_filesystem_path, display_path


class ExportCancelled(Exception):
    pass


class WorkerProgress:
    def __init__(self, job_dir, timeout_seconds):
        self.job_dir = Path(job_dir)
        self.deadline = time.monotonic() + timeout_seconds if timeout_seconds > 0 else None
        self.last_write = 0.0
        self.last_stage = ''

    def check(self):
        if (self.job_dir / 'CANCEL').exists():
            raise ExportCancelled('Paket oluşturma iptal edildi.')
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise TimeoutError('Paket oluşturma süre sınırını aştı; önceki paket korundu.')

    def __call__(self, stage, current=None, total=None, message=''):
        self.check()
        now = time.monotonic()
        if stage != self.last_stage or now - self.last_write >= 0.1 or (total is not None and current == total):
            atomic_json(self.job_dir / 'status.json', {'state': 'running', 'stage': str(stage),
                'current': current, 'total': total, 'message': str(message)[:2000]})
            self.last_stage, self.last_write = stage, now


def snapshot_database(source, destination, progress):
    destination.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(Path(source).resolve().as_uri() + '?mode=ro', uri=True, timeout=1)
    dst = sqlite3.connect(destination)
    try:
        src.backup(dst, pages=128, sleep=0.05,
                   progress=lambda status, remaining, total: progress('database_snapshot', total - remaining,
                       total, 'Veritabanının tutarlı çalışma kopyası alınıyor.'))
        os.chmod(destination, 0o600)
    finally:
        dst.close()
        src.close()


def index_selected_pool(base, db, options, progress):
    # Reuse the existing source classification/parser, without invoking its Tk UI.
    from pool_spread import _pick, _float, _norm_chrom, _onc, _variant_key
    from ai_export import _scope_match
    group = db.pool_group_name(options['pool_group'])
    scope = options['sample_scope']
    selected_runs = {str(x) for x in (options.get('selected_run_uuids') or []) if str(x)}
    samples = [dict(s) for s in db.samples()
               if (str(s['run_uuid']) in selected_runs if selected_runs
                   else db.pool_group_name(s['pool_name']) == group)
               and _scope_match(s['sample_type'], scope)]
    db.ensure_analysis_schema()
    for n, sample in enumerate(samples, 1):
        progress('selected_pool_index', n - 1, len(samples), 'Seçilen POOL için kaynak varyant indeksi denetleniyor.')
        source, file_record = None, None
        for row in db.files_for_record(sample['record_key'], 'snv'):
            candidate = Path(row['path'])
            candidate = candidate if candidate.is_absolute() else Path(base) / candidate
            if candidate.is_file() and candidate.suffix.lower() in ('.tsv', '.txt'):
                source, file_record = candidate, dict(row)
                break
        if source is None:
            continue
        signature = file_record.get('sha256') or f'{source.stat().st_size}:{int(source.stat().st_mtime)}'
        old = db.get_indexed_file(sample['record_key'], 'snv_oncogenic')
        if old and old['source_sha'] == signature:
            continue
        observations = []
        with source.open('r', encoding='utf-8-sig', errors='replace', newline='') as stream:
            for line, row in enumerate(csv.DictReader(stream, delimiter='\t'), 1):
                if line % 1000 == 0:
                    progress.check()
                oncogenic = _onc(_pick(row, 'Oncogenic'))
                key = _variant_key(row)
                if not oncogenic or not key or '|||' in key:
                    continue
                observations.append({**sample, 'variant_key': key, 'chrom': _norm_chrom(_pick(row, 'Chrom', 'Chr')),
                    'start': _pick(row, 'Start', 'Pos'), 'ref': _pick(row, 'Ref'), 'alt': _pick(row, 'Alt'),
                    'gene': _pick(row, 'Gene_refGene', 'Gene'),
                    'change': _pick(row, 'aaChange', 'Protein_Change', 'AAChange'),
                    'oncogenic': oncogenic, 'filter': _pick(row, 'Filter'),
                    'af': _float(_pick(row, 'AF', 'VAF')), 'ad': _pick(row, 'AD'), 'dp': _pick(row, 'DP'),
                    'highest_level': _pick(row, 'HighestLevel', 'HighestSensitiveLevel', 'HighestResistanceLevel'),
                    'source_sha': signature, 'source_path': str(file_record.get('path') or source)})
        db.replace_oncogenic_observations(sample['record_key'], observations, signature, len(observations))
    progress('selected_pool_index', len(samples), len(samples), 'Seçilen POOL indeksi hazır.')


def _digest(path, progress):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            progress.check()
            digest.update(chunk)
    return digest.hexdigest()


def verify_bundle(result, staging_root, progress):
    folder, archive = export_filesystem_path(result['folder']).resolve(), export_filesystem_path(result['zip']).resolve()
    root = export_filesystem_path(staging_root).resolve()
    if folder.parent != root or archive.parent != root or not archive.is_file() or not zipfile.is_zipfile(archive):
        raise ValueError('Paket çıktısı geçerli çalışma klasörü veya ZIP değil.')
    manifest_path = folder / 'MANIFEST.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    validation = manifest.get('json_generator_validation') or result.get('json_validation') or {}
    if any(validation.get(name) != 'PASS' for name in ('schema_validation', 'cross_reference_validation', 'duplicate_key_validation')):
        raise ValueError('Paket JSON doğrulaması tamamlanmadı.')
    expected = {}
    entries = manifest.get('files') or []
    for n, entry in enumerate(entries, 1):
        progress('verify_manifest', n - 1, len(entries), 'Paket dosyaları ve SHA-256 değerleri doğrulanıyor.')
        relative = Path(entry['name'])
        path = (folder / relative).resolve()
        if relative.is_absolute() or not path.is_relative_to(folder) or not path.is_file():
            raise ValueError('Manifest dosya yolu geçersiz.')
        if relative.as_posix() in expected:
            raise ValueError('Manifest yinelenen dosya içeriyor.')
        digest = _digest(path, progress)
        if path.stat().st_size != entry['size_bytes'] or digest != entry['sha256']:
            raise ValueError('Paket dosyası manifest ile uyuşmuyor: ' + relative.as_posix())
        expected[relative.as_posix()] = digest
    actual = {p.relative_to(folder).as_posix() for p in folder.rglob('*') if p.is_file()}
    if actual != set(expected) | {'MANIFEST.json'}:
        raise ValueError('Manifest bütün paket dosyalarını kapsamıyor.')
    expected['MANIFEST.json'] = _digest(manifest_path, progress)
    with zipfile.ZipFile(archive) as zf:
        members = [info for info in zf.infolist() if not info.is_dir()]
        names = [info.filename for info in members]
        if len(names) != len(set(names)) or set(names) != {folder.name + '/' + name for name in expected}:
            raise ValueError('ZIP dosyaları doğrulanmış klasörle uyuşmuyor.')
        for n, info in enumerate(members, 1):
            progress('verify_zip', n - 1, len(members), 'ZIP içeriği ve bütünlüğü doğrulanıyor.')
            digest = hashlib.sha256()
            with zf.open(info) as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                    progress.check()
                    digest.update(chunk)
            if digest.hexdigest() != expected[info.filename[len(folder.name) + 1:]]:
                raise ValueError('ZIP içeriği manifest ile uyuşmuyor.')
    progress('verify_zip', len(expected), len(expected), 'ZIP doğrulandı.')
    return folder, archive


def publish_bundle(result, request, job_dir, staging_root, progress):
    folder, archive = verify_bundle(result, staging_root, progress)
    output_root = export_filesystem_path(request['output_root']).resolve()
    final_folder, final_archive = output_root / folder.name, output_root / archive.name
    if request['job_id'] not in folder.name or request['job_id'] not in archive.name:
        raise ValueError('Paket adı benzersiz işlem kimliği içermiyor.')
    if final_folder.exists() or final_archive.exists():
        raise FileExistsError('Paket adı zaten kullanılıyor; mevcut dosya korunuyor.')
    pointer = output_root / 'SON_YZ_PAKETI.txt'
    previous = pointer.read_text(encoding='utf-8') if pointer.is_file() else ''
    atomic_json(job_dir / 'publish.json', {'job_id': request['job_id'], 'folder': str(final_folder),
                'zip': str(final_archive), 'previous_pointer': previous})
    progress('publish', 0, 1, 'Doğrulanmış paket teslim klasörüne alınıyor.')
    os.replace(folder, final_folder)
    progress.check()
    os.replace(archive, final_archive)  # The final ZIP appears only after verification.
    progress.check()
    temporary_pointer = job_dir / 'last_package.txt'
    temporary_pointer.write_text(display_path(final_archive), encoding='utf-8')
    os.replace(temporary_pointer, pointer)
    small_result = {'folder': display_path(final_folder), 'zip': display_path(final_archive)}
    for key in ('prompt', 'compact', 'full', 'igv_request_template'):
        if result.get(key):
            relative = export_filesystem_path(result[key]).resolve().relative_to(folder)
            small_result[key] = display_path(final_folder / relative)
    package = result.get('package') or {}
    small_result['package'] = {key: package.get(key) for key in
        ('pool_group', 'sample_scope', 'generator_version', 'versions', 'bundle_completeness')}
    small_result['json_validation'] = result.get('json_validation') or {}
    return small_result


def run_job(job_file):
    job_file = export_filesystem_path(job_file).resolve()
    job_dir = job_file.parent
    request = json.loads(job_file.read_text(encoding='utf-8'))
    progress = WorkerProgress(job_dir, float(request['timeout_seconds']))
    db = None
    try:
        progress('starting', message='Ayrı paket işlemi başladı.')
        snapshot = job_dir / 'DATA' / 'snapshot.sqlite3'
        snapshot_database(request['db_path'], snapshot, progress)
        from db import StateDB
        db = StateDB(snapshot)
        options = dict(request['options'])
        if options.pop('reindex_selected', True):
            index_selected_pool(request['base'], db, options, progress)
        from export_runtime import set_progress_callback
        set_progress_callback(lambda payload: progress(payload['stage'], payload.get('current'),
            payload.get('total'), payload.get('message', '')))
        from ai_export import write_pool_ai_bundle
        cfg = dict(request['cfg'])
        staging_root = job_dir / 'staging'
        cfg['ai_export_dir'] = str(staging_root)
        cfg['ai_export_job_id'] = request['job_id']
        # Internal selection context: never passed as a new public bundle kwarg.
        cfg['ai_selected_run_uuids'] = options.pop('selected_run_uuids', [])
        progress('build_package', message='Seçilen POOL için YZ paketi hazırlanıyor.')
        result = write_pool_ai_bundle(Path(request['base']), db, cfg, **options)
        result = publish_bundle(result, request, job_dir, staging_root, progress)
        atomic_json(job_dir / 'status.json', {'state': 'succeeded', 'stage': 'complete',
            'current': 1, 'total': 1, 'message': 'Paket hazır; ZIP bütünlüğü doğrulandı.', 'result': result})
        return 0
    except BaseException as exc:
        cleanup_publication(job_dir, request['output_root'], request['job_id'])
        cancelled = isinstance(exc, ExportCancelled)
        atomic_json(job_dir / 'status.json', {'state': 'cancelled' if cancelled else 'failed',
            'stage': progress.last_stage or 'starting', 'current': None, 'total': None,
            'message': str(exc)[:2000] or type(exc).__name__})
        if not cancelled:
            traceback.print_exc()
        return 2 if cancelled else 1
    finally:
        if db is not None:
            db.cx.close()
        job_file.unlink(missing_ok=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--job', required=True)
    raise SystemExit(run_job(parser.parse_args().job))
