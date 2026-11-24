# -*- coding: utf-8 -*-
"""v1.4 patient-scoped source explorer for NGS v0.4.95.

All reads of the patient DB use one exact record_key. Manual imported report
copies live in a separate local SQLite file; the clinical DB is never written. Source files are opened from Collector's
own files table only; there are no network calls or automatic report actions.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import sqlite3
import zipfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

MAX_FILE_SIZE = 90 * 1024 * 1024
MAX_MEMBER_SIZE = 16 * 1024 * 1024
MAX_TABLE_ROWS = 120000
MAX_REPORT = 450000
ALLOWED_ASSAYS = {'DNA', 'RNA'}
VARIANT_TYPES = {'snv', 'cnv', 'fusion'}
MANUAL_TYPES = ('Klinik bilgi', 'Raporlama', 'Varyant notu', 'QC notu',
                'Enlil', 'GenNext', 'Literatur / web', 'YZ yorumu', 'Diger')
GENE_RE = re.compile(r'[A-Z][A-Z0-9-]{1,25}\Z')
MP_RE = re.compile(r'(?i)(?<![A-Z0-9])MP\s*0*(\d{1,5})\s*[-/]\s*(?:20)?(\d{2})(?!\d)')


def s(x, n=2000):
    return str(x if x is not None else '').strip()[:n]


def now():
    return datetime.now().astimezone().isoformat(timespec='seconds')


def same_mp(label, patient_id):
    matches = MP_RE.findall(s(label, 500))
    b = MP_RE.fullmatch(s(patient_id, 100))
    return bool(len(matches)==1 and b and (int(matches[0][0]),matches[0][1]) == (int(b.group(1)), b.group(2)))


def number(raw, lo=None, hi=None):
    if raw is None or str(raw).strip() in ('', '-', '.', 'NA', 'N/A'):
        return None
    val = str(raw).strip().replace(' ', '').replace('%', '').replace(',', '.')
    try:
        result = float(val)
    except (ValueError, OverflowError):
        return None
    if result != result or result in (float('inf'), float('-inf')):
        return None
    if lo is not None and result < lo:
        return None
    if hi is not None and result > hi:
        return None
    return result


def pick(row, *keys):
    lower = {str(k).strip().casefold(): v for k, v in row.items()}
    for k in keys:
        v = lower.get(k.casefold())
        if v is not None and str(v).strip() not in ('', '.'):
            return s(v, 250)
    return ''


def numeric(row, *keys, lo=0, hi=None):
    return number(pick(row, *keys), lo, hi)


def format_value(value, unit=''):
    if value is None:
        return 'Veri yok / eslesmedi'
    if isinstance(value, float):
        val = ('%.4f' % value).rstrip('0').rstrip('.')
    else:
        val = str(value)
    return val.replace('.', ',') + unit


def resolved_source(base, row):
    raw = s(row.get('path'), 1600)
    if not raw:
        return None
    p = Path(raw)
    if not p.is_absolute():
        p = Path(base) / p
        # Untrusted/malformed relative paths must not escape the app root.
        root = Path(base).resolve()
        try:
            p.resolve().relative_to(root)
        except ValueError:
            return None
    try:
        if p.is_file() and p.stat().st_size <= MAX_FILE_SIZE:
            return p
    except OSError:
        return None
    return None


@contextmanager
def open_readonly_db(base):
    path = Path(base) / 'DATA' / 'collector_state.sqlite3'
    conn = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=8)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA query_only=ON')
    try:
        yield conn
    finally:
        conn.close()


def saved_reporting_mutation_comment(base, record_key, assay, patient_id=''):
    """Read the SAME TECH::<record_key> comment used by the Rapor Arşivi.

    Never infer a RUN from a patient-only/legacy key; never write the source.
    The report archive's reader is the fallback when the exact form lookup is
    absent.  The returned text remains a *draft*, not signed Enlil evidence.
    """
    issues=[]
    rk=s(record_key,350)
    assay=s(assay,10).upper()
    segments=rk.split('::')
    if len(segments)!=3 or assay not in ALLOWED_ASSAYS or segments[-1].upper()!=assay:
        return None,['Teknik RUN / ornek / DNA-RNA anahtari dogrulanamadi.']
    path=Path(base)/'DATA'/'QC_ENLIL'/'hasta_form_verileri.json'
    comment=''
    blocked=False
    exact_exists=False
    if path.exists():
        try:
            if not path.is_file() or path.stat().st_size>32*1024*1024:
                raise ValueError('Taslak JSON boyutu desteklenen siniri asiyor')
            obj=json.loads(path.read_text(encoding='utf-8-sig'))
            if not isinstance(obj,dict):raise ValueError('Taslak JSON kok yapisi hatali')
            exact=obj.get('TECH::'+rk)
            if exact is not None:
                exact_exists=True
                if not isinstance(exact,dict):raise ValueError('Teknik taslak kaydi hatali')
                for key,expected in (('record_key',rk),('run_uuid',segments[0]),
                                     ('sample_uuid',segments[1]),('sample_type',assay),
                                     ('analysis_type',assay),('assay',assay)):
                    present=exact.get(key)
                    if present not in (None,'') and str(present).upper()!=str(expected).upper():
                        raise ValueError('Teknik taslak icindeki '+key+' kimligi uyusmuyor')
                stored_mp=exact.get('patient_id')
                if stored_mp not in (None,'') and patient_id and not same_mp(stored_mp,patient_id):
                    raise ValueError('Teknik taslak icindeki patient_id kimligi uyusmuyor')
                value=exact.get('diagnosis_comment','')
                if not isinstance(value,str):raise ValueError('Mutasyon yorum metni string degil')
                comment=value.strip()
            else:
                # Old patient-wide drafts are NEVER silently linked to a RUN.
                old=[k for k in obj if isinstance(k,str) and k.startswith(('PATIENT:','SAMPLE:'))
                     and patient_id and same_mp(k,patient_id)]
                if old:
                    issues.append('Eski hasta-bazli taslak anahtari var; bir RUNa otomatik baglanmadi.')
        except (OSError,ValueError,TypeError,UnicodeError) as exc:
            if exact_exists:blocked=True  # Never let an archive fallback bypass a TECH key identity failure.
            issues.append('Kayitli Raporlama formu okunamadi: '+s(exc,140))
    if blocked:
        return None,issues
    if not comment:
        # Official Rapor Arsivi reads saved comments with the same TECH key.
        # This is intentionally a read-only cross-check, not a second patient
        # matching algorithm. Failure does not invent a report.
        try:
            from report_archive_model import load_archive_rows
            dbpath=Path(base)/'DATA'/'collector_state.sqlite3'
            if dbpath.is_file():
                archive=load_archive_rows(dbpath,base,form_data=None)
                found=[r for r in archive if s(r.get('record_key'))==rk and
                       s(r.get('sample_type')).upper()==assay and
                       s(r.get('run_uuid'))==segments[0] and
                       s(r.get('sample_uuid'))==segments[1]]
                if len(found)==1 and found[0].get('comment_saved') and found[0].get('form_saved'):
                    comment=s(found[0].get('comment'),MAX_REPORT)
                elif len(found)>1:
                    issues.append('Rapor Arsivi teknik anahtar tekil degil; yorum aktarilmadi.')
        except Exception as exc:
            # The form file is still the primary source; archive errors are
            # diagnostic only and never damage QC or clinical work.
            issues.append('Rapor Arsivi salt okunur kontrolu tamamlanamadi: '+type(exc).__name__)
    if comment:
        return {'origin':'RAPORLAMA_MUTASYON_YORUMU',
                'status':'KAYITLI TEKNIK TASLAK / ENLIL E-IMZA DOGRULANMADI',
                'label':'Raporlama > Mutasyon Yorum > Pozitif mutasyon / rapor yorumu',
                'created_at':'', 'text':s(comment,MAX_REPORT)},issues
    return None,issues


def latest_report_sources(base, record_key, case_family="", patient_id="", assay="", issues=None):
    """Read local technical-report records, keeping source and approval labels."""
    rows = []
    with open_readonly_db(base) as cx:
        cx.execute('BEGIN')
        tables = {r[0] for r in cx.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'archive_report_documents' in tables:
            for d in cx.execute('''SELECT id,version,title,report_text,document_status,created_at
                                   FROM archive_report_documents WHERE record_key=?
                                   ORDER BY version DESC LIMIT 30''', (record_key,)):
                rows.append({'origin': 'archive_report_documents', 'status': s(d['document_status']),
                             'label': 'Rapor arsivi v' + str(d['version']),
                             'created_at': s(d['created_at']), 'text': s(d['report_text'], MAX_REPORT)})
        if 'reporting_approvals' in tables:
            for r in cx.execute('''SELECT id,state,payload_json,approved_at FROM reporting_approvals
                                   WHERE record_key=? ORDER BY id DESC LIMIT 30''', (record_key,)):
                try:
                    payload = json.loads(r['payload_json'] or '{}')
                except (TypeError, ValueError):
                    payload = {}
                txt = payload.get('text') if isinstance(payload, dict) else ''
                if isinstance(txt, str) and txt.strip():
                    rows.append({'origin': 'reporting_approvals',
                                 'status': s(r['state']),
                                 'label': 'Yerel icerik onay gecmisi',
                                 'created_at': s(r['approved_at']), 'text': s(txt, MAX_REPORT)})
        if 'reporting_fields' in tables:
            for r in cx.execute('''SELECT field_name,source_json,manual_json,revision,updated_at
                                   FROM reporting_fields WHERE record_key=? ORDER BY field_name''', (record_key,)):
                value = r['manual_json'] if r['manual_json'] is not None else r['source_json']
                try:
                    obj = json.loads(value or '{}')
                except (TypeError, ValueError):
                    obj = {}
                if isinstance(obj, dict) and obj.get('value') is not None:
                    fields = s(obj['value'], 5000)
                    rows.append({'origin': 'reporting_fields', 'status': 'manuel' if r['manual_json'] is not None else 'kaynak',
                                 'label': s(r['field_name']), 'created_at': s(r['updated_at']), 'text': fields})
    current, report_warnings=saved_reporting_mutation_comment(base,record_key,assay,patient_id)
    if current:
        # Current, saved technical comment is distinct from historical reports.
        rows.insert(0,current)
    # Preserve the previous version's report-section draft visibility, but
    # do not reinterpret formatting/section overrides as confirmed mutations.
    forms=Path(base)/'DATA'/'QC_ENLIL'/'hasta_form_verileri.json'
    try:
        if forms.is_file() and forms.stat().st_size<=32*1024*1024:
            obj=json.loads(forms.read_text(encoding='utf-8-sig'))
            form=obj.get('TECH::'+record_key) if isinstance(obj,dict) else None
            if isinstance(form,dict):
                scope=form.get('sample_type')
                if scope is None or s(scope).upper()==s(assay).upper():
                    value=form.get('report_section_overrides')
                    if value:
                        txt=value if isinstance(value,str) else json.dumps(value,ensure_ascii=False)
                        rows.append({'origin':'RAPORLAMA_SECILI_BOLUM_TASLAGI',
                                     'status':'duzenlenebilir teknik taslak',
                                     'label':'Rapor formu: report_section_overrides',
                                     'created_at':'','text':s(txt,MAX_REPORT)})
    except (OSError,ValueError,UnicodeError,TypeError):
        pass  # The primary comment reader already reports a source error.
    if issues is not None:
        issues.extend(report_warnings)
    # NGS prior-report imports are not stored in collector_state.sqlite3.
    # They are user-pasted/Enlil-screen captures; NOT signed-report evidence.
    if case_family and patient_id and assay in ALLOWED_ASSAYS:
        try:
            digest = hashlib.sha256(str(case_family).encode('utf-8')).hexdigest()[:30]
            f = Path(base)/'DATA'/'NGS_PRIOR_REPORTS'/(digest+'.json')
            if f.is_file() and f.stat().st_size <= 12*1024*1024:
                obj = json.loads(f.read_text(encoding='utf-8'))
                if (isinstance(obj,dict) and obj.get('schema')=='ngs_user_pasted_historical_report_v1'
                        and obj.get('case_family')==case_family and isinstance(obj.get('reports'),list)):
                    for item in obj['reports'][:100]:
                        if not isinstance(item,dict):continue
                        if (same_mp(item.get('prior_mp'),patient_id)
                                and s(item.get('assay')).upper()==assay
                                and s(item.get('source')) in ('manual_enlil_paste_unverified','enlil_open_screen_jab_read_only')):
                            rows.append({'origin':'NGS_PRIOR_REPORTS',
                                         'status':'ENLIL ekranindan / manuel aktarim; imza dogrulanmadi',
                                         'label':'Ayni MP ve DNA/RNA ile eslesen yerel rapor kopyasi',
                                         'created_at':s(item.get('imported_at')),
                                         'text':s(item.get('text'),MAX_REPORT)})
        except (OSError,ValueError,UnicodeError,TypeError):
            rows.append({'origin':'NGS_PRIOR_REPORTS','status':'kaynak hatasi',
                         'label':'Yerel rapor kopyasi acilamadi', 'created_at':'', 'text':''})
    return rows


def extract_reported_candidates(sources):
    """Only report text candidates; never silently mark them as signed/confirmed."""
    from report_parser import extract_reported_items
    extracted = []
    seen = set()
    for source in sources:
        if source.get('origin') in ('reporting_fields','RAPORLAMA_SECILI_BOLUM_TASLAGI'):
            continue
        body = source.get('text', '')
        if not body:
            continue
        parsed = extract_reported_items(body)
        for item in parsed.get('positives', []):
            key = (s(item.get('gene')).upper(),s(item.get('type')).upper(),
                   s(item.get('cdna')).upper(),
                   s(item.get('protein')).upper().replace('X','*'),
                   s(item.get('feature')).upper())
            if key in seen:
                continue
            seen.add(key)
            extracted.append({'gene': s(item.get('gene'), 50), 'type': s(item.get('type'), 15),
                              'cdna': s(item.get('cdna'), 150), 'protein': s(item.get('protein'), 150),
                              'feature': s(item.get('feature'), 150),
                              'origin': s(source.get('origin'), 100), 'source_status': s(source.get('status'), 100),
                              'label': s(source.get('label'), 100),
                              'raw_text': s(item.get('raw_text'), 600)})
    return extracted


def _read_table(path, delimiter):
    rows = []
    with path.open('r', encoding='utf-8-sig', errors='replace', newline='') as f:
        rd = csv.DictReader(f, delimiter=delimiter)
        headers = [s(x, 100) for x in (rd.fieldnames or [])]
        if len(headers) > 300:
            raise ValueError('Cok fazla tablo sutunu')
        for i, r in enumerate(rd):
            if i >= MAX_TABLE_ROWS:
                raise ValueError('Tablo cok buyuk; Mutasyonlar sekmesinde acin (120000+ satir).')
            rows.append({s(k, 100): s(v, 2000) for k, v in r.items() if k is not None})
    return headers, rows


def _read_json(path):
    obj = json.loads(path.read_text(encoding='utf-8-sig'))
    if isinstance(obj, list):
        return obj
    if isinstance(obj, dict):
        for key in ('rows', 'data', 'variants', 'fusions', 'fusion_candidates'):
            value = obj.get(key)
            if isinstance(value, list):
                return value
    return []


def variant_sources(base, files, kind):
    out = []
    for row in files:
        if s(row.get('file_type')).lower() != kind:
            continue
        p = resolved_source(base, row)
        entry = {'origin': kind, 'file': Path(s(row.get('path'))).name,
                 'sha256': s(row.get('sha256'), 70), 'rows': [], 'headers': [], 'warning': ''}
        if p is None:
            entry['warning'] = 'Collector kaynak dosyasi bulunamadi veya boyut sinirini asti.'
            out.append(entry)
            continue
        try:
            if p.suffix.lower() in ('.tsv', '.csv'):
                entry['headers'], entry['rows'] = _read_table(p, '\t' if p.suffix.lower() == '.tsv' else ',')
            elif p.suffix.lower() == '.json' and p.stat().st_size < 16 * 1024 * 1024:
                content = _read_json(p)
                entry['rows'] = [{s(k,100):s(v,2000) for k,v in x.items()} for x in content if isinstance(x, dict)][:MAX_TABLE_ROWS]
                entry['headers'] = list(entry['rows'][0]) if entry['rows'] else []
            else:
                entry['warning'] = 'Kaynak tablo biciminde degil: orijinal dosyada acilmali.'
        except (OSError, UnicodeError, ValueError, csv.Error) as exc:
            entry['warning'] = 'Kaynak tam okunamadi: ' + s(exc, 200)
        out.append(entry)
    return out


def _tsv_in_zip(zf, name, max_size=MAX_MEMBER_SIZE):
    paths = [p for p in zf.namelist() if Path(p).name.lower() == name.lower()]
    if len(paths) != 1:
        return []
    info = zf.getinfo(paths[0])
    if info.file_size > max_size:
        return []
    txt = zf.read(paths[0]).decode('utf-8-sig', errors='replace')
    return list(csv.DictReader(io.StringIO(txt), delimiter='\t'))


def _length_bases(zf, patient_id):
    paths = [n for n in zf.namelist() if Path(n).name.lower() == 'fastqc_sequence_length_distribution_plot.tsv']
    if len(paths) != 1 or zf.getinfo(paths[0]).file_size > MAX_MEMBER_SIZE:
        return None
    rows = list(csv.reader(io.StringIO(zf.read(paths[0]).decode('utf-8-sig', errors='replace')), delimiter='\t'))
    if len(rows) < 2:
        return None
    widths = [(i, number(x, 0)) for i,x in enumerate(rows[0]) if i>0]
    widths = [(i,w) for i,w in widths if w is not None]
    if not widths:
        return None
    used = []
    for row in rows[1:]:
        if not row:
            continue
        label = s(row[0])
        if not same_mp(label, patient_id) or not re.search(r'_(?:1|2)\Z', label):
            continue
        used.append((label, sum((number(row[i],0) or 0)*w for i,w in widths if len(row)>i)))
    # At least one R1 and R2, unique labels; avoids mixing unrelated patients.
    if len(used)<2 or len({k for k,_ in used}) != len(used):
        return None
    if not any(k.endswith('_1') for k,_ in used) or not any(k.endswith('_2') for k,_ in used):
        return None
    return sum(v for _,v in used)/1e9


def _numeric_evidence(rows, names, lo=0, hi=None):
    """Return a numeric value only when all matching source rows agree.

    Multiple general-stats rows for one MP are NOT inherently multiple patients:
    they often represent distinct MultiQC tools. A conflict in one metric must
    not erase other independently verifiable metrics. Never take the first row.
    """
    values = []
    for row in rows:
        found = numeric(row, *names, lo=lo, hi=hi)
        if found is not None:
            values.append(found)
    if not values:
        return None, False, 0
    if len({round(v, 5) for v in values}) == 1:
        return values[0], False, len(values)
    return None, True, len(values)


def _coverage_evidence(rows, names, *, lo=0, hi=None, generic=False):
    """Reject ambiguous generic Mean columns without a paired median column."""
    values = []
    for r in rows:
        v = numeric(r, *names, lo=lo, hi=hi)
        if v is None and generic:
            # Mean/Median without a mosdepth marker are too ambiguous.
            paired = numeric(r, 'Median Cov.', 'Median Coverage', 'Median', lo=0, hi=100000)
            if paired is not None:
                v = numeric(r, 'Mean', lo=lo, hi=hi)
        if v is not None:
            values.append(v)
    if not values:
        return None, False, 0
    if len({round(x, 5) for x in values}) == 1:
        return values[0], False, len(values)
    return None, True, len(values)


def _qc_single(path, patient_id, assay):
    out = {'file': path.name, 'assay': assay, 'metrics': {}, 'checks': [], 'warning': ''}
    if path.suffix.lower() != '.zip':
        out['warning'] = 'MultiQC ZIP disindaki QC kaynagi; otomatik yorumlanmadi.'
        return out
    warnings = []
    try:
        with zipfile.ZipFile(path) as zf:
            general = _tsv_in_zip(zf, 'general_stats_table.tsv')
            if not general:
                out['warning'] = 'general_stats_table.tsv bulunamadi.'
                return out
            matches = [r for r in general if same_mp(pick(r,'Sample'), patient_id)]
            if not matches:
                out['warning'] = 'Tekil MP/protokol eslesmesi yok; bu QC kaynaginda secilen protokole ait satir bulunamadi.'
                return out
            main = [r for r in matches if not re.search(r'(\.dupes|_L\d+_\d+)$',pick(r,'Sample'),re.I)]
            raw = [r for r in matches if re.search(r'_L\d+_\d+$',pick(r,'Sample'),re.I)]
            dup = [r for r in matches if pick(r,'Sample').lower().endswith('.dupes')]
            metrics = out['metrics']
            out['checks'].append('Collector record_key -> QC ZIP -> exact MP: %d eslesen satir; %d ana analiz satiri.' % (len(matches),len(main)))
            if len(main) > 1:
                out['checks'].append('Ana satirlar farkli MultiQC araclardan olabilir; degerler alan bazinda uzlastirilir.')
            if not main:
                warnings.append('Ana analiz satiri yok; kapsama metrikleri gosterilmiyor.')
            for key, names, hi, generic in (
                ('mean_depth_x', ('Mean Cov.','Mean Coverage'),100000,True),
                ('median_depth_x', ('Median Cov.','Median Coverage','Median'),100000,False),
                ('coverage_50x_pct', ('>= 50X','>=50X','50X','\u2265 50X'),100,False),
                ('coverage_100x_pct', ('>= 100X','>=100X','100X','\u2265 100X'),100,False),
                ('on_target_pct', ('On Target','On Target %','On-target Rate'),100,False)):
                value, conflict, count = _coverage_evidence(main,names,lo=0,hi=hi,generic=generic)
                if value is not None:
                    metrics[key] = value
                    out['checks'].append('%s: %d satirda ayni sayisal deger.' % (key,count))
                if conflict:
                    warnings.append('%s: ayni MP icin celiskili sayisal deger; kullanilmadi.' % key)
            # Raw per-lane information is only accepted if unique/identical;
            # no implicit mean of R1/R2 with unknown base weights.
            for key,names in (
                ('gc_pct',('GC content','GC','% GC')),
                ('q30_pct',('Q30','% Q30','Q30 Rate','Q30 bases','Bases Q30'))):
                value,conflict,count = _numeric_evidence(raw,names,lo=0,hi=100)
                if value is not None:
                    metrics[key]=value
                if conflict:
                    warnings.append('%s: R1/R2 veya lane degerleri farkli; agirlikli ortalama dogrulanmadi.'%key)
            filtered = _tsv_in_zip(zf, 'fastp_filtered_reads_plot.tsv')
            matched_filtered = [r for r in filtered if same_mp(pick(r,'Sample','Name'),patient_id)]
            count, conflict, evidence = _numeric_evidence(matched_filtered,
                ('Passed Filter','Passed Filter Reads','Reads After Filtering'),lo=0)
            if count is not None:
                # fastp plot numbers represent individual paired-end reads.
                metrics['paired_end_pairs_m']=count/2e6
            elif conflict:
                warnings.append('Filtrelenmis okuma sayilari celiskili; kullanilmadi.')
            if 'paired_end_pairs_m' not in metrics and len(raw)==1:
                reads=numeric(raw[0],'Reads After Filtering','Passed Filter Reads','Reads',lo=0)
                if reads is not None:
                    # general_stats values, unlike fastp_filtered plot, are millions.
                    metrics['paired_end_pairs_m']=reads/2.0
            bases = _length_bases(zf,patient_id)
            if bases is not None:
                metrics['clean_bases_gb']=bases
            value,conflict,_ = _numeric_evidence(dup,
                ('Duplication','% Duplication','Percent Duplication'),lo=0,hi=100)
            if value is not None: metrics['duplication_pct']=value
            if conflict: warnings.append('Duplikasyon satirlari celiskili; kullanilmadi.')
            if assay == 'DNA':
                verify = _tsv_in_zip(zf, 'verifybamid-results.tsv')
                rows = [r for r in verify if same_mp(pick(r,'Sample','Name'),patient_id)]
                value,conflict,_ = _numeric_evidence(rows,
                    ('Contamination (Seq)','FREEMIX','FREEMIX (Seq)','Contamination'),lo=0,hi=100)
                if value is not None: metrics['freemix_pct']=value
                if conflict: warnings.append('FREEMIX satirlari celiskili; kullanilmadi.')
            c50=metrics.get('coverage_50x_pct');c100=metrics.get('coverage_100x_pct')
            if c50 is not None and c100 is not None and c100 > c50 + 1e-5:
                metrics.pop('coverage_50x_pct',None); metrics.pop('coverage_100x_pct',None)
                warnings.append('100x kapsama 50x kapsama oranindan buyuk; iki esik de bloke edildi.')
            if not metrics:
                warnings.append('Eslesen MP icin uzlastirilmis sayisal QC alani bulunamadi.')
    except (OSError,ValueError,zipfile.BadZipFile,csv.Error) as exc:
        out['warning'] = 'QC ZIP okunamadi: '+s(exc,200)
        return out
    out['warning']=' '.join(warnings)
    return out


def qc_sources(base, files, sample):
    results=[]
    kind=s(sample.get('sample_type')).upper()
    for row in files:
        if s(row.get('file_type')).lower()!='qc':
            continue
        p=resolved_source(base,row)
        if not p:
            results.append({'file':Path(s(row.get('path'))).name,'assay':kind,
                            'metrics':{},'warning':'Collector QC kaynak dosyasi yok veya boyut siniri asildi.','checks':[]})
        else:
            results.append(_qc_single(p,s(sample.get('patient_id')),kind))
    return results


class ManualEntryStore:
    """Append-only patient notes; does not mutate main reporting or GenNext data."""
    def __init__(self,base):
        self.path=Path(base)/'DATA'/'bilgi_atlasi_hasta_notlari.sqlite3'

    def save(self,record_key,kind,content,source,acquisition,gene='',variant='',author=''):
        if not record_key or kind not in MANUAL_TYPES:
            raise ValueError('Hasta/analiz veya kategori secimi gerekli.')
        if not s(content,50000) or not s(source,600) or not s(acquisition,1000):
            raise ValueError('Bilgi, kaynak ve elde edilme yontemi zorunludur.')
        if len(content)>50000 or len(source)>600 or len(acquisition)>1000:
            raise ValueError('Metin uzunlugu siniri asildi.')
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with sqlite3.connect(self.path,timeout=8) as cx:
            cx.execute('''CREATE TABLE IF NOT EXISTS patient_atlas_manual_entries(
                id INTEGER PRIMARY KEY, record_key TEXT NOT NULL, kind TEXT NOT NULL,
                gene TEXT NOT NULL, variant TEXT NOT NULL, content TEXT NOT NULL,
                source TEXT NOT NULL, acquisition TEXT NOT NULL, author TEXT NOT NULL,
                created_at TEXT NOT NULL)''')
            cx.execute('INSERT INTO patient_atlas_manual_entries(record_key,kind,gene,variant,content,source,acquisition,author,created_at) VALUES(?,?,?,?,?,?,?,?,?)',
                       (record_key,kind,s(gene,100),s(variant,250),content,source,acquisition,s(author,100),now()))
            return cx.execute('SELECT last_insert_rowid()').fetchone()[0]

    def list_for(self,record_key):
        if not self.path.is_file():
            return []
        with sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro',uri=True) as cx:
            cx.row_factory=sqlite3.Row
            try:
                return [dict(r) for r in cx.execute('SELECT * FROM patient_atlas_manual_entries WHERE record_key=? ORDER BY id DESC LIMIT 5000',(record_key,))]
            except sqlite3.OperationalError:
                return []


class ManualReportCopyStore:
    """Locally pasted, UNVERIFIED Enlil report copies linked to ONE technical record.

    Separate from the clinical DB and GenNext; append-only and SHA-deduplicated.
    Does not claim a verified electronic signature or modify clinical decisions.
    """
    SCHEMA = 'ngs_atlas_manual_report_copy_v1'

    def __init__(self, base):
        self.path = Path(base) / 'DATA' / 'bilgi_atlasi_hasta_rapor_kopyalari.sqlite3'

    @staticmethod
    def identity(sample):
        if not isinstance(sample, dict):
            raise ValueError('Teknik kayit bilgisi yok.')
        assay = s(sample.get('sample_type'), 20).upper()
        rk = s(sample.get('record_key'), 300)
        run = s(sample.get('run_uuid'), 300)
        suid = s(sample.get('sample_uuid'), 300)
        patient = s(sample.get('patient_id'), 100).upper()
        if assay not in ALLOWED_ASSAYS or not rk or not run or not suid:
            raise ValueError('DNA/RNA/RUN/ornek kimligi eksik.')
        if rk != f'{run}::{suid}::{assay}':
            raise ValueError('Kayit kimligi ile RUN/ornek/eser eslesmiyor.')
        if not MP_RE.fullmatch(patient):
            raise ValueError('Tekil MP protokolu okunamadi.')
        return rk, run, suid, assay, patient

    def save(self, sample, report_text, retyped_mp):
        rk, run, suid, assay, patient = self.identity(sample)
        if s(retyped_mp,100).upper() != patient:
            raise ValueError('Girdiginiz MP numarasi secili teknik kayitla ayni degil. Rapor kaydedilmedi.')
        content = str(report_text or '').strip()
        if not 30 <= len(content) <= 60000:
            raise ValueError('Enlil rapor kopyasi 30-60000 karakter olmali.')
        # A report mentioning another MP is NOT safe to attach to this exact sample.
        expected = MP_RE.fullmatch(patient)
        desired = (int(expected.group(1)), expected.group(2))
        extracted = {(int(a),b) for a,b in MP_RE.findall(content)}
        if extracted and extracted != {desired}:
            raise ValueError('Rapor metninde baska bir MP protokolu goruldu. Karisik hastaya aktarim durduruldu.')
        digest = hashlib.sha256(content.encode('utf-8')).hexdigest()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path, timeout=12) as cx:
            cx.execute('PRAGMA busy_timeout=12000')
            cx.execute("""CREATE TABLE IF NOT EXISTS atlas_report_copies(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                record_key TEXT NOT NULL,
                run_uuid TEXT NOT NULL,
                sample_uuid TEXT NOT NULL,
                assay TEXT NOT NULL,
                patient_id TEXT NOT NULL,
                source TEXT NOT NULL,
                verification TEXT NOT NULL,
                report_text TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                imported_at TEXT NOT NULL,
                UNIQUE(record_key, sha256))""")
            exists = cx.execute('SELECT id FROM atlas_report_copies WHERE record_key=? AND sha256=?',
                                (rk,digest)).fetchone()
            if exists:
                return int(exists[0]), False
            cursor = cx.execute("""INSERT INTO atlas_report_copies(
                record_key,run_uuid,sample_uuid,assay,patient_id,source,verification,
                report_text,sha256,imported_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (rk,run,suid,assay,patient,'Enlil ekranindan kullanici tarafindan yapistirilan metin',
                 'E-IMZA VE KLINIK ONAY DOGRULANMADI',content,digest,now()))
            return int(cursor.lastrowid), True

    def list_for(self, sample):
        rk,run,suid,assay,patient = self.identity(sample)
        if not self.path.is_file():
            return []
        with sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro',uri=True,timeout=8) as cx:
            cx.execute('PRAGMA query_only=ON')
            cx.row_factory=sqlite3.Row
            try:
                rows = cx.execute("""SELECT * FROM atlas_report_copies
                    WHERE record_key=? AND run_uuid=? AND sample_uuid=? AND assay=? AND patient_id=?
                    ORDER BY id DESC LIMIT 100""", (rk,run,suid,assay,patient)).fetchall()
            except sqlite3.OperationalError:
                return []
        # Cross-check the stored SHA to detect corrupted report copies.
        verified=[]
        for row in rows:
            item=dict(row)
            if hashlib.sha256(item['report_text'].encode('utf-8')).hexdigest()!=item['sha256']:
                continue
            verified.append(item)
        return verified


