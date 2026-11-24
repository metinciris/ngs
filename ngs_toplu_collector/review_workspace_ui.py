"""Roomy v0.4.83 review tabs; existing IGV engine and source safeguards retained."""
from __future__ import annotations
import json
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog
from ai_review import AIReviewIGV as LegacyIGV, load_ai_review_return, validate_ai_review_return
from review_workspace_store import (ReviewRepository, RELEASE_LABELS, MANUAL_LABELS, ADJUDICATION_LABELS,
    feature_key, patient_text, feature_text, review_status, readable, selection_for_record,
    selection_label, decision_for_key, local_note, save_local_note, adjudicate, adjudication)


def text_area(parent, editable=False, height=10):
    host = ttk.Frame(parent); host.pack(fill='both', expand=True)
    host.columnconfigure(0, weight=1); host.rowconfigure(0, weight=1)
    text = tk.Text(host, wrap='word', font=('Segoe UI', 11), padx=16, pady=12,
        relief='flat', height=height, undo=editable, background='#ffffff', foreground='#243b53')
    text.grid(row=0, column=0, sticky='nsew')
    bar = ttk.Scrollbar(host, orient='vertical', command=text.yview); bar.grid(row=0, column=1, sticky='ns')
    text.configure(yscrollcommand=bar.set, state='normal' if editable else 'disabled')
    return text


def put(text, value):
    text.configure(state='normal'); text.delete('1.0', 'end'); text.insert('1.0', str(value)); text.configure(state='disabled')


def tree_area(parent, columns, height=10, multiple=False):
    host = ttk.Frame(parent); host.pack(fill='both', expand=True)
    host.columnconfigure(0, weight=1); host.rowconfigure(0, weight=1)
    tree = ttk.Treeview(host, columns=[c[0] for c in columns], show='headings', height=height,
                        selectmode='extended' if multiple else 'browse')
    for key, label, width in columns:
        tree.heading(key, text=label, anchor='w'); tree.column(key, width=width, minwidth=min(width, 65), anchor='w')
    ys = ttk.Scrollbar(host, orient='vertical', command=tree.yview)
    xs = ttk.Scrollbar(host, orient='horizontal', command=tree.xview)
    tree.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
    tree.grid(row=0, column=0, sticky='nsew'); ys.grid(row=0, column=1, sticky='ns'); xs.grid(row=1, column=0, sticky='ew')
    return tree


def page(nb, title):
    frame = ttk.Frame(nb, padding=12); nb.add(frame, text='  ' + title + '  '); return frame


def buttons(parent, specs):
    bar = ttk.Frame(parent); bar.pack(fill='x', pady=(6, 8))
    for label, command in specs:
        ttk.Button(bar, text=label, command=command).pack(side='left', padx=(0, 8))
    return bar


