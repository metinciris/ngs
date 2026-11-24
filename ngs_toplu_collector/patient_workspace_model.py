"""Read-only views for the unified patient workspace (no clinical inference).

The server's run/sample order is preserved independently of dates and labels.
Technical records, not normalised MP labels, own QC and report drafts.
"""
from __future__ import annotations

import csv
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

ORDER_KEY = 'gennext_runs_order_v1'


def case_key(row: dict) -> str:
    """Only a whole simple MP label is normalised; composite labels are not split."""
    label = str(row.get('case_family') or row.get('patient_id') or '').strip().upper()
    m = re.fullmatch(r'MP0*(\d+)(?:--?|/|_)(\d{2}|\d{4})', label)
    return f'MP{int(m[1])}-{m[2][-2:]}' if m else label


def is_repeat(row: dict) -> bool:
    return bool(row.get('is_rerun')) or bool(re.fullmatch(
        r'MP\d+--\d{2,4}', str(row.get('patient_id') or '').strip(), re.I))


def related_records(db, selected: dict, rows: list[dict] | None = None) -> list[dict]:
    family = case_key(selected)
    return [dict(r) for r in (rows if rows is not None else db.samples())
            if family and case_key(dict(r)) == family]


def ordered_runs(db) -> tuple[list[dict], str]:
    """Return source run cards and technical children without merging duplicate names.

    Old installations do not have full row positions. Until the next Runs scan,
    the fallback is explicitly labelled rather than pretending to reproduce GenNext.
    """
    pools = [dict(r) for r in db.cx.execute('SELECT * FROM pools ORDER BY id').fetchall()]
    samples = [dict(r) for r in db.cx.execute('SELECT * FROM samples ORDER BY id').fetchall()]
    by_run: dict[str, list[dict]] = defaultdict(list)
    for s in samples:
        by_run[str(s.get('run_uuid') or '')].append(s)
    pool_map = {str(p.get('run_uuid')): p for p in pools if p.get('run_uuid')}
    try:
        snapshot = json.loads(db.get_meta(ORDER_KEY, '') or '{}')
    except (ValueError, TypeError):
        snapshot = {}
    raw_order = snapshot.get('runs') or []
    cards: list[dict] = []
    seen: set[str] = set()
    for source in raw_order:
        run_id = str(source.get('run_uuid') or '')
        if not run_id or run_id in seen:
            continue
        seen.add(run_id)
        p = dict(pool_map.get(run_id) or source)
        p['run_uuid'] = run_id
        rank = {str(k): i for i, k in enumerate(source.get('record_keys') or [])}
        children = list(by_run.get(run_id, []))
        children.sort(key=lambda r: (rank.get(r['record_key'], 10**9), r.get('id') or 0))
        cards.append({**p, 'rows': children, 'source_order': True,
                      'source_page': source.get('page'), 'source_position': source.get('position')})
    # Rows not seen on the last scan are kept, visibly separated; never deleted.
    for p in pools:
        run_id = str(p.get('run_uuid') or '')
        if run_id in seen:
            continue
        seen.add(run_id)
        cards.append({**p, 'rows': list(by_run.get(run_id, [])), 'source_order': False})
    for run_id, children in by_run.items():
        if run_id not in seen:
            cards.append({'run_uuid': run_id, 'pool_name': children[0].get('pool_name') or 'POOL yok',
                          'run_type': children[0].get('sample_type') or '',
                          'rows': children, 'source_order': False})
    if raw_order:
        hint = 'GenNext ana sayfa / RUNS sırası'
        if not snapshot.get('complete'):
            hint += ' · son tarama kısmi'
        if any(not c['source_order'] for c in cards):
            hint += ' · son taramada görülmeyen yerel kayıtlar altta'
    else:
        # Use the last known top run first, but do not invent page ordering.
        try:
            latest = json.loads(db.get_meta('latest_run_uuids', '') or '[]')
        except (ValueError, TypeError):
            latest = []
        rank = {k: i for i, k in enumerate(latest)}
        cards.sort(key=lambda c: (rank.get(c.get('run_uuid'), 10**9),
                                  c.get('source_page') or 10**9, c.get('id') or 0))
        hint = 'Kaynak sıra henüz kaydedilmedi. Toplama > RUNS’u Tara ile GenNext sırasını alın.'
    return cards, hint


def latest_path(base: Path, db, record_key: str, kind: str) -> Path | None:
    for row in db.files_for_record(record_key, kind):
        p = Path(row['path'])
        p = p if p.is_absolute() else base / p
        if p.is_file():
            return p
    return None