def report_biomarker_disagreements(data,complete):
    """Compare copied/saved report text to independent sample fields; DO NOT override."""
    if s(data.get('sample',{}).get('sample_type')).upper()!='DNA':return []
    sample=data.get('sample',{})
    override=data.get('overrides') or {}
    raw=override.get('hrd')
    if raw is None or s(raw)=='':raw=sample.get('hrd')
    raw_text=s(raw,100)
    observed=re.match(r'^\s*([0-9]+(?:[.,][0-9]+)?)',raw_text)
    if not observed:return []
    field_val=number(observed.group(1),0,100000)
    if field_val is None:return []
    out=[];seen=set()
    for r in complete.get('reports',[]):
        if r.get('origin') not in ('RAPORLAMA_MUTASYON_YORUMU',
                    'ATLAS_MANUEL_ENLIL_RAPOR_KOPYASI','archive_report_documents'):
            continue
        body=s(r.get('text'),MAX_REPORT)
        m=re.search(r'(?i)\bHRD\b[^\n\r]{0,60}?\bskor(?:u)?\s*[:=]?\s*([0-9]+(?:[.,][0-9]+)?)',body)
        if not m:continue
        from_report=number(m.group(1),0,100000)
        if from_report is None or abs(from_report-field_val)<1e-8:continue
        label=s(r.get('label'),150)
        key=(from_report,field_val,label)
        if key in seen:continue
        seen.add(key)
        out.append('HRD UYUSMAZLIGI: '+label+' skoru '+format_value(from_report)+
            '; teknik biyobelirtec kaydi '+format_value(field_val)+
            '. Otomatik duzeltme YAPILMADI; hekim kontrolu gerekli.')
    return out[:8]