class PatientReviewPanel(ttk.Frame):
    def __init__(self, parent, base, db, cfg=None, on_change=None, open_mutations=None):
        super().__init__(parent, padding=(10, 8))
        self.base, self.db, self.cfg = Path(base), db, cfg or {}
        self.on_change, self.open_mutations = on_change, open_mutations
        self.repo = ReviewRepository(db); self.record_key = ''; self.payload = {}
        self._note_record = ''; self._note_saved = ''; self._note_timer = None; self._loading = False
        self._feature_rows = {}; self._session_map = {}
        top = ttk.Frame(self); top.pack(fill='x', pady=(0, 8))
        self.title_var = tk.StringVar(value='Hasta YZ de\u011ferlendirmesi')
        ttk.Label(top, textvariable=self.title_var, font=('Segoe UI', 12, 'bold')).pack(side='left')
        self.session_var = tk.StringVar()
        self.session_box = ttk.Combobox(top, state='readonly', textvariable=self.session_var, width=40)
        self.session_box.pack(side='right', padx=(8, 0)); self.session_box.bind('<<ComboboxSelected>>', self._choose_session)
        self.caption = tk.StringVar(value='YZ \u00f6nerisi, Selection ve rapor karar\u0131 ayr\u0131 tutulur.')
        ttk.Label(self, textvariable=self.caption, wraplength=1050, foreground='#52657a').pack(fill='x', pady=(0, 8))
        self.nb = ttk.Notebook(self); self.nb.pack(fill='both', expand=True)
        self.overview_page = page(self.nb, 'K\u0131sa YZ \u00d6zeti')
        self.features_page = page(self.nb, 'Varyant \u00d6nerileri')
        self.rna_page = page(self.nb, 'RNA / F\u00fczyon')
        self.qc_page = page(self.nb, 'QC / Uyar\u0131lar')
        self.note_page = page(self.nb, 'Benim Notum')
        self.history_page = page(self.nb, 'Karar Ge\u00e7mi\u015fi')
        self.summary_text = text_area(self.overview_page); self.rna_text = text_area(self.rna_page)
        self.qc_text = text_area(self.qc_page); self.history_text = text_area(self.history_page)
        specs = [('YZ \u00f6nerisini kabul', lambda: self.decide('accepted')),
                 ('YZ \u00f6nerisini reddet', lambda: self.decide('rejected')), ('Karar\u0131 beklet', lambda: self.decide('pending'))]
        if open_mutations: specs.append(('Mutasyon ekran\u0131nda a\u00e7', self.open_selected_mutation))
        buttons(self.features_page, specs)
        splitter = ttk.Panedwindow(self.features_page, orient='vertical'); splitter.pack(fill='both', expand=True)
        upper, lower = ttk.Frame(splitter), ttk.Frame(splitter); splitter.add(upper, weight=3); splitter.add(lower, weight=2)
        self.features_tree = tree_area(upper, [('kind', 'T\u00fcr', 55), ('gene', 'Gen / f\u00fczyon', 115),
            ('change', 'De\u011fi\u015fiklik', 145), ('ai', 'YZ \u00f6nerisi', 155), ('selection', 'GenNext Selection', 155),
            ('manual', 'Yerel rapor karar\u0131', 145), ('accepted', 'YZ hakk\u0131nda karar', 155)], height=8)
        self.features_tree.tag_configure('attention', foreground='#9a4e00'); self.features_tree.tag_configure('report', foreground='#086b53')
        self.features_tree.bind('<<TreeviewSelect>>', self._feature_selected)
        self.feature_detail = text_area(lower, height=9)
        note_bar = buttons(self.note_page, [('Notu Kaydet', self.flush_note)])
        self.note_status = tk.StringVar(value='Not rapora otomatik yaz\u0131lmaz; otomatik kay\u0131t a\u00e7\u0131k.')
        ttk.Label(note_bar, textvariable=self.note_status, wraplength=780).pack(side='left', padx=8)
        self.note_text = text_area(self.note_page, editable=True); self.note_text.bind('<<Modified>>', self._note_modified)
        self.bind('<Destroy>', self._destroyed, add='+')

    def load_record(self, rk, sid=None):
        if rk != self.record_key: self.flush_note()
        self.record_key = rk or ''; row = self.db.get_sample(rk) if rk else None
        self.title_var.set(' | '.join(str((row or {}).get(k) or '') for k in ('patient_id', 'sample_type', 'pool_name')) or 'Teknik kay\u0131t se\u00e7ilmedi')
        self.payload = self.repo.record(self.record_key, sid); self._session_map = {}
        ids = list(self.payload['sessions'])
        if sid and int(sid) not in ids: ids.insert(0, int(sid))
        for item in ids:
            meta, _ = self.repo.session(item); label = f"#{item} | {meta.get('imported_at', '')} | {meta.get('sample_scope', '')}"
            self._session_map[label] = item
            if item == self.payload['session_id']: self.session_var.set(label)
        self.session_box['values'] = list(self._session_map)
        if not ids: self.session_var.set('YZ d\u00f6n\u00fc\u015f\u00fc yok')
        self.snapshot = selection_for_record(self.db, rk, (row or {}).get('sample_type', 'DNA')) if row else {}
        old = bool(ids and self.payload['session_id'] and self.payload['session_id'] != max(ids))
        self.caption.set(('GE\u00c7M\u0130\u015e YZ OTURUMU. ' if old else '') + 'YZ kabul/ret karar\u0131 raporland\u0131 anlam\u0131na gelmez; Selection ayr\u0131d\u0131r.')
        put(self.summary_text, patient_text(self.payload)); p = self.payload['patient']
        rna = {k: p[k] for k in ('fusion_review', 'fusion_reviews', 'rna_review', 'biomarker_scope') if k in p}
        roots = [r for r in self.payload['features'] if r['_kind'] == 'RNA']
        if roots: rna['fusion_reviews'] = roots
        put(self.rna_text, readable(rna) if rna else 'RNA / f\u00fczyon YZ notu yok. Bu, negatif f\u00fczyon sonucu de\u011fildir.')
        qc = {k: p[k] for k in ('qc_review', 'qc_summary', 'technical_review', 'contamination_assessment', 'contamination_review', 'warnings', 'technical_actions') if k in p}
        put(self.qc_text, readable(qc) if qc else 'Ayr\u0131 QC yorum alan\u0131 yok. Hasta \u00f6zetini ve ger\u00e7ek QC kaynaklar\u0131n\u0131 kontrol edin.')
        self._populate_features()
        current_note = local_note(self.db, rk) if row else ''
        unchanged = self.note_text.get('1.0', 'end-1c') == self._note_saved
        if self._note_record != self.record_key or (unchanged and current_note != self._note_saved):
            self._loading = True; self._note_record = self.record_key; self._note_saved = local_note(self.db, rk) if row else ''
            self.note_text.delete('1.0', 'end'); self.note_text.insert('1.0', self._note_saved); self.note_text.edit_modified(False); self._loading = False
            self.note_status.set('Yerel not; otomatik kay\u0131t a\u00e7\u0131k.' if row else 'Hasta se\u00e7in.')
        self._populate_history()

    def refresh(self):
        if self.record_key: self.load_record(self.record_key, self.payload.get('session_id'))
    def _choose_session(self, event=None):
        self.load_record(self.record_key, self._session_map.get(self.session_var.get()))
    def _populate_features(self):
        selected = self.features_tree.selection(); self.features_tree.delete(*self.features_tree.get_children()); self._feature_rows = {}
        for n, row in enumerate(self.payload['features']):
            key = feature_key(row); d = decision_for_key(self.db, self.record_key, key) if key else {}
            a = adjudication(self.db, self.payload['session_id'], self.record_key, key) if key else {}; iid = str(n)
            self.features_tree.insert('', 'end', iid=iid, values=(row['_kind'], row.get('gene') or row.get('fusion') or '',
                row.get('change') or row.get('protein') or key, review_status(row), selection_label(self.snapshot, key, row['_kind']),
                MANUAL_LABELS.get(d.get('status'), 'Karar yok'), ADJUDICATION_LABELS.get(a.get('status'), 'Karar bekliyor')),
                tags=('report' if row.get('review_status') == 'report' else 'attention',))
            self._feature_rows[iid] = row
        target = selected[0] if selected and self.features_tree.exists(selected[0]) else next(iter(self._feature_rows), None)
        if target is not None: self.features_tree.selection_set(target); self._feature_selected()
        else: put(self.feature_detail, 'Varyant bazl\u0131 YZ notu yok. Hasta / RNA / QC notlar\u0131 yine de saklan\u0131r.')
    def _selected_feature(self):
        sel = self.features_tree.selection(); return self._feature_rows.get(sel[0]) if sel else None
    def _feature_selected(self, event=None):
        row = self._selected_feature()
        if row: put(self.feature_detail, feature_text(row, self.db, self.record_key, self.snapshot))
    def decide(self, status):
        row = self._selected_feature()
        if not row: return
        current = adjudication(self.db, self.payload['session_id'], self.record_key, feature_key(row))
        note = simpledialog.askstring('YZ karar\u0131', 'Karar notu (iste\u011fe ba\u011fl\u0131). Rapor / Selection de\u011fi\u015fmez:',
                                      initialvalue=current.get('note') or '', parent=self)
        if note is None: return
        try:
            adjudicate(self.db, self.payload['session_id'], self.record_key, feature_key(row), status, note)
            self._populate_features(); self._populate_history()
            if self.on_change: self.on_change()
        except Exception as exc: messagebox.showerror('YZ karar\u0131 kaydedilemedi', str(exc), parent=self)
    def open_selected_mutation(self):
        row = self._selected_feature() or {}
        if self.open_mutations and self.record_key:
            self.open_mutations(self.record_key, {'gene': row.get('gene'), 'protein': row.get('change'), 'type': row.get('_kind'), 'variant_key': feature_key(row)})
    def _populate_history(self):
        rk = self.record_key
        lines = ['YZ / yerel karar / web Selection ba\u011f\u0131ms\u0131zd\u0131r. Eski karar g\u00fcncel pozitiflik veya imzal\u0131 rapor anlam\u0131na gelmez.', '']
        for r in self.db.cx.execute('SELECT * FROM ai_adjudication_history_v0483 WHERE record_key=? ORDER BY id DESC LIMIT 300', (rk,)):
            lines += [f"{r['created_at']} | YZ #{r['session_id']} | {r['feature_key']}", ADJUDICATION_LABELS.get(r['status'], r['status']) + ' | ' + r['note'], '']
        for r in self.db.cx.execute('SELECT * FROM variant_decision_history_v0483 WHERE record_key=? ORDER BY id DESC LIMIT 300', (rk,)):
            lines += [f"{r['created_at']} | {r['variant_key']}", MANUAL_LABELS.get(r['previous_status'], 'Karar yok') + ' -> ' + MANUAL_LABELS.get(r['status'], 'Karar temizlendi'), r['note'] or '', '']
        if self.db._table_exists('reported_variant_history'):
            for r in self.db.cx.execute('SELECT * FROM reported_variant_history WHERE record_key=? ORDER BY id DESC LIMIT 100', (rk,)):
                lines += [f"{r['created_at']} | Yerel rapor ge\u00e7mi\u015fi | {r['gene']} {r['protein'] or r['cdna'] or ''}", 'Kaynak: ' + str(r['source']) + ' | ' + str(r['variant_key']), '']
        if len(lines) == 2: lines.append('Karar ge\u00e7mi\u015fi yok. Eski YZ oturumlar\u0131 sa\u011f \u00fcstteki listeden a\u00e7\u0131l\u0131r.')
        put(self.history_text, '\n'.join(lines))
    def _note_modified(self, event=None):
        if not self.note_text.edit_modified(): return
        self.note_text.edit_modified(False)
        if self._loading or not self._note_record: return
        self.note_status.set('Kaydediliyor...')
        if self._note_timer: self.after_cancel(self._note_timer)
        self._note_timer = self.after(650, self.flush_note)
    def flush_note(self):
        if self._note_timer: self.after_cancel(self._note_timer); self._note_timer = None
        if not self._note_record: return
        value = self.note_text.get('1.0', 'end-1c')
        if value == self._note_saved: return
        try:
            save_local_note(self.db, self._note_record, value, expected_note=self._note_saved); self._note_saved = value
            self.note_status.set('Not kaydedildi; rapora otomatik eklenmez.')
        except Exception as exc:
            self.note_status.set('NOT KAYDED\u0130LEMED\u0130: ' + str(exc)); raise
    def _destroyed(self, event):
        if event.widget is self and self._note_timer:
            try: self.after_cancel(self._note_timer)
            except tk.TclError: pass
            self._note_timer = None