def load_record_metrics(base: Path, db, row: dict):
    """Read validated QC sources only for this exact technical record.

    Local RNA raw-QC data can complement native MultiQC coverage values, but
    no unrelated sample, run, assay or different MP year is ever used.
    """
    from qc_enlil import (SampleMetrics, parse_multiqc_zip, parse_csv_file,
                         parse_report_file, analysis_protocol_numbers)
    from source_validation import validate_qc_source
    from rna_qc_bundle import read_imported_metrics

    key = str(row['record_key'])
    typ = str(row.get('sample_type') or '').upper()
    patient = str(row.get('patient_id') or '')
    m = SampleMetrics(sample=key, sample_type=typ)
    status = 'QC kaynak dosyasi yok / henuz indirilmedi'
    sources = []
    errors = []
    for file in db.files_for_record(key, 'qc'):
        path = Path(file['path'])
        path = path if path.is_absolute() else Path(base) / path
        if not path.is_file():
            continue
        try:
            ok, reason, _ = validate_qc_source(path, patient)
            if not ok:
                errors.append(reason)
                continue
            if path.suffix.lower() == '.json':
                imported = read_imported_metrics(path, patient, key)
                if imported is not None:
                    candidates = [imported]
                    is_raw = True
                else:
                    # Do not assume an unrelated generic JSON is a QC metric row.
                    errors.append('QC JSON turu okunamadi')
                    continue
            elif path.suffix.lower() == '.zip':
                candidates = parse_multiqc_zip(path)
                is_raw = False
            elif path.suffix.lower() in ('.tsv', '.csv'):
                candidates = parse_csv_file(path)
                is_raw = False
            else:
                item = parse_report_file(path)
                candidates = [item] if item else []
                is_raw = False
            exact = []
            family = case_key(row)
            for item in candidates:
                if item is None:
                    continue
                labels = analysis_protocol_numbers(item.multiqc_analysis_id or
                                                   item.patient_protocol or item.sample)
                families = {case_key({'patient_id': f'MP{n}-{y}'}) for n, y in labels if y}
                if families and family not in families:
                    continue
                if len(candidates) > 1 and item.sample_type != typ:
                    continue
                exact.append(item)
            if len(exact) != 1:
                errors.append('QC ornek eslesmesi belirsiz; otomatik secim yapilmadi')
                continue
            sources.append((exact[0], path, is_raw))
        except (OSError, ValueError, RuntimeError, KeyError) as exc:
            errors.append('QC okunamadi: ' + str(exc))
    if sources:
        # Newest source is primary, older validated sources fill only blanks.
        # Raw RNA QC takes precedence for exact filtered pair counts and Q30.
        raw = [entry for entry in sources if entry[2]]
        primary = raw[0] if typ == 'RNA' and raw else sources[0]
        m = primary[0]
        for extra, extra_path, is_raw in sources:
            if extra is m or extra.sample_type not in (typ, ''):
                continue
            m.merge(extra)
        status = ('QC: RNA ham dosyalari (Cutadapt + fqStat)' if primary[2]
                  else 'QC: secili teknik kaydin yerel dosyasi')
        if len(sources) > 1:
            status += ' + ' + str(len(sources)-1) + ' diger dogrulanmis kaynak'
        for item, path, _ in sources:
            m.source_files.add(str(path))
    elif errors:
        status = 'QC kontrol gerekli: ' + errors[0]
    m.sample = key
    m.sample_type = typ
    m.collector_exact = True
    m.collector_record_key = key
    m.collector_case_family = row.get('case_family') or row.get('patient_id') or ''
    m.collector_patient_id = patient
    m.patient_protocol = patient
    m.report_protocol = patient
    m.collector_source_note = status
    return m, status


def _pick(row: dict, *names: str) -> str:
    lower = {str(k).casefold().replace(' ', '').replace('_', ''): v for k, v in row.items()}
    for name in names:
        v = lower.get(name.casefold().replace(' ', '').replace('_', ''))
        if v is not None and str(v).strip():
            return str(v).strip()
    return ''


def _read_rows(path: Path) -> list[dict]:
    if path.suffix.lower() == '.json':
        obj = json.loads(path.read_text(encoding='utf-8-sig'))
        if isinstance(obj, dict):
            obj = obj.get('rows') or obj.get('data') or obj.get('results') or []
        if not isinstance(obj, list):
            return []
        return [dict(x) for x in obj if isinstance(x, dict)]
    with path.open('r', encoding='utf-8-sig', newline='', errors='replace') as f:
        sample = f.read(8192)
        f.seek(0)
        return [dict(r) for r in csv.DictReader(f, delimiter='\t' if '\t' in sample else ',')]


def _af_text(value: str) -> str:
    if not value:
        return '—'
    try:
        n = float(value.rstrip('%').replace(',', '.'))
        if '%' not in value:
            if not 0 <= n <= 1:
                return value + ' (ham; birim belirtilmemiş)'
            n *= 100
        return f'%{n:.4g}'
    except ValueError:
        return value


