"""Patient/report archive and research workspace (offline Tk interface)."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import queue
import threading
import tkinter as tk
from tkinter import ttk, messagebox, filedialog, simpledialog
from pathlib import Path

from archive_store import (ArchiveStore, canonical_key, date_only, norm, number,
                           filter_records, select_variants, variant_catalogue,
                           research_summary, export_study_zip)
from variant_links import oncokb_url, franklin_url, open_url, search_text

DATASETS = {'Raporlanan varyantlar':'reported', 'Onkojenik indeksi (bulunan)':'observed', 'Manuel artefaktlar':'artifacts', 'GenNext rapor dışı adaylar':'gennext_candidates'}
REPORT_FILTERS = {'T\u00fcm hastalar':'all', 'Raporlanan varyant\u0131 var':'reported', 'Raporlanan kayd\u0131 yok':'without_reported', 'Rapor / vaka metni var':'with_text', 'Onayl\u0131 rapor metni var':'confirmed_text'}


def join_values(values):
    return ', '.join(str(x) for x in values if x is not None and str(x)) or '\u2014'


def af_text(value):
    n=number(value)
    return '' if n is None else ('%.2f' % (n*100)).replace('.', ',')+'%'


def shown_date(value):
    iso=date_only(value)
    return dt.date.fromisoformat(iso).strftime('%d.%m.%Y') if iso else '\u2014'


class DataTable(ttk.Frame):
    """Scrollable table; map rows to domain data and preserve selection on sort."""
    def __init__(self, parent, columns, height=8, select=None, double=None):
        super().__init__(parent)
        self.columns=columns;self.data={};self.selection_callback=select
        self.tree=ttk.Treeview(self,columns=[x[0] for x in columns],show='headings',height=height,selectmode='browse')
        for key,title,width in columns:
            self.tree.heading(key,text=title,anchor='w',command=lambda k=key:self.sort(k,False))
            self.tree.column(key,width=width,minwidth=min(width,65),stretch=False,anchor='w')
        if columns:self.tree.column(columns[0][0],stretch=True)
        ys=ttk.Scrollbar(self,orient='vertical',command=self.tree.yview)
        xs=ttk.Scrollbar(self,orient='horizontal',command=self.tree.xview)
        self.tree.configure(yscrollcommand=ys.set,xscrollcommand=xs.set)
        self.tree.grid(row=0,column=0,sticky='nsew');ys.grid(row=0,column=1,sticky='ns');xs.grid(row=1,column=0,sticky='ew')
        self.rowconfigure(0,weight=1);self.columnconfigure(0,weight=1)
        self.tree.tag_configure('odd',background='#f2f6fa')
        self.tree.tag_configure('warning',background='#fff0d5')
        self.tree.bind('<<TreeviewSelect>>',lambda e:select(self.selected()) if select and self.selected() else None)
        if double:self.tree.bind('<Double-1>',lambda e:double(self.selected()) if self.selected() else None)
        self.tree.bind('<Control-c>',self.copy_row)

    def fill(self, rows, values, preferred=None, identity=None, select_first=False):
        self.tree.delete(*self.tree.get_children());self.data={}
        target=None
        for i,r in enumerate(rows):
            iid='row'+str(i);self.data[iid]=r
            tags=('warning',) if r.get('decision_conflict') or r.get('identity_conflict') else ('odd',) if i%2 else ()
            self.tree.insert('', 'end',iid=iid,values=values(r),tags=tags)
            if identity and preferred is not None and identity(r)==preferred:target=iid
        if not target and select_first and rows:target='row0'
        if target:self.tree.selection_set(target);self.tree.focus(target);self.tree.see(target)

    def selected(self):
        selected=self.tree.selection()
        return self.data.get(selected[0]) if selected else None

    def sort(self,key,reverse):
        def sortval(iid):
            v=self.tree.set(iid,key);n=number(str(v).replace('%',''))
            return (0,n) if n is not None else (1,norm(v))
        for i,iid in enumerate(sorted(self.tree.get_children(),key=sortval,reverse=reverse)):
            self.tree.move(iid,'',i)
        self.tree.heading(key,command=lambda:self.sort(key,not reverse))

    def copy_row(self,event=None):
        sel=self.tree.selection()
        if sel:
            text='\t'.join(str(x) for x in self.tree.item(sel[0],'values'))
            self.clipboard_clear();self.clipboard_append(text)
        return 'break'


class ArchiveWorkspace(ttk.Frame):
    def __init__(self, parent, base, db, cfg, open_metadata=None, open_mutations=None, open_review=None, reindex=None):
        super().__init__(parent,padding=10)
        self.base=Path(base);self.db=db;self.cfg=cfg
        self.store=ArchiveStore(self.base,db)
        self.open_metadata_cb=open_metadata;self.open_mutations_cb=open_mutations
        self.open_review_cb=open_review;self.reindex_cb=reindex
        self.snap=None;self.events=queue.Queue();self.loading=False;self.dirty=True
        self.current_entity=None;self.current_record=None;self.doc_map={}
        self.catalogue=[];self.catalogue_page=0;self.page_size=150
        self.active_study=None;self.cohort=[];self.cohort_variants=[];self.applied_criteria={};self.applied_dataset='reported'
        self.refresh_pending=False
        self._build()
        self.after(120,self._poll)

    def _build(self):
        head=ttk.Frame(self);head.pack(fill='x',pady=(0,7))
        ttk.Label(head,text='Hasta sonu\u00e7lar\u0131 ve ara\u015ft\u0131rma ar\u015fivi',font=('Segoe UI',15,'bold')).pack(side='left')
        ttk.Button(head,text='Ar\u015fivi yenile',command=self.refresh).pack(side='right')
        self.status=tk.StringVar(value='Ar\u015fiv, bu b\u00f6l\u00fcm a\u00e7\u0131l\u0131nca yerel veritaban\u0131ndan okunur. Taray\u0131c\u0131 / login gerekmez.')
        ttk.Label(self,textvariable=self.status,foreground='#52606d').pack(fill='x',pady=(0,6))
        self.nb=ttk.Notebook(self);self.nb.pack(fill='both',expand=True)
        self.patients_tab=ttk.Frame(self.nb,padding=8)
        self.variants_tab=ttk.Frame(self.nb,padding=8)
        self.research_tab=ttk.Frame(self.nb,padding=8)
        self.gennext_tab=ttk.Frame(self.nb,padding=8)
        self.nb.add(self.patients_tab,text='  Hasta / Raporlar  ')
        self.nb.add(self.variants_tab,text='  T\u00fcm Raporlanan Varyantlar / Tarih\u00e7e  ')
        self.nb.add(self.research_tab,text='  Ara\u015ft\u0131rma / \u00c7al\u0131\u015fmalar  ')
        self.nb.add(self.gennext_tab,text='  GenNext Analiz / Selection  ')
        self._build_patients();self._build_variants();self._build_research();self._build_gennext()

    @staticmethod
    def _gennext_label(record):
        is_rna=(record.get('sample_type') or '').upper()=='RNA'
        state=(record.get('gennext_fusion_state') if is_rna else record.get('gennext_selection_state')) or 'awaiting_selection'
        return {
            'selected_positive':'Analyzed - secili pozitif',
            'no_snv_selection':'Analyzed - SNV 0 secim',
            'analysis_incomplete':'Analiz bitmemis',
            'analysis_incomplete_unread':'Analyzed kapali / secim bekliyor',
            'run_incomplete':'RUN bitmedi / bekliyor',
            'run_unknown':'RUN durumu belirsiz / bekliyor',
            'run_failed':'RUN hatali / tekrar kontrol',
            'identity_review':'Birlesik MP / kimlik kontrolu - kesin degil',
            'needs_selection_refresh':'Yeniden dogrula',
            'awaiting_selection':'Taranmadi / kontrol',
            'fusion_selected_positive':'RNA Fusion - secili pozitif',
            'no_fusion_selection':'RNA Fusion 0 secim (panel negatif degil)',
            'fusion_analysis_incomplete':'RNA Analyzed kapali',
            'fusion_analysis_incomplete_unread':'RNA analiz bitmemis',
            'fusion_identity_review':'RNA Fusion / kimlik kontrolu',
            'needs_fusion_refresh':'RNA Fusion yeniden dogrula',
            'awaiting_fusion_selection':'RNA Fusion taranmadi',
        }.get(state,state)

    def _build_gennext(self):
        intro=('GenNext Clinical SNVs ve RNA Fusion Genes > Fusions > Selection. Analyzed durumu ve isaretli rapor secimi '
               'ayri dogrulanir. Secilmemis adaylar raporlanmis degildir. '
               'Bu gorunum salt okunur; Enlil rapor metnini degistirmez. '
               '0 secim yalniz kendi SNV/indel veya Fusion kapsamindadir; tum panel negatifligi degildir.')
        ttk.Label(self.gennext_tab,text=intro,wraplength=1340,foreground='#7c4800').pack(fill='x',pady=(0,8))
        bar=ttk.Frame(self.gennext_tab);bar.pack(fill='x',pady=5)
        ttk.Label(bar,text='MP / POOL / gen:').pack(side='left')
        self.gennext_query=tk.StringVar()
        en=ttk.Entry(bar,textvariable=self.gennext_query,width=36);en.pack(side='left',padx=6)
        en.bind('<Return>',lambda _:self._populate_gennext())
        self.gennext_filter=tk.StringVar(value='Tumu')
        cb=ttk.Combobox(bar,textvariable=self.gennext_filter,state='readonly',
                        values=['Tumu','Secili pozitif','0 SNV secim','RNA secili fusion','RNA Fusion 0 secim','Analiz bitmemis','Kontrol / taranmadi'],width=22)
        cb.pack(side='left',padx=5)
        cb.bind('<<ComboboxSelected>>',lambda _:self._populate_gennext())
        ttk.Button(bar,text='Filtrele',command=self._populate_gennext).pack(side='left',padx=5)
        ttk.Button(bar,text='Arsivi yenile',command=self.refresh).pack(side='left')
        self.gennext_overview=tk.StringVar(value='Henuz tarama verisi bulunmuyor.')
        ttk.Label(self.gennext_tab,textvariable=self.gennext_overview,foreground='#475569').pack(fill='x',pady=4)
        self.gennext_table=DataTable(self.gennext_tab,
          [('mp','MP',120),('pool','POOL',110),('status','ANALIZ / SELECTION',245),
           ('selected','SECILI',75),('candidates','ADAY',75),('checked','SON OKUMA',205),
           ('error','KONTROL / HATA',300)],height=13,select=self._show_gennext_row)
        self.gennext_table.pack(fill='both',expand=True,pady=(0,8))
        ttk.Label(self.gennext_tab,text='Secilen teknik kaydin gecmisi ve adaylari (salt okunur):').pack(anchor='w')
        self.gennext_details=tk.Text(self.gennext_tab,wrap='word',height=12,
                                     font=('Consolas',10),state='disabled')
        self.gennext_details.pack(fill='both',expand=True,pady=6)

    def _populate_gennext(self):
        if not self.snap:return
        q=norm(self.gennext_query.get())
        scope=self.gennext_filter.get()
        rows=[]
        for rk,s in self.snap['records'].items():
            is_rna=(s.get('sample_type') or '').upper()=='RNA'
            status=(s.get('gennext_fusion_state') if is_rna else s.get('gennext_selection_state')) or 'awaiting_selection'

            if scope=='Secili pozitif' and status not in ('selected_positive','fusion_selected_positive'):continue
            if scope=='0 SNV secim' and status!='no_snv_selection':continue
            if scope=='RNA secili fusion' and status!='fusion_selected_positive':continue
            if scope=='RNA Fusion 0 secim' and status!='no_fusion_selection':continue
            if scope=='Analiz bitmemis' and status not in ('analysis_incomplete','analysis_incomplete_unread','fusion_analysis_incomplete','fusion_analysis_incomplete_unread','run_incomplete','run_unknown'):continue
            if scope=='Kontrol / taranmadi' and status not in ('needs_selection_refresh','needs_fusion_refresh','awaiting_selection','awaiting_fusion_selection','run_failed','identity_review','fusion_identity_review'):continue
            if q and not any(q in norm(v) for v in (s.get('patient_id'),s.get('pool_name'),
                      s.get('full_name'),json.dumps(s.get('gennext_selection') or {},ensure_ascii=False),
                      json.dumps(s.get('gennext_fusion') or {},ensure_ascii=False))):continue
            rows.append(s)
        rows.sort(key=lambda s:(s.get('run_date') or '',s.get('patient_id') or ''),reverse=True)
        self.gennext_overview.set(f'{len(rows)} teknik kayit · DNA {len(self.snap.get("gennext_selection_history",[]))} / RNA Fusion {len(self.snap.get("gennext_fusion_history",[]))} degisiklik gecmisi')
        def values(s):
            rna=(s.get('sample_type') or '').upper()=='RNA'
            d=(s.get('gennext_fusion') if rna else s.get('gennext_selection')) or {}
            error=(s.get('gennext_fusion_error') if rna else s.get('gennext_selection_error')) or ''
            return (s.get('patient_id') or '',s.get('pool_name') or '',self._gennext_label(s),
                    d.get('included_count',''),d.get('candidate_count',''),
                    d.get('checked_at',''),error)
        self.gennext_table.fill(rows,values)
        self._set_text(self.gennext_details,'Ayrinti icin listeden bir teknik kayit secin.')

    def _show_gennext_row(self,s):
        if not s:return
        is_rna=(s.get('sample_type') or '').upper()=='RNA'
        d=(s.get('gennext_fusion') if is_rna else s.get('gennext_selection')) or {}
        rk=s.get('record_key')
        if is_rna:
            lines=[f"{s.get('patient_id')} | {s.get('pool_name')} | RNA",
                f"Durum: {self._gennext_label(s)}",
                f"RUN / SAMPLE: {s.get('run_status') or '?'} / {s.get('sample_status') or '?'}",
                f"Analyzed: {s.get('gennext_fusion_analyzed')}",
                f"Guncel RNA secimi dogrulandi: {d.get('currently_verified',False)}",
                f"RNA Fusion rapor secimi: {d.get('included_count','okunamadi')}",
                'Kaynak: GenNext Fusion Genes > Fusions > Selection; imzali Enlil raporu DEGILDIR.',
                '0 secim: rapora secilen fusion yok; RNA testi negatif demek DEGILDIR.',
                f"Son tarama: {d.get('checked_at') or '-'}",
                f"Son hata: {s.get('gennext_fusion_error') or '-'}",'',
                'RNA FUSION - RAPORA SECILENLER:']
            for v in d.get('selected') or []:
                lines.append(' - '+str(v.get('fusion') or '')+' | '+str(v.get('callers') or '')+
                   (' [GENNEXT SECILI]' if d.get('result_status')=='fusion_selected_positive'
                     else ' [ANALIZ/KIMLIK KONTROLU - KESIN DEGIL]'))
            if not d.get('selected'):lines.append(' (secili fusion yok / henuz okunmadi)')
            lines.append('\nFUSION RAPOR DISI ARASTIRMA ADAYLARI:')
            for v in d.get('candidates') or []:
                lines.append(' - '+str(v.get('fusion') or '')+' | '+str(v.get('callers') or '')+
                            ' [SELECTION ISARETLI DEGIL]')
            if not d.get('candidates'):lines.append(' (aday yok / henuz okunmadi)')
            lines.append('\nRNA FUSION DEGISIM GECMISI:')
            for h in reversed([h for h in self.snap.get('gennext_fusion_history',[])
                               if h.get('record_key')==rk][-20:]):
                lines.append(' - '+str(h.get('changed_at'))+' | Analyzed '+str(h.get('analyzed'))+
                             ' | secili '+str(h.get('included_count'))+' | aday '+str(h.get('candidate_count')))
            self._set_text(self.gennext_details,'\n'.join(lines))
            return
        lines=[f"{s.get('patient_id')} | {s.get('pool_name')} | {s.get('sample_type')}",
               f"Durum: {self._gennext_label(s)}",
               f"GenNext RUN: {s.get('run_status') or '?'} / SAMPLE: {s.get('sample_status') or '?'}",
               f"Guncel Analyzed: {s.get('gennext_analyzed')}",
               f"Selection dogrulandi: {d.get('currently_verified',False)}",
               f"SNV / Indel secim adedi: {d.get('included_count','okunamadi')}",
               'Kaynak: GenNext Clinical SNVs > Selection; imzali / manuel Enlil raporu degildir.',
               'Onemli: SNV secimi yok = tum DNA/RNA paneli negatif demek DEGILDIR.',
               f"Son tarama: {d.get('checked_at') or '-'}",f"Son hata: {s.get('gennext_selection_error') or '-'}",'']
        lines.append('SECILI / GENNEXT RAPOR SECMELERI:')
        for v in d.get('selected') or []:
            lines.append(' - '+str(v.get('gene') or '')+' '+str(v.get('protein') or '')+' '+str(v.get('cdna') or '')
                         +' '+str(v.get('genomic') or '')+' | VAF '+str(v.get('vaf') or '')
                         +(' [KESIN SELECTION]' if d.get('result_status')=='selected_positive' else ' [KIMLIK/ANALIZ KONTROLU - KESIN DEGIL]'))
        if not d.get('selected'):lines.append(' (secili varyant yok / henuz okunmadi)')
        lines.extend(['','RAPOR DISI / TARTISILAN ADAYLAR:'])
        for v in d.get('candidates') or []:
            lines.append(' - '+str(v.get('gene') or '')+' '+str(v.get('protein') or '')
                         +' '+str(v.get('genomic') or '')+' [RAPOR SECIMINDE ISARETLI DEGIL]')
        if not d.get('candidates'):lines.append(' (aday yok / henuz okunmadi)')
        history=[h for h in self.snap.get('gennext_selection_history',[]) if h.get('record_key')==rk]
        lines.extend(['','GENNEXT SELECTION DEGISIM GECMISI:'])
        for h in reversed(history[-20:]):
            lines.append(' - '+str(h.get('changed_at'))+' | Analyzed '+str(h.get('analyzed'))+
                         ' | secili '+str(h.get('included_count'))+' | aday '+str(h.get('candidate_count')))
        if (s.get('sample_type') or '').upper()=='DNA+RNA':
            fusion=s.get('gennext_fusion') or {}
            lines.append('\n=== DNA+RNA: AYRI RNA FUSION SELECTION ===')
            lines.append('RNA Fusion durum: '+str(s.get('gennext_fusion_state') or 'taranmadi'))
            lines.append('RNA Fusion secili: '+str(fusion.get('included_count','okunamadi')))
            lines.append('RNA Fusion aday: '+str(fusion.get('candidate_count','okunamadi')))
            lines.append('DNA SNV rapor secimi RNA Fusion icin kullanilmaz.')
            for v in fusion.get('selected') or []:
                lines.append(' - SECILI RNA Fusion: '+str(v.get('fusion') or ''))
            for v in fusion.get('candidates') or []:
                lines.append(' - SADECE ADAY RNA Fusion: '+str(v.get('fusion') or ''))
        self._set_text(self.gennext_details,'\n'.join(lines))

    def activate(self):
        if self.dirty or self.snap is None:self.refresh()

    def invalidate(self):
        self.dirty=True

    def refresh(self):
        if self.loading:
            self.refresh_pending=True;return
        self.loading=True;self.status.set('Ar\u015fiv okunuyor\u2026 Ham dosyalar yeniden indirilmiyor.')
        def run():
            try:self.events.put(('loaded',self.store.snapshot()))
            except Exception as e:self.events.put(('error',str(e)))
        threading.Thread(target=run,daemon=True,name='NGS-ArchiveReader').start()

    def _poll(self):
        try:
            while True:
                kind,data=self.events.get_nowait();self.loading=False
                if kind=='error':
                    self.status.set('Ar\u015fiv okunamad\u0131: '+data);messagebox.showerror('Ar\u015fiv',data,parent=self)
                else:
                    self.snap=data;self.dirty=False
                    self.status.set(f"{len(data['groups'])} hasta grubu \u00b7 {len(data['records'])} teknik \u00e7al\u0131\u015fma \u00b7 {len(data['reported'])} raporlanan g\u00f6zlem \u00b7 {len(data['artifacts'])} manuel artefakt karar\u0131")
                    self._populate_patients();self._catalogue_search();self._update_filter_choices();self._populate_studies();self._populate_gennext()
                    self._apply_research()
                    pending=getattr(self,'_requested_variant',None)
                    if pending:
                        self._requested_variant=None
                        self.open_variant(*pending)
                if self.refresh_pending:
                    self.refresh_pending=False;self.after_idle(self.refresh)
        except queue.Empty:pass
        if self.winfo_exists():self.after(120,self._poll)

    # ARSIV_SCROLL_V6: independent patient-detail scrollbar
    def _make_patient_detail_scroll_area(self, host):
        """Scroll the patient detail without moving the patient list on the left.

        Nested Treeview/Text widgets keep their own independent scrollbars.
        """
        canvas = tk.Canvas(host, borderwidth=0, highlightthickness=0)
        scrollbar = ttk.Scrollbar(host, orient='vertical', command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side='right', fill='y')
        canvas.pack(side='left', fill='both', expand=True)
        detail = ttk.Frame(canvas)
        window_id = canvas.create_window((0, 0), window=detail, anchor='nw')
        self._patient_detail_canvas = canvas

        def resize_detail(event=None):
            try:
                if not canvas.winfo_exists():
                    return
                w = max(1, canvas.winfo_width())
                h = max(1, canvas.winfo_height(), detail.winfo_reqheight())
                if (int(canvas.itemcget(window_id, 'width')) != w or
                        int(canvas.itemcget(window_id, 'height')) != h):
                    canvas.itemconfigure(window_id, width=w, height=h)
                canvas.configure(scrollregion=canvas.bbox('all'))
            except (tk.TclError, ValueError):
                pass

        detail.bind('<Configure>', resize_detail, add='+')
        canvas.bind('<Configure>', resize_detail, add='+')
        self._patient_detail_resize = resize_detail
        return detail

    def _scroll_patient_detail(self, event):
        canvas = getattr(self, '_patient_detail_canvas', None)
        if canvas is None or not canvas.winfo_viewable():
            return None
        number = getattr(event, 'num', None)
        if number in (4, 5):
            step = -1 if number == 4 else 1
        else:
            delta = getattr(event, 'delta', 0)
            if not delta:
                return None
            step = (-1 if delta > 0 else 1) * max(1, int(abs(delta) / 120))
        canvas.yview_scroll(step, 'units')
        return 'break'

    def _enable_patient_detail_wheel(self, detail):
        """Wheel over labels scrolls details; tables/text still scroll themselves.

        Hold Shift to scroll the entire right detail panel even over a table.
        """
        own_scroll = (ttk.Treeview, tk.Text, tk.Listbox, ttk.Combobox,
                      ttk.Entry, ttk.Scrollbar, tk.Entry)

        def wire(widget):
            widget.bind('<Shift-MouseWheel>', self._scroll_patient_detail, add='+')
            widget.bind('<Shift-Button-4>', self._scroll_patient_detail, add='+')
            widget.bind('<Shift-Button-5>', self._scroll_patient_detail, add='+')
            if not isinstance(widget, own_scroll):
                widget.bind('<MouseWheel>', self._scroll_patient_detail, add='+')
                widget.bind('<Button-4>', self._scroll_patient_detail, add='+')
                widget.bind('<Button-5>', self._scroll_patient_detail, add='+')
            for child in widget.winfo_children():
                wire(child)
        wire(detail)
        self._patient_detail_resize()

    def _build_patients(self):
        top=ttk.Frame(self.patients_tab);top.pack(fill='x',pady=(0,7))
        ttk.Label(top,text='Ad / MP / tan\u0131:').pack(side='left')
        self.patient_query=tk.StringVar();ent=ttk.Entry(top,textvariable=self.patient_query,width=34);ent.pack(side='left',padx=6)
        ent.bind('<Return>',lambda e:self._populate_patients())
        self.patient_filter=tk.StringVar(value='T\u00fcm hastalar')
        cb=ttk.Combobox(top,textvariable=self.patient_filter,values=list(REPORT_FILTERS),state='readonly',width=30);cb.pack(side='left',padx=5)
        cb.bind('<<ComboboxSelected>>',lambda e:self._populate_patients())
        ttk.Button(top,text='Ara',command=self._populate_patients).pack(side='left')
        ttk.Button(top,text='T\u00fcm\u00fcn\u00fc g\u00f6ster',command=self._reset_patient_filter).pack(side='left',padx=5)
        self.patient_count=tk.StringVar();ttk.Label(top,textvariable=self.patient_count,foreground='#52606d').pack(side='right')
        pan=ttk.Panedwindow(self.patients_tab,orient='horizontal');pan.pack(fill='both',expand=True)
        left=ttk.Frame(pan);right_host=ttk.Frame(pan,padding=(10,0,0,0))
        pan.add(left,weight=3);pan.add(right_host,weight=6)
        right=self._make_patient_detail_scroll_area(right_host)
        right.columnconfigure(0,weight=1);right.rowconfigure(6,weight=1,minsize=600)
        self.patient_table=DataTable(left,[('name','HASTA',185),('mps','MP KAYITLARI',155),('reported','RAPORLANAN',100),('pools','POOL',95)],height=28,select=self._select_patient)
        self.patient_table.pack(fill='both',expand=True)
        ttk.Label(left,text='Ayn\u0131 TC / vaka ailesi gruplan\u0131r.\n\u0130sim benzerli\u011fiyle hasta birle\u015ftirilmez.',foreground='#667085').pack(fill='x',pady=(6,0))
        self.patient_head=tk.StringVar(value='Soldan bir hasta se\u00e7in.')
        ttk.Label(right,textvariable=self.patient_head,font=('Segoe UI',13,'bold'),wraplength=1100).grid(row=0,column=0,sticky='ew')
        self.patient_context=tk.StringVar();ttk.Label(right,textvariable=self.patient_context,foreground='#475569',wraplength=1080).grid(row=1,column=0,sticky='ew',pady=(2,5))
        self.quick_metrics=tk.StringVar(value='MSI: —  |  HRD: —  |  Ortalama okuma: —')
        ttk.Label(right,textvariable=self.quick_metrics,font=('Segoe UI',10,'bold'),foreground='#0f766e').grid(row=2,column=0,sticky='ew',pady=(1,5))
        action=ttk.Frame(right);action.grid(row=3,column=0,sticky='ew',pady=(0,6))
        ttk.Button(action,text='Hasta / Blok d\u00fczenle',command=self._edit_current).pack(side='left')
        ttk.Button(action,text='Kaynak mutasyonlar / rapora ekle',command=self._raw_mutation_explorer).pack(side='left',padx=4)
        ttk.Button(action,text='Enlil MP kopyala',command=self._copy_enlil).pack(side='left',padx=4)
        ttk.Button(action,text='QC / Biyobelirteç',command=self._technical_summary).pack(side='left',padx=4)
        ttk.Button(action,text='Rapor eşleştirme / eksikler',command=self._reconcile_current_report).pack(side='left',padx=4)
        ttk.Button(action,text='YZ / IGV',command=lambda:self.open_review_cb() if self.open_review_cb else None).pack(side='left',padx=4)
        self.all_patient_records=tk.BooleanVar(value=True)
        ttk.Checkbutton(action,text='Varyantlarda t\u00fcm \u00e7al\u0131\u015fmalar',variable=self.all_patient_records,command=self._fill_patient_variants).pack(side='right')
        self.timeline=DataTable(right,[('mp','TEKN\u0130K \u00d6RNEK',115),('date','\u00c7ALI\u015eMA',95),('type','T\u0130P',50),('reported','RAP. V.',65),('result','RAPOR SONUCU',150),('msi','MSI',105),('hrd','HRD',55),('mean','ORT. OKUMA',90),('report','RAPOR',105),('gennext','GENNEXT',155)],height=4,select=self._select_technical)
        self.timeline.grid(row=4,column=0,sticky='ew',pady=(0,5))
        vertical=ttk.Panedwindow(right,orient='vertical');vertical.grid(row=6,column=0,sticky='nsew')
        varframe=ttk.LabelFrame(vertical,text='Raporlanan varyantlar  \u00b7  \u00e7ift t\u0131k: varyant tarih\u00e7esi',padding=4)
        textframe=ttk.LabelFrame(vertical,text='Rapor metni / s\u00fcr\u00fcmler',padding=5)
        vertical.add(varframe,weight=2);vertical.add(textframe,weight=3)
        self.patient_variants=DataTable(varframe,[('gene','GEN',90),('change','DE\u011e\u0130\u015e\u0130KL\u0130K',200),('mp','MP',115),('type','T\u0130P',55),('af','VAF',80),('dp','DP',65),('date','KAYIT TAR\u0130H\u0130',115),('state','KAYNAK / UYARI',210)],height=6,double=self._show_variant_history)
        self.patient_variants.pack(fill='both',expand=True)
        self.variant_count=tk.StringVar();ttk.Label(varframe,textvariable=self.variant_count,foreground='#52606d').pack(fill='x')
        report_nb=ttk.Notebook(textframe);report_nb.pack(fill='both',expand=True)
        reports_page=ttk.Frame(report_nb,padding=4);note_page=ttk.Frame(report_nb,padding=4)
        report_nb.add(reports_page,text='Raporlar');report_nb.add(note_page,text='Vaka notu')
        bar=ttk.Frame(reports_page);bar.pack(fill='x',pady=(0,5))
        self.doc_var=tk.StringVar();self.doc_box=ttk.Combobox(bar,textvariable=self.doc_var,state='readonly',width=52);self.doc_box.pack(side='left',fill='x',expand=True)
        self.doc_box.bind('<<ComboboxSelected>>',lambda e:self._show_document())
        ttk.Button(bar,text='Kopyala',command=self._copy_report).pack(side='left',padx=4)
        ttk.Button(bar,text='Büyük oku',command=self._full_report).pack(side='left',padx=4)
        self.report_source=tk.StringVar();ttk.Label(reports_page,textvariable=self.report_source,foreground='#8a5a00',wraplength=1060).pack(fill='x')
        self.reconcile_info=tk.StringVar(value='Rapor seçildiğinde pozitif varyant eşleştirme durumu burada görünür.')
        recbar=ttk.Frame(reports_page);recbar.pack(fill='x',pady=(3,3))
        ttk.Label(recbar,textvariable=self.reconcile_info,foreground='#0f766e').pack(side='left',fill='x',expand=True)
        ttk.Button(recbar,text='Eksikleri düzelt',command=self._reconcile_current_report).pack(side='right')
        area=ttk.Frame(reports_page);area.pack(fill='both',expand=True,pady=4)
        self.report_text=tk.Text(area,wrap='word',font=('Segoe UI',10),height=7,padx=9,pady=6,state='disabled')
        ys=ttk.Scrollbar(area,orient='vertical',command=self.report_text.yview);self.report_text.configure(yscrollcommand=ys.set)
        self.report_text.pack(side='left',fill='both',expand=True);ys.pack(side='right',fill='y')
        ttk.Label(note_page,text='Vaka notu',font=('Segoe UI',10,'bold')).pack(anchor='w')
        ttk.Label(note_page,text='Bu alan vaka/tümör düzeyindedir; aynı vaka ailesindeki teknik tekrarlarla ortak olabilir. Rapor sürümü değildir.',foreground='#667085',wraplength=1000).pack(fill='x',pady=(0,4))
        note_area=ttk.Frame(note_page);note_area.pack(fill='both',expand=True)
        self.case_note_text=tk.Text(note_area,wrap='word',font=('Segoe UI',10),height=8,padx=9,pady=6,state='disabled')
        note_scroll=ttk.Scrollbar(note_area,orient='vertical',command=self.case_note_text.yview);self.case_note_text.configure(yscrollcommand=note_scroll.set)
        self.case_note_text.pack(side='left',fill='both',expand=True);note_scroll.pack(side='right',fill='y')
        footer=ttk.Frame(right);footer.grid(row=7,column=0,sticky='ew',pady=(7,0))
        self.report_save_button=ttk.Button(footer,text='Yeni rapor / sürüm ekle',command=self._save_report_dialog)
        self.report_save_button.pack(side='left')
        self.assay_result_var=tk.StringVar(value='Belirtilmedi')
        ttk.Label(footer,text='Rapor sonucu:').pack(side='left',padx=(14,4))
        self.assay_result_box=ttk.Combobox(footer,textvariable=self.assay_result_var,state='readonly',width=34,values=('Belirtilmedi','NEGATİF — raporlanabilir değişiklik saptanmadı','POZİTİF — raporlanmış değişiklik var','BELİRSİZ / tekrar değerlendirme'))
        self.assay_result_box.pack(side='left')
        ttk.Button(footer,text='Sonucu kaydet',command=self._save_assay_result).pack(side='left',padx=4)
        ttk.Label(footer,text='Negatiflik yalnız manuel rapor sonucu olarak saklanır; veri yokluğu negatif sayılmaz.',foreground='#667085').pack(side='right')
        self._initial_split(pan,.33);self._initial_split(vertical,.40)
        self._enable_patient_detail_wheel(right)

    @staticmethod
    def _initial_split(pan,fraction):
        pan._split_ready=False
        def place(event=None):
            if pan._split_ready or not pan.winfo_viewable():return
            length=pan.winfo_width() if str(pan.cget('orient'))=='horizontal' else pan.winfo_height()
            if length<300:return
            pan._split_ready=True
            pan.sashpos(0,int(length*fraction))
        pan.bind('<Configure>',place,add='+')

    @staticmethod
    def _sash(pan,index,fraction):
        try:
            length=pan.winfo_width() if str(pan.cget('orient'))=='horizontal' else pan.winfo_height()
            if length>200:pan.sashpos(index,int(length*fraction))
        except tk.TclError:pass

    def _reset_patient_filter(self):
        self.patient_query.set('');self.patient_filter.set('T\u00fcm hastalar');self._populate_patients()

    def _populate_patients(self):
        if not self.snap:return
        matched=filter_records(self.snap,{'query':self.patient_query.get(),'report_filter':REPORT_FILTERS[self.patient_filter.get()]})
        ids={s['entity_key'] for s in matched}
        groups=[g for g in self.snap['groups'].values() if g['entity_key'] in ids]
        groups.sort(key=lambda g:norm(g['display_name']))
        self.patient_count.set(f'{len(groups)} hasta grubu / {len(matched)} teknik kay\u0131t')
        self.patient_table.fill(groups,lambda g:(g['display_name'],join_values(g['mps']),g['reported_count'],join_values(g['pools'])),self.current_entity,lambda g:g['entity_key'],True)
        if not groups:
            self.current_entity=self.current_record=None;self.patient_head.set('Filtreye uygun hasta bulunamad\u0131.')
            self.timeline.fill([],lambda r:());self.patient_variants.fill([],lambda r:());self._set_text(self.report_text,'');self.doc_box.configure(values=[]);self.doc_var.set('');self.patient_context.set('');self.report_source.set('');self.variant_count.set('')

    def select_record(self,record_key):
        self._requested_record=record_key
        if self.snap is None:
            self.refresh();return
        s=self.snap['records'].get(record_key)
        if not s:return
        self.current_entity=s['entity_key'];self.current_record=record_key
        self._reset_patient_filter();self.nb.select(self.patients_tab)

    def _select_patient(self,group):
        if not self.snap:return
        requested=getattr(self,'_requested_record',None)
        if requested:
            self._requested_record=None
            if requested in self.snap['records'] and self.snap['records'][requested]['entity_key']!=group['entity_key']:
                self.after_idle(lambda:self.select_record(requested));return
            self.current_record=requested
        self.current_entity=group['entity_key']
        recs=[self.snap['records'][rk] for rk in group['records']]
        self.patient_head.set(group['display_name']+'  |  '+join_values(group['mps']))
        warning='  \u26a0 TC grubu / demografi uyu\u015fmazl\u0131\u011f\u0131; otomatik birle\u015ftirilmedi.' if group['identity_conflict'] else ''
        self.patient_context.set(join_values(group['diagnoses'])+'  |  '+join_values(group['types'])+'  |  '+str(len(recs))+' teknik \u00e7al\u0131\u015fma'+warning)
        selected=self.current_record if self.current_record in group['records'] else group['records'][0]
        for r in recs:
            metrics=self.store.technical_metrics(r['record_key']);r['_metrics']=metrics
            old_label = {'negative':'NEGATİF','positive':'POZİTİF','indeterminate':'BELİRSİZ'}.get(r.get('assay_result_state'),'Belirtilmedi')
            closure = r.get('manual_report_close_status')
            manual = {'reported_negative': 'NEGATİF (manuel)', 'reported_positive': 'POZİTİF (manuel)',
                      'cancelled': 'İPTAL', 'insufficient': 'YETERSİZ',
                      'review_closed': 'İNCELENDİ / RAPOR YOK'}.get(closure)
            r['_assay_label'] = manual or old_label
        self.timeline.fill(recs,lambda s:(s['patient_id'],shown_date(s['run_date']),s['sample_type'],s['reported_count'],s['_assay_label'],s['_metrics'].get('msi') or '—',s['_metrics'].get('hrd') or '—',(('%.1fx' % s['_metrics']['mean_coverage_x']) if s['_metrics'].get('mean_coverage_x') is not None else '—'),s['document_state'],self._gennext_label(s)),selected,lambda s:s['record_key'],True)

    def _select_technical(self,s):
        self.current_record=s['record_key'];self._fill_patient_variants()
        self.doc_map={}
        for d in s['documents']:
            status='Kullanıcı onaylı' if d['document_status']=='user_confirmed' else 'Taslak'
            label=f"v{d['version']} · {status} · {d['created_at'][:16]} · {d['title']}"
            self.doc_map[label]=d['id']
        self.doc_box.configure(values=list(self.doc_map))
        self.doc_var.set(next(iter(self.doc_map),'') )
        self._set_text(self.case_note_text,s.get('case_note') or 'Vaka notu bulunmuyor.')
        metrics=s.get('_metrics') or self.store.technical_metrics(self.current_record)
        mean=('%.1fx' % metrics['mean_coverage_x']) if metrics.get('mean_coverage_x') is not None else '—'
        closure = s.get('manual_report_close_status')
        close_label = {'reported_negative': 'NEGATİF', 'reported_positive': 'POZİTİF',
                       'cancelled': 'İPTAL', 'insufficient': 'YETERSİZ',
                       'review_closed': 'İNCELENDİ / RAPOR YOK', 'open': 'YENİDEN AÇILDI'}.get(closure, '')
        self.quick_metrics.set(f"MSI: {metrics.get('msi') or '—'}  |  HRD: {metrics.get('hrd') or '—'}  |  Ortalama okuma: {mean}  |  GenNext: {self._gennext_label(s)}"
                               + ('  |  Manuel kapanış: ' + close_label if close_label else ''))
        self.assay_result_var.set({'negative':'NEGATİF — raporlanabilir değişiklik saptanmadı','positive':'POZİTİF — raporlanmış değişiklik var','indeterminate':'BELİRSİZ / tekrar değerlendirme'}.get(s.get('assay_result_state'),'Belirtilmedi'))
        self._show_document()

    def _fill_patient_variants(self):
        if not self.snap or not self.current_entity:return
        group=self.snap['groups'].get(self.current_entity)
        if not group:return
        keys=set(group['records']) if self.all_patient_records.get() else {self.current_record}
        rows=[r for r in self.snap['reported'] if r['record_key'] in keys]
        rows.sort(key=lambda r:(norm(r.get('gene')),r.get('variant_key','')))
        def values(v):
            s=self.snap['records'].get(v['record_key'],{})
            state='\u26a0 Rapor ge\u00e7mi\u015fi / g\u00fcncel karar \u00e7eli\u015fkili' if v.get('decision_conflict') else 'GenNext Selection (Analyzed; imzas\u0131z)' if v.get('source')=='gennext_selection' else 'Rapordan onayl\u0131' if v.get('history_event_count') else 'Manuel rapor karar\u0131'
            return(v.get('gene') or '\u2014',v.get('change') or v['variant_key'],s.get('patient_id',''),v.get('variant_type',''),af_text(v.get('af')),v.get('dp',''),shown_date(v.get('last_reported_at')),state)
        self.patient_variants.fill(rows,values)
        self.variant_count.set(f'{len(rows)} raporlanan g\u00f6zlem \u00b7 Kay\u0131t yoklu\u011fu, negatif sonu\u00e7 veya raporlanmad\u0131 anlam\u0131na gelmez.')

    @staticmethod
    def _set_text(widget,text):
        widget.configure(state='normal');widget.delete('1.0','end');widget.insert('1.0',text or '');widget.configure(state='disabled')

    def _show_document(self):
        if not self.snap or not self.current_record:return
        doc_id=self.doc_map.get(self.doc_var.get())
        if not doc_id:
            self._raw_report_text='';self._current_reconciliation=None
            self._set_text(self.report_text,'Bu teknik örnek için ayrı rapor sürümü kaydedilmemiş. Vaka notu ayrı sekmede gösterilir.')
            self.report_source.set('Rapor yok. Vaka notu rapor sürümü sayılmaz.')
            self.reconcile_info.set('Rapor sürümü yok; eşleştirme yapılmadı.')
            return
        d=self.store.document(doc_id);text=d.get('report_text','')
        self.report_source.set('Seçili teknik örneğe bağlı ayrı rapor sürümü. Düzeltme yeni sürüm olarak saklanır.')
        self._set_text(self.report_text,text);self._raw_report_text=text
        try:
            rec=self.store.report_reconciliation(self.current_record,text);self._current_reconciliation=rec
            total=len(rec['matches'])
            self.reconcile_info.set(f"Raporda {total} pozitif varyant/CNV satırı · {rec['reported_count']} raporlanmış · {rec['matched_unconfirmed_count']} ham veride eşleşmiş/onay bekliyor · {rec['not_found_count']} otomatik bulunamadı")
        except Exception as e:
            self._current_reconciliation=None;self.reconcile_info.set('Rapor eşleştirme özeti hazırlanamadı: '+str(e))

    def _copy_report(self):
        if getattr(self,'_raw_report_text',''):
            self.clipboard_clear();self.clipboard_append(self._raw_report_text);self.status.set('Rapor metni yerel panoya kopyaland\u0131.')

    def _full_report(self):
        self._text_window('Rapor metni',getattr(self,'_raw_report_text',''))

    def _text_window(self,title,text):
        win=tk.Toplevel(self);win.title(title);win.geometry('1080x800');win.minsize(600,400)
        area=ttk.Frame(win,padding=12);area.pack(fill='both',expand=True)
        txt=tk.Text(area,wrap='word',font=('Segoe UI',11),padx=12,pady=12)
        sy=ttk.Scrollbar(area,orient='vertical',command=txt.yview);txt.configure(yscrollcommand=sy.set)
        txt.pack(side='left',fill='both',expand=True);sy.pack(side='right',fill='y');self._set_text(txt,text)
        ttk.Button(win,text='Kapat',command=win.destroy).pack(pady=8)

    def _save_report_dialog(self):
        if not self.current_record:return
        rk=self.current_record;smp=self.snap['records'][rk];current=getattr(self,'_raw_report_text','')
        win=tk.Toplevel(self);win.title('Yeni rapor / sürüm ekle');win.geometry('900x700');win.transient(self.winfo_toplevel());win.grab_set()
        f=ttk.Frame(win,padding=14);f.pack(fill='both',expand=True)
        ttk.Label(f,text=f"{smp['patient_id']} | {smp['pool_name']} | {smp['sample_type']}",font=('Segoe UI',11,'bold')).pack(anchor='w')
        ttk.Label(f,text='Her rapor ayrı sürüm olarak saklanır. Vaka notu otomatik rapor kabul edilmez.',foreground='#667085').pack(anchor='w',pady=(2,8))
        title=tk.StringVar(value='NGS raporu');ttk.Entry(f,textvariable=title).pack(fill='x')
        area=ttk.Frame(f);area.pack(fill='both',expand=True,pady=8)
        txt=tk.Text(area,wrap='word',font=('Segoe UI',10),padx=8,pady=8);ys=ttk.Scrollbar(area,orient='vertical',command=txt.yview);txt.configure(yscrollcommand=ys.set)
        txt.pack(side='left',fill='both',expand=True);ys.pack(side='right',fill='y');txt.insert('1.0',current)
        row=ttk.Frame(f);row.pack(fill='x')
        ttk.Button(row,text='Vaka notunu metne al',command=lambda:(txt.delete('1.0','end'),txt.insert('1.0',smp.get('case_note') or ''))).pack(side='left')
        confirmed=tk.BooleanVar(value=False);ttk.Checkbutton(row,text='Kullanıcı doğrulanmış nihai rapor metni',variable=confirmed).pack(side='left',padx=12)
        def save():
            text=txt.get('1.0','end').strip()
            try:
                self.store.save_document(rk,text,title.get(),'user_confirmed' if confirmed.get() else 'draft')
                win.destroy();self.invalidate();self.refresh()
            except Exception as e:messagebox.showerror('Rapor arşivi',str(e),parent=win)
        ttk.Button(row,text='Yeni sürümü kaydet',command=save).pack(side='right')
        ttk.Button(row,text='Vazgeç',command=win.destroy).pack(side='right',padx=5)

    def _save_assay_result(self):
        if not self.current_record:return
        state={'Belirtilmedi':'unset','NEGATİF — raporlanabilir değişiklik saptanmadı':'negative','POZİTİF — raporlanmış değişiklik var':'positive','BELİRSİZ / tekrar değerlendirme':'indeterminate'}.get(self.assay_result_var.get(),'unset')
        s=self.snap['records'].get(self.current_record,{})
        typ=s.get('sample_type') or ''
        note=('RNA: raporlanabilir füzyon/değişiklik saptanmadı' if state=='negative' and typ=='RNA' else
              'DNA: raporlanabilir mutasyon/değişiklik saptanmadı' if state=='negative' else '')
        try:
            self.store.set_assay_result(self.current_record,state,note)
            self.invalidate();self.refresh();self.status.set(f"{s.get('patient_id','')} rapor sonucu kaydedildi: {self.assay_result_var.get()}")
        except Exception as e:messagebox.showerror('Rapor sonucu',str(e),parent=self)

    def _raw_mutation_explorer(self, prefill_gene='', callback=None, current_only=False):
        if not self.snap or not self.current_entity:return
        group=self.snap['groups'][self.current_entity]
        records=[self.current_record] if current_only else list(group['records'])
        all_rows=[]
        for rk in records:
            sample=self.snap['records'][rk]
            for raw in self.store.raw_variants(rk):
                x=dict(raw);x['_sample']=sample;all_rows.append(x)
        win=tk.Toplevel(self);win.title('Hastanın kaynak mutasyonları / rapora ekle');win.geometry('1560x820');win.transient(self.winfo_toplevel())
        top=ttk.Frame(win,padding=10);top.pack(fill='x')
        ttk.Label(top,text='Ara (gen / HGVS / füzyon / özellik):').pack(side='left')
        q=tk.StringVar(value=prefill_gene);ttk.Entry(top,textvariable=q,width=28).pack(side='left',padx=5)
        typ=tk.StringVar(value='Tümü');ttk.Combobox(top,textvariable=typ,state='readonly',values=('Tümü','SNV','CNV','FUSION'),width=11).pack(side='left',padx=5)
        onc=tk.StringVar(value='Tümü');ttk.Combobox(top,textvariable=onc,state='readonly',values=('Tümü','Oncogenic','Likely Oncogenic','Oncogenic + Likely Oncogenic'),width=26).pack(side='left',padx=5)
        scope=tk.StringVar(value='Bu teknik örnek' if current_only else 'Hastanın tüm çalışmaları');ttk.Label(top,textvariable=scope,foreground='#667085').pack(side='right')
        info=tk.StringVar();ttk.Label(win,textvariable=info,foreground='#0f766e',padding=(10,0,10,5)).pack(fill='x')
        cols=('mp','type','gene','change','onc','filter','af','dp','reported','key')
        table=DataTable(win,[( 'mp','MP',105),('type','TİP',70),('gene','GEN',120),('change','DEĞİŞİKLİK / ÖZELLİK',260),('onc','ONKOJENİK',150),('filter','FİLTRE',100),('af','VAF',75),('dp','DP',65),('reported','RAPOR',95),('key','VARYANT ANAHTARI',360)],height=22)
        table.pack(fill='both',expand=True,padx=10,pady=5)
        reported_by_record={rk:{canonical_key(v['variant_key']) for v in self.snap['reported'] if v['record_key']==rk} for rk in records}
        def filtered():
            words=norm(q.get()).split();want_type=typ.get();want_onc=onc.get();rows=[]
            for r in all_rows:
                if want_type!='Tümü' and r['variant_type']!=want_type:continue
                cls=r.get('oncogenic') or ''
                if want_onc=='Oncogenic' and cls!='Oncogenic':continue
                if want_onc=='Likely Oncogenic' and cls!='Likely Oncogenic':continue
                if want_onc=='Oncogenic + Likely Oncogenic' and cls not in ('Oncogenic','Likely Oncogenic'):continue
                hay=norm(' '.join(str(r.get(k) or '') for k in ('gene','change','cdna','transcript','feature','variant_key')))
                if words and not all(w in hay for w in words):continue
                x=dict(r);smp=x['_sample'];x.update(mp=smp['patient_id'],type=x['variant_type'],onc=cls,af=af_text(x.get('af')),reported='EVET' if canonical_key(x['variant_key']) in reported_by_record.get(x['record_key'],set()) else '')
                rows.append(x)
            info.set(f'{len(rows)} eşleşen kaynak kayıt · Sınıf filtresi ham dosyanın Oncogenic alanıdır; Tümü tüm yerel SNV/CNV/Fusion kayıtlarını gösterir.')
            table.fill(rows,lambda x:(x['mp'],x['type'],x.get('gene') or '—',x.get('change') or x.get('feature') or x.get('cdna') or '—',x.get('onc') or '—',x.get('filter') or '—',x.get('af') or '',x.get('dp') or '',x.get('reported') or '',x['variant_key']),select_first=True)
        def add_selected():
            r=table.selected()
            if not r:return
            try:
                if callback:callback(r);win.destroy();return
                self.store.add_raw_as_reported(r['record_key'],r)
                self.status.set(f"{r['_sample']['patient_id']} · {r.get('gene') or r.get('change')} raporlandı olarak eklendi.")
                self.invalidate();self.refresh();win.destroy()
            except Exception as e:messagebox.showerror('Raporlanan varyant',str(e),parent=win)
        q.trace_add('write',lambda *_:filtered());typ.trace_add('write',lambda *_:filtered());onc.trace_add('write',lambda *_:filtered())
        foot=ttk.Frame(win,padding=10);foot.pack(fill='x')
        ttk.Button(foot,text='Seçili kaynak varyantı RAPORLANDI olarak ekle',command=add_selected).pack(side='left')
        ttk.Button(foot,text='Manuel rapor kaydı ekle',command=lambda:self._manual_report_variant(win)).pack(side='left',padx=6)
        ttk.Button(foot,text='Kapat',command=win.destroy).pack(side='right');filtered()

    def _manual_report_variant(self, parent=None, seed=None, record_key=None):
        rk=record_key or self.current_record
        if not rk:return
        seed=seed or {};win=tk.Toplevel(parent or self);win.title('Manuel raporlanan değişiklik');win.geometry('690x520');win.transient(parent or self.winfo_toplevel());win.grab_set()
        vars_={k:tk.StringVar(value=str(seed.get(k) or '')) for k in ('gene','transcript','cdna','protein','feature','note')}
        typ=tk.StringVar(value=seed.get('type') or 'SNV')
        f=ttk.Frame(win,padding=14);f.pack(fill='both',expand=True);f.columnconfigure(1,weight=1)
        fields=[('TİP',None),('Gen / füzyon', 'gene'),('Transkript','transcript'),('cDNA','cdna'),('Protein','protein'),('Özellik (CNV GAIN/LOSS, Fold Change vb.)','feature'),('Not','note')]
        for i,(lab,key) in enumerate(fields):
            ttk.Label(f,text=lab).grid(row=i,column=0,sticky='w',pady=5,padx=(0,8))
            w=ttk.Combobox(f,textvariable=typ,state='readonly',values=('SNV','CNV','FUSION'),width=18) if key is None else ttk.Entry(f,textvariable=vars_[key])
            w.grid(row=i,column=1,sticky='ew',pady=5)
        def save():
            gene=vars_['gene'].get().strip();kind=typ.get()
            if not gene:messagebox.showerror('Manuel kayıt','Gen / füzyon zorunludur.',parent=win);return
            raw='|'.join([kind,gene,vars_['cdna'].get(),vars_['protein'].get(),vars_['feature'].get()])
            key='MANUAL|'+kind+'|'+hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]
            try:
                self.db.add_reported_variant(rk,key,kind,gene,vars_['transcript'].get().strip(),vars_['cdna'].get().strip(),vars_['protein'].get().strip(),source='archive_manual',source_text=vars_['note'].get().strip(),match_score=100,confirmed=True,feature=vars_['feature'].get().strip())
                self.db.set_variant_decision(rk,key,'reported','Arşivden manuel raporlanan değişiklik')
                self.invalidate();self.refresh();win.destroy()
            except Exception as e:messagebox.showerror('Manuel kayıt',str(e),parent=win)
        ttk.Button(f,text='RAPORLANDI olarak kaydet',command=save).grid(row=len(fields),column=1,sticky='w',pady=14)

    def _reconcile_current_report(self):
        if not self.current_record:return
        text=getattr(self,'_raw_report_text','')
        if not text.strip():messagebox.showinfo('Rapor eşleştirme','Önce ayrı bir rapor sürümü seçin.',parent=self);return
        try:rec=self.store.report_reconciliation(self.current_record,text)
        except Exception as e:messagebox.showerror('Rapor eşleştirme',str(e),parent=self);return
        win=tk.Toplevel(self);win.title('Rapordaki varyantları ham veriyle karşılaştır');win.geometry('1580x760');win.transient(self.winfo_toplevel())
        ttk.Label(win,text=f"Pozitif rapor satırı: {len(rec['matches'])} · Raporlandı: {rec['reported_count']} · Eşleşti/onay bekliyor: {rec['matched_unconfirmed_count']} · Otomatik bulunamadı: {rec['not_found_count']}",font=('Segoe UI',11,'bold'),padding=10).pack(fill='x')
        ttk.Label(win,text='Otomatik bulunamayan satırlar kaybolmaz. Seçip hastanın kaynak mutasyonlarında arayabilir veya gen/özellik ile manuel rapor kaydı ekleyebilirsiniz.',foreground='#8a5a00',padding=(10,0,10,5)).pack(fill='x')
        table=DataTable(win,[('state','DURUM',180),('type','TİP',65),('gene','GEN',95),('reported','RAPORDA YAZAN',410),('matched','HAM VERİ EŞLEŞMESİ',410),('score','SKOR',60)],height=18)
        rows=[]
        for m in rec['matches']:
            state={'reported':'RAPORLANDI','matched_unconfirmed':'EŞLEŞTİ · ONAY BEKLİYOR','not_found':'OTOMATİK BULUNAMADI'}[m['reconcile_state']]
            shown=' '.join(x for x in [m.get('transcript'),m.get('cdna'),m.get('protein'),m.get('feature')] if x) or m.get('raw_text','')
            matched=' '.join(x for x in [m.get('matched_transcript'),m.get('matched_cdna'),m.get('matched_protein'),m.get('matched_feature')] if x) or m.get('variant_key') or '—'
            x=dict(m);x.update(state=state,type=m.get('type'),reported=shown,matched=matched,score=m.get('match_score',0));rows.append(x)
        table.pack(fill='both',expand=True,padx=10,pady=5);table.fill(rows,lambda x:(x['state'],x['type'],x.get('gene') or '',x['reported'],x['matched'],x['score']),select_first=True)
        def confirm_match():
            m=table.selected()
            if not m:return
            if m['reconcile_state']=='reported':return
            if not m.get('canonical_variant_key'):
                messagebox.showinfo('Rapor eşleştirme','Bu satır için otomatik ham varyant bulunamadı. Kaynak mutasyonlarda ara veya manuel ekle kullanın.',parent=win);return
            kind=m.get('type') or 'SNV';feature=m.get('matched_feature') or m.get('feature') or ''
            self.db.add_reported_variant(self.current_record,m['canonical_variant_key'],kind,m.get('matched_gene') or m.get('gene') or '',m.get('matched_transcript') or m.get('transcript') or '',m.get('matched_cdna') or m.get('cdna') or '',m.get('matched_protein') or m.get('protein') or '',source='archive_report_reconcile',source_text=m.get('raw_text') or '',report_hash=rec['report_hash'],match_score=m.get('match_score') or 0,confirmed=True,feature=feature)
            self.db.set_variant_decision(self.current_record,m['canonical_variant_key'],'reported','Rapor metni / ham veri eşleştirmesi manuel onay')
            self.invalidate();win.destroy();self.refresh()
        def search_raw():
            m=table.selected()
            if not m:return
            def choose(raw):
                self.store.add_raw_as_reported(raw['record_key'],raw,source='archive_report_manual_match',source_text=m.get('raw_text') or '')
                self.invalidate();self.refresh()
            self._raw_mutation_explorer(prefill_gene=m.get('gene') or '',callback=choose,current_only=False)
        def manual():
            m=table.selected()
            if not m:return
            self._manual_report_variant(win,{'type':m.get('type') or 'SNV','gene':m.get('gene') or '', 'transcript':m.get('transcript') or '', 'cdna':m.get('cdna') or '', 'protein':m.get('protein') or '', 'feature':m.get('feature') or '', 'note':m.get('raw_text') or ''})
        foot=ttk.Frame(win,padding=10);foot.pack(fill='x')
        ttk.Button(foot,text='Eşleşen seçili satırı RAPORLANDI onayla',command=confirm_match).pack(side='left')
        ttk.Button(foot,text='Hastanın kaynak mutasyonlarında ara',command=search_raw).pack(side='left',padx=6)
        ttk.Button(foot,text='Manuel gen / özellik ile ekle',command=manual).pack(side='left',padx=6)
        ttk.Button(foot,text='Kapat',command=win.destroy).pack(side='right')

    def _edit_current(self):
        if self.current_record and self.open_metadata_cb:self.open_metadata_cb(self.current_record)

    def _mutations_current(self):
        if self.current_record and self.open_mutations_cb:self.open_mutations_cb(self.current_record)

    def _technical_summary(self):
        if not self.snap or not self.current_record:return
        s=self.snap['records'][self.current_record]
        text=[f"{s['patient_id']} | {s['pool_name']} | {s['sample_type']}",
              'Kaynak veriler; otomatik kalite onayı veya klinik yorum değildir.', '',
              f"TMB (kaynak): {s.get('tmb') or 'Veri yok'}",
              f"MSI (kaynak): {s.get('msi') or 'Veri yok'}",
              f"HRD (kaynak): {s.get('hrd') or 'Veri yok'}",
              f"Run derinliği: {s.get('depth') or 'Veri yok'}", '', 'Toplama adımları:']
        for t in s['tasks']:text.append(f"{t['task_type']}: {t['status']} | {t.get('last_error') or ''}")
        path=None
        for f in s['files']:
            if f.get('file_type')=='qc':
                candidate=Path(f['path']);candidate=candidate if candidate.is_absolute() else self.base/candidate
                if candidate.is_file():path=candidate;break
        if path:
            try:
                from ai_export import _read_qc_package
                qc=_read_qc_package(path,s['patient_id'],False)
                text.extend(['', 'MultiQC (yerel dosyadan):', json.dumps(qc,ensure_ascii=False,indent=2)])
            except Exception as e:text.extend(['','QC kaynağı okunamadı: '+str(e)])
        else:text.extend(['','Yerelde erişilebilir QC dosyası bulunamadı.'])
        if s['igv']:
            text.extend(['','IGV işleri:'])
            for q in s['igv']:text.append(f"{q.get('gene') or ''} {q.get('change') or q.get('variant_key') or ''} | {q['status']} | {q.get('manual_decision') or 'Karar yok'}")
        self._text_window('Teknik özet / QC / biyobelirteçler','\n'.join(text))

    def _copy_enlil(self):
        if self.snap and self.current_record:
            from metadata_editor import MetadataEditor
            mp=MetadataEditor.enlil_patient_id(self.snap['records'][self.current_record]['patient_id'])
            self.clipboard_clear();self.clipboard_append(mp);self.status.set('Enlil MP kopyaland\u0131: '+mp)

    def _build_variants(self):
        top=ttk.Frame(self.variants_tab);top.pack(fill='x',pady=(0,7))
        self.catalogue_dataset=tk.StringVar(value='Raporlanan varyantlar')
        cb=ttk.Combobox(top,textvariable=self.catalogue_dataset,values=list(DATASETS),state='readonly',width=31);cb.pack(side='left');cb.bind('<<ComboboxSelected>>',lambda e:self._catalogue_search())
        ttk.Label(top,text='Gen / HGVS / koordinat:').pack(side='left',padx=(12,4))
        self.variant_query=tk.StringVar();ent=ttk.Entry(top,textvariable=self.variant_query,width=35);ent.pack(side='left');ent.bind('<Return>',lambda e:self._catalogue_search())
        ttk.Button(top,text='Ara',command=self._catalogue_search).pack(side='left',padx=6)
        ttk.Button(top,text='T\u00fcm varyantlar',command=self._clear_variant_search).pack(side='left',padx=4)
        self.catalogue_info=tk.StringVar();ttk.Label(self.variants_tab,textvariable=self.catalogue_info,foreground='#475569').pack(fill='x',pady=(0,6))
        pan=ttk.Panedwindow(self.variants_tab,orient='vertical');pan.pack(fill='both',expand=True)
        a=ttk.LabelFrame(pan,text='Varyant katalo\u011fu \u00b7 t\u00fcm POOL\u2019lar',padding=4);b=ttk.LabelFrame(pan,text='Se\u00e7ili varyant\u0131n hastalardaki g\u00f6zlemleri \u00b7 \u00e7ift t\u0131k: hasta kart\u0131',padding=4)
        pan.add(a,weight=2);pan.add(b,weight=3)
        self.catalogue_table=DataTable(a,[('gene','GEN',110),('change','DE\u011e\u0130\u015e\u0130KL\u0130K',240),('type','T\u0130P',75),('patients','HASTA',80),('samples','TEKN\u0130K KAYIT',105),('pools','POOL',75),('reported','AR\u015e\u0130V RAPOR',110),('artifact','AR\u015e\u0130V ARTEFAKT',125),('key','ANAHTAR (DERLEME DO\u011eRULANMALI)',400)],height=10,select=self._catalogue_selected)
        self.catalogue_table.pack(fill='both',expand=True)
        paging=ttk.Frame(a);paging.pack(fill='x',pady=(4,0))
        ttk.Button(paging,text='\u2039 \u00d6nceki',command=lambda:self._page(-1)).pack(side='left')
        ttk.Button(paging,text='Sonraki \u203a',command=lambda:self._page(1)).pack(side='left',padx=4)
        self.page_info=tk.StringVar();ttk.Label(paging,textvariable=self.page_info).pack(side='left',padx=8)
        ttk.Label(paging,text='Sayfalama sadece g\u00f6r\u00fcn\u00fcm i\u00e7indir; say\u0131mlar t\u00fcm sonu\u00e7lar\u0131 kapsar.',foreground='#667085').pack(side='right')
        self.observation_table=DataTable(b,[('name','HASTA',185),('mp','MP',105),('pool','POOL',100),('date','\u00c7ALI\u015eMA',110),('dx','TANI',240),('af','VAF',80),('ad','AD',65),('dp','DP',65),('filter','F\u0130LTRE',95),('report','RAPOR GE\u00c7M\u0130\u015e\u0130',115),('artifact','ARTEFAKT KARARI',130),('age','YA\u015e',65),('tumor','T\u00dcM\u00d6R %',90)],height=12,double=self._observation_open_patient)
        self.observation_table.pack(fill='both',expand=True)
        foot=ttk.Frame(self.variants_tab);foot.pack(side='bottom',fill='x',pady=(7,0),before=pan)
        ttk.Button(foot,text='Se\u00e7ili hastan\u0131n raporu',command=lambda:self._observation_open_patient(self.observation_table.selected())).pack(side='left')
        ttk.Button(foot,text='Varyant ayr\u0131nt\u0131s\u0131 / karar ge\u00e7mi\u015fi',command=self._selected_variant_details).pack(side='left',padx=5)
        ttk.Button(foot,text='OncoKB',command=self._oncokb).pack(side='left',padx=5)
        ttk.Button(foot,text='Franklin',command=self._franklin).pack(side='left',padx=5)
        ttk.Label(foot,text='Raporland\u0131 ve artefakt karar\u0131 ayn\u0131 \u00f6rnekte \u00e7eli\u015fiyorsa ikisi de g\u00f6sterilir.',foreground='#8a5a00').pack(side='right')
        self._initial_split(pan,.48)

    def _clear_variant_search(self):
        self.variant_query.set('');self._catalogue_search()

    def _catalogue_search(self):
        if not self.snap:return
        ds=DATASETS[self.catalogue_dataset.get()]
        rows=select_variants(self.snap,list(self.snap['records'].values()),ds,self.variant_query.get())
        self.catalogue=variant_catalogue(rows,self.snap['records']);self.catalogue_page=0
        reports={};artifacts={}
        for v in self.snap['reported']:reports.setdefault(v['variant_key'],set()).add(v['record_key'])
        for v in self.snap['artifacts']:artifacts.setdefault(v['variant_key'],set()).add(v['record_key'])
        for v in self.catalogue:
            v['reported_count']=len(reports.get(v['variant_key'],set()))
            v['artifact_count']=len(artifacts.get(v['variant_key'],set()))
        self.catalogue_info.set(f'{len(self.catalogue)} farkl\u0131 anahtar \u00b7 {len(rows)} teknik g\u00f6zlem \u00b7 Bulunanlar mevcut onkojenik indeksle s\u0131n\u0131rl\u0131d\u0131r; bo\u015f kay\u0131t negatif de\u011fildir.')
        self._render_catalogue()
        if not self.catalogue:self.observation_table.fill([],lambda x:())

    def _page(self,delta):
        end=max(0,(len(self.catalogue)-1)//self.page_size)
        self.catalogue_page=max(0,min(end,self.catalogue_page+delta));self._render_catalogue()

    def _render_catalogue(self,prefer=None):
        start=self.catalogue_page*self.page_size;rows=self.catalogue[start:start+self.page_size]
        self.catalogue_table.fill(rows,lambda v:(v['gene'] or '\u2014',v['change'],v['variant_type'],v['patient_count'],v['sample_count'],v['pool_count'],v['reported_count'],v['artifact_count'],v['variant_key']),prefer,lambda v:v['variant_key'],True)
        self.page_info.set(f'{start+1 if rows else 0}\u2013{start+len(rows)} / {len(self.catalogue)}')

    def _catalogue_selected(self,v):
        self._focused_variant=v
        key=v['variant_key']
        # Merge all three factual layers for drill-down, even if catalogue is reported-only.
        merged={}
        for ds in ('observed','artifacts','reported'):
            for row in self.snap[ds]:
                if row['variant_key']!=key:continue
                k=row['record_key'];combined=merged.setdefault(k,{})
                for field,val in row.items():
                    if val is not None and val!='':combined[field]=val
        rows=list(merged.values())
        rows.sort(key=lambda r:norm(self.snap['records'].get(r['record_key'],{}).get('full_name')))
        def values(r):
            s=self.snap['records'].get(r['record_key'],{})
            return(s.get('full_name') or '(isim yok)',s.get('patient_id'),s.get('pool_group'),shown_date(s.get('run_date')),s.get('diagnosis'),af_text(r.get('af')),r.get('ad',''),r.get('dp',''),r.get('filter',''),'Evet' if r.get('recorded_as_reported') else 'Kay\u0131t yok','Evet' if r.get('manual_artifact') else 'Kay\u0131t yok',s.get('age',''),s.get('tumor_percentage',''))
        self.observation_table.fill(rows,values,select_first=True)

    def _show_variant_history(self,row):
        if not row:return
        key=canonical_key(row['variant_key'])
        ds=next((d for d in ('reported','observed','artifacts','gennext_candidates') if any(v['variant_key']==key for v in self.snap[d])),'reported')
        self.catalogue_dataset.set(next(k for k,v in DATASETS.items() if v==ds));self.variant_query.set(key)
        self._catalogue_search();self.nb.select(self.variants_tab)

    def open_variant(self,key='',gene='',change=''):
        if self.snap is None:
            self._requested_variant=(key,gene,change);self.refresh();return
        self.catalogue_dataset.set('Onkojenik indeksi (bulunan)');self.variant_query.set(key or gene or change);self._catalogue_search()
        if not self.catalogue:
            self.catalogue_dataset.set('Raporlanan varyantlar');self._catalogue_search()
        self.nb.select(self.variants_tab)

    def _observation_open_patient(self,row):
        if row:self.select_record(row['record_key'])

    def _selected_variant_details(self):
        row=self.observation_table.selected()
        if not row:return
        key=canonical_key(row['variant_key']);rk=row['record_key']
        hist=[h for h in self.snap['history'] if h['record_key']==rk and canonical_key(h['variant_key'])==key]
        text=json.dumps({'observation':row,'report_events':hist},ensure_ascii=False,indent=2)
        self._text_window('Varyant / karar ge\u00e7mi\u015fi (yerel)',text)

    def _oncokb(self):
        v=self.catalogue_table.selected()
        if v:open_url(oncokb_url(v.get('gene',''),v.get('change',''),v['variant_key']))

    def _franklin(self):
        v=self.catalogue_table.selected()
        if v:
            query=search_text(v.get('gene',''),v.get('change',''),v['variant_key'])
            self.clipboard_clear();self.clipboard_append(query)
            open_url(franklin_url(v['variant_key'],self.cfg.get('franklin_reference','auto')))

    def _build_research(self):
        pan=ttk.Panedwindow(self.research_tab,orient='horizontal');pan.pack(fill='both',expand=True)
        left=ttk.Frame(pan,padding=(0,0,10,0));right=ttk.Frame(pan);pan.add(left,weight=1);pan.add(right,weight=4)
        right.columnconfigure(0,weight=1);right.rowconfigure(3,weight=1)
        sidebar=left
        sidebar.columnconfigure(0,weight=1);sidebar.rowconfigure(0,weight=3);sidebar.rowconfigure(1,weight=1)
        filter_view=ttk.Frame(sidebar);filter_view.grid(row=0,column=0,sticky='nsew')
        filter_view.columnconfigure(0,weight=1);filter_view.rowconfigure(0,weight=1)
        canvas=tk.Canvas(filter_view,borderwidth=0,highlightthickness=0,width=380)
        vs=ttk.Scrollbar(filter_view,orient='vertical',command=canvas.yview);canvas.configure(yscrollcommand=vs.set)
        canvas.grid(row=0,column=0,sticky='nsew');vs.grid(row=0,column=1,sticky='ns')
        left=ttk.Frame(canvas)
        item=canvas.create_window((0,0),window=left,anchor='nw')
        def resize(event=None):
            canvas.itemconfigure(item,width=max(1,canvas.winfo_width()))
            canvas.configure(scrollregion=canvas.bbox('all'))
        left.bind('<Configure>',resize);canvas.bind('<Configure>',resize)
        canvas.bind('<MouseWheel>',lambda e:canvas.yview_scroll(-1 if e.delta>0 else 1,'units'))
        self.research_vars={k:tk.StringVar() for k in ('query','diagnosis','pool','sample_type','sex','assay_result','age_min','age_max','date_from','date_to','variant_query')}
        self.dataset_var=tk.StringVar(value='Raporlanan varyantlar')
        ttk.Label(left,text='Kohort / \u00e7al\u0131\u015fma se\u00e7imi',font=('Segoe UI',12,'bold')).pack(anchor='w',pady=(0,6))
        form=ttk.Frame(left);form.pack(fill='x');form.columnconfigure(1,weight=1)
        entries=[('Ad / MP','query'),('Tan\u0131 i\u00e7erir','diagnosis'),('POOL (iste\u011fe ba\u011fl\u0131)','pool'),('DNA / RNA','sample_type'),('Cinsiyet','sex')]
        self.research_boxes={}
        for i,(title,key) in enumerate(entries):
            ttk.Label(form,text=title).grid(row=i,column=0,sticky='w',pady=3,padx=(0,5))
            if key in ('pool','sample_type','sex'):
                values=['T\u00fcm\u00fc'] if key=='pool' else ['T\u00fcm\u00fc','DNA','RNA'] if key=='sample_type' else ['T\u00fcm\u00fc','Erkek','Kad\u0131n']
                w=ttk.Combobox(form,textvariable=self.research_vars[key],values=values,state='readonly',width=21);self.research_vars[key].set('T\u00fcm\u00fc');self.research_boxes[key]=w
            else:w=ttk.Entry(form,textvariable=self.research_vars[key],width=23)
            w.grid(row=i,column=1,sticky='ew',pady=3)
        ttk.Label(form,text='Ya\u015f aral\u0131\u011f\u0131').grid(row=6,column=0,sticky='w',pady=3)
        age=ttk.Frame(form);age.grid(row=6,column=1,sticky='ew',pady=3)
        ttk.Entry(age,textvariable=self.research_vars['age_min'],width=7).pack(side='left');ttk.Label(age,text=' \u2013 ').pack(side='left');ttk.Entry(age,textvariable=self.research_vars['age_max'],width=7).pack(side='left')
        for i,key,lab in ((7,'date_from','\u00c7al\u0131\u015fma ba\u015flang\u0131\u00e7'),(8,'date_to','\u00c7al\u0131\u015fma biti\u015f')):
            ttk.Label(form,text=lab).grid(row=i,column=0,sticky='w',pady=3);ttk.Entry(form,textvariable=self.research_vars[key],width=23).grid(row=i,column=1,sticky='ew',pady=3)
        ttk.Label(left,text='Tarih: GG.AA.YYYY veya YYYY-AA-GG\nYa\u015f: \u00e7al\u0131\u015fma tarihi / eski kay\u0131t; eksik ya\u015f aral\u0131\u011fa al\u0131nmaz.',foreground='#667085',wraplength=330).pack(anchor='w',pady=5)
        ttk.Label(left,text='Varyant veri k\u00fcmesi').pack(anchor='w')
        ttk.Combobox(left,textvariable=self.dataset_var,values=list(DATASETS),state='readonly',width=35).pack(fill='x',pady=3)
        ttk.Label(left,text='Gen / cDNA / protein filtresi').pack(anchor='w',pady=(5,0))
        ttk.Entry(left,textvariable=self.research_vars['variant_query']).pack(fill='x',pady=3)
        self.exclude_reruns=tk.BooleanVar(value=False);self.only_matching=tk.BooleanVar(value=False)
        ttk.Checkbutton(left,text='Tekrar \u00e7al\u0131\u015fmalar\u0131 d\u0131\u015fla',variable=self.exclude_reruns).pack(anchor='w')
        ttk.Checkbutton(left,text='Yaln\u0131z varyant e\u015fle\u015fen \u00f6rnekler',variable=self.only_matching).pack(anchor='w')
        buttons=ttk.Frame(left);buttons.pack(fill='x',pady=7)
        ttk.Button(buttons,text='Kohortu olu\u015ftur',command=self._new_research).pack(side='left')
        ttk.Button(buttons,text='Temizle',command=self._reset_research).pack(side='right')
        saved=ttk.Frame(sidebar);saved.grid(row=1,column=0,sticky='nsew',pady=(8,0))
        saved.columnconfigure(0,weight=1);saved.rowconfigure(1,weight=1)
        left=saved
        ttk.Label(left,text='Kay\u0131tl\u0131 \u00e7al\u0131\u015fmalar',font=('Segoe UI',10,'bold')).grid(row=0,column=0,sticky='w')
        self.studies_table=DataTable(left,[('name','\u00c7ALI\u015eMA',185),('revision','S\u00dcR\u00dcM',65)],height=6,double=lambda r:self._load_study(r))
        self.studies_table.grid(row=1,column=0,sticky='nsew',pady=4)
        sb=ttk.Frame(left);sb.grid(row=2,column=0,sticky='ew')
        self.load_study_button=ttk.Button(sb,text='Se\u00e7ili \u00e7al\u0131\u015fmay\u0131 a\u00e7',command=lambda:self._load_study(self.studies_table.selected()))
        self.load_study_button.pack(side='left')
        self.study_status=tk.StringVar(value='Dinamik kohort \u00b7 t\u00fcm ar\u015fiv')
        ttk.Label(right,textvariable=self.study_status,font=('Segoe UI',12,'bold'),wraplength=1050).grid(row=0,column=0,sticky='ew',pady=(0,7))
        stats=ttk.Frame(right);stats.grid(row=1,column=0,sticky='ew',pady=(0,7));self.kpis={}
        for i,(key,title) in enumerate([('patients','HASTA GRUBU'),('technical','TEKN\u0130K \u00c7ALI\u015eMA'),('variants','VARYANT G\u00d6ZLEM\u0130'),('matching','E\u015eLE\u015eEN HASTA')]):
            frame=ttk.LabelFrame(stats,text=title,padding=9);frame.grid(row=0,column=i,sticky='ew',padx=3);stats.columnconfigure(i,weight=1)
            var=tk.StringVar(value='0');self.kpis[key]=var;ttk.Label(frame,textvariable=var,font=('Segoe UI',19,'bold'),foreground='#164e63').pack(anchor='w')
        self.research_summary_var=tk.StringVar()
        ttk.Label(right,textvariable=self.research_summary_var,foreground='#52606d',wraplength=1060).grid(row=2,column=0,sticky='ew',pady=(0,7))
        tabs=ttk.Notebook(right);tabs.grid(row=3,column=0,sticky='nsew')
        samples=ttk.Frame(tabs,padding=4);genes=ttk.Frame(tabs,padding=4);variants=ttk.Frame(tabs,padding=4)
        tabs.add(samples,text='Kohort / Teknik Kay\u0131tlar');tabs.add(genes,text='Gen Da\u011f\u0131l\u0131m\u0131');tabs.add(variants,text='Varyant Da\u011f\u0131l\u0131m\u0131')
        self.cohort_table=DataTable(samples,[('name','HASTA',175),('mp','MP',115),('dx','TANI',230),('date','\u00c7ALI\u015eMA',110),('type','T\u0130P',55),('age','YA\u015e',55),('pool','POOL',100),('reported','RAPORLANAN',105),('match','E\u015eLE\u015eEN',85)],height=18,double=self._observation_open_patient)
        self.cohort_table.pack(fill='both',expand=True)
        self.gene_table=DataTable(genes,[('gene','GEN (KAYNAK ET\u0130KET\u0130)',240),('patients','HASTA GRUBU',180),('samples','TEKN\u0130K KAYIT',180)],height=18)
        self.gene_table.pack(fill='both',expand=True)
        self.research_variants=DataTable(variants,[('gene','GEN',100),('change','DE\u011e\u0130\u015e\u0130KL\u0130K',225),('patients','HASTA',85),('samples','TEKN\u0130K KAYIT',110),('pools','POOL',85),('key','VARYANT ANAHTARI',450)],height=18,double=self._show_variant_history)
        self.research_variants.pack(fill='both',expand=True)
        self.result_limit_label=tk.StringVar();ttk.Label(right,textvariable=self.result_limit_label,foreground='#667085').grid(row=4,column=0,sticky='ew',pady=(5,0))
        ttk.Label(right,text='Kay\u0131t yok \u2260 negatif. Bu say\u0131mlar prevalans de\u011fildir. Ayn\u0131 hasta farkl\u0131 \u00e7al\u0131\u015fmalarda hasta say\u0131s\u0131n\u0131 art\u0131rmaz.',foreground='#8a5a00',wraplength=1060).grid(row=6,column=0,sticky='ew',pady=(3,5))
        footer=ttk.Frame(right);footer.grid(row=7,column=0,sticky='ew')
        ttk.Button(footer,text='Kohortu \u00e7al\u0131\u015fma olarak kaydet',command=self._save_study_dialog).pack(side='left')
        self.export_button=ttk.Button(footer,text='Ara\u015ft\u0131rma ZIP / CSV d\u0131\u015fa aktar',command=self._export_research)
        self.export_button.pack(side='left',padx=6)
        self._initial_split(pan,.26)

    def _update_filter_choices(self):
        pools=sorted({s['pool_group'] for s in self.snap['records'].values()})
        self.research_boxes['pool'].configure(values=['T\u00fcm\u00fc']+pools)

    def _criteria(self):
        c={k:v.get().strip() for k,v in self.research_vars.items()}
        c['exclude_reruns']=self.exclude_reruns.get();c['only_matching']=self.only_matching.get()
        for key in ('date_from','date_to'):
            if c[key] and not date_only(c[key]):raise ValueError('Tarih ge\u00e7ersiz: '+c[key])
        for key in ('age_min','age_max'):
            if c[key] and (number(c[key]) is None or not 0<=number(c[key])<=130):raise ValueError('Ya\u015f s\u0131n\u0131r\u0131 ge\u00e7ersiz.')
        if c['age_min'] and c['age_max'] and number(c['age_min'])>number(c['age_max']):raise ValueError('Alt ya\u015f s\u0131n\u0131r\u0131 \u00fcst s\u0131n\u0131rdan b\u00fcy\u00fck.')
        if c['date_from'] and c['date_to'] and date_only(c['date_from'])>date_only(c['date_to']):raise ValueError('Ba\u015flang\u0131\u00e7 tarihi biti\u015ften sonra.')
        return c

    def _new_research(self):
        self.active_study=None;self._apply_research()

    def _reset_research(self):
        self.active_study=None
        for k,v in self.research_vars.items():v.set('T\u00fcm\u00fc' if k in ('pool','sex','sample_type') else '')
        self.exclude_reruns.set(False);self.only_matching.set(False);self.dataset_var.set('Raporlanan varyantlar');self._apply_research()

    def _apply_research(self):
        if not self.snap:return
        try:
            criteria=self.active_study['criteria'] if self.active_study else self._criteria()
            ds=self.active_study['dataset'] if self.active_study else DATASETS[self.dataset_var.get()]
            records=filter_records(self.snap,criteria,self.active_study['record_keys'] if self.active_study else None)
            variants=select_variants(self.snap,records,ds,criteria.get('variant_query',''))
            if criteria.get('only_matching') and not self.active_study:
                matching={v['record_key'] for v in variants};records=[s for s in records if s['record_key'] in matching]
            self.cohort=records;self.cohort_variants=variants;self.applied_criteria=criteria;self.applied_dataset=ds
            summary=research_summary(self.snap,records,variants,ds)
            self.kpis['patients'].set(str(summary['cohort_patient_groups']));self.kpis['technical'].set(str(len(records)));self.kpis['variants'].set(str(len(variants)));self.kpis['matching'].set(str(summary['patients_with_matching_records']))
            title=f"{self.active_study['name']} \u00b7 v{self.active_study['revision']} \u00b7 Sabit \u00fcyelik / g\u00fcncel sonu\u00e7lar" if self.active_study else 'Dinamik kohort \u00b7 t\u00fcm ar\u015fivden filtrelenir'
            self.study_status.set(title)
            self.research_summary_var.set(f"{summary['distinct_variant_keys']} farkl\u0131 anahtar \u00b7 {summary['no_matching_variant_record']} teknik kay\u0131tta se\u00e7ili varyant kayd\u0131 yok \u00b7 {summary['indexed_technical_records']}/{len(records)} onkojenik indeksli \u00b7 {summary['identity_warning_groups']} kimlik uyar\u0131s\u0131")
            counts={}
            for v in variants:counts[v['record_key']]=counts.get(v['record_key'],0)+1
            self.cohort_table.fill(records,lambda s:(s['full_name'] or '(isim yok)',s['patient_id'],s['diagnosis'],shown_date(s['run_date']),s['sample_type'],s['age'] if s['age'] is not None else '',s['pool_group'],s['reported_count'],counts.get(s['record_key'],0)))
            self.gene_table.fill(summary['gene_counts'],lambda r:(r['gene'],r['patient_groups'],r['technical_records']))
            catalogue=variant_catalogue(variants,self.snap['records'])
            self.research_variants.fill(catalogue[:1000],lambda r:(r['gene'],r['change'],r['patient_count'],r['sample_count'],r['pool_count'],r['variant_key']))
            self.result_limit_label.set(f'Varyant tablosu: ilk {min(len(catalogue),1000)} / {len(catalogue)} anahtar. ZIP t\u00fcm e\u015fle\u015fmeleri i\u00e7erir.')
        except Exception as e:messagebox.showerror('Ara\u015ft\u0131rma',str(e),parent=self)

    def _populate_studies(self):
        studies=sorted(self.snap['studies'],key=lambda r:r['id'],reverse=True)
        self.studies_table.fill(studies,lambda s:(s['name'],s['revision']))

    def _load_study(self,row):
        if not row:return
        try:
            self.active_study=self.store.study(row['id'])
            for k,v in self.research_vars.items():v.set(self.active_study['criteria'].get(k,''))
            self.exclude_reruns.set(bool(self.active_study['criteria'].get('exclude_reruns')));self.only_matching.set(bool(self.active_study['criteria'].get('only_matching')))
            self.dataset_var.set(next(k for k,v in DATASETS.items() if v==self.active_study['dataset']))
            self._apply_research()
        except Exception as e:messagebox.showerror('\u00c7al\u0131\u015fma',str(e),parent=self)

    def _save_study_dialog(self):
        if not self.cohort:
            messagebox.showinfo('\u00c7al\u0131\u015fma','\u00d6nce bir kohort olu\u015fturun.',parent=self);return
        name=simpledialog.askstring('\u00c7al\u0131\u015fma ad\u0131','Kohort \u00fcyeleri sabit kaydedilir. Ayn\u0131 ad yeni s\u00fcr\u00fcm olu\u015fturur.\nSonu\u00e7/kararlar sonradan g\u00fcncellenebilir; tarihli ZIP ile an\u0131 dondurun.',parent=self)
        if not name:return
        try:
            sid=self.store.save_study(name,self.applied_criteria,self.applied_dataset,[s['record_key'] for s in self.cohort])
            self.active_study=self.store.study(sid);self.refresh()
        except Exception as e:messagebox.showerror('\u00c7al\u0131\u015fma',str(e),parent=self)

    def _export_research(self):
        if not self.cohort:
            messagebox.showinfo('Ara\u015ft\u0131rma aktar\u0131m\u0131','\u00d6nce bir kohort olu\u015fturun.',parent=self);return
        root=self.base/'RESEARCH_EXPORT';root.mkdir(exist_ok=True)
        path=filedialog.asksaveasfilename(parent=self,title='Maskeli ara\u015ft\u0131rma paketi',initialdir=root,
                                        initialfile='CALISMA_'+dt.datetime.now().strftime('%Y%m%d_%H%M%S')+'.zip',defaultextension='.zip',filetypes=[('ZIP paketi','*.zip')])
        if not path:return
        try:
            p=export_study_zip(self.store,self.snap,self.cohort,self.cohort_variants,self.applied_criteria,self.applied_dataset,Path(path),self.active_study)
            messagebox.showinfo('Ara\u015ft\u0131rma aktar\u0131m\u0131',f'{len(self.cohort)} teknik kay\u0131t / {len(self.cohort_variants)} varyant g\u00f6zlemi aktar\u0131ld\u0131.\n\nAdlar maskeli; TC, do\u011fum tarihi ve serbest rapor metni yok. Bu yine sa\u011fl\u0131k verisidir.\n\n{p}',parent=self)
        except Exception as e:messagebox.showerror('Aktar\u0131m',str(e),parent=self)
