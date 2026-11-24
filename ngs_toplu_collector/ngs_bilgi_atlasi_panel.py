# -*- coding: utf-8 -*-
"""NGS Bilgi Atlasi integrated Tkinter tab, no external packages/network."""
from __future__ import annotations

import threading
import traceback
import webbrowser
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from ngs_bilgi_atlasi_store import (
    AtlasStore, CATEGORIES, SOURCE_KINDS, STATUSES, seed_technical_memory,
)

PAGE_SIZE = 24
BG = '#f4f7fb'
TEXT = '#18324c'
MUTED = '#5c6b7a'


def _time_label(value):
    return str(value or '').replace('T', ' ')[:19]


class BilgiAtlasiPanel(ttk.Frame):
    """Local card feed; only writes to the separate BILGI_ATLASI/atlas.sqlite3."""

    def __init__(self, master, base, version='0.4.95'):
        super().__init__(master)
        self.base = Path(base)
        self.store = AtlasStore(self.base / 'BILGI_ATLASI')
        seed_technical_memory(self.store)
        self.version = version
        self.page_offset = 0
        self.total = 0
        self.loading = False
        self._last_query = None
        self._debounce = None
        self._indexing = False
        self._card_expanded = set()
        self._build()
        self.after(60,self.refresh_feed)
        self.after(350,lambda:self.index_code(background=True))

    def _build(self):
        top = tk.Frame(self,bg='#17324d')
        top.pack(fill='x')
        tk.Label(top,text='NGS Bilgi Atlası',font=('Segoe UI',17,'bold'),
                 bg='#17324d',fg='white').pack(side='left',padx=(18,12),pady=14)
        tk.Label(top,text='Kaynak · Nasıl elde edildi · Teknik hafıza · Değişiklik geçmişi',
                 font=('Segoe UI',10),bg='#17324d',fg='#cbd8e7').pack(side='left')
        toolbar = ttk.Frame(self,padding=(12,10))
        toolbar.pack(fill='x')
        self.query_var=tk.StringVar()
        self.query_var.trace_add('write',lambda *_:self._search_later())
        ttk.Label(toolbar,text='Ara:').pack(side='left',padx=(2,5))
        ttk.Entry(toolbar,textvariable=self.query_var,width=34).pack(side='left',padx=(0,8))
        self.category=tk.StringVar(value='Tümü')
        self.source=tk.StringVar(value='Tümü')
        categories=['Tümü']+list(CATEGORIES)
        sources=['Tümü']+list(SOURCE_KINDS)
        ttk.Combobox(toolbar,textvariable=self.category,values=categories,
                     state='readonly',width=19).pack(side='left',padx=(0,7))
        ttk.Combobox(toolbar,textvariable=self.source,values=sources,
                     state='readonly',width=13).pack(side='left',padx=(0,8))
        self.category.trace_add('write',lambda *_:self._search_later())
        self.source.trace_add('write',lambda *_:self._search_later())
        ttk.Button(toolbar,text='+ Yeni bilgi',command=self.new_entry).pack(side='left',padx=4)
        ttk.Button(toolbar,text='Kod haritasını yenile',command=lambda:self.index_code(background=True)).pack(side='left',padx=4)
        ttk.Button(toolbar,text='Hasta bazlı sonuçlar',command=self.open_patient_view).pack(side='right',padx=4)

        row = ttk.Frame(self,padding=(12,0,12,8))
        row.pack(fill='x')
        self.stat_var=tk.StringVar(value='Bilgiler hazırlanıyor...')
        ttk.Label(row,textvariable=self.stat_var).pack(side='left')
        ttk.Button(row,text='Markdown',command=lambda:self.export('md')).pack(side='right',padx=4)
        ttk.Button(row,text='JSON',command=lambda:self.export('json')).pack(side='right',padx=4)
        ttk.Button(row,text='SQLite yedeği',command=self.backup).pack(side='right',padx=4)
        ttk.Label(row,text='Not: hasta kimlik/veri girmeyin; YZ bilgisi klinik onay değildir.',
                  foreground='#9d3c17').pack(side='right',padx=13)

        shell=ttk.Frame(self)
        shell.pack(fill='both',expand=True,padx=12,pady=(0,3))
        self.canvas=tk.Canvas(shell,bg=BG,highlightthickness=0)
        self.scrollbar=ttk.Scrollbar(shell,orient='vertical',command=self._scroll_cmd)
        self.canvas.configure(yscrollcommand=self._scroll_changed)
        self.scrollbar.pack(side='right',fill='y')
        self.canvas.pack(side='left',fill='both',expand=True)
        self.feed=tk.Frame(self.canvas,bg=BG)
        self._feed_id=self.canvas.create_window((0,0),window=self.feed,anchor='nw')
        self.feed.bind('<Configure>',self._resize_scroll)
        self.canvas.bind('<Configure>',self._resize_width)
        self.canvas.bind('<MouseWheel>',self._wheel)
        self.canvas.bind('<Button-4>',lambda ev:self._wheel_step(-4))
        self.canvas.bind('<Button-5>',lambda ev:self._wheel_step(4))
        self.feed.bind('<MouseWheel>',self._wheel)
        self.load_more_button=ttk.Button(self,text='Daha fazla kayıt göster',command=self.load_more)
        self.load_more_button.pack(pady=(2,7))
        self.patient_area=ttk.Frame(self)
        self.patient_widget=None
        self._atlas_list_row=row
        self._atlas_feed_shell=shell

    def open_patient_view(self):
        if self.patient_widget is None:
            try:
                from ngs_bilgi_atlasi_hasta import PatientAtlasWorkspace
                self.patient_widget=PatientAtlasWorkspace(self.patient_area,self.base,self.store,
                                                           on_back=self.close_patient_view)
                self.patient_widget.pack(fill='both',expand=True)
            except Exception as ex:
                from tkinter import messagebox
                messagebox.showerror('Bilgi Atlası', 'Hasta bölümü açılamadı: ' + str(ex),parent=self)
                return
        else:
            self.patient_widget.refresh()
        self._atlas_list_row.pack_forget()
        self._atlas_feed_shell.pack_forget()
        self.load_more_button.pack_forget()
        self.patient_area.pack(fill='both',expand=True,padx=12,pady=(0,8))

    def close_patient_view(self):
        self.patient_area.pack_forget()
        self._atlas_list_row.pack(fill='x')
        self._atlas_feed_shell.pack(fill='both',expand=True,padx=12,pady=(0,3))
        if self.page_offset < self.total:
            self.load_more_button.pack(pady=(2,7))

    def _resize_scroll(self,event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox('all'))

    def _resize_width(self,event):
        self.canvas.itemconfigure(self._feed_id,width=event.width)

    def _scroll_cmd(self,*args):
        self.canvas.yview(*args)
        self._schedule_load_if_bottom()

    def _scroll_changed(self,first,last):
        self.scrollbar.set(first,last)
        if float(last) > .95:
            self._schedule_load_if_bottom()

    def _schedule_load_if_bottom(self):
        if not self.loading and self.page_offset < self.total:
            self.after_idle(self._maybe_load_more)

    def _maybe_load_more(self):
        if not self.loading and self.page_offset < self.total:
            if self.canvas.yview()[1] > .94:
                self.load_more()

    def _wheel(self,event):
        if getattr(event,'delta',0):
            self.canvas.yview_scroll(int(-event.delta / 120) or (-1 if event.delta > 0 else 1),'units')
            self._schedule_load_if_bottom()
        return 'break'

    def _wheel_step(self,n):
        self.canvas.yview_scroll(n,'units')
        self._schedule_load_if_bottom()
        return 'break'

    def _wheel_binding(self,widget):
        widget.bind('<MouseWheel>',self._wheel)
        widget.bind('<Button-4>',lambda e:self._wheel_step(-3))
        widget.bind('<Button-5>',lambda e:self._wheel_step(3))

    def _search_later(self):
        if self._debounce is not None:
            self.after_cancel(self._debounce)
        self._debounce=self.after(260,self.refresh_feed)

    def refresh_feed(self):
        self._debounce=None
        self.loading=True
        self.page_offset=0
        self._card_expanded.clear()
        for child in list(self.feed.winfo_children()):
            child.destroy()
        self.canvas.yview_moveto(0)
        self.loading=False
        self.load_more()

    def load_more(self):
        if self.loading:
            return
        self.loading=True
        try:
            entries,total=self.store.list_entries(query=self.query_var.get(),
                    category=self.category.get(),source_kind=self.source.get(),
                    limit=PAGE_SIZE,offset=self.page_offset)
            self.total=total
            for entry in entries:
                self._render_card(entry)
            self.page_offset += len(entries)
            counts = self.store.counts()
            self.stat_var.set(f'{total} eşleşen kayıt · {counts["curated"]} açıklama / bilgi · '
                              f'{counts["code"]} otomatik kod kaydı · {counts["notes"]} ek not')
            if not entries and not self.page_offset:
                label=ttk.Label(self.feed,text='Bu filtreye uygun kayıt yok. + Yeni bilgi ile ekleyebilirsiniz.',
                                font=('Segoe UI',11))
                label.pack(padx=18,pady=30)
            if self.page_offset < self.total:
                self.load_more_button.pack(pady=(2,7))
            else:
                self.load_more_button.pack_forget()
        except Exception as ex:
            messagebox.showerror('Bilgi Atlası',f'Atlas kayıtları okunamadı: {ex}',parent=self)
        finally:
            self.loading=False

    def _render_card(self,item):
        frame = tk.Frame(self.feed,bg='#fff',bd=1,relief='solid',highlightbackground='#d8e3ef')
        frame.pack(fill='x',padx=(2,10),pady=(4,8),ipadx=11,ipady=8)
        for widget in (frame,): self._wheel_binding(widget)
        head = tk.Frame(frame,bg='#fff')
        head.pack(fill='x',padx=10,pady=(5,2))
        title=tk.Label(head,text=item['title'],bg='#fff',fg=TEXT,
                       anchor='w',justify='left',font=('Segoe UI',11,'bold'),wraplength=880)
        title.pack(side='left',fill='x',expand=True,anchor='w')
        self._wheel_binding(title)
        tk.Label(head,text=item['status'],bg='#eff3f8',fg='#304b62',
                 padx=9,pady=3,font=('Segoe UI',9)).pack(side='right')
        subt=f"{item['category']}   ·   {item['source_kind']}   ·   {_time_label(item['updated_at'])}"
        meta=tk.Label(frame,text=subt,bg='#fff',fg=MUTED,anchor='w',font=('Segoe UI',9))
        meta.pack(fill='x',padx=11)
        self._wheel_binding(meta)
        summary=item['summary'] or 'Açıklama henüz eklenmemiş.'
        sumlab=tk.Label(frame,text=summary[:630]+('...' if len(summary)>630 else ''),
                        bg='#fff',fg=TEXT,justify='left',anchor='w',wraplength=1100,
                        font=('Segoe UI',10))
        sumlab.pack(fill='x',padx=11,pady=(5,6))
        self._wheel_binding(sumlab)
        btns=tk.Frame(frame,bg='#fff')
        btns.pack(fill='x',padx=10)
        more=ttk.Button(btns,text='Açıklama / kaynaklar ▾')
        more.pack(side='left',padx=2)
        ttk.Button(btns,text='Not ekle',command=lambda rid=item['id']:self.new_note(rid)).pack(side='left',padx=2)
        ttk.Button(btns,text='Kaynak ekle',command=lambda rid=item['id']:self.new_source(rid)).pack(side='left',padx=2)
        if not item['generated']:
            ttk.Button(btns,text='Düzenle',command=lambda rid=item['id']:self.new_entry(rid)).pack(side='left',padx=2)
        ttk.Button(btns,text='Kopyala',command=lambda rid=item['id']:self.copy_record(rid)).pack(side='right',padx=2)
        details=tk.Frame(frame,bg='#f9fbfd')
        def toggle():
            if details.winfo_manager():
                details.pack_forget()
                more.configure(text='Açıklama / kaynaklar ▾')
            else:
                self._fill_details(details,item)
                details.pack(fill='x',padx=10,pady=(9,3))
                more.configure(text='Açıklamayı kapat ▴')
        more.configure(command=toggle)

    def _text_label(self,parent,title,text):
        tk.Label(parent,text=title,bg='#f9fbfd',fg='#27435c',font=('Segoe UI',9,'bold'),
                 anchor='w').pack(fill='x',padx=8,pady=(8,2))
        widget=tk.Label(parent,text=str(text or '—'),bg='#f9fbfd',fg=TEXT,
                         justify='left',anchor='w',wraplength=1040,font=('Segoe UI',10))
        widget.pack(fill='x',padx=8,pady=(0,3))
        self._wheel_binding(widget)

    def _fill_details(self,body,item):
        for ch in body.winfo_children(): ch.destroy()
        self._text_label(body,'AÇIKLAMA',item['details'] or item['summary'])
        self._text_label(body,'KAYNAK · ELDE EDİLME YÖNTEMİ',
                         f"Kaynak: {item['source_label'] or 'Belirtilmedi'}\n"
                         f"Konum / bağlantı: {item['source_uri'] or 'Belirtilmedi'}\n"
                         f"Nasıl alındı: {item['acquisition'] or 'Belirtilmedi'}\n"
                         f"Kaynak tarihi: {item['source_date'] or 'Belirtilmedi'}\n"
                         f"Kaynak sürümü: {item['source_version'] or 'Belirtilmedi'}\n"
                         f"Kod modülü: {item['linked_module'] or '—'}\n"
                         f"Yazar / editör: {item['author'] or 'Belirtilmedi'}\n"
                         f"Etiketler: {item['tags'] or '—'}\n"
                         f"Kayıt sürümü: {item['revision']}")
        if item['source_uri'].startswith(('https://','http://')):
            ttk.Button(body,text='Web kaynağını aç',
                       command=lambda u=item['source_uri']:webbrowser.open(u)).pack(anchor='w',padx=8,pady=5)
        elif item['source_kind'] == 'Kod' or item['linked_module']:
            ttk.Button(body,text='Kaynak kodunu görüntüle',
                       command=lambda e=item:self.view_code_source(e)).pack(anchor='w',padx=8,pady=5)
        sources=self.store.get_sources(item['id'])
        if sources:
            s='\n\n'.join(f"{x['source_kind']} | {x['label']} | {x['uri']}\n"
                          f"Elde edilme: {x['acquisition']} | {x['status']}" for x in sources)
            self._text_label(body,'EK KAYNAKLAR',s)
        notes=self.store.get_notes(item['id'])
        if notes:
            s='\n\n'.join(f"{x['kind']} | {x['created_at']} | {x['author']}\n"
                          f"{x['content']}\nKaynak: {x['source_uri']} | Yöntem: {x['acquisition']}" for x in notes)
            self._text_label(body,'EK NOTLAR (DEĞİŞTİRİLEMEZ, YENİSİ EKLENİR)',s)
        history=self.store.get_history(item['id'])
        if history:
            s='\n'.join(f"r{x['revision']} | {x['event']} | {x['edited_at']} | "
                        f"{x['actor']} | {x['reason']}" for x in history[:15])
            self._text_label(body,'DEĞİŞİKLİK GEÇMİŞİ',s)

    def view_code_source(self,entry):
        """Read-only source window, constrained to top-level local .py files."""
        import re
        ref=str(entry.get('source_uri') or entry.get('linked_module') or '')
        matched=re.fullmatch(r'([^/\\]+\.pyw?)(?:#L(\d+)-L(\d+))?',ref)
        if not matched:
            messagebox.showinfo('Kod kaynağı','Kaynak bir yerel Python dosyası olarak belirtilmemiş.',parent=self)
            return
        file=self.base/matched.group(1)
        if not file.is_file() or file.resolve().parent!=self.base.resolve():
            messagebox.showwarning('Kod kaynağı','Kaynak kodu belirtilen yerde bulunamadı.',parent=self)
            return
        lines=file.read_text(encoding='utf-8-sig',errors='replace').splitlines()
        start=int(matched.group(2) or 1)
        end=min(len(lines), int(matched.group(3) or start+90), start+240)
        start=max(1,start)
        win=self._dialog('Kod kaynağı · '+file.name, '1050x690')
        frame=ttk.Frame(win,padding=10)
        frame.pack(fill='both',expand=True)
        ttk.Label(frame,text=f'{file.name} · satır {start}-{end} · salt okunur',
                  font=('Segoe UI',10,'bold')).pack(anchor='w',pady=(0,7))
        editor=tk.Text(frame,wrap='none',font=('Consolas',10))
        editor.pack(side='left',fill='both',expand=True)
        sb=ttk.Scrollbar(frame,orient='vertical',command=editor.yview)
        sb.pack(side='right',fill='y')
        editor.configure(yscrollcommand=sb.set)
        editor.insert('1.0','\n'.join(f'{i:5d}: {lines[i-1]}' for i in range(start,end+1)))
        editor.configure(state='disabled')

    def _dialog(self,title,geometry='850x720'):
        dialog=tk.Toplevel(self)
        dialog.title(title)
        dialog.geometry(geometry)
        dialog.minsize(690,530)
        dialog.transient(self.winfo_toplevel())
        dialog.grab_set()
        return dialog

    def _labeled_input(self,parent,row,label,value='',options=None,width=65):
        ttk.Label(parent,text=label).grid(row=row,column=0,sticky='nw',padx=9,pady=7)
        var=tk.StringVar(value=value)
        widget=(ttk.Combobox(parent,textvariable=var,state='readonly',values=options,width=width)
                if options else ttk.Entry(parent,textvariable=var,width=width))
        widget.grid(row=row,column=1,sticky='ew',padx=9,pady=5)
        return var

    def new_entry(self,record_id=None):
        old=self.store.get_entry(record_id) if record_id else None
        if old and old['generated']:
            messagebox.showinfo('Bilgi Atlası','Kod sembolü değiştirilemez; not ekleyebilirsiniz.',parent=self)
            return
        dialog=self._dialog('Kaydı düzenle' if old else 'Yeni bilgi / kaynak notu', '910x790')
        pane=ttk.Frame(dialog,padding=12)
        pane.pack(fill='both',expand=True)
        pane.columnconfigure(1,weight=1)
        d=old or {}
        title=self._labeled_input(pane,0,'Başlık',d.get('title',''))
        category=self._labeled_input(pane,1,'Kategori',d.get('category','Çalışma notu'),CATEGORIES)
        source=self._labeled_input(pane,2,'Kaynak türü',d.get('source_kind','Manuel'),SOURCE_KINDS)
        source_label=self._labeled_input(pane,3,'Kaynağın adı',d.get('source_label',''))
        source_uri=self._labeled_input(pane,4,'URL / dosya yolu',d.get('source_uri',''))
        acquired=self._labeled_input(pane,5,'NASIL ELDE EDİLDİ? *',d.get('acquisition',''))
        date=self._labeled_input(pane,6,'Kaynak tarihi',d.get('source_date',''))
        version=self._labeled_input(pane,7,'Sürüm / tarih',d.get('source_version',''))
        status=self._labeled_input(pane,8,'Doğrulama durumu',d.get('status','Doğrulama bekliyor'),STATUSES[:-1])
        tags=self._labeled_input(pane,9,'Etiketler',d.get('tags',''))
        author=self._labeled_input(pane,10,'Kayıt sahibi',d.get('author',''))
        summary=self._labeled_input(pane,11,'Kısa özet',d.get('summary',''))
        ttk.Label(pane,text='Açıklama / bilimsel sentez').grid(row=12,column=0,sticky='nw',padx=9,pady=6)
        text=tk.Text(pane,height=9,wrap='word',font=('Segoe UI',10))
        text.grid(row=12,column=1,sticky='nsew',padx=9,pady=6)
        text.insert('1.0',d.get('details',''))
        pane.rowconfigure(12,weight=1)
        why=self._labeled_input(pane,13,'Değişiklik nedeni', '') if old else None
        foot=ttk.Frame(dialog,padding=(12,0,12,12))
        foot.pack(fill='x')
        ttk.Label(foot,text='Bu bölüm hasta kimlik/veri kaydı için değildir.',foreground='#9d3c17').pack(side='left')
        def save():
            try:
                val={
                    'id':record_id, 'title':title.get(),'category':category.get(),
                    'source_kind':source.get(),'source_label':source_label.get(),
                    'source_uri':source_uri.get(),'acquisition':acquired.get(),
                    'source_date':date.get(),'source_version':version.get(),
                    'status':status.get(),'tags':tags.get(),'author':author.get(),
                    'summary':summary.get(),'details':text.get('1.0','end-1c'),
                    'linked_module':d.get('linked_module','')
                }
                self.store.save_entry(val,reason=why.get() if why else 'Yeni bilgi')
            except Exception as ex:
                messagebox.showerror('Kayıt yapılamadı',str(ex),parent=dialog)
                return
            dialog.destroy()
            self.refresh_feed()
        ttk.Button(foot,text='Kaydet',command=save).pack(side='right',padx=5)
        ttk.Button(foot,text='İptal',command=dialog.destroy).pack(side='right',padx=5)

    def new_note(self,record_id):
        dialog=self._dialog('Kaynağa ek not ekle','750x540')
        box=ttk.Frame(dialog,padding=12)
        box.pack(fill='both',expand=True)
        box.columnconfigure(1,weight=1)
        kind=self._labeled_input(box,0,'Not türü', 'Manuel', SOURCE_KINDS)
        url=self._labeled_input(box,1,'Kaynak URL / yol')
        method=self._labeled_input(box,2,'Nasıl elde edildi? *')
        author=self._labeled_input(box,3,'Notu ekleyen')
        ttk.Label(box,text='Not').grid(row=4,column=0,sticky='nw',padx=9,pady=7)
        text=tk.Text(box,height=12,wrap='word',font=('Segoe UI',10))
        text.grid(row=4,column=1,sticky='nsew',padx=9,pady=7)
        box.rowconfigure(4,weight=1)
        ttk.Label(box,text='Notlar append-only tutulur; önceki notlar silinmez.',foreground=MUTED).grid(row=5,column=1,sticky='w')
        def save():
            try:
                self.store.add_note(record_id,text.get('1.0','end-1c'),kind.get(),url.get(),method.get(),author.get())
            except Exception as ex:
                messagebox.showerror('Not eklenmedi',str(ex),parent=dialog)
                return
            dialog.destroy();self.refresh_feed()
        ttk.Button(box,text='Kaydet',command=save).grid(row=6,column=1,sticky='e',pady=8)

    def new_source(self,record_id):
        dialog=self._dialog('Ek kaynak kaydı','770x430')
        box=ttk.Frame(dialog,padding=12)
        box.pack(fill='both',expand=True)
        box.columnconfigure(1,weight=1)
        kind=self._labeled_input(box,0,'Kaynak türü', 'Web/PubMed', SOURCE_KINDS)
        label=self._labeled_input(box,1,'Kaynak adı *')
        url=self._labeled_input(box,2,'URL / dosya yolu')
        method=self._labeled_input(box,3,'Nasıl elde edildi? *')
        status=self._labeled_input(box,4,'Doğrulama durumu','Doğrulama bekliyor',STATUSES[:-1])
        def save():
            try:
                self.store.add_source(record_id,kind.get(),label.get(),url.get(),method.get(),status.get())
            except Exception as ex:
                messagebox.showerror('Kaynak eklenmedi',str(ex),parent=dialog)
                return
            dialog.destroy();self.refresh_feed()
        ttk.Button(box,text='Kaynağı ekle',command=save).grid(row=5,column=1,sticky='e',pady=12)

    def copy_record(self,record_id):
        r=self.store.get_entry(record_id)
        if not r: return
        text=(f"{r['title']}\n{r['summary']}\n{r['details']}\n\n"
              f"Kaynak: {r['source_kind']} / {r['source_label']}\n"
              f"Konum: {r['source_uri']}\nElde edilme: {r['acquisition']}\n"
              f"Durum: {r['status']}\nSon güncelleme: {r['updated_at']}")
        self.clipboard_clear();self.clipboard_append(text)
        messagebox.showinfo('Bilgi Atlası','Kayıt metni panoya kopyalandı.',parent=self)

    def index_code(self,background=True):
        if self._indexing:
            return
        self._indexing=True
        self.stat_var.set('Kod haritası indeksleniyor (mevcut veri tabanları değiştirilmez)...')
        def worker():
            try:
                result=self.store.index_code(self.base)
                message=f"{result['indexed_files']} dosya indekslendi, {result['indexed_symbols']} sembol güncellendi. "
                message+=f"{result['unchanged_or_skipped']} dosya değişmemiş/atlandı."
                if result['errors']:message+=' Hatalar: '+', '.join(result['errors'][:4])
            except Exception as ex:
                message='Kod haritası güncellenemedi: '+str(ex)
            def finish():
                self._indexing=False
                if self.winfo_exists():
                    self.refresh_feed()
                    self.stat_var.set(message)
            try:
                self.after(0,finish)
            except (RuntimeError,tk.TclError):
                pass
        if background:
            threading.Thread(target=worker,daemon=True).start()
        else:worker()

    def _save_path(self,title,ext):
        return filedialog.asksaveasfilename(parent=self,title=title,
            defaultextension=ext,initialfile='NGS_BILGI_ATLASI_'+datetime.now().strftime('%Y%m%d_%H%M')+ext,
            filetypes=[(ext.upper()+' dosyası','*'+ext),('Tümü','*.*')])

    def export(self,fmt):
        if not messagebox.askyesno('Dışa aktarma gizliliği',
            'Atlas notlarında kişisel bilgi bulunmadığından ve dosyayı güvenli bir yere kaydedeceğinizden emin misiniz?',parent=self):
            return
        path=self._save_path('Bilgi Atlası dışa aktar', '.md' if fmt=='md' else '.json')
        if not path:return
        try:
            count=self.store.export_markdown(path) if fmt=='md' else self.store.export_json(path)
            messagebox.showinfo('Bilgi Atlası',f'{count} kayıt dışa aktarıldı.\n{path}',parent=self)
        except Exception as ex:
            messagebox.showerror('Dışa aktarma başarısız',str(ex),parent=self)

    def backup(self):
        path=self._save_path('Atlas SQLite yedeği', '.sqlite3')
        if not path:return
        try:
            self.store.backup(path)
            messagebox.showinfo('Bilgi Atlası',f'Atlas veritabanı yedeklendi.\n{path}',parent=self)
        except Exception as ex:
            messagebox.showerror('Yedek başarısız',str(ex),parent=self)