def read_observations(base: Path, db, record_key: str, assay: str) -> tuple[list[dict], list[str]]:
    """Exact observed calls, including Unknown and filtered rows. Not negative evidence."""
    kinds = ('snv', 'cnv') if assay == 'DNA' else ('fusion',)
    result: list[dict] = []
    notes: list[str] = []
    for kind in kinds:
        path = latest_path(base, db, record_key, kind)
        if not path:
            notes.append(f'{kind.upper()}: kaynak yok')
            continue
        try:
            rows = _read_rows(path)
            notes.append(f'{kind.upper()}: {len(rows)} gözlem')
            for i, raw in enumerate(rows):
                gene = _pick(raw, 'Gene_refGene', 'Gene.refGene', 'GeneName', 'Gene')
                change = _pick(raw, 'aaChange', 'HGVSp', 'Protein', 'txChange', 'HGVSc')
                chrom = _pick(raw, 'Chrom', 'Chr', 'Chromosome').lower().removeprefix('chr')
                start = _pick(raw, 'Start', 'Pos', 'Position')
                build = _pick(raw, 'GenomeBuild', 'ReferenceBuild', 'Assembly')
                grade = _pick(raw, 'Oncogenic', 'Oncogenicity') or 'Unknown'
                uncertain = False
                if kind == 'snv':
                    ref = _pick(raw, 'Ref', 'Reference')
                    alt = _pick(raw, 'Alt', 'Alternate')
                    complete = all((chrom, start, ref, alt))
                    identity = '|'.join(('SNV', build, chrom, start, ref.upper(), alt.upper())) if complete else f'SNV|UNRESOLVED|{record_key}|{i}'
                    location = f'chr{chrom}:{start} {ref}>{alt}' if complete else change or 'Konum eksik'
                elif kind == 'cnv':
                    end = _pick(raw, 'End')
                    event = _pick(raw, 'SVType', 'CNVType', 'Type')
                    complete = all((chrom, start, end, gene, event))
                    identity = '|'.join(('CNV', build, chrom, start, end, gene, event)) if complete else f'CNV|UNRESOLVED|{record_key}|{i}'
                    location = f'{gene} {event} chr{chrom}:{start}-{end}'
                    change = event
                else:
                    g1 = _pick(raw, 'Gene1', 'LeftGene', 'Gene A', '5p_gene')
                    g2 = _pick(raw, 'Gene2', 'RightGene', 'Gene B', '3p_gene')
                    name = _pick(raw, 'Fusion', 'FusionName', 'Name', 'Event') or '--'.join(x for x in (g1, g2) if x)
                    if not name:
                        continue
                    bp1 = _pick(raw, 'Breakpoint1', 'LeftBreakpoint', 'Position1', 'Pos1')
                    bp2 = _pick(raw, 'Breakpoint2', 'RightBreakpoint', 'Position2', 'Pos2')
                    uncertain = not (bp1 and bp2)
                    # With no breakpoints, a matching name is only a candidate pair.
                    identity = '|'.join(('FUSION_NAME' if uncertain else 'FUSION', build, name.upper(), bp1, bp2))
                    gene = name
                    change = name
                    location = f'{name} {bp1} / {bp2}' if not uncertain else f'{name} (kırılma noktası yok)'
                if kind == 'cnv':
                    measure = 'FC ' + (_pick(raw, 'FoldChange', 'Fold Change') or '—')
                    support = 'Prob ' + (_pick(raw, 'Probes', 'Probe Count') or '—')
                elif kind == 'fusion':
                    measure = _pick(raw, 'Supporting Reads', 'Support', 'JunctionReadCount', 'JunctionReads', 'SplitReads', 'AD') or '—'
                    support = _pick(raw, 'SpanningReads', 'SpanningFragCount', 'DP', 'Total Reads') or '—'
                else:
                    measure = _af_text(_pick(raw, 'AF', 'VAF'))
                    support = _pick(raw, 'DP', 'Depth') or '—'
                result.append({'key': identity, 'kind': kind.upper(), 'gene': gene,
                               'change': change, 'location': location, 'measure': measure,
                               'support': support, 'filter': _pick(raw, 'Filter') or '—',
                               'oncogenic': grade, 'raw': raw, 'uncertain': uncertain})
        except Exception as exc:
            notes.append(f'{kind.upper()}: okunamadı ({exc})')
    return result, notes


def compare_observations(a: list[dict], b: list[dict]) -> list[dict]:
    groups_a: dict[str, list[dict]] = defaultdict(list)
    groups_b: dict[str, list[dict]] = defaultdict(list)
    for r in a:
        groups_a[r['key']].append(r)
    for r in b:
        groups_b[r['key']].append(r)
    out = []
    for key in sorted(set(groups_a) | set(groups_b)):
        aa, bb = groups_a[key], groups_b[key]
        lead = (aa or bb)[0]
        presence = 'İki çalışmada gözlem' if aa and bb else 'Yalnız A’da gözlem' if aa else 'Yalnız B’de gözlem'
        if aa and bb and lead.get('uncertain'):
            presence = 'Aynı füzyon adı; kırılma noktası doğrulanmadı'
        def joined(rows, field):
            return ' / '.join(dict.fromkeys(r[field] for r in rows)) if rows else 'Gözlem yok'
        out.append({**lead, 'presence': presence, 'a': aa, 'b': bb,
                    'a_measure': joined(aa, 'measure'), 'b_measure': joined(bb, 'measure'),
                    'a_support': joined(aa, 'support'), 'b_support': joined(bb, 'support'),
                    'a_filter': joined(aa, 'filter'), 'b_filter': joined(bb, 'filter'),
                    'oncogenic': joined(aa + bb, 'oncogenic')})
    return out
