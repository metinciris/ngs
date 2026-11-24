"""Local report archive: exact technical records and readable manual comments."""
from __future__ import annotations

import copy
import math
import queue
import re
import threading
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox

from manual_report_closure import STATUSES as MANUAL_CLOSURE_LABELS, CLOSED_STATUSES, save_manual_closure

from report_archive_model import (
    filter_archive_rows, format_archive_summary, load_archive_rows, normalize_search,
)


REPORT_FILTERS = {
    'Tüm rapor durumları': '',
    'Yalnız taslak kaydı': 'draft',
    'Arşivlenmiş raporu var': 'archived',
    'Onay geçmişi var': 'approval_history',
    'Teknik rapor kaydı yok': 'missing',
}
CLOSURE_OPTIONS = {
    'Durum seçiniz': '',
    'Raporlandı - NEGATİF': 'reported_negative',
    'Raporlandı - POZİTİF': 'reported_positive',
    'İPTAL - inceleme tamamlandı': 'cancelled',
    'YETERSİZ - inceleme tamamlandı': 'insufficient',
    'İnceleme bitti - rapor verilmedi': 'review_closed',
    'Kapanışı geri al / yeniden aç': 'open',
}
CLOSURE_FILTERS = {'Tüm manuel kapanışlar': '',
                   'Manuel kapanışı olmayan': 'none',
                   'Manuel pozitif': 'reported_positive',
                   'Manuel negatif': 'reported_negative',
                   'İptal': 'cancelled',
                   'Yetersiz': 'insufficient',
                   'İnceleme bitti / rapor yok': 'review_closed',
                   'Yeniden açılan': 'open'}

ANALYSIS_FILTERS = {
    'Tüm analiz durumları': '', 'Tamamlanmış': 'complete',
    'Tamamlanmamış': 'incomplete', 'Kontrol gerekli': 'review',
    'Durum bilinmiyor': 'unknown',
}
ALL_POOLS = 'Tüm POOL’lar'
ALL_DOCTORS = 'Tüm doktorlar'


def _shown(value, empty='—'):
    value = str(value if value is not None else '').strip()
    return value or empty


def _natural(value):
    return tuple((0, int(part)) if part.isdigit() else (1, part)
                 for part in re.split(r'(\d+)', normalize_search(value)))