def gather_complete(base, data):
    rk=s(data['sample'].get('record_key'))
    if not rk or rk != f"{data['sample'].get('run_uuid') or 'NO_RUN'}::{data['sample'].get('sample_uuid') or 'NO_SAMPLE'}::{s(data['sample'].get('sample_type')).upper()}":
        raise ValueError('RUN/sample/assay eslesmesi bulunamadi.')
    issues=[]
    out={'reports':latest_report_sources(base,rk, data['sample'].get('case_family') or data['sample'].get('patient_id') or '',data['sample'].get('patient_id') or '',s(data['sample'].get('sample_type')).upper(),issues=issues),'reported_from_text':[],
         'raw':{},'qc':[], 'manual':ManualEntryStore(base).list_for(rk), 'closure':{},'reporting_warnings':issues}
    # A local manual report-closure status is not evidence of a signed Enlil report.
    with open_readonly_db(base) as cx:
        if cx.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='manual_report_closure'").fetchone():
            item=cx.execute('SELECT status,note,recorded_by,updated_at FROM manual_report_closure WHERE record_key=?',(rk,)).fetchone()
            if item is not None:out['closure']=dict(item)
    # Explicitly attached Enlil text is NOT an approved clinical report;
    # exact technical identity and content checksum are checked at read time.
    for item in ManualReportCopyStore(base).list_for(data['sample']):
        out['reports'].append({
            'origin': 'ATLAS_MANUEL_ENLIL_RAPOR_KOPYASI',
            'status': 'MANUEL KOPYA / E-IMZA VE KLINIK ONAY DOGRULANMADI',
            'label': 'Secilen RUN/ornek/DNA-RNA kaydina elle bagli Enlil raporu #' + str(item['id']),
            'created_at': s(item['imported_at']), 'text': item['report_text'],
        })
    out['reported_from_text']=extract_reported_candidates(out['reports'])
    out['biomarker_disagreements']=report_biomarker_disagreements(data,out)
    for kind in VARIANT_TYPES:
        out['raw'][kind]=variant_sources(base,data.get('files',[]),kind)
    out['qc']=qc_sources(base,data.get('files',[]),data['sample'])
    return out


