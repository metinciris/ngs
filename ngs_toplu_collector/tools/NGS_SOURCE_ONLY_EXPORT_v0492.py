"""Export only top-level Python program source, never patient/run/session data.

Usage (Windows): py NGS_SOURCE_ONLY_EXPORT_v0492.py "C:\ngs\ngs_toplu_collector"
The resulting ZIP must be reviewed before sharing. Do NOT upload to public GitHub.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import sys
import zipfile

# Do not recurse: subdirectories can contain data, diagnostics or patient exports.
ALLOW_SUFFIXES = {'.py', '.pyw'}
ALLOW_NAMES = {'VERSION'}
MAX_SINGLE_FILE = 4 * 1024 * 1024
SECRET_ASSIGNMENT = re.compile(
    r'''(?im)^\s*\w*(?:token|api_?key|secret|passwd|password|sifre|parola)\w*\s*=\s*['\"][^'\"\r\n]{6,}['\"]'''
)
TOKEN_MARKERS = re.compile(r'(?i)\b(?:ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,})\b')


def create_source_bundle(root: Path, output: Path) -> list[str]:
    root = root.expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError('Target is not a directory.')
    if not (root / 'BASLAT.bat').is_file():
        raise ValueError('BASLAT.bat not found. Wrong NGS installation folder?')
    files: list[tuple[str, bytes]] = []
    flagged: list[str] = []
    for item in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if item.is_symlink() or not item.is_file():
            continue
        if item.suffix.lower() not in ALLOW_SUFFIXES and item.name not in ALLOW_NAMES:
            continue
        if item.stat().st_size > MAX_SINGLE_FILE:
            flagged.append(f'{item.name}: too large')
            continue
        data = item.read_bytes()
        text = data.decode('utf-8-sig', errors='replace')
        if SECRET_ASSIGNMENT.search(text) or TOKEN_MARKERS.search(text):
            flagged.append(f'{item.name}: possible credential in source')
            continue
        files.append((item.name, data))
    if flagged:
        raise RuntimeError('Export refused. Review these files locally (do not share secrets):\n' + '\n'.join(flagged))
    if not any(n == 'collector.py' for n, _ in files):
        raise RuntimeError('collector.py not found; source-only export not safe to treat as complete.')
    if not any(n in {'app.py', 'app.pyw'} for n, _ in files):
        raise RuntimeError('app.py not found; source-only export not safe to treat as complete.')
    if output.resolve().parent == root:
        raise ValueError('Output must be outside application folder.')
    output.parent.mkdir(parents=True, exist_ok=True)
    inventory = ['SOURCE-ONLY EXPORT: NOT FOR PUBLIC GITHUB',
                 'Only top-level .py/.pyw and VERSION; no DATA/OUTPUT/PROFILE/AI_EXPORT/config/keys.',
                 'Review all files for personal or institutional data before sending privately.', '',
                 'name\tbytes\tsha256']
    for name, data in files:
        inventory.append(f'{name}\t{len(data)}\t{hashlib.sha256(data).hexdigest()}')
    # Use a temporary archive then atomically replace to avoid leaving incomplete output.
    temporary = output.with_suffix('.zip.part')
    try:
        with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            for name, data in files:
                archive.writestr(name, data)
            archive.writestr('SOURCE_ONLY_MANIFEST.txt', '\n'.join(inventory) + '\n')
        os.replace(temporary, output)
    finally:
        if temporary.exists():
            temporary.unlink()
    return [name for name, _ in files]


def main() -> int:
    try:
        base = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(r'C:\ngs\ngs_toplu_collector')
        target = Path.home() / 'Downloads' / 'NGS_v0492_SOURCE_ONLY_FOR_PRIVATE_REVIEW.zip'
        names = create_source_bundle(base, target)
        print(f'SOURCE ONLY ZIP: {target}')
        print('Source files: ' + ', '.join(names))
        print('Do NOT upload this archive to public GitHub. Review it locally first.')
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        print('EXPORT FAILED: ' + str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
