"""Explicit LOCAL decision archive, never an automatic AI upload or BAM backup."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import tempfile
import zipfile
from review_workspace_store import ReviewRepository, local_note, patient_text, selection_for_record, stamp


def encode(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')


def write_review_archive(db, session_id, destination):
    """Snapshot SQLite, create a checksummed ZIP, then publish atomically."""
    destination = Path(destination).resolve()
    if destination.suffix.lower() != '.zip': raise ValueError('ZIP hedefi se\u00e7in.')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.ngs_review_', dir=destination.parent) as td:
        from db import StateDB
        snapshot = StateDB(Path(td) / 'snapshot.sqlite3')
        try:
            db.cx.backup(snapshot.cx)
            repo = ReviewRepository(snapshot)
            meta, doc = repo.session(session_id)
            if not meta: raise ValueError('YZ oturumu bulunamad\u0131.')
            contents = {'AI_REVIEW_RETURN.json': encode(doc)}
            summaries = ['# Yerel POOL YZ / karar ar\u015fivi', '',
                'YZ g\u00f6r\u00fc\u015fleri nihai klinik rapor de\u011fildir. Yerel ar\u015fiv hassas veri i\u00e7erebilir.', '']
            for p in repo.summaries(session_id):
                rk = p['record_key']; s = snapshot.get_sample(rk) or {}
                record = {'record_key': rk, 'patient_id': s.get('patient_id') or p.get('patient_id'),
                          'pool_name': s.get('pool_name'), 'sample_type': s.get('sample_type'),
                          'local_note': local_note(snapshot, rk),
                          'selection': selection_for_record(snapshot, rk, s.get('sample_type', 'DNA'))}
                for table in ('variant_decisions', 'variant_decision_history_v0483', 'ai_adjudications_v0483',
                              'ai_adjudication_history_v0483', 'ai_note_history_v0483', 'reported_variant_history'):
                    record[table] = [dict(r) for r in snapshot.cx.execute(f'SELECT * FROM {table} WHERE record_key=?', (rk,))] if snapshot._table_exists(table) else []
                # Include all source review versions linked to this exact record.
                for sid in repo.session_ids(rk):
                    _, prior = repo.session(sid)
                    contents.setdefault(f'YZ_GECMISI/OTURUM_{sid}.json', encode(prior))
                name = hashlib.sha256(rk.encode('utf-8')).hexdigest()[:20]
                contents[f'HASTALAR/{name}.json'] = encode(record)
                summaries += ['## ' + str(record['patient_id'] or '?'), patient_text(repo.record(rk, session_id)), '',
                              'Yerel not: ' + record['local_note'], '']
            contents['POOL_YZ_OZETI.md'] = '\n'.join(summaries).encode('utf-8')
            contents['OKU.txt'] = ('YEREL KARAR ARSIVI - v0.4.83\n'
                'Bu ZIP tam BAM/QC/kaynak yedegi degildir. Otomatik YZ gonderimi yoktur.\n'
                'AI_REVIEW_RETURN.json yeniden ice alinabilir. Yerel karar/gecmis JSON dosyalari\n'
                'salt okunur arsivdir; otomatik klinik geri yukleme yapilmaz.\n').encode('utf-8')
            manifest = {'schema': 'ngs_local_review_archive_v1', 'created_at': stamp(), 'version': '0.4.83',
                        'session_id': int(session_id), 'scope': 'local_decisions_not_raw_sequencing_backup',
                        'files': [{'name': n, 'size_bytes': len(b), 'sha256': hashlib.sha256(b).hexdigest()} for n, b in contents.items()]}
            contents['MANIFEST.json'] = encode(manifest)
            temporary = Path(td) / 'archive.zip'
            with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as z:
                for name, data in contents.items(): z.writestr(name, data)
            with zipfile.ZipFile(temporary) as z:
                if z.testzip() is not None: raise ValueError('ZIP b\u00fct\u00fcnl\u00fc\u011f\u00fc do\u011frulanamad\u0131.')
                for f in manifest['files']:
                    if hashlib.sha256(z.read(f['name'])).hexdigest() != f['sha256']:
                        raise ValueError('Ar\u015fiv SHA-256 uyu\u015fmazl\u0131\u011f\u0131.')
            os.replace(temporary, destination)
            return manifest
        finally:
            snapshot.cx.close()