def display_reports(data, complete):
    lines=['YEREL RAPORLAR VE RAPORLANMIS VARYANT GECMISI',
           'Bu kaynaklar farkli is akislari. Onayli klinik rapor olarak esitlenmez.','']
    if complete.get('closure'):
        c=complete['closure']
        lines.append('MANUEL RAPOR KAPANIS DURUMU (Enlil e-imza veya varyant listesi kaniti DEGIL)')
        lines.append(' - '+s(c.get('status'),80)+' | '+s(c.get('updated_at'),50)+' | '+s(c.get('note'),600))
        lines.append('')
    if data.get('reported'):
        lines.append('RAPORLANMIS VARYANT INDEKSI [reported_variant_history]')
        for r in data['reported']:
            lines.append(' - '+s(r.get('gene'))+' '+s(r.get('protein') or r.get('cdna') or r.get('feature')) +
                         ' | kaynak='+s(r.get('source')))
        lines.append('')
    else:
        lines.append('reported_variant_history: Bu teknik kayitta onay bayrakli indeks kaydi bulunamadi.\n')
    if data.get('decisions'):
        lines.append('MANUEL RAPOR KARARLARI [variant_decisions; rapor metni degil]')
        for d in data['decisions']:
            lines.append(' - '+s(d.get('variant_key'),200)+' | '+s(d.get('status'),50)+' | '+s(d.get('note'),400))
        lines.append('')
    if complete['reported_from_text']:
        lines.append('RAPOR METNINDEN AYRISTIRILAN POZITIF ADAYLAR - MUTLAKA KONTROL')
        for v in complete['reported_from_text']:
            lines.append(' - '+s(v['gene'])+' '+s(v['protein'] or v['cdna'] or v['feature'])+
                         ' | kaynak='+v['label']+' ['+v['source_status']+']')
    else:
        lines.append('Ek rapor metinlerinden pozitif varyant cikarilamadi; orijinal metinleri inceleyin.')
    lines.append('')
    for r in complete['reports']:
        lines.extend(['--- '+r['label']+' ['+r['origin']+' / '+r['status']+'] ---',r['text'] or '(metin yok)',''])
    if not complete['reports']:
        lines.append('Bu teknik kayitta YEREL rapor metni kaydi yok. Enlil imzali raporunu otomatik olarak okudugumuzu varsaymayin.')
    for warning in complete.get('reporting_warnings',[]):
        lines.append('KAYNAK UYARISI: '+s(warning,300))
    for warning in complete.get('biomarker_disagreements',[]):
        lines.append('BIYOBELIRTEC KONTROLU: '+s(warning,450))
    return '\n'.join(lines)


