# -*- coding: utf-8 -*-
"""Patient-scoped Atlas reader and local draft workspace for NGS v0.4.95.

All source queries use a read-only connection and an exact technical record_key.
General Atlas knowledge and patient data remain in separate SQLite databases.
No network, API, GenNext or Enlil login, no clinical sign-off, no report overwrites.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox

MAX_RESULTS = 80
MAX_TEXT = 120000
VALID_ASSAYS = {'DNA', 'RNA'}
FINAL_RUN = {'COMPLETED', 'COMPLETE', 'DONE', 'FINISHED', 'SUCCEEDED', 'SUCCESS'}
SINGLE_MP = re.compile(r'MP[0-9]{1,5}-{1,2}[0-9]{2,4}\Z', re.I)


def text(value, limit=1500):
    return str(value if value is not None else '').strip()[:limit]


def rowdict(row):
    return dict(row) if row else {}


def safe_json(value, default):
    try:
        obj = json.loads(value or '')
        return obj if isinstance(obj, type(default)) else default
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def label_variant(row):
    typ = text(row.get('variant_type'), 30).upper()
    gene = text(row.get('gene'), 70)
    change = text(row.get('protein') or row.get('cdna') or row.get('feature') or row.get('change'), 160)
    if typ == 'FUSION' and not gene:
        gene = text(row.get('feature'), 100)
    if not gene:
        gene = text(row.get('variant_key'), 120) or 'Belirtilmemiş'
    return ' '.join(part for part in (gene, change) if part).strip() + ((' [' + typ + ']') if typ and typ != 'SNV' else '')


def run_ready(sample):
    status = text(sample.get('run_status')).upper()
    sub = text(sample.get('sample_status')).upper()
    return status in FINAL_RUN and (not sub or sub in FINAL_RUN)


def selection_verified(snap, state, sample, expected_scope):
    """Only source-verified selections may appear as a *selection* in draft."""
    if not snap or not state or not run_ready(sample):
        return False, 'RUN, analiz veya kaynak dogrulamasi eksik'
    if expected_scope != text(snap.get('clinical_scope')):
        return False, 'DNA/RNA analiz kapsami uyusmuyor'
    if not SINGLE_MP.fullmatch(text(sample.get('patient_id'))):
        return False, 'Tekil MP / hasta protokolu dogrulanmadi'
    if not state.get('analyzed') and state.get('analyzed') != 0:
        return False, 'Son Analyzed durumu yok'
    if str(state.get('analyzed')) != str(snap.get('analyzed')):
        return False, 'Son Analyzed durumu degismis'
    if text(state.get('last_error')):
        return False, 'Son kaynak kontrolu hatali'
    attempt = text(state.get('last_attempt_at'))
    checked = text(snap.get('checked_at'))
    if not checked or (attempt and attempt > checked):
        return False, 'Secim sonrasi dogrulanmamis kontrol var'
    try:
        selected = json.loads(snap.get('selected_json') or '')
    except (TypeError, ValueError):
        return False, 'Selection JSON verisi gecersiz'
    if not isinstance(selected, list) or any(not isinstance(x,dict) for x in selected):
        return False, 'Selection satir yapisi gecersiz'
    if expected_scope=='RNA_FUSION_ONLY' and any(not text(x.get('fusion')) for x in selected):
        return False, 'Fusion secim adi eksik'
    if expected_scope=='SNV_INDEL_ONLY' and any(not text(x.get('gene')) for x in selected):
        return False, 'SNV secim geni eksik'
    if snap.get('included_count') is None or int(snap['included_count']) != len(selected):
        return False, 'Secim sayaclari tutarsiz'
    if not bool(snap.get('analyzed')):
        return False, 'Analiz tamamlanmamis'
    return True, ''


class PatientReadOnly:
    """Reads one exact technical record through immutable SQL text and bound values."""
    def __init__(self, base):
        self.base = Path(base)
        self.path = self.base / 'DATA' / 'collector_state.sqlite3'

    def _connect(self):
        if not self.path.is_file():
            raise FileNotFoundError('Hasta veri tabani bulunamadi: DATA/collector_state.sqlite3')
        conn = sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True, timeout=8)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA query_only=ON')
        conn.execute('PRAGMA busy_timeout=8000')
        return conn

    @staticmethod
    def tables(cx):
        return {r[0] for r in cx.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    @staticmethod
    def one(cx, tables, table, where, params):
        if table not in tables:
            return {}
        # Fixed query names; never pass a table/where value from a UI field.
        return rowdict(cx.execute('SELECT * FROM ' + table + ' WHERE ' + where + ' LIMIT 1', params).fetchone())

    @staticmethod
    def many(cx, tables, table, where, params, order='', limit=150):
        if table not in tables:
            return []
        sql = 'SELECT * FROM ' + table + ' WHERE ' + where + (' ORDER BY ' + order if order else '') + ' LIMIT ?'
        return [dict(x) for x in cx.execute(sql, (*params, min(limit, 25000)))]

    def search(self, query='', limit=MAX_RESULTS):
        q = text(query, 80)
        with self._connect() as cx:
            tables = self.tables(cx)
            if 'samples' not in tables:
                raise RuntimeError('samples tablosu bulunamadi')
            # Search is confined to local technical labels/pool, never by TC or UUID.
            sql = '''SELECT record_key, patient_id, case_family, pool_name, sample_type,
                            run_uuid, updated_at, is_rerun FROM samples'''
            params = []
            if q:
                sql += ' WHERE patient_id LIKE ? OR case_family LIKE ? OR pool_name LIKE ?'
                params = [f'%{q}%'] * 3
            sql += ' ORDER BY updated_at DESC, id DESC LIMIT ?'
            return [dict(r) for r in cx.execute(sql, (*params, min(max(1, limit), 150)))]

    def read(self, record_key):
        rk = text(record_key, 300)
        if not rk:
            raise ValueError('Teknik ornek secilmedi')
        with self._connect() as cx:
            cx.execute('BEGIN')
            tables = self.tables(cx)
            s = self.one(cx, tables, 'samples', 'record_key=?', (rk,))
            if not s:
                raise ValueError('Secilen teknik kayit veri tabaninda yok')
            assay = text(s.get('sample_type')).upper()
            if assay not in VALID_ASSAYS:
                raise ValueError('DNA/RNA analiz tipi dogrulanamadi')
            expected = f"{s.get('run_uuid') or 'NO_RUN'}::{s.get('sample_uuid') or 'NO_SAMPLE'}::{assay}"
            if expected != rk:
                raise ValueError('RUN/sample/assay kimligi tutarsiz, aktarim durduruldu')
            family = s.get('case_family') or s.get('patient_id') or ''
            data = {
                'sample': s,
                'case': self.one(cx, tables, 'case_metadata', 'case_family=?', (family,)),
                'block': self.one(cx, tables, 'sample_metadata', 'record_key=?', (rk,)),
                'overrides': self.one(cx, tables, 'manual_biomarker_overrides', 'record_key=?', (rk,)),
                'assay': self.one(cx, tables, 'archive_assay_results', 'record_key=?', (rk,)),
                'reported': self.many(cx, tables, 'reported_variant_history', 'record_key=? AND confirmed=1', (rk,), 'id DESC', 12000),
                'decisions': self.many(cx, tables, 'variant_decisions', 'record_key=?', (rk,), 'updated_at DESC', 12000),
                'observed': self.many(cx, tables, 'oncogenic_observations', 'record_key=?', (rk,), 'id DESC', 20000),
                'ai_patient': self.many(cx, tables, 'ai_patient_reviews', 'record_key=?', (rk,), 'id DESC', 500),
                'ai_variant': self.many(cx, tables, 'ai_variant_reviews', 'record_key=?', (rk,), 'id DESC', 5000),
                'igv': self.many(cx, tables, 'igv_queue', 'record_key=?', (rk,), 'id DESC', 5000),
                'files': self.many(cx, tables, 'files', 'record_key=?', (rk,), 'id DESC', 5000),
                'selection': {}, 'selection_status': {}, 'selection_valid': False, 'selection_reason': '',
                'report_doc': {},
            }
            if assay == 'RNA':
                current, status, scope = 'gennext_fusion_current', 'gennext_fusion_status', 'RNA_FUSION_ONLY'
            else:
                current, status, scope = 'gennext_selection_current', 'gennext_selection_status', 'SNV_INDEL_ONLY'
            data['selection'] = self.one(cx, tables, current, 'record_key=?', (rk,))
            data['selection_status'] = self.one(cx, tables, status, 'record_key=?', (rk,))
            if data['selection']:
                data['selection_valid'], data['selection_reason'] = selection_verified(
                    data['selection'], data['selection_status'], s, scope)
            if 'archive_report_documents' in tables:
                data['report_doc'] = rowdict(cx.execute(
                    'SELECT id,version,title,document_status,created_at FROM archive_report_documents '
                    'WHERE record_key=? ORDER BY version DESC LIMIT 1', (rk,)).fetchone())
            cx.commit()
            return data


def linked_atlas_notes(store, genes, max_each=4):
    """Only curated Atlas entries, with exact gene in title/tags; never code index."""
    result = []
    seen = set()
    for gene in list(dict.fromkeys(genes))[:20]:
        if not re.fullmatch(r'[A-Z][A-Z0-9-]{1,25}', gene):
            continue
        rows, total = store.list_entries(query=gene, limit=100)
        count = 0
        for e in rows:
            if e.get('generated') or e.get('status') == 'Gecersiz/eski':
                continue
            if not re.search(r'(?<![A-Za-z0-9])' + re.escape(gene) + r'(?![A-Za-z0-9])',
                             (e.get('title') or '') + ' ' + (e.get('tags') or ''), re.I):
                continue
            if e['id'] in seen:
                continue
            seen.add(e['id']); count += 1
            result.append({'gene': gene, 'title': e['title'], 'summary': e.get('summary') or '',
                           'status': e['status'], 'source_kind': e['source_kind'],
                           'source_label': e.get('source_label') or '', 'source_uri': e.get('source_uri') or '',
                           'acquisition': e.get('acquisition') or '', 'entry_id': e['id']})
            if count >= max_each:
                break
    return result


def build_draft(data, atlas_notes=None):
    """Deterministic, source-tagged research draft. No medical inference or treatment advice."""
    s, cm, block = data['sample'], data['case'], data['block']
    assay = text(s.get('sample_type')).upper()
    rk = text(s.get('record_key'))
    lines = [
        'HASTAYA ÖZEL NGS DEĞERLENDİRME TASLAĞI - HEKİM KONTROLÜ GEREKLİ',
        'Klinik rapor değildir; otomatik onay veya rapor aktarımı yapılmaz.',
        '',
        'TEKNİK KAYIT',
        'Protokol: ' + (text(s.get('patient_id')) or 'Belirtilmemiş'),
        'Analiz: ' + assay + ' | Pool: ' + (text(s.get('pool_name')) or '-'),
        'Tanısal ön bilgi: ' + (text(cm.get('diagnosis')) or 'Kayıt yok') + ' [case_metadata: manuel/Enlil; teyit gerekli]',
        'Blok: ' + (text(block.get('block_id')) or '-') +
        ' | Tümör oranı: ' + (text(block.get('tumor_percentage')) or '-') + ' [sample_metadata]',
        '',
        'KAYITLI RAPOR VARYANTLARI [reported_variant_history; onaylı klinik rapor yerine geçmez]',
    ]
    decision_map = {text(x.get('variant_key')): x for x in data['decisions']}
    observation_map = {text(x.get('variant_key')): x for x in data['observed']}
    valid = []
    conflict = []
    seen = set()
    for v in data['reported']:
        key = text(v.get('variant_key'))
        if not key or key in seen:
            continue
        seen.add(key)
        d = decision_map.get(key) or decision_map.get('SNV|' + key) or {}
        if text(d.get('status')).lower() in {'artifact', 'not_reported'}:
            conflict.append(label_variant(v))
            continue
        valid.append(v)
        obs = observation_map.get(key) or observation_map.get('SNV|' + key) or {}
        extra = []
        if text(obs.get('oncogenic')):
            extra.append('Oncogenicity=' + text(obs['oncogenic']))
        for attr in ('af', 'ad', 'dp'):
            if obs.get(attr) is not None and text(obs.get(attr)):
                extra.append(attr.upper() + '=' + text(obs[attr]))
        lines.append('- ' + label_variant(v) + (' | ' + ', '.join(extra) if extra else '') +
                     ' [kaynak=' + (text(v.get('source'), 70) or 'rapor gecmisi') + ']')
    if not valid:
        lines.append('- Doğrulanmış rapor geçmişinde gösterilebilen varyant yok. Bu NEGATİF sonuç anlamına gelmez.')
    if conflict:
        lines.extend(['', 'UYUŞMAZLIK - RAPORDA KULLANMAYIN'])
        lines.extend('- ' + x + ' | Geçmiş rapor ile mevcut manuel karar çelişiyor' for x in conflict)
    lines.extend(['', 'ONKOJENİK VARYANT İNDEKSİ [oncogenic_observations; t\u00fcm ham varyantlar DE\u011e\u0130L]'])
    if data['observed']:
        for obs in data['observed'][:100]:
            # This is a local observation only, not a clinical reporting decision.
            detail = ' '.join(x for x in (text(obs.get('gene'), 40), text(obs.get('change'), 100)) if x)
            lines.append('- ' + (detail or text(obs.get('variant_key'),100)) +
                         ' | Oncogenicity: ' + (text(obs.get('oncogenic'), 60) or 'Kayıt yok') +
                         ' | FILTER=' + (text(obs.get('filter'), 40) or '-') +
                         ' | VAF=' + (text(obs.get('af'), 50) or '-') +
                         ' | AD=' + (text(obs.get('ad'), 50) or '-') +
                         ' | DP=' + (text(obs.get('dp'), 50) or '-') +
                         ' [teknik indeks; raporlanmış bulgu sayılmaz]')
    else:
        lines.append('- Onkojenik indeks kaydı yok; bu mutasyon yokluğu anlamına gelmez.')
    lines.extend(['', 'GENNEXT SEÇİM BİLGİSİ [seçili rapor maddeleri; tüm varyant araması değildir]'])
    sel = data['selection']
    if not sel:
        lines.append('- Kayıt yok; buradan negatiflik cikarsanamaz.')
    elif not data['selection_valid']:
        lines.append('- Eski/eksik Selection var, kullanılamaz: ' + text(data['selection_reason']))
    else:
        chosen = safe_json(sel.get('selected_json'), [])
        if not chosen:
            lines.append('- GenNext kontrolünde seçilmiş madde yok; panel NEGATİF sonuç olarak yorumlanamaz.')
        else:
            for v in chosen[:100]:
                if assay == 'RNA':
                    desc = text(v.get('fusion') or (text(v.get('gene_5prime')) + '::' + text(v.get('gene_3prime'))))
                else:
                    desc = ' '.join(x for x in (text(v.get('gene')), text(v.get('protein') or v.get('cdna'))) if x)
                lines.append('- ' + (desc or '[Isimsiz secim]') + ' [GenNext doğrulanmış seçimi; klinik kontrol gerekir]')
    if assay == 'DNA':
        override = data['overrides']
        lines.extend(['', 'DNA BİYOBELİRTEÇLERİ [samples; manuel düzeltme varsa overrides]'])
        for key, title in [('tmb','TMB'),('msi','MSI'),('hrd','HRD')]:
            item = text(override.get(key)) if override else ''
            source = 'manual_biomarker_overrides' if item else 'samples'
            item = item or text(s.get(key))
            lines.append('- ' + title + ': ' + (item or 'Veri yok') + ' [' + source + ']')
    else:
        assay_state = data['assay']
        if assay_state.get('result_state'):
            lines.extend(['', 'RNA MANUEL SONUÇ DURUMU [archive_assay_results; kaydı oluşturan hekimin kontrolü gerekir]',
                          '- ' + text(assay_state['result_state']) + ' | ' + text(assay_state.get('note'), 1000)])
    lines.extend(['', 'MANUEL VE ENLİL KAYITLARI'])
    if text(cm.get('note')):
        lines.append('- Klinik not: ' + text(cm.get('note'), 4000) + ' [case_metadata; manuel]')
    if text(block.get('note')):
        lines.append('- Blok notu: ' + text(block.get('note'), 4000) + ' [sample_metadata; manuel]')
    if text(cm.get('enlil_last_lookup_at')):
        lines.append('- Enlil son sorgu: ' + text(cm['enlil_last_lookup_at']) +
                     ' | biyopsi no: ' + text(cm.get('enlil_biopsy_no')) + ' [case_metadata]')
    if not any(text(x) for x in (cm.get('note'), block.get('note'), cm.get('enlil_last_lookup_at'))):
        lines.append('- Manuel/Enlil ek açıklama kaydı yok.')
    lines.extend(['', 'ELLE VERİLMİŞ VARYANT KARARLARI [variant_decisions]'])
    if data['decisions']:
        for decision in data['decisions'][:50]:
            lines.append('- ' + text(decision.get('variant_key'), 120) +
                         ' | karar=' + (text(decision.get('status'), 80) or '-') +
                         ' | not=' + text(decision.get('note'), 500) +
                         ' [manuel karar; genetik rapor/onay ile ayn\u0131 de\u011fil]')
    else:
        lines.append('- Varyant düzeyinde manuel karar kaydı yok.')
    lines.extend(['', 'YZ DEĞERLENDİRMELERİ [KLİNİK DOĞRULAMA DEĞİL]'])
    if data['ai_patient']:
        last = data['ai_patient'][0]
        lines.append('- ' + text(last.get('summary'), 2400) + ' [ai_patient_reviews, oturum=' +
                     text(last.get('session_id'), 50) + ', ' + text(last.get('imported_at'), 50) + ']')
    else:
        lines.append('- Hasta bazında YZ değerlendirme kaydı yok.')
    if data['ai_variant']:
        lines.append('- Varyant baz\u0131nda son YZ notlar\u0131:')
        seen_ai = set()
        for av in data['ai_variant']:
            key = text(av.get('variant_key'), 120)
            if key in seen_ai:
                continue
            seen_ai.add(key)
            lines.append('  * ' + ' '.join(x for x in (text(av.get('gene'), 40),
                text(av.get('change'), 100)) if x) +
                ' | karar taslagi=' + text(av.get('report_recommendation'), 100) +
                ' | guven=' + text(av.get('confidence'), 40) +
                ' | gerekce=' + text(av.get('rationale'), 400) +
                ' [ai_variant_reviews; dogrulama gerekli]')
            if len(seen_ai)>=25: break
    if data['igv']:
        counts = {}
        for q in data['igv']:
            k = text(q.get('manual_decision') or q.get('status') or 'belirsiz', 45)
            counts[k] = counts.get(k, 0) + 1
        lines.append('- IGV kuyrugu: ' + ', '.join(str(v) + ' ' + k for k, v in sorted(counts.items())) +
                     ' [igv_queue; teknik destek, klinik kanit degil]')
    lines.extend(['', 'İLİŞKİLİ ATLAS KAYNAKLARI [hastaya özel tedavi önerisi DEĞİL]'])
    notes = atlas_notes or []
    for item in notes[:50]:
        lines.append('- ' + text(item['gene']) + ': ' + text(item['title'], 300) +
                     ' | ' + text(item['source_kind']) + ' | ' + text(item['status']) +
                     ' | ' + text(item['source_uri'], 600) +
                     ' | nasıl elde edildi: ' + text(item['acquisition'], 400))
    if not notes:
        lines.append('- Seçilen varyant genleriyle doğrudan ilişkili küratörlü Atlas notu bulunamadı.')
    lines.extend(['', 'KAYNAK DOSYALARI VE ELDE EDİLME YOLU'])
    kinds = sorted({text(f.get('file_type'), 55) for f in data['files'] if text(f.get('file_type'))})
    if kinds:
        lines.append('- Kayıtla bağlı Collector dosya türleri: ' + ', '.join(kinds) +
                     ' [files; exact record_key; dosya içeriği bu bölümde yeniden yorumlanmadı]')
    else:
        lines.append('- Collector dosya envanteri yok.')
    if sel:
        lines.append('- GenNext Selection: ' + text(sel.get('checked_at')) +
                     ', kapsam=' + text(sel.get('clinical_scope')) +
                     ' [gennext_*_current; webden önceden toplanmış kayıt]')
    if data['report_doc']:
        lines.append('- Rapor arşivi: surum=' + text(data['report_doc'].get('version')) +
                     ', durum=' + text(data['report_doc'].get('document_status')) +
                     ' [archive_report_documents; rapor metni bu ekranda değiştirilmedi]')
    lines.extend(['', 'SINIRLAR VE KONTROLLER',
                  '- Veri bu teknik kaydın record_key alanı ile sınırlıdır; diğer RUN/hastalar birleştirilmedi.',
                  '- Kayıtlı rapor geçmişi tüm ham varyantları kapsamayabilir.',
                  '- GenNext Selection listesi tüm panelin negatifliğini kanıtlamaz.',
                  '- Klinik sınıflandırma, ilaç/tedavi ve rapor onayı otomatik oluşturulmaz.',
                  '- Kaynak, gen, varyant, IGV ve analiz türü raporlanmadan önce hekim tarafından doğrulanmalıdır.'])
    result = '\n'.join(lines)
    if len(result) > MAX_TEXT:
        raise ValueError('Taslak cok uzun; daha az hasta verisiyle yeniden deneyin.')
    return result


class PatientDraftStore:
    """Explicit user save only; stored in DATA, separate from public Atlas exports."""
    def __init__(self, base):
        self.path = Path(base) / 'DATA' / 'bilgi_atlasi_hasta_taslaklari.sqlite3'

    def save(self, record_key, text_value, origin='Bilgi Atlasi hasta taslagi'):
        if not record_key or not text_value:
            raise ValueError('Hasta kaydi ve taslak gerekli')
        if len(text_value) > MAX_TEXT:
            raise ValueError('Taslak boyutu siniri asildi')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path, timeout=10) as cx:
            cx.execute('PRAGMA busy_timeout=10000')
            cx.execute('''CREATE TABLE IF NOT EXISTS patient_atlas_drafts(
                id INTEGER PRIMARY KEY AUTOINCREMENT, record_key TEXT NOT NULL,
                revision INTEGER NOT NULL, draft_text TEXT NOT NULL,
                content_sha256 TEXT NOT NULL, origin TEXT NOT NULL,
                created_at TEXT NOT NULL, UNIQUE(record_key,revision))''')
            rev = cx.execute('SELECT COALESCE(MAX(revision),0)+1 FROM patient_atlas_drafts WHERE record_key=?',
                             (record_key,)).fetchone()[0]
            cx.execute('INSERT INTO patient_atlas_drafts(record_key,revision,draft_text,content_sha256,origin,created_at) '
                       'VALUES(?,?,?,?,?,?)', (record_key,rev,text_value,hashlib.sha256(text_value.encode('utf-8')).hexdigest(),
                                             origin,datetime.now().astimezone().isoformat(timespec='seconds')))
            return rev



from ngs_bilgi_atlasi_detay import (gather_complete, display_qc, display_reports,
    generate_deidentified, variant_summary, ManualEntryStore, ManualReportCopyStore,
    MANUAL_TYPES, s as detail_text)


class PatientAtlasWorkspace(ttk.Frame):
    """Complete local work-up, paged raw data and independent de-identified AI text."""
    ROWS_PER_PAGE = 250

    def __init__(self, parent, base, atlas_store, on_back):
        super().__init__(parent)
        self.base = Path(base)
        self.reader = PatientReadOnly(base)
        self.drafts = PatientDraftStore(base)
        self.manual = ManualEntryStore(base)
        self.report_copies = ManualReportCopyStore(base)
        self.atlas_store = atlas_store
        self.on_back = on_back
        self.keys = {}
        self.current_key = None
        self.current_text = ''
        self.current_data = None
        self.complete = None
        self.raw_widgets = {}
        self._build()
        self.refresh()

    def _make_text(self, container, height=17):
        shell=ttk.Frame(container)
        shell.pack(fill='both',expand=True,padx=5,pady=5)
        txt=tk.Text(shell,wrap='word',font=('Consolas',10),undo=False,height=height)
        txt.pack(side='left',fill='both',expand=True)
        sc=ttk.Scrollbar(shell,orient='vertical',command=txt.yview)
        sc.pack(side='right',fill='y')
        txt.configure(yscrollcommand=sc.set,state='disabled')
        return txt

    @staticmethod
    def _set_text(box,content):
        box.configure(state='normal')
        box.delete('1.0','end')
        box.insert('1.0',content)
        box.configure(state='disabled')

    def _build(self):
        head=ttk.Frame(self,padding=(8,8,8,5));head.pack(fill='x')
        ttk.Button(head,text='< Genel Atlasa don',command=self.on_back).pack(side='left',padx=(0,10))
        ttk.Label(head,text='Hasta Bilgi Atlasi - tum kaynaklar / veri girisi',
                  font=('Segoe UI',13,'bold')).pack(side='left')
        ttk.Label(head,text='Klinik rapor / otomatik onay degil',foreground='#9b5225').pack(side='right')
        tools=ttk.Frame(self,padding=(8,0,8,5));tools.pack(fill='x')
        ttk.Label(tools,text='MP veya POOL:').pack(side='left')
        self.search_var=tk.StringVar()
        ent=ttk.Entry(tools,textvariable=self.search_var,width=24);ent.pack(side='left',padx=5)
        ent.bind('<Return>',lambda e:self.refresh())
        ttk.Button(tools,text='Ara',command=self.refresh).pack(side='left',padx=4)
        ttk.Button(tools,text='Secili kaydin TUM verilerini ac',command=self.generate).pack(side='left',padx=5)
        self.savebtn=ttk.Button(tools,text='Taslagi kaydet',command=self.save)
        self.savebtn.pack(side='right',padx=5)
        ttk.Button(tools,text='Taslagi kopyala',command=self.copy).pack(side='right')
        self.status=tk.StringVar(value='Teknik kayit seciniz.')
        ttk.Label(self,textvariable=self.status,foreground='#3f617b').pack(fill='x',padx=12,pady=(0,4))
        split=ttk.PanedWindow(self,orient='horizontal');split.pack(fill='both',expand=True,padx=8,pady=8)
        left=ttk.Frame(split);right=ttk.Frame(split)
        split.add(left,weight=2);split.add(right,weight=5)
        columns=('mp','type','pool','rerun')
        self.tree=ttk.Treeview(left,columns=columns,show='headings',selectmode='browse',height=23)
        for k,label,width in [('mp','MP',110),('type','Tip',52),('pool','Pool',120),('rerun','Tekrar',52)]:
            self.tree.heading(k,text=label);self.tree.column(k,width=width,stretch=k=='pool')
        self.tree.pack(side='left',fill='both',expand=True)
        ys=ttk.Scrollbar(left,command=self.tree.yview);ys.pack(side='right',fill='y')
        self.tree.configure(yscrollcommand=ys.set)
        self.tree.bind('<<TreeviewSelect>>',lambda e:self.preview())
        self.tabs=ttk.Notebook(right);self.tabs.pack(fill='both',expand=True)
        def page(label):
            p=ttk.Frame(self.tabs);self.tabs.add(p,text=label);return p
        self.textbox=self._make_text(page('Ozet'))
        reports_page=page('Raporlar')
        rbar=ttk.Frame(reports_page,padding=(6,6));rbar.pack(fill='x')
        ttk.Label(rbar,text='Imzali Enlil raporlari otomatik aktarilmaz; manuel kopya onay kaniti degildir.',
                  foreground='#8c4b21').pack(side='left')
        ttk.Button(rbar,text='Enlil rapor metnini ekle',command=self.import_report_copy).pack(side='right')
        self.report_box=self._make_text(reports_page)
        self.qc_box=self._make_text(page('QC / okumalar'))
        variant_page=page('Tum varyantlar')
        self.variant_tabs=ttk.Notebook(variant_page)
        self.variant_tabs.pack(fill='both',expand=True,padx=4,pady=4)
        for kind,label in [('snv','Ham SNV / indel'),('cnv','CNV'),('fusion','RNA fuzyonlari'),('index','Onkojenik indeks')]:
            sub=ttk.Frame(self.variant_tabs)
            self.variant_tabs.add(sub,text=label)
            self._raw_tab(sub,kind)
        self.clinical_box=self._make_text(page('Klinik / kaynak'))
        self.ai_box=self._make_text(page('YZ / IGV'))
        self._make_manual_page(page('Veri girisi'))
        ai_page=page('YZ kimliksiz')
        bar=ttk.Frame(ai_page,padding=(6,6));bar.pack(fill='x')
        self.include_raw=tk.BooleanVar(value=False)
        ttk.Checkbutton(bar,text='Tum okunabilir ham mutasyonlari da ekle',variable=self.include_raw,
                        command=self._refresh_ai).pack(side='left')
        ttk.Button(bar,text='Kimliksiz metni kopyala',command=self.copy_deidentified).pack(side='right')
        self.deid_box=self._make_text(ai_page)
        self._show('Teknik kayit secin ve Tum verilerini ac butonuna basin.\n'
                   'Bu alan sadece yerel hekim incelemesi icindir.')

    def _raw_tab(self,frame,kind):
        bar=ttk.Frame(frame,padding=5);bar.pack(fill='x')
        ttk.Label(bar,text='Gen / degisiklik / filtre ara:').pack(side='left')
        query=tk.StringVar();ent=ttk.Entry(bar,textvariable=query,width=25);ent.pack(side='left',padx=4)
        ttk.Button(bar,text='Filtrele',command=lambda k=kind:self._fill_raw(k,reset=True)).pack(side='left')
        ent.bind('<Return>',lambda e,k=kind:self._fill_raw(k,reset=True))
        ttk.Button(bar,text='< Onceki',command=lambda k=kind:self._page_delta(k,-1)).pack(side='right',padx=3)
        ttk.Button(bar,text='Sonraki >',command=lambda k=kind:self._page_delta(k,1)).pack(side='right',padx=3)
        status=tk.StringVar(value='Once bir teknik kayit acin.')
        ttk.Label(frame,textvariable=status,foreground='#465f75').pack(fill='x',padx=8,pady=4)
        holder=ttk.Frame(frame);holder.pack(fill='both',expand=True)
        columns=('source','gene','change','oncogenic','filter','vaf','ad','dp')
        tree=ttk.Treeview(holder,columns=columns,show='headings',height=12)
        widths={'source':100,'gene':98,'change':155,'oncogenic':135,'filter':135,
                'vaf':90,'ad':78,'dp':78}
        for col in columns:
            tree.heading(col,text=col.upper())
            tree.column(col,width=widths[col],stretch=col=='change')
        tree.pack(side='left',fill='both',expand=True)
        sb=ttk.Scrollbar(holder,command=tree.yview);sb.pack(side='right',fill='y')
        tree.configure(yscrollcommand=sb.set)
        ttk.Label(frame,text='Teknik kaynak satiri (tum orijinal sutunlar):').pack(anchor='w',padx=5)
        detail=self._make_text(frame,height=5)
        obj={'kind':kind,'tree':tree,'status':status,'detail':detail,'query':query,
             'rows':[],'filtered':[],'page':0,'items':{}}
        self.raw_widgets[kind]=obj
        tree.bind('<<TreeviewSelect>>',lambda e,k=kind:self._row_details(k))

    def _make_manual_page(self,frame):
        top=ttk.Frame(frame,padding=8);top.pack(fill='x')
        self.note_kind=tk.StringVar(value=MANUAL_TYPES[0])
        ttk.Label(top,text='Tur:').grid(row=0,column=0,sticky='w')
        ttk.Combobox(top,textvariable=self.note_kind,values=MANUAL_TYPES,state='readonly',width=24).grid(row=0,column=1,sticky='ew',padx=6)
        self.note_gene=tk.StringVar();self.note_variant=tk.StringVar()
        ttk.Label(top,text='Gen:').grid(row=0,column=2,sticky='w')
        ttk.Entry(top,textvariable=self.note_gene,width=15).grid(row=0,column=3,padx=5)
        ttk.Label(top,text='Varyant:').grid(row=0,column=4,sticky='w')
        ttk.Entry(top,textvariable=self.note_variant,width=25).grid(row=0,column=5,sticky='ew')
        self.note_source=tk.StringVar();self.note_method=tk.StringVar();self.note_author=tk.StringVar()
        ttk.Label(top,text='Kaynak:').grid(row=1,column=0,sticky='w',pady=6)
        ttk.Entry(top,textvariable=self.note_source).grid(row=1,column=1,columnspan=2,sticky='ew',padx=6)
        ttk.Label(top,text='Nasil elde edildi:').grid(row=1,column=3,sticky='w')
        ttk.Entry(top,textvariable=self.note_method).grid(row=1,column=4,columnspan=2,sticky='ew',padx=5)
        ttk.Label(top,text='Yazar:').grid(row=2,column=0,sticky='w')
        ttk.Entry(top,textvariable=self.note_author).grid(row=2,column=1,sticky='ew',padx=6)
        ttk.Button(top,text='BU TEKNIK KAYDA NOT EKLE',command=self.save_manual).grid(row=2,column=4,columnspan=2,sticky='e',pady=6)
        top.columnconfigure(1,weight=1);top.columnconfigure(5,weight=1)
        ttk.Label(frame,text='Yeni manuel / Enlil / bilimsel bilgi (orijinal DB ve Enlil raporlarini degistirmez):',
                  foreground='#49637a').pack(anchor='w',padx=8)
        self.note_content=tk.Text(frame,wrap='word',height=5,font=('Segoe UI',10))
        self.note_content.pack(fill='x',padx=8,pady=5)
        ttk.Label(frame,text='Bu teknik kayda ait surumlu manuel notlar:').pack(anchor='w',padx=8)
        self.manual_box=self._make_text(frame,height=10)

    def _show(self,value):
        self._set_text(self.textbox,value)

    def refresh(self):
        self.current_key=None;self.current_text='';self.current_data=None;self.complete=None
        self.keys.clear()
        self.tree.delete(*self.tree.get_children())
        try:
            items=self.reader.search(self.search_var.get())
        except Exception as exc:
            self.status.set('Veritabani acilamadi: '+text(exc,500));return
        for i,row in enumerate(items):
            key='item'+str(i);self.keys[key]=row['record_key']
            self.tree.insert('','end',iid=key,values=(row.get('patient_id') or '-',
                row.get('sample_type') or '-',row.get('pool_name') or '-',
                'Evet' if row.get('is_rerun') else ''))
        self.status.set(str(len(items))+' teknik kayit listelendi. MP/RUN secerek acin.')
        self._clear_views()

    def _clear_views(self):
        self._show('Teknik kayit seciniz. Eski hastanin verileri temizlendi.')
        for widget in (self.report_box,self.clinical_box,self.qc_box,self.ai_box,self.manual_box,self.deid_box):
            self._set_text(widget,'')
        for k,obj in self.raw_widgets.items():
            obj['rows']=[];self._fill_raw(k,reset=True)

    def _selected_key(self):
        chosen=self.tree.selection()
        if len(chosen)!=1:
            raise ValueError('Tek bir teknik kayit secin.')
        return self.keys[chosen[0]]

    def preview(self):
        try:
            selected=self._selected_key()
        except ValueError:
            return
        if selected==self.current_key and self.current_data is not None:
            # A delayed TreeviewSelect for the SAME record must not erase new data.
            return
        self.current_key=None;self.current_text='';self.current_data=None;self.complete=None
        self._clear_views()
        self._show('Teknik kayit secildi. Tum verilerini ac butonuna basin.')

    def generate(self):
        try:
            rk=self._selected_key()
            data=self.reader.read(rk)
            full=gather_complete(self.base,data)
            genes=[]
            for v in data['reported']:
                gene=text(v.get('gene'),40).upper()
                if gene and gene not in genes:genes.append(gene)
            for r in full['reported_from_text']:
                gene=text(r.get('gene'),40).upper()
                if gene and gene not in genes:genes.append(gene)
            if data['selection_valid']:
                for v in safe_json(data['selection'].get('selected_json'),[]):
                    items=([v.get('gene_5prime'),v.get('gene_3prime')]
                           if data['sample']['sample_type'].upper()=='RNA' else [v.get('gene')])
                    for g in items:
                        gene=text(g,40).upper()
                        if gene and gene not in genes:genes.append(gene)
            linked=linked_atlas_notes(self.atlas_store,genes)
            draft=build_draft(data,linked)
            # Supplement rather than overwrite the v1.1 report core.
            summary=['','',
                     '===== RAPORLAMA / MUTASYON YORUM - TEKNIK TASLAK =====',
                     'Bu bolum onayli/imzali klinik rapor DEGIL; yalniz secili teknik kayit.']
            active=[r for r in full.get('reports',[]) if r.get('origin')=='RAPORLAMA_MUTASYON_YORUMU']
            if active:
                summary.append('Raporlama ekranindan otomatik okunan kayitli yorum: VAR')
                for v in full.get('reported_from_text',[]):
                    if v.get('origin')=='RAPORLAMA_MUTASYON_YORUMU':
                        summary.append(' - '+text(v.get('gene'),35)+' '+text(v.get('protein') or v.get('cdna') or v.get('feature'),120)+
                                       ' [kayitli teknik yorumdan aday; HEKIM KONTROLU]')
            else:
                summary.append('Raporlama ekraninda bu teknik kayda bagli kaydedilmis yorum bulunamadi; NEGATIF sonuc degil.')
            for warning in full.get('biomarker_disagreements',[]):
                summary.append('UYARI: '+text(warning,450))
            summary.extend(['',
                     '===== TUM VERI KAYNAKLARI - v1.6 =====',
                     'Raporlar / onaylar, QC, ham SNV-CNV-RNA, teknik indeks ve manuel notlar',
                     'ayri sekmelerde tum okunabilir kayitlariyla bulunur.'])
            for kind in ('snv','cnv','fusion'):
                sources=full['raw'].get(kind,[])
                count=sum(len(v['rows']) for v in sources)
                summary.append(kind.upper()+': '+str(count)+' okunabilir ham satir / '+str(len(sources))+' kaynak dosya')
                for source in sources:
                    if source.get('warning'):
                        summary.append('  '+kind+': '+source['warning'])
            summary.append('QC kaynagi: '+str(len(full['qc']))+' | yerel rapor kaynagi: '+str(len(full['reports'])))
            summary.append('Rapor metninden pozitif aday: '+str(len(full['reported_from_text'])) +
                           ' (otomatik hekim onayi degildir)')
            summary.append('Manuel Atlas notu: '+str(len(full['manual'])))
            summary.append('')
            self.current_text=draft+'\n'.join(summary)
            self.current_key=rk;self.current_data=data;self.complete=full
            self._show(self.current_text)
            self._set_text(self.report_box,display_reports(data,full))
            self._set_text(self.clinical_box,self._clinical_sources(data))
            self._set_text(self.qc_box,display_qc(full,data))
            for kind in ('snv','cnv','fusion'):
                raw=[]
                for src in full['raw'].get(kind,[]):
                    raw.extend({'source':src['file'],'row':item} for item in src['rows'])
                self.raw_widgets[kind]['rows']=raw
                self._fill_raw(kind,reset=True)
            self.raw_widgets['index']['rows']=[{'source':'oncogenic_observations','row':item}
                                               for item in data.get('observed',[])]
            self._fill_raw('index',reset=True)
            self._set_text(self.ai_box,self._ai_local(data))
            self._refresh_manual()
            self._refresh_ai()
            self.tabs.select(0)
            self.status.set(text(data['sample'].get('patient_id'))+' / '+data['sample']['sample_type']+
                ' | Kaynaklar acildi; rapor/mutasyon/QC/manuel alanlarini sekmelerden inceleyin.')
        except Exception as exc:
            self.current_key=None;self.current_data=None;self.complete=None;self.current_text=''
            self._clear_views()
            messagebox.showerror('Hasta Bilgi Atlasi','Kapsamli veri acilamadi: '+text(exc,700),parent=self)

    def _clinical_sources(self,data):
        # Local only: may contain identifying metadata. It is NEVER passed to de-identified export.
        import json
        lines=['YEREL KLINIK / ENLIL / COLLECTOR KAYITLARI',
               'Bu alan kimlik bilgisi icerebilir; yalniz yerel ekranda gosterilir.',
               'Kaynaklar dosya turu ve exact record_key uzerinden listele nir.'.replace('listele nir','listelenir'),'']
        for title,key in [('TEKNIK ANALIZ / samples','sample'),('ENLIL / case_metadata','case'),
                          ('BLOK / sample_metadata','block'),('MANUEL BIYOBELIRTEC / overrides','overrides')]:
            lines.append('--- '+title+' ---')
            item=data.get(key) or {}
            for field,value in sorted(item.items()):
                if value is not None and str(value).strip():
                    lines.append(str(field)+': '+str(value)[:2000])
            lines.append('')
        lines.append('--- COLLECTOR BAGLI DOSYALAR / files ---')
        for item in data.get('files',[]):
            lines.append('tur='+str(item.get('file_type') or '-')+
                         ' | boyut='+str(item.get('size') or '-')+
                         ' | dosya='+str(item.get('path') or '-')+
                         ' | SHA256='+str(item.get('sha256') or ''))
        lines.append('')
        lines.append('Elde edilme: Collector dosyalar tablosu tekil teknik record_key sorgusu. '
                     'Bu envanter orijinal kaynaklarin dogrulandigi anlamina gelmez.')
        return '\n'.join(lines)

    def _ai_local(self,data):
        lines=['YEREL YZ VE IGV NOTLARI - TIBBI ONAY DEGIL','']
        for p in data.get('ai_patient',[]):
            lines.append('Hasta YZ notu: '+text(p.get('summary'),6000))
        for v in data.get('ai_variant',[]):
            lines.append('Varyant YZ: '+text(v.get('gene'))+' '+text(v.get('change'))+
                         ' | '+text(v.get('report_recommendation'))+' | '+text(v.get('rationale'),1000))
        for ig in data.get('igv',[]):
            lines.append('IGV: '+text(ig.get('gene'))+' '+text(ig.get('change'))+' | '+
                         text(ig.get('status'))+' | '+text(ig.get('manual_decision'))+
                         ' | '+text(ig.get('manual_note'),600))
        if len(lines)==2:lines.append('Bu teknik kayit icin YZ/IGV notu yok.')
        return '\n'.join(lines)

    def _fill_raw(self,kind,reset=False):
        obj=self.raw_widgets.get(kind)
        if obj is None:return
        if reset:obj['page']=0
        q=obj['query'].get().casefold().strip()
        all_rows=obj['rows']
        if q:
            rows=[x for x in all_rows if q in ' '.join(str(v) for v in x['row'].values()).casefold()]
        else:rows=all_rows
        obj['filtered']=rows
        total=len(rows)
        max_page=max(0,(total-1)//self.ROWS_PER_PAGE)
        obj['page']=min(max(0,obj['page']),max_page)
        a=obj['page']*self.ROWS_PER_PAGE;b=min(a+self.ROWS_PER_PAGE,total)
        tree=obj['tree'];tree.delete(*tree.get_children());obj['items']={}
        for i,rec in enumerate(rows[a:b]):
            v=variant_summary(rec['row'],'snv' if kind=='index' else kind)
            iid=str(i)
            tree.insert('','end',iid=iid,values=(rec['source'],v['gene'],v['change'],
                v['oncogenic'],v['filter'],v['vaf'],v['ad'],v['dp']))
            obj['items'][iid]=rec
        obj['status'].set(f'{total} satir eslesti / toplam {len(all_rows)}. '
                          f'Sayfa {obj["page"]+1}/{max_page+1}. Ilk satir otomatik raporlanmaz.')
        self._set_text(obj['detail'],'Bir satira tiklayin: kaynak dosyadaki tum sutunlar burada gorunur.')

    def _row_details(self,kind):
        obj=self.raw_widgets[kind];selection=obj['tree'].selection()
        if not selection:return
        row=obj['items'].get(selection[0])
        if row is None:return
        import json
        self._set_text(obj['detail'],'Kaynak: '+row['source']+'\n'+
                       json.dumps(row['row'],ensure_ascii=False,indent=2,default=str))

    def _page_delta(self,kind,delta):
        self.raw_widgets[kind]['page']+=delta
        self._fill_raw(kind)

    def _refresh_manual(self):
        if not self.current_key or self.complete is None:return
        entries=self.manual.list_for(self.current_key)
        self.complete['manual']=entries
        lines=['ELLE / ENLIL / YZ ILE EKLENEN KAYITLAR - SADECE ATLAS',
               'Rapor/veritabani kararlarini degistirmez. Eski notlar silinmez.','']
        for entry in entries:
            lines.extend(['#'+str(entry['id'])+' | '+entry['created_at']+' | '+entry['kind'],
                          'Gen/varyant: '+entry['gene']+' '+entry['variant'],
                          'Kaynak: '+entry['source'], 'Nasil elde edildi: '+entry['acquisition'],
                          'Yazan: '+entry['author'],entry['content'],''])
        if len(entries)==0:lines.append('Bu kayitta manuel Atlas notu yok.')
        self._set_text(self.manual_box,'\n'.join(lines))

    def _save_report_copy(self, content, retyped_mp):
        if not self.current_key or not self.current_data:
            raise ValueError('Once secili kaydin tum verilerini acin.')
        if self._selected_key()!=self.current_key:
            raise ValueError('Hasta secimi degisti; rapor eski kayda eklenmedi.')
        report_id, created = self.report_copies.save(self.current_data['sample'],content,retyped_mp)
        # Immediately re-read the exact source list; never change a clinical report.
        self.generate()
        return report_id,created

    def import_report_copy(self):
        if not self.current_data or not self.current_key:
            messagebox.showinfo('Enlil rapor metni','Once hastanin tum verilerini acin.',parent=self)
            return
        sample=self.current_data['sample']
        current_mp=text(sample.get('patient_id'),100)
        win=tk.Toplevel(self)
        win.title('Enlil rapor metnini bu teknik kayda ekle (ONAYSIZ KOPYA)')
        win.geometry('840x620');win.transient(self)
        heading=ttk.Frame(win,padding=10);heading.pack(fill='x')
        ttk.Label(heading,text='Secilen kayit: '+current_mp+' / '+text(sample.get('sample_type'))+
                  ' / '+text(sample.get('pool_name'),100),font=('Segoe UI',11,'bold')).pack(anchor='w')
        ttk.Label(heading,text='Enlil raporunu metin olarak asagiya yapistirin. '
                  'Elektronik imza veya rapor onayi burada dogrulanmaz.',foreground='#974c24').pack(anchor='w',pady=5)
        ttk.Label(heading,text='Bilgiler yalniz kurum bilgisayarinda AYRI yerel SQLite dosyasina (acik metin) kaydedilir.').pack(anchor='w')
        entry=tk.Text(win,height=23,wrap='word',font=('Consolas',10))
        entry.pack(fill='both',expand=True,padx=10,pady=6)
        bottom=ttk.Frame(win,padding=10);bottom.pack(fill='x')
        ttk.Label(bottom,text='Secili MP yi tekrar yazin:').pack(side='left')
        confirmed=tk.StringVar()
        ttk.Entry(bottom,textvariable=confirmed,width=16).pack(side='left',padx=6)
        def submit():
            content=entry.get('1.0','end-1c')
            if not messagebox.askyesno('YEREL rapor kopyasi',
                 current_mp+' / '+text(sample.get('sample_type'))+' icin, E-IMZA DOGRULANMAMIS '
                 'bir Enlil METIN KOPYASI yerel olarak saklansin mi? Klinik rapor degismez.',parent=win):
                return
            try:
                _,added=self._save_report_copy(content,confirmed.get())
            except Exception as exc:
                messagebox.showwarning('Enlil rapor metni','Islem reddedildi: '+text(exc,600),parent=win)
                return
            win.destroy()
            self.tabs.select(1)
            self.status.set('Enlil rapor kopyasi kaydedildi.' if added else 'Bu metin zaten kayitli; tekrar eklenmedi.')
        ttk.Button(bottom,text='Secili kayda METIN KOPYASI ekle',command=submit).pack(side='right')
        ttk.Button(bottom,text='Vazgec',command=win.destroy).pack(side='right',padx=8)
        entry.focus_set()

    def save_manual(self):
        if not self.current_key or not self.complete:
            messagebox.showinfo('Manuel not','Once teknik kayit acin.',parent=self);return
        try:
            rk=self._selected_key()
            if rk!=self.current_key:
                raise ValueError('Hasta secimi degisti; yeniden kayit acin.')
            content=self.note_content.get('1.0','end-1c').strip()
            if not messagebox.askyesno('Yerel not ekleme',
                'Bu not secilen teknik kaydin ayri Atlas dosyasinda saklanacak. '
                'Klinik raporu ve GenNext durumunu degistirmez. Devam?',parent=self):return
            self.manual.save(rk,self.note_kind.get(),content,self.note_source.get().strip(),
                             self.note_method.get().strip(),self.note_gene.get().strip(),
                             self.note_variant.get().strip(),self.note_author.get().strip())
            self.note_content.delete('1.0','end')
            self._refresh_manual()
            self.status.set('Yeni not bu teknik kayda kaydedildi. Klinik rapor degismedi.')
        except Exception as exc:
            messagebox.showwarning('Manuel not','Kayit yapilamadi: '+text(exc,500),parent=self)

    def _refresh_ai(self):
        if not self.current_data or not self.complete:
            self._set_text(self.deid_box,'Once teknik hasta kaydini acin.')
            return
        self._set_text(self.deid_box,generate_deidentified(
            self.current_data,self.complete,include_raw=self.include_raw.get()))

    def copy_deidentified(self):
        if not self.current_data or not self.complete:
            messagebox.showinfo('Kimliksiz export','Once hastayi acin.',parent=self);return
        if not messagebox.askyesno('Kimliksiz teknik veri',
                'Kimlik alanlari cikarildi, ancak nadir genomik varyantlar da yeniden tanimlamaya yol acabilir. '
                'Panoya kopyalansin mi?',parent=self):return
        self.clipboard_clear();self.clipboard_append(self.deid_box.get('1.0','end-1c'))
        self.status.set('Kimlik alani bulunmayan teknik veri panoya kopyalandi; kurumsal gizlilik kurallari gecerli.')

    def copy(self):
        if not self.current_text:
            messagebox.showinfo('Hasta Atlasi','Once taslak olusturun.',parent=self);return
        if not messagebox.askyesno('Hasta verisi panosu',
                'Tam taslak yerel hasta/MP bilgisi icerir. Panoya kopyalansin mi?',parent=self):return
        self.clipboard_clear();self.clipboard_append(self.current_text)
        self.status.set('Tam taslak panoda. Kimliksiz metin icin ayri sekmeyi kullanin.')

    def save(self):
        if not self.current_key or not self.current_text:
            messagebox.showinfo('Hasta Atlasi','Once taslak olusturun.',parent=self);return
        if not messagebox.askyesno('Taslak sakla',
             'Taslak ayri SQLite dosyasina surumlu olarak kaydedilir. Enlil raporu degismez. Devam?',parent=self):return
        try:
            rev=self.drafts.save(self.current_key,self.current_text)
            self.status.set('Yerel taslak revizyon '+str(rev)+' kaydedildi; klinik rapor degismedi.')
        except Exception as exc:
            messagebox.showerror('Hasta Atlasi','Taslak kaydedilemedi: '+text(exc,500),parent=self)
