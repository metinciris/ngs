"""v0.4.83 mutation view: AI advice, web Selection and manual decisions separate."""
from __future__ import annotations
import json
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog
from mutation_viewer import MutationViewer as LegacyViewer, _vkey, _pick
from review_workspace_store import (ReviewRepository, canonical_key, feature_key, compatible_build,
    review_status, feature_text, patient_text, selection_for_record, selection_label,
    decision_for_key, adjudicate, adjudication, ADJUDICATION_LABELS, MANUAL_LABELS, readable)
from review_workspace_ui import text_area, tree_area, page, put, buttons

FOCUS_ALL = 'T\u00fcm\u00fc (ara\u015ft\u0131rma)'
FOCUS_AI = 'YZ notu olanlar'


class MutationWorkspace(LegacyViewer):
    def __init__(self, parent, base, db, cfg, archive_callback=None, on_change=None):
        self.repo = ReviewRepository(db)
        self._review_by_key = {}; self._selection = {}; self._payload = {}; self._review_keys = set()
        super().__init__(parent, base, db, cfg, archive_callback, on_change)

    def _build(self):
        header = ttk.Frame(self); header.pack(fill='x', pady=(0, 8))
        ttk.Label(header, text='Mutasyonlar / YZ \u00d6nerileri', font=('Segoe UI', 13, 'bold')).pack(side='left')
        ttk.Button(header, text='Hasta YZ / RNA / QC', command=self.open_patient_review).pack(side='right')
        filters = ttk.LabelFrame(self, text='Teknik \u00f6rnek ve g\u00f6r\u00fcn\u00fcm', padding=10); filters.pack(fill='x')
        filters.columnconfigure(1, weight=1)
        ttk.Label(filters, text='Hasta / \u00f6rnek:').grid(row=0, column=0, sticky='w', padx=(0, 8))
        self.sample_var = tk.StringVar(); self.sample_box = ttk.Combobox(filters, textvariable=self.sample_var, state='readonly', width=48)
        self.sample_box.grid(row=0, column=1, columnspan=3, sticky='ew', pady=(0, 8)); self.sample_box.bind('<<ComboboxSelected>>', lambda e: self.load())
        ttk.Label(filters, text='Kaynak:').grid(row=0, column=4, padx=8)
        self.source = tk.StringVar(value='SNV')
        source = ttk.Combobox(filters, textvariable=self.source, state='readonly', values=('SNV', 'CNV', 'Clinical', 'T\u00fcm\u00fc'), width=10)
        source.grid(row=0, column=5, pady=(0, 8)); source.bind('<<ComboboxSelected>>', lambda e: self.load())
        self.view_var = tk.StringVar(value='YZ + Selection')
        view = ttk.Combobox(filters, textvariable=self.view_var, state='readonly', values=('YZ + Selection', 'Selection', FOCUS_AI, FOCUS_ALL), width=25)
        view.grid(row=1, column=0, columnspan=2, sticky='w'); view.bind('<<ComboboxSelected>>', lambda e: self.load())
        self.filt = tk.StringVar(value='T\u00fcm\u00fc')
        filt = ttk.Combobox(filters, textvariable=self.filt, state='readonly', values=('T\u00fcm\u00fc', 'PASS', 'Onkojenik/Level'), width=17)
        filt.grid(row=1, column=2, padx=8); filt.bind('<<ComboboxSelected>>', lambda e: self.load())
        self.search = tk.StringVar(); entry = ttk.Entry(filters, textvariable=self.search, width=22)
        entry.grid(row=1, column=3, sticky='ew', padx=6); entry.bind('<Return>', lambda e: self.load())
        ttk.Button(filters, text='Ara / Yenile', command=self.load).grid(row=1, column=4, columnspan=2, sticky='ew')
        self.info = tk.StringVar(value='YZ / Selection odakl\u0131 liste. T\u00fcm ham varyantlar ara\u015ft\u0131rma g\u00f6r\u00fcn\u00fcm\u00fcndedir.')
        ttk.Label(self, textvariable=self.info, wraplength=1200).pack(fill='x', pady=8)
        splitter = ttk.Panedwindow(self, orient='vertical'); splitter.pack(fill='both', expand=True)
        self._detail_splitter=splitter;self._initial_split_done=False
        self.bind('<Map>',lambda e:self.after_idle(self._initial_split),add='+')
        upper, lower = ttk.Frame(splitter), ttk.Frame(splitter); splitter.add(upper, weight=3); splitter.add(lower, weight=2)
        columns = [('report','Yerel rapor karar\u0131',150),('source','Kaynak',60),('gene','Gen',95),('change','De\u011fi\u015fiklik',150),
            ('vaf','VAF (kaynak)',85),('dp','DP',70),('filter','Filter',85),('oncogenic','OncoKB (kaynak)',130),('tier','Tier (kaynak)',100),
            ('location','Koordinat',190),('detail','Ayr\u0131nt\u0131',220),('ai','YZ \u00f6nerisi',170),('selection','GenNext Selection',165),('ai_note','YZ / IGV k\u0131sa notu',300)]
        self.tree = tree_area(upper, columns, height=10, multiple=True)
        self.tree.configure(displaycolumns=('gene','change','ai','selection','report','vaf','filter','ai_note'))
        self.tree.tag_configure('attention', background='#fff4dd', foreground='#7c3c00')
        self.tree.tag_configure('ai_report', background='#e8f4ef', foreground='#135544')
        self.tree.bind('<<TreeviewSelect>>', self._selection_changed)
        self.tree.bind('<Double-1>', self._double_click); self.tree.bind('<Button-3>', self._context_menu)
        self.tree.bind('<Motion>', self._motion); self.tree.bind('<Leave>', lambda e: self.tree.configure(cursor=''))
        for c in self.tree['columns']: self.tree.heading(c, command=lambda cc=c: self._sort_tree(cc, False))
        self.detail_tabs = ttk.Notebook(lower); self.detail_tabs.pack(fill='both', expand=True, pady=(8, 0))
        self.ai_page = page(self.detail_tabs, 'YZ Notu'); self.raw_page = page(self.detail_tabs, 'Teknik Ayr\u0131nt\u0131')
        self.history_page = page(self.detail_tabs, 'Karar Ge\u00e7mi\u015fi'); self.actions_page = page(self.detail_tabs, 'Rapor Karar\u0131 / Ara\u00e7lar')
        buttons(self.ai_page, [('YZ \u00f6nerisini kabul', lambda: self.decide_ai('accepted')), ('YZ \u00f6nerisini reddet', lambda: self.decide_ai('rejected')), ('Karar\u0131 beklet', lambda: self.decide_ai('pending'))])
        self.ai_text = text_area(self.ai_page, height=7); self.raw_text = text_area(self.raw_page, height=8); self.history_text = text_area(self.history_page, height=8)
        ttk.Label(self.actions_page, text='Yerel rapor karar\u0131d\u0131r; GenNext Selection / Enlil raporu otomatik de\u011fi\u015fmez.').pack(anchor='w', pady=(0, 6))
        buttons(self.actions_page, [('Raporland\u0131', lambda: self.set_decision('reported')), ('Raporlanmad\u0131', lambda: self.set_decision('not_reported')),
            ('Artefakt', lambda: self.set_decision('artifact')), ('Karar\u0131 Temizle', lambda: self.set_decision(None))])
        buttons(self.actions_page, [('T\u00fcm hastalarda ara', self.show_all_patients), ('OncoKB', self.open_oncokb), ('Franklin', self.open_franklin)])
        ttk.Label(self.actions_page, text='OncoKB / Franklin yaln\u0131z a\u00e7\u0131k d\u00fc\u011fme t\u0131klamas\u0131nda a\u00e7\u0131l\u0131r.').pack(anchor='w', pady=8)

    def _initial_split(self):
        if not self._initial_split_done and self._detail_splitter.winfo_height()>160:
            self._detail_splitter.sashpos(0,int(self._detail_splitter.winfo_height()*0.40))
            self._initial_split_done=True

    def _column_name(self, event):
        cols = self.tree['displaycolumns']
        if cols == ('#all',): cols = self.tree['columns']
        try: return cols[int(self.tree.identify_column(event.x)[1:]) - 1]
        except (ValueError, IndexError): return ''

    def _match(self, row, source):
        if not super()._match(row, source): return False
        if self.view_var.get() == FOCUS_ALL: return True
        key = canonical_key(row.get('_workspace_key') or _vkey(source, row))
        keys = self._review_keys if self.view_var.get() == FOCUS_AI else self._selection_keys if self.view_var.get() == 'Selection' else self._review_keys | self._selection_keys
        return key in keys

    def _display(self, source, row, decision):
        original = list(super()._display(source, row, decision))
        key = canonical_key(row.get('_workspace_key') or _vkey(source, row)); review = self._review_by_key.get(key)
        if review:
            label = review_status(review) if compatible_build(review, self.cfg.get('genome_build')) else 'Referans uyu\u015fmazl\u0131\u011f\u0131'
            note = str(review.get('report_recommendation') or review.get('igv_reason') or review.get('rationale') or '')
        else: label, note = '', ''
        if row.get('_source_missing'): note = 'HAM TSV SATIRI YOK. ' + note
        return original + [label, selection_label(self._selection, key, source), ' '.join(note.split())[:400]]

    def load(self):
        old = self._selected(); previous_key = canonical_key(old[0]) if old else None
        self.tree.delete(*self.tree.get_children()); self.row_map = {}
        label = self.sample_var.get(); rk = self.sample_map.get(label)
        if not rk: self.info.set('\u00d6rnek se\u00e7ilmedi.'); return
        self.current_record = rk; sample = self.db.get_sample(rk) or {}
        self._payload = self.repo.record(rk); self._review_by_key = {}
        for r in self._payload['features']:
            key = canonical_key(feature_key(r))
            if key: self._review_by_key[key] = r
        self._review_keys = set(self._review_by_key)
        self._selection = selection_for_record(self.db, rk, sample.get('sample_type', 'DNA'))
        self._selection_keys = {canonical_key(feature_key(r)) for field in ('selected','candidates') for r in self._selection.get(field) or [] if feature_key(r)}
        decisions = {canonical_key(r['variant_key']): r['status'] for r in self.db.cx.execute('SELECT * FROM variant_decisions WHERE record_key=? ORDER BY updated_at,rowid', (rk,))}
        sources = ['SNV', 'CNV', 'Clinical'] if self.source.get() == 'T\u00fcm\u00fc' else [self.source.get()]
        limit = max(1, int(self.cfg.get('mutation_view_limit', 5000))); seen = set(); missing = []; matched = 0
        def add(src, row):
            nonlocal matched
            key = row.get('_workspace_key') or _vkey(src, row); canon = canonical_key(key)
            if (src, canon) in seen: return
            seen.add((src, canon)); matched += 1
            if len(self.row_map) >= limit: return
            iid = str(len(self.row_map)); review = self._review_by_key.get(canon)
            tag = ('ai_report' if review.get('review_status') == 'report' else 'attention') if review else ''
            self.tree.insert('', 'end', iid=iid, values=self._display(src, row, decisions.get(canon)), tags=(tag,) if tag else ())
            self.row_map[iid] = (key, src, row)
        try:
            for src in sources:
                path = self._latest_file(rk, src.lower())
                if not path: missing.append(src); continue
                iterator = self._snv_rows(path) if src == 'SNV' else self._cnv_rows(path) if src == 'CNV' else self._clinical_rows(path)
                for row in iterator: add(src, row)
            # Preserve advice even when the raw source file/row is missing.
            if self.view_var.get() != FOCUS_ALL:
                extras = list(self._payload['features'])
                for field in ('selected', 'candidates'):
                    extras += [dict(r, _kind='SNV') for r in self._selection.get(field) or [] if sample.get('sample_type') == 'DNA']
                for r in extras:
                    kind = r.get('_kind', 'SNV'); key = feature_key(r); canon = canonical_key(key)
                    if kind not in sources or not key or (kind, canon) in seen: continue
                    row = {'_workspace_key': key, '_source_missing': True, 'Gene_refGene': r.get('gene') or '',
                           'GeneName': r.get('gene') or '', 'aaChange': r.get('change') or r.get('protein') or '', 'txChange': r.get('cdna') or ''}
                    parts = canon.split('|')
                    if kind == 'SNV' and len(parts) == 4 and parts[1].isdigit():
                        row.update(zip(('Chrom','Start','Ref','Alt'), parts))
                    if self._match(row, kind): add(kind, row)
            msg = f"{sample.get('patient_id') or '?'} | {sample.get('sample_type') or '?'} | {len(self.row_map)}/{matched} sat\u0131r | YZ #{self._payload.get('session_id') or '-'}"
            if missing: msg += ' | Ham kaynak yok: ' + ', '.join(missing)
            if matched > limit: msg += f' | G\u00f6sterim s\u0131n\u0131r\u0131: {limit}'
            if not self.row_map: msg += ' | Bu filtrede kay\u0131t yok; NEGAT\u0130F SONU\u00c7 DE\u011e\u0130LD\u0130R.'
            self.info.set(msg)
            target = next((iid for iid, item in self.row_map.items() if canonical_key(item[0]) == previous_key), next(iter(self.row_map), None))
            if target is not None: self.tree.selection_set(target); self._selection_changed()
            else: put(self.ai_text, patient_text(self._payload)); put(self.raw_text, 'Sat\u0131r se\u00e7ilmedi.'); put(self.history_text, '')
        except Exception as exc:
            self.info.set('Mutasyon okuma hatas\u0131: ' + str(exc)); messagebox.showerror('Mutasyonlar', str(exc), parent=self)

    def _selection_changed(self, event=None):
        item = self._selected()
        if not item: return
        key, source, row = item; canon = canonical_key(key); review = self._review_by_key.get(canon)
        text = feature_text(review, self.db, self.current_record, self._selection) if review else 'Bu varyant i\u00e7in g\u00fcncel YZ notu yok.\n\n' + patient_text(self._payload)
        if review and not compatible_build(review, self.cfg.get('genome_build')): text = 'UYARI: GENOM REFERANSI UYU\u015eMUYOR; tam kaynak e\u015fle\u015fmesi say\u0131lmaz.\n\n' + text
        if row.get('_source_missing'): text = 'HAM TSV SATIRI YOK. Yaln\u0131z YZ / Selection kayd\u0131 g\u00f6steriliyor; kaynak kan\u0131t\u0131 de\u011fildir.\n\n' + text
        put(self.ai_text, text)
        put(self.raw_text, json.dumps({'record_key': self.current_record, 'variant_key': key, 'source': source, 'source_missing': bool(row.get('_source_missing')), 'data': row}, ensure_ascii=False, indent=2))
        history = []
        for r in self.db.cx.execute('SELECT * FROM variant_decision_history_v0483 WHERE record_key=? ORDER BY id DESC', (self.current_record,)):
            if canonical_key(r['variant_key']) == canon: history.append(dict(r))
        for r in self.db.cx.execute('SELECT * FROM ai_adjudication_history_v0483 WHERE record_key=? ORDER BY id DESC', (self.current_record,)):
            if canonical_key(r['feature_key']) == canon: history.append(dict(r))
        put(self.history_text, readable(history) if history else 'Bu varyant i\u00e7in yeni karar ge\u00e7mi\u015fi yok. Eski YZ oturumlar\u0131 Hasta YZ sekmesindedir.')

    def details(self, event=None):
        self._selection_changed(); self.detail_tabs.select(self.raw_page)

    def decide_ai(self, status):
        item = self._selected(); review = self._review_by_key.get(canonical_key(item[0])) if item else None
        if not review: messagebox.showinfo('YZ karar\u0131', 'Tam anahtarla e\u015fle\u015fen YZ notu yok.', parent=self); return
        note = simpledialog.askstring('YZ karar\u0131', 'Karar notu (iste\u011fe ba\u011fl\u0131). Rapor / Selection de\u011fi\u015fmez:', parent=self)
        if note is None: return
        try:
            adjudicate(self.db, review['_session_id'], self.current_record, feature_key(review), status, note)
            self._selection_changed(); self.winfo_toplevel().event_generate('<<AIReviewsChanged>>', when='tail')
        except Exception as exc: messagebox.showerror('YZ karar\u0131', str(exc), parent=self)

    def set_decision(self, status):
        if status == 'reported' and any(self.row_map[i][2].get('_source_missing') for i in self.tree.selection() if i in self.row_map):
            if not messagebox.askyesno('Kaynak sat\u0131r\u0131 yok', 'Ham kaynak sat\u0131r\u0131 olmayan kay\u0131t var. Ger\u00e7ekte raporland\u0131\u011f\u0131n\u0131z\u0131 ba\u011f\u0131ms\u0131z olarak do\u011frulad\u0131n\u0131z m\u0131?', parent=self): return
        super().set_decision(status)

    def focus_report_variant(self, record_key, item):
        exact = canonical_key(item.get('variant_key'))
        self.view_var.set('YZ + Selection' if exact else FOCUS_ALL)
        if exact:
            self.refresh_samples()
            self.filt.set('T\u00fcm\u00fc'); self.search.set('')
            self.source.set('CNV' if item.get('type') == 'CNV' else 'SNV')
            self.select_record(record_key)
            result = 'Tam anahtar i\u00e7in ham kaynak / YZ kayd\u0131 bulunamad\u0131.'
        else:
            result = super().focus_report_variant(record_key, item)
        if exact:
            for iid, payload in self.row_map.items():
                if canonical_key(payload[0]) == exact:
                    self.tree.selection_set(iid); self.tree.see(iid); self._selection_changed()
                    return 'Tam teknik kay\u0131t / varyant anahtar\u0131 e\u015fle\u015fti; YZ ayr\u0131 nottur.'
        return result

    def open_patient_review(self):
        host = self.winfo_toplevel()
        if self.current_record and hasattr(host, 'patient_workspace'):
            if host.patient_workspace.select_record(self.current_record, 'ai'): host.workspaces.select(host.patient_workspace)