def display_qc(complete, data):
    lines=['TEKNIK QC - SECILEN DNA/RNA KAYDI',
           'Sadece record_key ile bagli QC dosyalari ve tekil MultiQC MP satiri kullanilir.',
           'Varyant DP degeri ortalama derinlik yerine gecmez.',
           '']
    sample=data['sample']
    depth=s(sample.get('depth'))
    if depth:
        lines.append('GenNext samples.depth alani: '+depth+' [olcum tipi dogrulanmadan Mean Depth sayilmaz]')
    keys=[('paired_end_pairs_m','Paired-end okuma cifti', ' milyon'),
          ('mean_depth_x','Ortalama hedef derinligi','x'),('median_depth_x','Medyan derinlik','x'),
          ('coverage_50x_pct','Hedef >=50x kapsama','%'),('coverage_100x_pct','Hedef >=100x kapsama','%'),
          ('gc_pct','GC','%'),('q30_pct','Q30','%'),('clean_bases_gb','Filtrelenmis temiz veri',' Gb'),
          ('duplication_pct','Duplikasyon','%'),('freemix_pct','FREEMIX','%')]
    if not complete.get('qc'):
        lines.append('Collector tablosunda bu teknik kayda bagli QC kaynagi bulunamadi.')
    for index, item in enumerate(complete.get('qc',[]),1):
        lines.extend(['',f"QC #{index} | {item.get('file','')}",
                      'Kaynak: files (exact record_key) -> MultiQC ZIP -> eslesen MP / analiz satiri'])
        for key,label,unit in keys:
            if key=='freemix_pct' and s(sample.get('sample_type')).upper()!='DNA':
                continue
            value=item.get('metrics',{}).get(key)
            lines.append(' - '+label+': '+format_value(value,unit))
        for ch in item.get('checks',[]):lines.append('Dogrulama: '+ch)
        if item.get('warning'):lines.append('UYARI: '+item['warning'])
    return '\n'.join(lines)


