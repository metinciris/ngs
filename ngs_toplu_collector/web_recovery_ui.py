"""Guided, local-only incomplete-source and manual-recovery workspace."""
from __future__ import annotations
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from urllib.parse import urljoin, urlparse
from web_recovery import RecoveryStore, manual_guide, import_manual_source, VALID_KINDS


def safe_source_url(sample,kind,cfg):
    """User-visible URL for the exact technical record, same origin only."""
    links=(sample or {}).get('links') or {}
    site=str((cfg or {}).get('runs_url') or (cfg or {}).get('base_url') or '')
    requested=links.get('run') if kind.startswith('selection_') else links.get(kind)
    requested=requested or links.get('run')
    if str(requested or '').startswith('RUNS_QC::'):
        requested=links.get('run') or site
    if not requested:return ''
    full=urljoin(site,str(requested))
    a=urlparse(site);b=urlparse(full)
    if a.scheme not in ('http','https') or (a.scheme,a.netloc)!=(b.scheme,b.netloc):
        return ''
    return full


class WebRecoveryPanel(ttk.Frame):
    def __init__(self,parent,base,db,submit,is_busy=lambda:False,cfg=None):
        super().__init__(parent,padding=12)
        self.base=base;self.db=db;self.submit=submit;self.is_busy=is_busy;self.cfg=cfg or {}
        self.all_records=tk.BooleanVar(value=False)
        self.summary=tk.StringVar(value='Kaynak tamlık durumu taranıyor…')
        self.instructions=tk.StringVar(value='Eksik bir satır seçin. Web sayfasını açıp veri indirme işlemini birlikte tamamlayabilirsiniz.')
        self.source_url=tk.StringVar(value='')
        self._rows={}
        title=ttk.Frame(self);title.pack(fill='x',pady=(0,8))
        ttk.Label(title,text='Web Kaynak / Eksikleri Tamamla',font=('Segoe UI',14,'bold')).pack(side='left')
        ttk.Checkbutton(title,text='Tüm arşivi göster',variable=self.all_records,command=self.refresh).pack(side='right')
        ttk.Label(self,textvariable=self.summary,foreground='#334155').pack(anchor='w',pady=(0,8))
        btn=ttk.Frame(self);btn.pack(fill='x',pady=(0,8))
        for label,cmd in (
            ('Durumu Yenile',self.refresh),
            ('Son POOL Eksiklerini Yeniden Dene',self.retry_due),
            ('Seçili Adımı Yeniden Dene',self.retry_selected),
            ('Webde Aç + İndirmeyi İzle',self.open_watch),
            ('Kaynak Linkini Kopyala',self.copy_source_link),
            ('Dosyayı Elle Ekle',self.attach_file),
            ('Talimatı Göster',self.show_guide),
        ):
            ttk.Button(btn,text=label,command=cmd).pack(side='left',padx=(0,6))
        headers=('state','pool','mp','kind','attempts','reason')
        self.tree=ttk.Treeview(self,columns=headers,show='headings',height=15)
        labels={'state':'DURUM','pool':'POOL','mp':'ÖRNEK','kind':'KAYNAK','attempts':'DENEME','reason':'HATA / SONRAKİ ADIM'}
        widths={'state':125,'pool':115,'mp':140,'kind':125,'attempts':85,'reason':640}
        for h in headers:
            self.tree.heading(h,text=labels[h]);self.tree.column(h,width=widths[h],anchor='center' if h in ('state','attempts') else 'w')
        self.tree.tag_configure('verified',background='#e7f6ec')
        self.tree.tag_configure('manual',background='#fff1cc')
        self.tree.tag_configure('retry',background='#ffe9e9')
        self.tree.tag_configure('waiting',background='#eef4ff')
        self.tree.tag_configure('blocked',background='#fae9e9')
        pane=ttk.Frame(self);pane.pack(fill='both',expand=True)
        scroll=ttk.Scrollbar(pane,orient='vertical',command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side='left',fill='both',expand=True);scroll.pack(side='right',fill='y')
        self.tree.bind('<<TreeviewSelect>>',lambda event:self.show_guide())
        desc=ttk.LabelFrame(self,text='Seçili kaynak için yapmanız gerekenler',padding=(10,8))
        desc.pack(fill='x',pady=(10,5))
        ttk.Label(desc,textvariable=self.instructions,wraplength=1250,justify='left').pack(fill='x')
        linkrow=ttk.Frame(desc);linkrow.pack(fill='x',pady=(5,0))
        ttk.Label(linkrow,text='Bu kaynak için web bağlantısı:').pack(side='left',padx=(0,8))
        ttk.Entry(linkrow,textvariable=self.source_url,state='readonly').pack(side='left',fill='x',expand=True)
        ttk.Label(self,text='İndirme gözlemi yalnız NGS programının kendi Chromium sekmesinde çalışır. '
                  'Başka tarayıcıdaki Ctrl+C / Ctrl+V izlenmez. Kaynak doğrulanmadan TAMAM kabul edilmez.',
                  foreground='#64748b',wraplength=1250).pack(anchor='w',pady=(4,0))
        self.refresh()

    def _keys(self):
        return None if self.all_records.get() else self.db.latest_record_keys()

    def refresh(self):
        store=RecoveryStore(self.base)
        try:
            keys=self._keys()
            if keys is None or keys:
                store.synchronize(self.db,keys)
            rows=store.rows(keys)
        finally:store.close()
        before=self._selected_raw()
        self._rows={}
        self.tree.delete(*self.tree.get_children())
        labels={'verified':'DOĞRULANDI','manual':'MANUEL GEREKLİ','retry':'YENİDEN DENE','waiting':'BEKLİYOR','blocked':'BLOKE'}
        for i,r in enumerate(rows):
            iid=str(i)
            self._rows[iid]=r
            reason=r['last_reason'] or r['source_reason']
            self.tree.insert('', 'end',iid=iid,values=(labels.get(r['state'],r['state']),r['pool'],r['mp'],r['kind'].upper(),r['attempts'],reason[:180]),tags=(r['state'],))
            if before and (r['record_key'],r['kind'])==before:self.tree.selection_set(iid)
        bad=sum(r['state']!='verified' for r in rows)
        manuel=sum(r['state']=='manual' for r in rows)
        self.summary.set(f'{len(rows)} kaynak kontrolü · {len(rows)-bad} doğrulandı · {bad} açık · {manuel} manuel iş bekliyor. '
                         'Selection, SNV/CNV, QC ve Fusion ayrı doğrulanır.')
        return rows

    def _selected_raw(self):
        sel=self.tree.selection()
        if sel:
            r=self._rows.get(sel[0]);return (r['record_key'],r['kind']) if r else None
        return None

    def _selected(self):
        sel=self.tree.selection()
        return self._rows.get(sel[0]) if sel else None

    def show_guide(self):
        r=self._selected()
        if not r:
            self.source_url.set('')
            return
        self.instructions.set(f"{r['pool']} / {r['mp']} / {r['kind'].upper()}: "
                              f"{manual_guide(r['kind'])}  Kaynak: {r['source_reason']} "
                              f"Son durum: {r['state']}.")
        self.source_url.set(safe_source_url(self.db.get_sample(r['record_key']),r['kind'],self.cfg))

    def copy_source_link(self):
        self.show_guide()
        url=self.source_url.get()
        if not url:
            return messagebox.showwarning('Kaynak bağlantısı',
                  'Güvenilir kaynak URL bulunamadı. Önce RUNS tarayın veya Webde Aç seçeneğini kullanın.')
        # Clipboard is touched ONLY on this explicit button click.
        self.clipboard_clear();self.clipboard_append(url)
        messagebox.showinfo('Kaynak bağlantısı','Bağlantı kopyalandı. URL yalnız aynı NGS platformuna aittir.')

    def _send(self,command,**kwargs):
        if self.is_busy():
            messagebox.showinfo('NGS Web Kurtarma','Tarayıcıda bir iş sürüyor. Bitince tekrar deneyin.')
            return
        if not self.submit(command,**kwargs):
            messagebox.showwarning('NGS Web Kurtarma','Tarayıcı işi başlatılamadı.')

    def retry_due(self):self._send('recovery_retry',limit=8,force=False)

    def retry_selected(self):
        r=self._selected()
        if r is None:return messagebox.showinfo('Eksik kaynak','Önce bir kaynak satırı seçin.')
        if r['state']=='verified':return messagebox.showinfo('Kaynak','Bu kaynak zaten doğrulanmış.')
        self._send('recovery_retry',record_key=r['record_key'],kind=r['kind'],limit=1,force=True)

    def open_watch(self):
        r=self._selected()
        if r is None:return messagebox.showinfo('Eksik kaynak','Önce bir kaynak satırı seçin.')
        if r['state']=='verified':return messagebox.showinfo('Kaynak','Bu kaynak zaten doğrulanmış.')
        self.show_guide()
        self._send('recovery_open_watch',record_key=r['record_key'],kind=r['kind'],watch_seconds=180)

    def attach_file(self):
        if self.is_busy():
            return messagebox.showinfo('NGS Web Kurtarma','Aktif web toplama sırasında manuel veri girişi yapılmaz. İşin bitmesini bekleyin.')
        r=self._selected()
        if r is None:return messagebox.showinfo('Eksik kaynak','Önce bir kaynak satırı seçin.')
        if r['kind'].startswith('selection_'):
            return messagebox.showwarning('Selection','Selection için dosya aktarımı yerine doğrulanmış platform Selection taraması gereklidir.')
        types=[('Kaynak dosyaları','*.tsv *.csv *.json *.zip'),('Tüm dosyalar','*.*')]
        src=filedialog.askopenfilename(title=f'{r["mp"]} için {r["kind"].upper()} kaynak dosyasını seçin',filetypes=types)
        if not src:return
        # Manual data can lack embedded sample UUID. Require explicit user
        # attestation; it must never be inferred from a similar MP label.
        yes=messagebox.askyesno('Kaynak örnek eşleştirmesi',
            f"{r['mp']} / {r['kind'].upper()}\n\nİndirilen kaynak dosyasının GenNext'te açtığınız "
            'teknik örneğe (RUN/SAMPLE UUID) ait olduğunu bizzat doğruladınız mı?\n\n'
            'Emin değilseniz Hayır seçin; veri raporlamaya hazır sayılmayacak.')
        try:
            out=import_manual_source(self.base,self.db,r['record_key'],r['kind'],src,identity_confirmed=yes)
        except Exception as exc:
            messagebox.showerror('Kaynak kabul edilmedi',str(exc));return
        self.refresh()
        if out['state']=='verified':messagebox.showinfo('Doğrulandı',out['message'])
        else:messagebox.showwarning('Kontrol gerekli',out['message'])