class AIReviewCenter(LegacyIGV):
    def _build(self):
        self._batch_active = False; self._selected_session = None; self._session_map = {}; self._patients = {}
        self.repo = ReviewRepository(self.db); self.rna_patient_count_var = tk.StringVar(); self.igv_evidence_scope_var = tk.StringVar()
        top = ttk.Frame(self, padding=(12, 10)); top.pack(fill='x')
        ttk.Label(top, text='POOL YZ De\u011ferlendirme Merkezi', font=('Segoe UI', 13, 'bold')).pack(side='left')
        ttk.Button(top, text='YZ JSON / ZIP \u0130\u00e7e Al', command=self.import_file).pack(side='right', padx=(8, 0))
        ttk.Button(top, text='Yenile', command=self.refresh).pack(side='right')
        bar = ttk.Frame(self, padding=(12, 0, 12, 8)); bar.pack(fill='x')
        ttk.Label(bar, text='YZ oturumu / POOL:').pack(side='left'); self.session_var = tk.StringVar()
        self.session_box = ttk.Combobox(bar, textvariable=self.session_var, state='readonly', width=65)
        self.session_box.pack(side='left', fill='x', expand=True, padx=8); self.session_box.bind('<<ComboboxSelected>>', self._session_changed)
        self.pages = ttk.Notebook(self); self.pages.pack(fill='both', expand=True, padx=10, pady=(0, 8))
        self.pool_page = page(self.pages, 'POOL \u00d6zeti'); self.patient_page = page(self.pages, 'Hasta \u0130nceleme')
        self.igv_page = page(self.pages, 'IGV Toplama'); self.igv_review_page = page(self.pages, 'IGV Karar / Kan\u0131t')
        self.archive_page = page(self.pages, 'Ge\u00e7mi\u015f / Ar\u015fiv')
        buttons(self.pool_page, [('Se\u00e7ili Hastan\u0131n YZ Notlar\u0131', self.show_selected_patient), ('Hasta / Enlil Raporlama', self.open_patient_workspace)])
        split = ttk.Panedwindow(self.pool_page, orient='vertical'); split.pack(fill='both', expand=True)
        upper, lower = ttk.Frame(split), ttk.Frame(split); split.add(upper, weight=2); split.add(lower, weight=3)
        self.pool_summary_text = text_area(upper, height=5)
        self.patients_tree = tree_area(lower, [('patient', 'Hasta / MP', 110), ('type', 'T\u00fcr', 60), ('release', 'YZ genel \u00f6nerisi', 160),
            ('features', 'Varyant notu', 95), ('summary', 'K\u0131sa hasta de\u011ferlendirmesi', 650)], height=12)
        self.patients_tree.bind('<Double-1>', lambda e: self.show_selected_patient())
        self.patient_panel = PatientReviewPanel(self.patient_page, self.base, self.db, self.cfg, on_change=self._notify_change, open_mutations=self._open_mutations)
        self.patient_panel.pack(fill='both', expand=True)
        self._build_igv_page(); self._build_review_page(); self._build_archive_page()
    def _build_igv_page(self):
        bar = buttons(self.igv_page, [('Se\u00e7ili IGV\u2019leri Haz\u0131rla', self.auto_capture_selected), ('Eksik / Hatal\u0131lar\u0131 Tekrarla', self.retry_missing), ('Toplamay\u0131 Durdur', self.stop_web_collection)])
        cb = ttk.Combobox(bar, textvariable=self.filter_var, values=('T\u00fcm\u00fc', 'Tamam', 'Partial', 'Bekleyen', 'Hata'), state='readonly', width=12)
        cb.pack(side='right'); cb.bind('<<ComboboxSelected>>', lambda e: self.refresh())
        ttk.Label(self.igv_page, textvariable=self.summary_var, wraplength=1000).pack(fill='x', pady=(0, 8))
        buttons(self.igv_page, [('IGV Paketini Olu\u015ftur', self.export_igv_ai), ('Se\u00e7ili IGV\u2019yi A\u00e7', self.open_igv), ('Kan\u0131t / Karar sekmesi', lambda: self.pages.select(self.igv_review_page))])
        self.tree = tree_area(self.igv_page, [('lamp', 'IGV', 90), ('patient', 'Hasta / MP', 130), ('type', 'T\u00fcr', 50),
            ('gene', 'Gen', 90), ('change', 'Varyant', 160), ('zoom', '4 g\u00f6r\u00fcnt\u00fc', 210), ('data', 'Veri', 65), ('error', 'Hata / Uyar\u0131', 300)], height=14, multiple=True)
        for tag, color in [('complete', '#087a2c'), ('partial', '#9a5b00'), ('pending', '#64748b'), ('error', '#b42318')]: self.tree.tag_configure(tag, foreground=color)
        self.tree.bind('<<TreeviewSelect>>', self._igv_selection); self.tree.bind('<Double-1>', lambda e: self.open_igv())
    def _build_review_page(self):
        self.igv_chosen = tk.StringVar(value='IGV Toplama sekmesinden bir sat\u0131r se\u00e7in.')
        ttk.Label(self.igv_review_page, textvariable=self.igv_chosen, font=('Segoe UI', 11, 'bold'), wraplength=1000).pack(fill='x', pady=(0, 12))
        buttons(self.igv_review_page, [('YZ IGV de\u011ferlendirmesi', self.show_igv_review_result), ('Varyant / t\u00fcm\u00f6r ba\u011flam\u0131', self.show_igv_variant_tumor_context)])
        buttons(self.igv_review_page, [('\u0130stek ge\u00e7mi\u015fi', self.show_igv_request_history), ('Kan\u0131t JSON', self.open_snapshot)])
        ttk.Label(self.igv_review_page, text='Patolog IGV karar\u0131 (raporland\u0131 anlam\u0131na gelmez)', font=('Segoe UI', 11, 'bold')).pack(anchor='w', pady=(20, 6))
        buttons(self.igv_review_page, [('Destekli', lambda: self.set_decision('supported')), ('Artefakt', lambda: self.set_decision('artifact')),
            ('Belirsiz', lambda: self.set_decision('uncertain')), ('Karar\u0131 Temizle', self.clear_decision)])
        ttk.Label(self.igv_review_page, textvariable=self.igv_evidence_scope_var, wraplength=1000).pack(fill='x', pady=12)
        ttk.Label(self.igv_review_page, text='IGV g\u00f6r\u00fcnt\u00fcs\u00fc, ger\u00e7ek okuma say\u0131m\u0131 ve patolog karar\u0131 ayr\u0131 kan\u0131tlard\u0131r.\nGenNext Selection bu d\u00fc\u011fmelerle de\u011fi\u015ftirilmez.', wraplength=1000).pack(fill='x', pady=12)
    def _build_archive_page(self):
        ttk.Label(self.archive_page, text='Eski YZ d\u00f6n\u00fc\u015fleri silinmez; \u00fcstteki listeden yeniden a\u00e7\u0131l\u0131r.', font=('Segoe UI', 11, 'bold')).pack(anchor='w', pady=(0, 12))
        buttons(self.archive_page, [('Se\u00e7ili POOL YZ / Karar Ar\u015fivini ZIP Kaydet', self.export_decision_archive)])
        ttk.Label(self.archive_page, text='Yerel ar\u015fiv: orijinal YZ d\u00f6n\u00fc\u015f\u00fc, hasta \u00f6zetleri, yerel notlar, kabul/ret, Selection ve yerel rapor ge\u00e7mi\u015fi.\nTam ham veri / BAM yede\u011fi de\u011fildir. Otomatik internete veya YZ\u2019ye g\u00f6nderilmez.', wraplength=1000).pack(fill='x', pady=12)
        self.archive_text = text_area(self.archive_page)
    def _latest_session_id(self):
        if self._selected_session: return self._selected_session
        rows = self.db.ai_review_sessions(1); return int(rows[0]['id']) if rows else None
    def refresh(self):
        if not hasattr(self, 'pages'): return
        rows = self.db.ai_review_sessions(500); self._session_map = {}
        for r in rows:
            self._session_map[f"#{r['id']} | {r['pool_group'] or '-'} | {r['sample_scope'] or '-'} | {r['imported_at']}"] = int(r['id'])
        if not self._selected_session and rows: self._selected_session = int(rows[0]['id'])
        self.session_box['values'] = list(self._session_map)
        for label, sid in self._session_map.items():
            if sid == self._selected_session: self.session_var.set(label)
        super().refresh(); self._refresh_pool()
    def _refresh_pool(self):
        sid = self._latest_session_id(); meta, doc = self.repo.session(sid)
        put(self.pool_summary_text, readable(doc.get('pool_review') or 'Hen\u00fcz doldurulmu\u015f YZ d\u00f6n\u00fc\u015f\u00fc al\u0131nmad\u0131.'))
        old = self.patients_tree.selection(); self.patients_tree.delete(*self.patients_tree.get_children()); self._patients = {}
        for n, p in enumerate(self.repo.summaries(sid)):
            rk = p['record_key']; s = self.db.get_sample(rk) or {}; iid = str(n)
            self.patients_tree.insert('', 'end', iid=iid, values=(p.get('patient_id') or s.get('patient_id') or '?', p.get('sample_type') or s.get('sample_type') or '?',
                RELEASE_LABELS.get(p.get('release_recommendation'), p.get('release_recommendation') or '-'), len(self.repo.record(rk, sid)['features']), p.get('summary') or ''))
            self._patients[iid] = rk
        if old and self.patients_tree.exists(old[0]): self.patients_tree.selection_set(old[0])
        elif self._patients: self.patients_tree.selection_set(next(iter(self._patients)))
        put(self.archive_text, readable({k: v for k, v in meta.items() if k in ('id', 'schema_name', 'pool_group', 'sample_scope', 'imported_at', 'source_sha256')}))
    def _session_changed(self, event=None):
        self.patient_panel.flush_note(); self._selected_session = self._session_map.get(self.session_var.get()); self.refresh(); self.show_selected_patient(switch=False)
    def show_selected_patient(self, switch=True):
        sel = self.patients_tree.selection(); rk = self._patients.get(sel[0]) if sel else None
        if rk:
            self.patient_panel.load_record(rk, self._latest_session_id())
            if switch: self.pages.select(self.patient_page)
    def show_patient_reviews(self): self.pages.select(self.pool_page)
    def open_patient_workspace(self):
        sel = self.patients_tree.selection(); rk = self._patients.get(sel[0]) if sel else None; host = self.winfo_toplevel()
        if rk and hasattr(host, 'patient_workspace') and host.patient_workspace.select_record(rk, 'ai'): host.workspaces.select(host.patient_workspace)
    def _open_mutations(self, rk, item=None):
        host = self.winfo_toplevel()
        if hasattr(host, 'open_record_mutations'): host.open_record_mutations(rk, item)
    def _igv_selection(self, event=None):
        ids = self._selected_ids(False); r = self.db.get_igv_queue_item(ids[0]) if ids else None
        self.igv_chosen.set((str(r.get('patient_id') or '') + ' | ' + str(r.get('gene') or '') + ' ' + str(r.get('change') or r.get('variant_key') or '')) if r else 'IGV Toplama sekmesinden bir sat\u0131r se\u00e7in.')
    def _notify_change(self): self.winfo_toplevel().event_generate('<<AIReviewsChanged>>', when='tail')
    def import_file(self):
        path = filedialog.askopenfilename(parent=self, title='Doldurulmu\u015f YZ / IGV d\u00f6n\u00fc\u015f\u00fc', filetypes=[('JSON / ZIP', '*.json *.zip'), ('T\u00fcm dosyalar', '*.*')])
        if not path: return
        try:
            from web_interface_panel import is_web_ai_review_file
            if is_web_ai_review_file(Path(path)):
                host = self.winfo_toplevel()
                if not hasattr(host, 'web_panel'): raise ValueError('Bu dosya Web Aray\u00fcz\u00fc de\u011ferlendirmesidir; genomik YZ de\u011fildir.')
                host.workspaces.select(host.web_panel); host.web_panel.import_ai_file(path); return
            data, source, digest = load_ai_review_return(Path(path)); errors, warnings = validate_ai_review_return(data)
            if errors: raise ValueError('\n'.join(errors))
            result = self.db.import_ai_review(data, source, digest)
            self._selected_session = int(result.get('session_id') or 0) or self._selected_session
            self.refresh(); self.pages.select(self.pool_page); self.show_selected_patient(switch=False); self._notify_change()
            lines = ['YZ d\u00f6n\u00fc\u015f\u00fc kaydedildi; hasta ve varyant notlar\u0131 sekmelerde g\u00f6r\u00fcn\u00fcr.',
                f"Hasta: {result.get('patient_reviews', 0)} | Varyant: {result.get('variant_reviews', 0)} | IGV: {result.get('igv_requests', 0)}",
                'Rapor metni, GenNext Selection ve yerel rapor karar\u0131 otomatik de\u011fi\u015ftirilmedi.']
            if result.get('duplicate_import'): lines.append('Bu d\u00f6n\u00fc\u015f zaten kay\u0131tl\u0131yd\u0131; kopya olu\u015fturulmad\u0131.')
            lines += list(warnings or [])[:5] + list(result.get('warnings') or [])[:8]
            messagebox.showinfo('YZ i\u00e7e alma', '\n\n'.join(lines), parent=self)
        except Exception as exc: messagebox.showerror('YZ al\u0131namad\u0131', str(exc), parent=self)
    def export_decision_archive(self):
        from review_workspace_archive import write_review_archive
        sid = self._latest_session_id()
        if not sid: messagebox.showinfo('Ar\u015fiv', '\u00d6nce bir YZ oturumu se\u00e7in.', parent=self); return
        self.patient_panel.flush_note()
        path = filedialog.asksaveasfilename(parent=self, title='Yerel YZ / karar ar\u015fivi', defaultextension='.zip', initialfile=f'NGS_YZ_KARAR_ARSIVI_{sid}.zip', filetypes=[('ZIP', '*.zip')])
        if not path: return
        try:
            write_review_archive(self.db, sid, Path(path)); messagebox.showinfo('Ar\u015fiv', 'Yerel karar ar\u015fivi kaydedildi:\n' + path, parent=self)
        except Exception as exc: messagebox.showerror('Ar\u015fiv olu\u015fturulamad\u0131', str(exc), parent=self)