def variant_summary(row,kind):
    if kind=='snv':
        return {'gene':pick(row,'GeneName','Gene_refGene','Gene.refGene','Gene','GeneSymbol','Hugo_Symbol'),
                'change':pick(row,'aaChange','AAChange','HGVSp','HGVSp_Short','ProteinChange','Protein','AAChange.refGene','HGVSc','cDNA','Change'),
                'oncogenic':pick(row,'Oncogenic','Oncogenicity'),
                'filter':pick(row,'Filter','FILTER'),
                'vaf':pick(row,'AF','VAF','Variant Allele Fraction'),
                'ad':pick(row,'AD','AltDepth','Alt Reads'),
                'dp':pick(row,'DP','Depth','TotalDepth')}
    if kind=='cnv':
        return {'gene':pick(row,'GeneName','Gene_refGene','Gene.refGene','Gene','GeneSymbol'),
                'change':pick(row,'SVType','CNV','CopyNumber','CopyNumberStatus'),
                'oncogenic':pick(row,'Oncogenic','Oncogenicity'),
                'filter':pick(row,'Filter','FILTER'),
                'vaf':pick(row,'FoldChange','Fold Change','FC'),
                'ad':pick(row,'ProbeCount','Probe Number','Probes'),
                'dp':''}
    return {'gene':pick(row,'Fusion','FusionName','gene','Gene','LeftGene','Gene_5prime'),
            'change':pick(row,'RightGene','Gene_3prime','Partner','Change'),
            'oncogenic':'','filter':pick(row,'Filter','Status'),
            'vaf':pick(row,'SplitReads','JunctionReads'),
            'ad':pick(row,'SpanningReads','SpanningFragments'), 'dp':''}