class ReportArchivePanel(ttk.Frame):
    """Reads snapshots in a worker and renders a bounded page on Tk's thread."""

    PAGE_SIZE = 5000
    COLUMNS = (
        ('pool_display', 'POOL', 96),
        ('dna_ids', 'DNA MP', 110),
        ('rna_ids', 'RNA MP', 110),
        ('other_ids', 'DİĞER / TEKRAR', 134),
        ('national_id', 'T.C.', 120),
    )

    def __init__(self, parent, base, db, *, form_provider=None,
                 open_report=None, open_metadata=None, open_dual=None):
        super().__init__(parent, padding=(12, 10))
        self.base, self.db = Path(base), db
        self.form_provider = form_provider
        self.open_report_cb, self.open_metadata_cb = open_report, open_metadata
        self.open_dual_cb = open_dual
        self.rows, self.filtered = [], []
        self.current_row = None
        self.current_group = None
        self._row_map = {}
        self._group_map = {}
        self.filtered_groups = []
        self._selected_group_key = ''
        self._study_rows = []
        self._events = queue.Queue()
        self._loading = False
        self._refresh_pending = False
        self._alive = True
        self._loaded_once = False
        self._dirty = True
        self._filling = False
        self._filtering = False
        self._filter_after = None
        self._poll_after = None
        self._page = 0
        self._sort_key = None
        self._sort_reverse = False
        self._selected_key = ''
        self._history_entries = []
        self._split_set = False
        self._configure_style()
        self._build()
        self.bind('<Destroy>', self._on_destroy, add='+')
        self._poll_after = self.after(90, self._poll)

    def _configure_style(self):
        style = ttk.Style(self)
        style.configure('ReportArchive.Treeview', rowheight=34, font=('Segoe UI', 10),
                        background='white', fieldbackground='white', foreground='#253e52')
        style.configure('ReportArchive.Treeview.Heading', font=('Segoe UI', 9, 'bold'), padding=(6, 8))
        style.map('ReportArchive.Treeview', background=[('selected', '#185b79')],
                  foreground=[('selected', 'white')])
        style.configure('ReportArchive.TNotebook.Tab', padding=(10, 5), font=('Segoe UI', 9, 'bold'))

    def _build(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(4, weight=1)
        head = ttk.Frame(self)
        head.grid(row=0, column=0, sticky='ew', pady=(0, 6))
        title = ttk.Frame(head)
        title.pack(side='left', fill='x', expand=True)
        ttk.Label(title, text='Rapor Arşivi', font=('Segoe UI', 18, 'bold'),
                  foreground='#153e56').pack(anchor='w')
        ttk.Label(title, text='DNA ve RNA Selection · GenNext Analyzed · özgün Enlil rapor cümleleri • Bütün POOL’lar',
                  foreground='#60758a', font=('Segoe UI', 9)).pack(anchor='w', pady=(1, 0))
        ttk.Button(head, text='Ara / Filtrele', command=self._toggle_filters).pack(side='right', padx=(8, 0))
        self.refresh_button = ttk.Button(head, text='Arşivi yenile', command=self.refresh)
        self.refresh_button.pack(side='right', padx=(10, 0))

        search = ttk.Frame(self)
        search.grid(row=1, column=0, sticky='ew', pady=(3, 6))
        search.columnconfigure(1, weight=1)
        ttk.Label(search, text='ARŞİVDE ARA', font=('Segoe UI', 9, 'bold'),
                  foreground='#33556d').grid(row=0, column=0, padx=(0, 10))
        self.query_var = tk.StringVar()
        self.search_entry = ttk.Entry(search, textvariable=self.query_var, font=('Segoe UI', 11))
        self.search_entry.grid(row=0, column=1, sticky='ew', ipady=4)
        self.search_entry.bind('<Return>', lambda _e: self._apply_filters())
        self.search_entry.bind('<Escape>', lambda _e: self.clear_filters())
        ttk.Button(search, text='Temizle', command=self.clear_filters).grid(row=0, column=2, padx=(8, 0))
        self.query_var.trace_add('write', self._schedule_filter)

        filters = ttk.Frame(self)
        filters.grid(row=2, column=0, sticky='ew', pady=(0, 5))
        self.pool_var = tk.StringVar(value=ALL_POOLS)
        self.assay_var = tk.StringVar(value='Tüm türler')
        self.doctor_var = tk.StringVar(value=ALL_DOCTORS)
        self.report_var = tk.StringVar(value=next(iter(REPORT_FILTERS)))
        self.analysis_var = tk.StringVar(value=next(iter(ANALYSIS_FILTERS)))
        self.closure_var = tk.StringVar(value=next(iter(CLOSURE_FILTERS)))
        specs = (
            ('POOL', self.pool_var, [ALL_POOLS], 22),
            ('Çalışma', self.assay_var, ['Tüm türler', 'DNA', 'RNA', 'DNA+RNA'], 12),
            ('Doktor / imza', self.doctor_var, [ALL_DOCTORS], 26),
            ('Rapor kaydı', self.report_var, list(REPORT_FILTERS), 25),
            ('Analiz', self.analysis_var, list(ANALYSIS_FILTERS), 23),
        )
        self.filter_boxes = []
        for col, (label, variable, values, width) in enumerate(specs):
            holder = ttk.Frame(filters)
            holder.grid(row=0, column=col, sticky='ew', padx=(0, 10 if col < 4 else 0))
            filters.columnconfigure(col, weight=1 if col in (0, 2) else 0)
            ttk.Label(holder, text=label, foreground='#60758a', font=('Segoe UI', 8)).pack(anchor='w')
            combo = ttk.Combobox(holder, textvariable=variable, values=values,
                                 state='readonly', width=width)
            combo.pack(fill='x', pady=(2, 0))
            combo.bind('<<ComboboxSelected>>', lambda _e: self._apply_filters())
            self.filter_boxes.append(combo)
        closure_holder = ttk.Frame(filters)
        closure_holder.grid(row=1, column=0, columnspan=2, sticky='w', pady=(7, 0))
        ttk.Label(closure_holder, text='MANUEL RAPOR KAPANIŞI', foreground='#60758a',
                  font=('Segoe UI', 8)).pack(anchor='w')
        closure_box = ttk.Combobox(closure_holder, textvariable=self.closure_var,
                                   state='readonly', width=32, values=list(CLOSURE_FILTERS))
        closure_box.pack(fill='x', pady=(2, 0))
        closure_box.bind('<<ComboboxSelected>>', lambda _e: self._apply_filters())

        self._filters_visible = False
        search.grid_remove()
        filters.grid_remove()
        self.search_frame, self.filters_frame = search, filters
        self.count_var = tk.StringVar(value='Arama: MP, hasta adı, tanı, gen / yorum, blok, doktor veya POOL')
        ttk.Label(self, textvariable=self.count_var, font=('Segoe UI', 9),
                  foreground='#52697b').grid(row=3, column=0, sticky='w', pady=(2, 5))

        self.panes = ttk.Panedwindow(self, orient='horizontal')
        self.panes.grid(row=4, column=0, sticky='nsew')
        listing = ttk.Frame(self.panes)
        detail = ttk.Frame(self.panes)
        self.panes.add(listing, weight=2)
        self.panes.add(detail, weight=8)
        self.panes.bind('<Configure>', self._initial_split, add='+')
        self._build_table(listing)
        self._build_detail(detail)

        self.status_var = tk.StringVar(value='Yerel kayıtlar sekme açıldığında yüklenir.')
        ttk.Label(self, textvariable=self.status_var, font=('Segoe UI', 9),
                  foreground='#52697b').grid(row=5, column=0, sticky='ew', pady=(5, 0))
        self._clear_detail()

    def _toggle_filters(self):
        self._filters_visible = not self._filters_visible
        for frame in (self.search_frame, self.filters_frame):
            if self._filters_visible:
                frame.grid()
            else:
                frame.grid_remove()
        if self._filters_visible:
            self.search_entry.focus_set()

    def _build_table(self, parent):
        parent.rowconfigure(0, weight=1)
        parent.columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(parent, columns=[x[0] for x in self.COLUMNS],
                                 show='tree headings', selectmode='browse', height=12,
                                 style='ReportArchive.Treeview')
        self.tree.heading('#0', text='AD SOYAD', anchor='w')
        self.tree.column('#0', width=220, minwidth=160, stretch=True)
        for key, title, width in self.COLUMNS:
            self.tree.heading(key, text=title, anchor='w')
            self.tree.column(key, width=width, minwidth=84,
                             stretch=key in ('pool_display', 'other_ids'), anchor='w')
        ys = ttk.Scrollbar(parent, orient='vertical', command=self.tree.yview)
        xs = ttk.Scrollbar(parent, orient='horizontal', command=self.tree.xview)
        self.tree.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        self.tree.grid(row=0, column=0, sticky='nsew')
        ys.grid(row=0, column=1, sticky='ns')
        xs.grid(row=1, column=0, sticky='ew')
        self.tree.tag_configure('odd', background='#f2f6fa')
        self.tree.tag_configure('incomplete', background='#fff8e7')
        self.tree.tag_configure('review', background='#fef0ed')
        self.tree.bind('<<TreeviewSelect>>', self._on_select)
        self.tree.bind('<ButtonRelease-1>', self._on_table_click)
        self.tree.bind('<Double-1>', self._double_click)
        self.tree.bind('<Return>', lambda _e: self._open('report'))
        footer = ttk.Frame(parent)
        footer.grid(row=2, column=0, columnspan=2, sticky='ew', pady=(4, 3))
        self.page_var = tk.StringVar(value='')
        ttk.Label(footer, textvariable=self.page_var, foreground='#60758a',
                  font=('Segoe UI', 8)).pack(side='left')
        self.next_button = ttk.Button(footer, text='Sonraki ›', command=lambda: self._change_page(1))
        self.next_button.pack(side='right')
        self.previous_button = ttk.Button(footer, text='‹ Önceki', command=lambda: self._change_page(-1))
        self.previous_button.pack(side='right', padx=5)

    def _build_detail(self, parent):
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(4, weight=1)
        banner = tk.Frame(parent, bg='#173f57', padx=12, pady=9)
        banner.grid(row=0, column=0, sticky='ew', pady=(4, 6))
        self.record_var = tk.StringVar()
        self.patient_var = tk.StringVar()
        tk.Label(banner, textvariable=self.record_var, bg='#173f57', fg='white',
                 font=('Segoe UI', 13, 'bold'), anchor='w').pack(side='left', fill='x', expand=True)
        tk.Label(banner, textvariable=self.patient_var, bg='#173f57', fg='#d5e7ee',
                 font=('Segoe UI', 9), anchor='e').pack(side='right')

        self.identity_var = tk.StringVar()
        self.identity_label = ttk.Label(parent, textvariable=self.identity_var,
                                        font=('Segoe UI', 11, 'bold'), foreground='#163f57', wraplength=900)
        self.identity_label.grid(row=1, column=0, sticky='ew', padx=6, pady=(3, 4))

        self.overview_var = tk.StringVar()
        self.overview_label = ttk.Label(parent, textvariable=self.overview_var,
                                       font=('Segoe UI', 10), foreground='#235874',
                                       justify='left', wraplength=900)
        self.overview_label.grid(row=2, column=0, sticky='ew', padx=6, pady=(0, 7))
        parent.bind('<Configure>', lambda e: (
            self.identity_label.configure(wraplength=max(300, e.width-35)),
            self.overview_label.configure(wraplength=max(300, e.width-35))) if e.widget is parent else None, add='+')

        jump = ttk.Frame(parent)
        jump.grid(row=3, column=0, sticky='ew', padx=4, pady=(1, 6))
        self.dna_jump = ttk.Button(jump, text='DNA raporuna git', command=lambda: self._jump_to_assay('DNA'))
        self.dna_jump.pack(side='left', padx=(0, 6))
        self.rna_jump = ttk.Button(jump, text='RNA raporuna git', command=lambda: self._jump_to_assay('RNA'))
        self.rna_jump.pack(side='left', padx=(0, 6))
        ttk.Label(jump, text='Raporlar aynı sayfada alt alta · MP numarasına tıklayabilirsiniz.',
                  foreground='#60758a', font=('Segoe UI', 8)).pack(side='left')

        text_frame = ttk.Frame(parent)
        text_frame.grid(row=4, column=0, sticky='nsew')
        text_frame.columnconfigure(0, weight=1)
        text_frame.rowconfigure(0, weight=1)
        self.full_report_text = tk.Text(text_frame, wrap='word', font=('Segoe UI', 10),
                                       bg='white', fg='#243f54', relief='flat', padx=12, pady=10,
                                       state='disabled', cursor='arrow', selectbackground='#d2e8f2')
        scroll = ttk.Scrollbar(text_frame, orient='vertical', command=self.full_report_text.yview)
        self.full_report_text.configure(yscrollcommand=scroll.set)
        self.full_report_text.grid(row=0, column=0, sticky='nsew')
        scroll.grid(row=0, column=1, sticky='ns')
        self.full_report_text.tag_configure('title', font=('Segoe UI', 12, 'bold'), foreground='#15445d', spacing1=8, spacing3=6)
        self.full_report_text.tag_configure('diagnosis', font=('Segoe UI', 10, 'bold'), foreground='#153f57', spacing3=4)
        self.full_report_text.tag_configure('mutations', font=('Segoe UI', 10), foreground='#1c3e51', spacing3=7)
        self.full_report_text.tag_configure('history', font=('Segoe UI', 10, 'bold'), foreground='#53697b', spacing1=9)
        self.full_report_text.tag_configure('uncertain', foreground='#9b6426')
        self._study_positions = {}
        self._study_tag_serial = 0

        actions = ttk.Frame(parent)
        actions.grid(row=5, column=0, sticky='ew', pady=(6, 0))
        self.action_buttons = []
        for label, callback in (
            ('Seçili raporu düzenle', lambda: self._open('report')),
            ('Raporu manuel kapat / aç', self.edit_manual_closure),
            ('Hasta / Blok', lambda: self._open('metadata')),
            ('DNA + RNA', lambda: self._open('dual')),
            ('Seçili yorumu büyüt', self.expand_comment),
            ('Özeti kopyala', self.copy_summary),
            ('Yorumu kopyala', self.copy_comment),
        ):
            button = ttk.Button(actions, text=label, command=callback)
            button.pack(side='left', padx=(0, 6))
            self.action_buttons.append(button)

    def _text_widget(parent, font):
        frame = ttk.Frame(parent)
        frame.pack(fill='both', expand=True)
        widget = tk.Text(frame, wrap='word', height=4, state='disabled', font=font,
                         bg='white', fg='#253e52', relief='flat', padx=10, pady=6,
                         selectbackground='#d2e8f2', selectforeground='#163d55')
        bar = ttk.Scrollbar(frame, orient='vertical', command=widget.yview)
        widget.configure(yscrollcommand=bar.set)
        widget.pack(side='left', fill='both', expand=True)
        bar.pack(side='right', fill='y')
        return widget

    @staticmethod
    def _set_text(widget, text):
        widget.configure(state='normal')
        widget.delete('1.0', 'end')
        widget.insert('1.0', text)
        widget.configure(state='disabled')
        widget.yview_moveto(0)

    def _initial_split(self, event):
        if event.width < 520:
            return
        preferred = max(230, min(355, int(event.width * .30)))
        if not self._split_set:
            self._split_set = True
            self.panes.sashpos(0, preferred)

    def activate(self):
        # Refresh each entry to also pick up local form edits without app-wide events.
        self.refresh()

    def invalidate(self):
        self._dirty = True

    def refresh(self):
        if not self._alive:
            return
        if self._loading:
            self._refresh_pending = True
            return
        try:
            forms = copy.deepcopy(self.form_provider()) if self.form_provider else None
        except Exception as exc:
            self.status_var.set('Açık rapor taslağı okunamadı: ' + str(exc))
            return
        self._loading = True
        self.status_var.set('Rapor arşivi okunuyor…')
        self.refresh_button.configure(text='Yükleniyor…')
        database, base, result_queue = self.db, self.base, self._events

        def worker():
            try:
                rows = load_archive_rows(database, base, form_data=forms)
                result_queue.put(('rows', rows))
            except Exception as exc:
                result_queue.put(('error', str(exc)))

        threading.Thread(target=worker, name='NGS-report-archive', daemon=True).start()

    def _poll(self):
        if not self._alive:
            return
        try:
            kind, data = self._events.get_nowait()
        except queue.Empty:
            pass
        else:
            self._loading = False
            self.refresh_button.configure(text='Arşivi yenile')
            if self._refresh_pending:
                self._refresh_pending = False
                self.refresh()
            elif kind == 'error':
                self.status_var.set('Arşiv yenilenemedi: ' + data)
            else:
                self.rows = data
                self._loaded_once = True
                self._dirty = False
                pools = sorted({r.get('pool_name', '') for r in data if r.get('pool_name')}, key=_natural)
                doctors = sorted({r.get('signer', '') for r in data if r.get('signer')}, key=normalize_search)
                self.filter_boxes[0].configure(values=[ALL_POOLS] + pools)
                self.filter_boxes[2].configure(values=[ALL_DOCTORS] + doctors)
                if self.pool_var.get() not in [ALL_POOLS] + pools:
                    self.pool_var.set(ALL_POOLS)
                if self.doctor_var.get() not in [ALL_DOCTORS] + doctors:
                    self.doctor_var.set(ALL_DOCTORS)
                self._apply_filters(reset_page=False)
                self.status_var.set('Arşiv güncel • Listeden seçin; çift tıklayarak ilgili raporlamayı açın.')
        self._poll_after = self.after(90, self._poll)

    def _schedule_filter(self, *_args):
        if self._filtering or not self._alive:
            return
        if self._filter_after:
            self.after_cancel(self._filter_after)
        self._filter_after = self.after(200, self._apply_filters)

    def clear_filters(self):
        self._filtering = True
        try:
            self.query_var.set('')
            self.pool_var.set(ALL_POOLS)
            self.assay_var.set('Tüm türler')
            self.doctor_var.set(ALL_DOCTORS)
            self.report_var.set(next(iter(REPORT_FILTERS)))
            self.analysis_var.set(next(iter(ANALYSIS_FILTERS)))
            self.closure_var.set(next(iter(CLOSURE_FILTERS)))
        finally:
            self._filtering = False
        self._apply_filters()
        self.search_entry.focus_set()

    def _apply_filters(self, reset_page=True):
        if self._filter_after:
            self.after_cancel(self._filter_after)
            self._filter_after = None
        self.filtered = filter_archive_rows(
            self.rows, query=self.query_var.get(),
            pool='' if self.pool_var.get() == ALL_POOLS else self.pool_var.get(),
            assay='' if self.assay_var.get() == 'Tüm türler' else self.assay_var.get(),
            report_status=REPORT_FILTERS.get(self.report_var.get(), ''),
            analysis_status=ANALYSIS_FILTERS.get(self.analysis_var.get(), ''),
            closure_status=CLOSURE_FILTERS.get(self.closure_var.get(), ''),
        )
        if self.doctor_var.get() != ALL_DOCTORS:
            doctor = normalize_search(self.doctor_var.get())
            self.filtered = [r for r in self.filtered if normalize_search(r.get('signer', '')) == doctor]
        if self._sort_key:
            self.filtered.sort(key=lambda r: _natural(r.get(self._sort_key, '')), reverse=self._sort_reverse)
        if reset_page:
            self._page = 0
        if self._selected_key:
            index = next((i for i, r in enumerate(self.filtered) if r['record_key'] == self._selected_key), None)
            if index is not None:
                self._page = index // self.PAGE_SIZE
        self.filtered_groups = self._build_patient_groups(self.filtered)
        if self._selected_group_key:
            index = next((i for i, g in enumerate(self.filtered_groups) if g['group_key'] == self._selected_group_key), None)
            if index is not None:
                self._page = index // self.PAGE_SIZE
        self._page = min(self._page, max(0, math.ceil(len(self.filtered_groups) / self.PAGE_SIZE) - 1))
        comments = sum(bool(r.get('comment')) for r in self.filtered)
        incomplete = sum(r.get('analysis_status_key') == 'incomplete' for r in self.filtered)
        history = sum(bool(r.get('history')) for r in self.filtered)
        manual_closed = sum(r.get('manual_closure_status_key') in CLOSED_STATUSES for r in self.filtered)
        self.count_var.set(f'{len(self.filtered_groups)} / {len({self._patient_group_key(r) for r in self.rows})} hasta   •   '
                           f'{len(self.filtered)} teknik çalışma   •   {comments} mutasyon / yorum   •   '
                           f'{history} rapor geçmişi   •   {manual_closed} manuel kapatılmış   •   '
                           f'{incomplete} tamamlanmamış analiz')
        self._render_page()

    def sort_by(self, key):
        self._sort_reverse = not self._sort_reverse if self._sort_key == key else False
        self._sort_key = key
        self._apply_filters(reset_page=False)

    def _change_page(self, step):
        self._page = max(0, min(self._page + step, max(0, math.ceil(len(self.filtered_groups)/self.PAGE_SIZE)-1)))
        self._selected_group_key = ''
        self._render_page()

    def _patient_group_key(self, row):
        national = normalize_search(row.get('national_id', ''))
        if national:
            return 'tc:' + national
        family = normalize_search(row.get('case_family', ''))
        if family:
            return 'family:' + family
        name = normalize_search(row.get('full_name', ''))
        if name:
            return 'name:' + name
        return 'record:' + str(row.get('record_key', ''))

    def _ordered_rows(self, rows):
        def assay_rank(row):
            sample_type = str(row.get('sample_type') or '').upper()
            return {'DNA': 2, 'RNA': 1, 'DNA+RNA': 0}.get(sample_type, 0)
        return sorted(rows, key=lambda r: (
            r.get('run_date') or r.get('updated_at') or '',
            assay_rank(r),
            r.get('patient_id') or ''), reverse=True)

    def _summarize_group(self, group_rows):
        all_rows = self._ordered_rows(group_rows)
        def unique(values):
            seen, items = set(), []
            for value in values:
                value = str(value or '').strip()
                if value and value not in seen:
                    seen.add(value)
                    items.append(value)
            return items
        pools = unique(r.get('pool_name') for r in all_rows)
        dna = unique(r.get('patient_id') for r in all_rows
                     if str(r.get('sample_type') or '').upper() in ('DNA', 'DNA+RNA') and not r.get('is_rerun'))
        rna = unique(r.get('patient_id') for r in all_rows
                     if str(r.get('sample_type') or '').upper() == 'RNA' and not r.get('is_rerun'))
        others = unique(r.get('patient_id') for r in all_rows
                        if r.get('is_rerun') or str(r.get('sample_type') or '').upper() not in ('DNA', 'RNA', 'DNA+RNA'))
        status = 'review' if any(r.get('analysis_status_key') == 'review' for r in all_rows) else (
            'incomplete' if any(r.get('analysis_status_key') == 'incomplete' for r in all_rows) else '')
        return {
            'group_key': self._patient_group_key(all_rows[0]),
            'all_rows': all_rows,
            'display_row': all_rows[0],
            'full_name': _shown(all_rows[0].get('full_name'), _shown(all_rows[0].get('patient_id'))),
            'pool_display': ', '.join(pools) if pools else '—',
            'dna_ids': ' / '.join(dna) if dna else '—',
            'rna_ids': ' / '.join(rna) if rna else '—',
            'other_ids': ' / '.join(others) if others else '—',
            'national_id': _shown(all_rows[0].get('national_id')),
            'latest_date': max((r.get('run_date') or r.get('updated_at') or '') for r in all_rows),
            'status_tag': status,
        }

    def _build_patient_groups(self, rows):
        all_by_key = {}
        for row in self.rows:
            key = self._patient_group_key(row)
            all_by_key.setdefault(key, []).append(row)
        grouped = {}
        for row in rows:
            key = self._patient_group_key(row)
            grouped.setdefault(key, []).append(row)
        result = []
        for key, matched_rows in grouped.items():
            all_rows = all_by_key.get(key, matched_rows)
            result.append(self._summarize_group(all_rows))
        result.sort(key=lambda item: (item['latest_date'], normalize_search(item['full_name'])), reverse=True)
        return result

    def _study_label(self, row):
        label = _shown(row.get('sample_type'), 'Çalışma') + ' · ' + _shown(row.get('patient_id'))
        if row.get('pool_name'):
            label += ' · ' + str(row['pool_name'])
        if row.get('run_date'):
            label += ' · ' + str(row['run_date'])[:10]
        if row.get('is_rerun'):
            label += ' · TEKRAR'
        return label

    def _render_page(self):
        self._filling = True
        self.tree.delete(*self.tree.get_children())
        self._group_map = {}
        preferred = None
        groups = self.filtered_groups[self._page * self.PAGE_SIZE:(self._page + 1) * self.PAGE_SIZE]
        for idx, group in enumerate(groups):
            iid = 'group_' + str(idx)
            self._group_map[iid] = group
            tags = []
            if idx % 2:
                tags.append('odd')
            if group.get('status_tag'):
                tags.append(group['status_tag'])
            self.tree.insert('', 'end', iid=iid, text=group['full_name'],
                             values=(group['pool_display'], group['dna_ids'], group['rna_ids'],
                                     group['other_ids'], group['national_id']),
                             tags=tuple(tags))
            rows = group.get('all_rows') or []
            if any(r.get('record_key') == self._selected_key for r in rows) or group.get('group_key') == self._selected_group_key:
                preferred = iid
        self._filling = False
        if self.filtered_groups:
            preferred = preferred or 'group_0'
            self.tree.selection_set(preferred)
            self.tree.focus(preferred)
            self.tree.see(preferred)
            self._show_group(self._group_map[preferred])
        else:
            self._selected_key = ''
            self._selected_group_key = ''
            self._clear_detail()
        pool_count = len({r.get('pool_name') for r in self.filtered if r.get('pool_name')})
        patient_count = len(self.filtered_groups)
        self.page_var.set(f'{pool_count} POOL · {patient_count} hasta · {len(self.filtered)} teknik kayıt')
        total_pages = max(1, math.ceil(len(self.filtered_groups) / self.PAGE_SIZE))
        self.previous_button.configure(state='normal' if self._page > 0 and total_pages > 1 else 'disabled')
        self.next_button.configure(state='normal' if self._page < total_pages - 1 else 'disabled')

    def _show_group(self, group, preferred_type=None):
        self.current_group = group
        self._selected_group_key = group.get('group_key', '')
        rows = group.get('all_rows') or []
        self._study_rows = rows
        selected = next((r for r in rows if preferred_type and preferred_type in str(r.get('sample_type','')).upper()), None)
        if selected is None and self.current_row:
            selected = next((r for r in rows if r.get('record_key') == self.current_row.get('record_key')), None)
        selected = selected or (rows[0] if rows else None)
        if selected:
            self._show_row(selected)
        else:
            self._clear_detail()

    def _on_study_change(self, _event=None):
        # Kept for compatibility with existing integrations. All studies are visible below.
        self._render_complete_patient()

    def _jump_to_assay(self, assay):
        for row in self._study_rows:
            if assay in str(row.get('sample_type') or '').upper():
                self.current_row = row
                self._selected_key = row['record_key']
                mark = self._study_positions.get(row['record_key'])
                if mark:
                    self.full_report_text.see(mark)
                self.status_var.set(assay + ' raporuna geçildi: ' + _shown(row.get('patient_id')))
                return

    def _on_select(self, _event=None):
        if self._filling:
            return
        selection = self.tree.selection()
        group = self._group_map.get(selection[0]) if selection else None
        if group:
            self._show_group(group)

    def _on_table_click(self, event):
        iid = self.tree.identify_row(event.y)
        if iid not in self._group_map:
            return
        group = self._group_map[iid]
        col = self.tree.identify_column(event.x)
        if col == '#2':
            assay = 'DNA'
        elif col == '#3':
            assay = 'RNA'
        else:
            assay = None
        self.tree.selection_set(iid)
        self._show_group(group, preferred_type=assay)
        if assay:
            self._jump_to_assay(assay)

    def _double_click(self, event):
        iid = self.tree.identify_row(event.y)
        if iid in self._group_map:
            self.tree.selection_set(iid)
            self._show_group(self._group_map[iid])

    def _show_row(self, row):
        self.current_row = row
        self._selected_key = row['record_key']
        self._render_complete_patient()
        for button in self.action_buttons:
            button.configure(state='normal')
        self.action_buttons[-1].configure(state='normal' if row.get('comment') else 'disabled')

    def _render_complete_patient(self):
        group = self.current_group
        if not group:
            return
        rows = group.get('all_rows') or []
        if not rows:
            return
        patient = self.current_row or rows[0]
        self.record_var.set(_shown(group.get('full_name')))
        self.patient_var.set(_shown(group.get('pool_display')) + ' · ' + str(len(rows)) + ' teknik çalışma')
        self.identity_var.set('  ·  '.join([
            'Yaş: ' + _shown(patient.get('age')),
            'Cinsiyet: ' + _shown(patient.get('sex')),
            'T.C.: ' + _shown(patient.get('national_id'), 'Enlil’den tamamlanacak'),
        ]))
        dna_reported = sum(len(r.get('verified_selection', {}).get('selected') or []) for r in rows)
        rna_reported = sum(len(r.get('verified_fusion_selection', {}).get('selected') or []) for r in rows)
        self.overview_var.set('DNA MP: ' + _shown(group.get('dna_ids')) + '     RNA MP: ' +
                              _shown(group.get('rna_ids')) + '     Ek/tekrar: ' +
                              _shown(group.get('other_ids')) + '\n' +
                              'GenNext güncel Selection: DNA ' + str(dna_reported) + ' · RNA füzyon ' + str(rna_reported) +
                              '     (doğrulanmamış kayıtlar sayılamaz)\n' +
                              'Manuel kapatılmış: ' + str(sum(r.get('manual_closure_status_key') in CLOSED_STATUSES for r in rows)) +
                              '/' + str(len(rows)) + '     İmzacı: ' + _shown(patient.get('signer')) +
                              '     Blok: ' + _shown(patient.get('block_id')))
        self.dna_jump.configure(state='normal' if any('DNA' in str(r.get('sample_type')).upper() for r in rows) else 'disabled')
        self.rna_jump.configure(state='normal' if any('RNA' in str(r.get('sample_type')).upper() for r in rows) else 'disabled')
        t = self.full_report_text
        t.configure(state='normal')
        t.delete('1.0','end')
        self._study_positions = {}
        self._study_tag_serial = 0
        # Every report has its own diagnosis, sample and biomarker status. Never merge reports.
        for i, record in enumerate(rows, 1):
            rk = record['record_key']
            position = t.index('end-1c')
            self._study_positions[rk] = position
            label = (str(i) + '. ' + _shown(record.get('sample_type')) + ' · MP ' +
                     _shown(record.get('patient_id')) + ' · ' + _shown(record.get('pool_name')))
            if record.get('is_rerun'):
                label += ' · TEKRAR'
            t.insert('end', label + '\n', 'title')
            t.insert('end', 'TANI: ' + _shown(record.get('diagnosis'), 'Tanı kaydı yok') + '\n', 'diagnosis')
            t.insert('end', 'Blok: ' + _shown(record.get('block_id')) +
                     '  ·  Tümör: %' + _shown(record.get('tumor_percentage')) +
                     '  ·  İmzacı: ' + _shown(record.get('signer')) + '\n')
            if 'DNA' in str(record.get('sample_type')).upper():
                t.insert('end', 'TMB: ' + _shown(record.get('tmb'), 'Veri yok') +
                         '  |  MSI: ' + _shown(record.get('msi'), 'Veri yok') +
                         '  |  HRD: ' + _shown(record.get('hrd'), 'Veri yok') + '\n')
            t.insert('end', 'GenNext Analyzed: ' + _shown(record.get('gennext_analyzed_label')) + '\n', 'history')
            t.insert('end', 'Enlil raporu: ' + _shown(record.get('report_status_label')) +
                     ' · Selection doğrulama: ' + _shown(record.get('analysis_status_label')) + '\n')
            t.insert('end', _shown(record.get('current_comment_status')) + '\n', 'uncertain')
            closing = record.get('manual_closure_status_key')
            if closing and closing != 'none':
                t.insert('end', 'MANUEL RAPOR KAPANIŞI: ' +
                         _shown(record.get('manual_closure_status_label')) + '\n', 'history')
                t.insert('end', 'Kaydeden: ' + _shown(record.get('manual_closure_by')) +
                         ' · Tarih: ' + _shown(record.get('manual_closure_at')) + '\n')
                t.insert('end', 'Gerekçe: ' + _shown(record.get('manual_closure_note')) + '\n')
                t.insert('end', 'Bu manuel durum GenNext Analyzed veya Enlil e-imza doğrulaması değildir.\n', 'uncertain')
                for event in (record.get('manual_closure_history') or [])[1:4]:
                    previous = MANUAL_CLOSURE_LABELS.get(event.get('previous_status'), 'İlk kayıt')
                    t.insert('end', '  Önceki işlem: ' + previous + ' → ' +
                             MANUAL_CLOSURE_LABELS.get(event.get('new_status'), 'Bilinmiyor') +
                             ' · ' + _shown(event.get('changed_at')) + '\n', 'uncertain')
            else:
                t.insert('end', 'MANUEL RAPOR KAPANIŞI: Kayıt yok (otomatik negatif sayılmaz).\n', 'uncertain')
            selection = record.get('verified_selection') or {}
            status = selection.get('status')
            if status == 'selected_positive':
                variants = selection.get('selected') or []
                t.insert('end', 'GENNEXT SELECTION · ANALYZED · ' + str(len(variants)) +
                         ' seçili SNV/indel · ' + _shown(selection.get('checked_at')) + '\n', 'history')
                for v in variants:
                    change = v.get('protein') or v.get('cdna') or '[HGVS belirtilmemiş]'
                    detail = (' · VAF ' + v['vaf']) if v.get('vaf') else ''
                    t.insert('end', '  • ' + _shown(v.get('gene')) + ' ' + change + detail + '\n', 'mutations')
                t.insert('end', 'Kaynak: GenNext Selection; imzalı rapor ile aynı değildir.\n\n', 'uncertain')
            elif status == 'no_snv_selection':
                t.insert('end', 'GENNEXT SELECTION · Analyzed; doğrulanmış boş SNV/indel seçimi\n\n', 'uncertain')
            elif status and 'DNA' in str(record.get('sample_type') or '').upper():
                t.insert('end', 'GENNEXT DNA SELECTION · Doğrulanmış güncel sonuç yok (' +
                         status + '); negatif kabul edilmez.\n\n', 'uncertain')
            fusion = record.get('verified_fusion_selection') or {}
            fusion_status = fusion.get('status')
            if fusion_status == 'fusion_selected_positive':
                fusions = fusion.get('selected') or []
                t.insert('end', 'GENNEXT RNA FUSION SELECTION · ANALYZED · ' + str(len(fusions)) +
                         ' seçilmiş füzyon · ' + _shown(fusion.get('checked_at')) + '\n', 'history')
                for item in fusions:
                    extra = (' · Caller: ' + item['callers']) if item.get('callers') else ''
                    t.insert('end', '  • ' + _shown(item.get('fusion')) + extra + '\n', 'mutations')
                t.insert('end', 'Kaynak: GenNext RNA Fusion Selection; imzalı rapor ile aynı değildir.\n\n', 'uncertain')
            elif fusion_status == 'no_fusion_selection':
                t.insert('end', 'GENNEXT RNA FUSION · Analyzed açık; doğrulanmış seçim 0 '
                         '(RNA panelinin tamamı negatif kabul edilmez).\n\n', 'uncertain')
            elif fusion_status and 'RNA' in str(record.get('sample_type') or '').upper():
                t.insert('end', 'GENNEXT RNA FUSION · Güncel seçim doğrulanmadı (' +
                         fusion_status + '); negatif kabul edilmez.\n\n', 'uncertain')
            report_lines = record.get('report_mutation_lines') or []
            if report_lines:
                t.insert('end', 'ENLİL RAPOR CÜMLELERİ · KAYDEDİLMİŞ ÖZGÜN METİN\n', 'history')
                for line in report_lines:
                    t.insert('end', '  • ' + line + '\n', 'mutations')
                t.insert('end', '\n')
            comment = str(record.get('comment') or '').strip()
            t.insert('end', 'RAPORLAMA / YORUM METNİ (ORİJİNAL)\n', 'history')
            t.insert('end', (comment or 'Bu teknik çalışmanın kayıtlı mutasyon / yorum metni yok.') + '\n\n', 'mutations')
            reports = record.get('history') or []
            if reports:
                t.insert('end', 'BU ÇALIŞMANIN ÖNCEKİ RAPOR SÜRÜMLERİ\n', 'history')
                for doc in reports:
                    clinical = doc.get('clinical') or {}
                    t.insert('end', _shown(doc.get('created_at')) + ' · ' + _shown(doc.get('status')) + '\n')
                    old_diagnosis = clinical.get('tumor_diagnosis') or clinical.get('diagnosis')
                    t.insert('end', 'Önceki tanı: ' + _shown(old_diagnosis, 'Önceki tanı kayıtlı değil') + '\n')
                    t.insert('end', _shown(doc.get('text'), 'Rapor metni kayıtlı değil') + '\n\n')
            t.insert('end', '────────────────────────────────────────────────\n\n')
        seen=set()
        prior=[]
        for record in rows:
            for doc in record.get('related_history') or []:
                marker = doc.get('id') or (doc.get('title'), doc.get('created_at'))
                if marker not in seen:
                    seen.add(marker)
                    prior.append(doc)
        if prior:
            t.insert('end', 'ENLİL’DEN AKTARILMIŞ DAHA ESKİ RAPORLAR · TEKNİK EŞLEŞME DOĞRULANMAMIŞ\n', 'history')
            for doc in prior:
                clinical = doc.get('clinical') or {}
                t.insert('end', _shown(doc.get('title')) + ' · ' + _shown(doc.get('created_at')) + '\n')
                t.insert('end', 'ESKİ TANI: ' + _shown(doc.get('tumor_diagnosis') or clinical.get('tumor_diagnosis')) + '\n', 'diagnosis')
                t.insert('end', _shown(doc.get('text'), 'Eski rapor metni yok') + '\n\n')
        if not prior and not any(r.get('history') for r in rows):
            t.insert('end', 'Kayıtlı önceki rapor bulunamadı. T.C. eksikse Enlil’den tek tek tamamlanabilir.\n', 'uncertain')
        t.configure(state='disabled')
        t.yview_moveto(0)

    def edit_manual_closure(self):
        """Explicit, auditable per-assay disposition. Never writes to GenNext."""
        if not self.current_group or not self._study_rows:
            return
        rows = list(self._study_rows)
        win = tk.Toplevel(self)
        win.title('Manuel rapor kapatma / yeniden açma')
        win.geometry('770x585')
        win.minsize(670, 510)
        win.transient(self.winfo_toplevel())
        main = ttk.Frame(win, padding=14)
        main.pack(fill='both', expand=True)
        ttk.Label(main, text='Manuel rapor kapatma / yeniden açma',
                  font=('Segoe UI', 15, 'bold')).pack(anchor='w')
        ttk.Label(main, text=('Bu kayıt yalnız SEÇİLEN teknik çalışmaya aittir. '
                   'GenNext Analyzed/Selection değiştirilmez; Enlil e-imzası doğrulanmaz.'),
                  foreground='#9b6426', wraplength=700, justify='left').pack(anchor='w', pady=(5, 12))
        options = []
        for item in rows:
            s = str(item.get('sample_uuid') or '')
            options.append(_shown(item.get('sample_type')) + ' · ' +
                           _shown(item.get('patient_id')) + ' · ' +
                           _shown(item.get('pool_name')) + ' [' + s[:8] + ']')
        study_var = tk.StringVar()
        ttk.Label(main, text='TEKNİK ÇALIŞMA', font=('Segoe UI', 9, 'bold')).pack(anchor='w')
        study_combo = ttk.Combobox(main, state='readonly', textvariable=study_var,
                                   values=options, width=72)
        study_combo.pack(fill='x', pady=(3, 8))
        current_idx = next((i for i, x in enumerate(rows)
                            if self.current_row and x['record_key'] == self.current_row['record_key']), 0)
        study_combo.current(current_idx)
        previous = tk.StringVar()
        ttk.Label(main, textvariable=previous, foreground='#33556d',
                  wraplength=710, justify='left').pack(anchor='w', pady=(0, 8))
        ttk.Label(main, text='YENİ DURUM', font=('Segoe UI', 9, 'bold')).pack(anchor='w')
        outcome_var = tk.StringVar(value='Durum seçiniz')
        outcome_combo = ttk.Combobox(main, textvariable=outcome_var, state='readonly',
                                     values=list(CLOSURE_OPTIONS), width=55)
        outcome_combo.pack(fill='x', pady=(3, 8))
        ttk.Label(main, text='KAYDEDEN (ad / rumuz)', font=('Segoe UI', 9, 'bold')).pack(anchor='w')
        operator_var = tk.StringVar()
        ttk.Entry(main, textvariable=operator_var).pack(fill='x', pady=(3, 8))
        ttk.Label(main, text='GEREKÇE / KONTROL NOTU (en az 10 karakter)',
                  font=('Segoe UI', 9, 'bold')).pack(anchor='w')
        note = tk.Text(main, height=5, wrap='word', font=('Segoe UI', 10))
        note.pack(fill='both', expand=True, pady=(3, 8))
        confirm_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(main, variable=confirm_var, text=(
            'Bu sonucun gerçek rapor/işlem kaydını kontrol ettim. '
            'İmza doğrulaması yapılmadığını biliyorum.')).pack(anchor='w', pady=(1, 10))
        ttk.Label(main, text=('NEGATİF: doğrulanmış rapor kararıdır; boş Selection’dan türetilmez. '
                   'İPTAL / YETERSİZ: negatif değildir. Yeniden açma geçmişi silmez.'),
                  foreground='#9b6426', wraplength=710).pack(anchor='w', pady=(0, 10))

        def selected_row():
            idx = study_combo.current()
            if idx < 0 and study_var.get() in options:
                idx = options.index(study_var.get())
            return rows[idx] if 0 <= idx < len(rows) else None

        def show_previous(_event=None):
            item = selected_row()
            if not item:
                return
            old = item.get('manual_closure_status_key', 'none')
            old_label = _shown(item.get('manual_closure_status_label'), 'Kapanış kaydı yok')
            dna = item.get('verified_selection') or {}
            rna = item.get('verified_fusion_selection') or {}
            count_dna = len(dna.get('selected') or [])
            count_rna = len(rna.get('selected') or [])
            previous.set('Önceki durum: ' + old_label + '\nGenNext: ' +
                         _shown(item.get('gennext_analyzed_label')) +
                         f' · Seçilmiş DNA: {count_dna} · RNA: {count_rna}' +
                         ('\nÖnceki not: ' + _shown(item.get('manual_closure_note')) if old != 'none' else ''))
            outcome_var.set('Durum seçiniz')
            note.delete('1.0', 'end')
            confirm_var.set(False)

        study_combo.bind('<<ComboboxSelected>>', show_previous)
        show_previous()

        def save():
            item = selected_row()
            if not item:
                messagebox.showwarning('Manuel kapanış', 'Teknik çalışma seçin.', parent=win)
                return
            status = CLOSURE_OPTIONS.get(outcome_var.get())
            if not status:
                messagebox.showwarning('Manuel kapanış', 'Yeni rapor durumunu seçin.', parent=win)
                return
            if not confirm_var.get():
                messagebox.showwarning('Manuel kapanış', 'Doğrulama kutusunu işaretleyin.', parent=win)
                return
            reason = note.get('1.0', 'end-1c').strip()
            author = operator_var.get().strip()
            conflict_ack = False
            if status == 'reported_negative':
                dna = item.get('verified_selection') or {}
                rna = item.get('verified_fusion_selection') or {}
                positive = dna.get('status') == 'selected_positive' or rna.get('status') == 'fusion_selected_positive'
                if positive:
                    conflict_ack = messagebox.askyesno(
                        'SELECTION İLE ÇELİŞKİ',
                        'GenNext Selection içinde seçilmiş bir varyant/füzyon var. '
                        'Buna rağmen NEGATİF rapor verildiğini bağımsız olarak doğruladınız mı? '
                        'Lütfen notta ayrıntılı gerekçe belirtin.', parent=win)
                    if not conflict_ack:
                        return
            if not messagebox.askyesno('Manuel durum kaydı',
                    _shown(item.get('patient_id')) + ' · ' + _shown(item.get('sample_type')) +
                    '\n\nYeni durum: ' + outcome_var.get() +
                    '\n\nİşlemi bu teknik kayıt için kaydetmek istiyor musunuz?', parent=win):
                return
            try:
                event_id, changed = save_manual_closure(
                    self.db, item['record_key'], status, reason, author,
                    expected_identity={k: item.get(k) for k in
                        ('run_uuid', 'sample_uuid', 'sample_type', 'patient_id', 'pool_name')},
                    expected_event_id=item.get('manual_closure_event_id'),
                    selection_conflict_ack=conflict_ack)
            except Exception as exc:
                messagebox.showerror('Manuel kapanış kaydedilemedi', str(exc), parent=win)
                return
            win.destroy()
            self.refresh()
            self.status_var.set('Manuel rapor işlemi kaydedildi (e-imza doğrulanmadı).' if changed
                                else 'Bu teknik çalışma için kapanış durumu değişmedi.')

        footer = ttk.Frame(main)
        footer.pack(fill='x', pady=(4, 0))
        ttk.Button(footer, text='Kaydet ve kapat', command=save).pack(side='left')
        ttk.Button(footer, text='Vazgeç', command=win.destroy).pack(side='right')
        win.bind('<Escape>', lambda _e: win.destroy())
        win.after_idle(lambda: win.grab_set() if win.winfo_exists() else None)

    def _show_history(self, _event=None):
        index = self.history_box.current()
        if index < 0 or index >= len(self._history_entries):
            return
        item, related = self._history_entries[index]
        heading = ('VAKA GEÇMİŞİ — bu teknik çalışmanın güncel raporu değildir.\n' if related else
                   'KAYITLI RAPOR GEÇMİŞİ — kaydedildiği tarihteki metin.\n')
        meta = [f'{_shown(item.get("title"), "Rapor")} · {_shown(item.get("created_at"))}',
                'Durum: ' + _shown(item.get('status')), 'Kaynak: ' + _shown(item.get('source'))]
        if item.get('signer'):
            meta.append('Kaydedilmiş imzacı: ' + str(item['signer']))
        clinical = item.get('clinical') or {}
        if clinical.get('tumor_diagnosis'):
            meta.append('Geçmiş raporun tanısı: ' + str(clinical['tumor_diagnosis']))
        if clinical.get('block_no'):
            meta.append('Geçmiş raporun bloğu: ' + str(clinical['block_no']))
        self._set_text(self.history_text, heading + '\n'.join(meta) + '\n\n' +
                       _shown(item.get('text'), 'Bu geçmiş kaydında metin bulunmuyor.'))

    def _clear_detail(self):
        self.current_row = None
        self.current_group = None
        self._study_rows = []
        self._study_positions = {}
        self.record_var.set('Listeden bir hasta seçin')
        self.patient_var.set('')
        self.identity_var.set('Hasta seçin')
        self.overview_var.set('')
        self.dna_jump.configure(state='disabled')
        self.rna_jump.configure(state='disabled')
        self._set_text(self.full_report_text, 'Rapor Arşivi yüklenince bir hasta seçin.')
        for button in self.action_buttons:
            button.configure(state='disabled')

    def _open(self, destination):
        if not self.current_row:
            return
        callback = {'report': self.open_report_cb, 'metadata': self.open_metadata_cb,
                    'dual': self.open_dual_cb}[destination]
        if callback:
            try:
                result = callback(self.current_row['record_key'])
                if result is False:
                    self.status_var.set('Seçilen çalışma açılamadı. Açık düzenlemeleri veya devam eden Enlil sorgusunu kontrol edin.')
            except Exception as exc:
                self.status_var.set('Çalışma açılamadı: ' + str(exc))

    def _copy(self, text):
        if not text:
            return
        self.clipboard_clear()
        self.clipboard_append(text)
        self.status_var.set('Seçilen çalışmanın metni panoya kopyalandı.')

    def copy_summary(self):
        if self.current_row:
            self._copy(format_archive_summary(self.current_row))

    def copy_comment(self):
        if self.current_row:
            self._copy(self.current_row.get('comment', ''))

    def expand_comment(self):
        if not self.current_row:
            return
        row = copy.deepcopy(self.current_row)
        window = tk.Toplevel(self)
        window.title('Mutasyon / Yorum — ' + _shown(row.get('patient_id')) + ' · ' + _shown(row.get('pool_name')))
        width = min(1100, self.winfo_screenwidth()-80)
        height = min(720, self.winfo_screenheight()-100)
        window.geometry(f'{width}x{height}')
        window.minsize(650, 420)
        window.transient(self.winfo_toplevel())
        head = ttk.Frame(window, padding=12)
        head.pack(fill='x')
        ttk.Label(head, text='  ·  '.join(_shown(row.get(k)) for k in ('patient_id', 'pool_name', 'sample_type')),
                  font=('Segoe UI', 14, 'bold'), foreground='#153e56').pack(anchor='w')
        diagnosis = ttk.Label(head, text='Tümör tanısı: ' + _shown(row.get('diagnosis')),
                              font=('Segoe UI', 11, 'bold'), wraplength=width-40)
        diagnosis.pack(anchor='w', pady=(6, 4))
        meta = ttk.Label(head, text='Blok: ' + _shown(row.get('block_id')) + '  ·  Doktor: ' + _shown(row.get('signer')) +
                          '\n' + _shown(row.get('current_comment_status')), wraplength=width-40)
        meta.pack(anchor='w')
        window.bind('<Configure>', lambda e: [label.configure(wraplength=max(400, e.width-40))
                    for label in (diagnosis, meta)] if e.widget is window else None, add='+')
        bottom = ttk.Frame(window, padding=10)
        bottom.pack(side='bottom', fill='x')
        ttk.Button(bottom, text='Özeti kopyala', command=lambda: self._copy(format_archive_summary(row))).pack(side='left')
        ttk.Button(bottom, text='Kapat', command=window.destroy).pack(side='right')
        text = self._text_widget(window, font=('Segoe UI', 12))
        self._set_text(text, row.get('comment') or 'Bu teknik çalışmaya ait mutasyon / yorum kaydı yok.')
        window.bind('<Escape>', lambda _e: window.destroy())

    def _on_destroy(self, event):
        if event.widget is not self:
            return
        self._alive = False
        for timer in (self._filter_after, self._poll_after):
            if timer:
                try:
                    self.after_cancel(timer)
                except tk.TclError:
                    pass