def redact_token(value, max_len=85):
    # White-list only molecular tokens. No arbitrary free-text annotations.
    val=s(value,max_len)
    if MP_RE.search(val) or re.fullmatch(r'\d{11}',val) or re.fullmatch(r'(?i)RUN[0-9]+|PATIENT.*|SAMPLE[0-9]+',val):
        return ''
    return val if re.fullmatch(r'[A-Za-z0-9_.+*?:>=/()-]{1,85}',val) else ''


def _safe_biomarker_ai(value):
    """Accept structured numeric biomarker values only; reject arbitrary free text."""
    raw=s(value,100)
    if not raw:
        return ''
    # E.g. 2.62 Muts/MB | TMB-L ; 4.05% | MSS ; 0.
    pattern=(r'[0-9]{1,8}(?:[.,][0-9]{1,8})?'
             r'(?:\s*(?:%|Muts?/MB|Mutations?/MB))?'
             r'(?:\s*\|\s*(?:TMB-[HL]|TMB-HIGH|TMB-LOW|MSS|MSI-H|MSI-L|HRD-[HL]|HRD\+|HRD-))?')
    return raw if re.fullmatch(pattern,raw,flags=re.I) else ''


def generate_deidentified(data, complete, include_raw=True):
    """Local-only technical export; reviewed candidates are distinct from reports.

    Never include MP, RUN, sample UUID, free-text clinical or AI rationale.
    This is not proof of de-identification; genomic findings carry residual risk.
    """
    sample=data['sample']; assay=s(sample.get('sample_type')).upper()
    out=['KIMLIK BILGISI CIKARILMIS NGS TEKNIK VERI - HEKIM KONTROLU',
         'Hasta/protokol/RUN/UUID/TC/ad/soyad/blok/tarih/dosya yolu icermemesi hedeflenmistir.',
         'Genomik bulgularin kendisi nadiren yeniden tanimlanabilir; kurum politikasi olmadan internete gondermeyin.',
         'Yeni YZ veya ag cagrisi yapilmaz.\n',
         'ANALIZ TIPI: '+assay]
    if assay=='DNA':
        for key in ('tmb','msi','hrd'):
            # An explicit zero must remain zero, not be lost to boolean fallback.
            override=data.get('overrides') or {}
            raw=override.get(key)
            if raw is None or str(raw).strip()=='':
                raw=sample.get(key)
            val=_safe_biomarker_ai(raw)
            if val:out.append(key.upper()+': '+val)
            elif raw is not None and str(raw).strip():
                out.append(key.upper()+': Veri formati kimliksiz cikti icin dogrulanamadi')
    out.append('')
    for i,item in enumerate(complete.get('qc',[]),1):
        out.append('QC kaynagi #'+str(i)+' (yerel, eslesmis veri)')
        for key,value in item.get('metrics',{}).items():
            if isinstance(value,(int,float)) and not isinstance(value,bool):
                out.append(' - '+key+': '+format_value(value))
        if item.get('warning'):
            out.append(' - Eksik / belirsiz QC: kaynak veya eslesme kontrolu gerekli.')
    out.append('')
    out.append('RAPORLANMIS VARYANT INDEKSI (yerel gecmis kaydi; imzali rapor kaniti degil)')
    reported_count=0
    for r in data.get('reported',[]):
        gene=redact_token(r.get('gene'),30)
        change=redact_token(r.get('protein') or r.get('cdna') or r.get('feature'))
        if gene:
            out.append(' - '+gene+' '+change)
            reported_count+=1
    if not reported_count:
        out.append(' - Yerel onay bayrakli indeks verisi yok; negatif sonuc ANLAMINA GELMEZ.')
    out.append('RAPOR METNINDEN AYRISTIRILAN ADAYLAR (klinik dogrulama gerekli)')
    extracted_count=0
    for r in complete.get('reported_from_text',[]):
        gene=redact_token(r.get('gene'),30)
        change=redact_token(r.get('protein') or r.get('cdna') or r.get('feature'))
        if gene:
            out.append(' - '+gene+' '+change)
            extracted_count+=1
    if not extracted_count:
        out.append(' - Yerel rapor kopyasindan aday saptanmadi; klinik rapor NEGATIF sayilamaz.')
    if complete.get('biomarker_disagreements'):
        out.append('BIYOBELIRTEC UYARISI: HRD, kayitli rapor taslagi/metni ile teknik ornek arasinda uyusmuyor; YZ oncesi hekim kontrolu gerekli.')
    if data.get('selection_valid'):
        from ngs_bilgi_atlasi_hasta import safe_json
        for r in safe_json(data['selection'].get('selected_json'),[]):
            if not isinstance(r,dict):continue
            gene=redact_token(r.get('gene') or r.get('fusion'),85)
            ch=redact_token(r.get('protein') or r.get('cdna'))
            if gene:out.append('GENNEXT SECIM (teknik, klinik onay degil): '+gene+' '+ch)
    # Show technical review targets without copying free-text AI/clinical notes.
    # A previous AI review is NOT a confirmed oncogenic finding or reported variant.
    reviewed=data.get('ai_variant') or []
    out.append('')
    out.append('ONCEKI YZ INCELEME ADAYLARI (teknik kontrol listesi; raporlanmis bulgu DEGIL)')
    seen=set(); displayed=0
    for r in reviewed:
        gene=redact_token(r.get('gene'),35)
        change=redact_token(r.get('change') or r.get('protein') or r.get('cdna'),85)
        if not gene or not change or (gene,change) in seen:continue
        seen.add((gene,change));displayed+=1
        out.append(' - '+gene+' '+change+' [onceki YZ incelemesi; klinik dogrulama gerekli]')
        if displayed>=24:break
    if not displayed:
        out.append(' - Kayit yok; aday mutasyon bulunmadigi anlamina gelmez.')
    if len(reviewed)>displayed:
        out.append(' - Diger inceleme kayitlari yerel YZ/IGV sekmesinde gorulebilir.')
    igv=data.get('igv') or []
    if igv:
        out.append('IGV teknik inceleme/kuyruk kaydi: '+str(len(igv))+' (klinik onay kaniti DEGIL)')
    out.append('')
    if not include_raw:
        out.append('HAM SNV/CNV/RNA LISTELERI BU KISA YZ METNINE EKLENMEDI.')
        out.append('Ham veri ve tum varyantlar yerel Atlas ekraninda gorulebilir; eksiklik NEGATIF sonuca isaret etmez.')
    else:
        for kind in ('snv','cnv','fusion'):
            sources=complete.get('raw',{}).get(kind,[])
            out.append('\n'+kind.upper()+' HAM VARYANTLAR (tumu, yalniz beyaz listedeki teknik alanlar)')
            for source in sources:
                for r in source['rows']:
                    v=variant_summary(r,kind)
                    gene=redact_token(v['gene'],45)
                    if not gene:continue
                    fields=[('gene',gene),('change',redact_token(v['change'])),
                            ('oncogenic',redact_token(v['oncogenic'])),('filter',redact_token(v['filter'])),
                            ('VAF_or_FC',redact_token(v['vaf'])),('AD_or_probe',redact_token(v['ad'])),
                            ('DP',redact_token(v['dp']))]
                    out.append(' - '+' | '.join(k+'='+vv for k,vv in fields if vv))
                if source['warning']:
                    out.append('Kaynak eksik/okunamadi; negatif sonuc cikarilamaz.')
    return '\n'.join(out)
