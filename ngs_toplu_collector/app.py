from __future__ import annotations
import queue, json, shutil, hashlib, re, time
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox, filedialog, simpledialog

from db import StateDB
from browser_worker import BrowserWorker
from mutation_workspace import MutationWorkspace as MutationViewer
from pool_spread import PoolSpreadAnalyzer
from metadata_editor import MetadataEditor
from patient_workspace import PatientWorkspace
from archive_workspace import ArchiveWorkspace
from ai_export import AIPackageExporter
from review_workspace_ui import AIReviewCenter as AIReviewIGV
from config_loader import load_config, config_ready
from qc_enlil import NGSQCPanel
from source_validation import validate_qc_source, validate_fusion_source
from remote_control import RemoteAppBridge
from web_interface_panel import WebInterfacePanel
from web_recovery_ui import WebRecoveryPanel
from web_recovery import RecoveryStore

BASE = Path(__file__).resolve().parent
CFG = load_config(BASE)
DB = StateDB(BASE/'DATA'/'collector_state.sqlite3')
from reporting_context import ensure_schema as ensure_reporting_schema
ensure_reporting_schema(DB)
VERSION=(BASE/'VERSION').read_text(encoding='utf-8-sig').strip()


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f'NGS Mutasyon Havuzu v{VERSION}')
        self.geometry('1800x1020')
        self.minsize(1180,740)
        self.configure(bg='#f3f6fa')
        self._fusion_review_migrated=DB.audit_fusion_fallbacks(BASE)
        self._qc_review_migrated=DB.audit_qc_sources(BASE)
        self.q=queue.Queue();self.browser=BrowserWorker(BASE,CFG,self.emit_event)
        self.browser_ready=False;self.runs_scanned=False;self.job_busy=False;self.current_command='';self.close_safe=False
        self.advanced_visible=False
        self._configure_style();self._build()
        # NGS_REPORT_ARCHIVE_V1: local report archive.
        from report_archive_integration import attach_report_archive
        attach_report_archive(self, BASE, DB, CFG)
        from ngs_accounts_ui import attach_account_manager
        attach_account_manager(self, BASE)  # NGS_ACCOUNTS_V1
        self.bind('<<AIReviewsChanged>>', self._ai_reviews_changed)
        self.remote_bridge=RemoteAppBridge(self,BASE,CFG,VERSION)
        self.after(120,self._maximize_for_fullhd);self.after(200,self.pump)
        self.refresh_table()
        self.after(8*60*1000,self._auto_web_recovery_tick)  # NGS_WEB_RECOVERY_v1_2
        if self._fusion_review_migrated:
            self.log.insert('end',f'⚠ v0.4.6: {self._fusion_review_migrated} Fusion kaynağı (Loading/[object Object]/özet-only veya eski fallback) TAMAM durumundan KONTROL GEREKLİ durumuna alındı.\n');self.log.see('end')
        if self._qc_review_migrated:
            self.log.insert('end',f'⚠ v0.4.6: {self._qc_review_migrated} QC görevi gerçek/örnekle eşleşen yerel kaynak doğrulanamadığı için KONTROL GEREKLİ durumuna alındı.\n');self.log.see('end')
        self.protocol('WM_DELETE_WINDOW',self.on_close)

    def _configure_style(self):
        st=ttk.Style(self)
        try:
            if 'vista' in st.theme_names(): st.theme_use('vista')
            elif 'clam' in st.theme_names(): st.theme_use('clam')
        except Exception: pass
        st.configure('TFrame',background='#f4f7fb')
        st.configure('TLabel',background='#f4f7fb',foreground='#243b53')
        st.configure('TNotebook',background='#f4f7fb',borderwidth=0)
        st.map('TNotebook.Tab',background=[('selected','#ffffff')],foreground=[('selected','#17324d')])
        st.configure('Treeview',background='#ffffff',fieldbackground='#ffffff')
        st.configure('TNotebook.Tab',padding=(12,9),font=('Segoe UI',10,'bold'))
        st.configure('Treeview',rowheight=31,font=('Segoe UI',10))
        st.configure('Treeview.Heading',font=('Segoe UI',9,'bold'))
        st.configure('Card.TLabelframe',padding=8)
        st.configure('Card.TLabelframe.Label',font=('Segoe UI',9,'bold'))
        st.configure('Small.TButton',padding=(7,4))
        st.configure('Accent.TButton',padding=(16,9),font=('Segoe UI',10,'bold'))

    def _maximize_for_fullhd(self):
        try:self.state('zoomed')
        except Exception:pass

    def _build(self):
        self._build_header()
        self.workspaces=ttk.Notebook(self)
        self.workspaces.pack(fill='both',expand=True,padx=8,pady=(5,7))
        self.workframe=ttk.Frame(self.workspaces)
        self.workspaces.add(self.workframe,text='  1. POOL / YZ  ')
        self._build_banner()

        self.nb=ttk.Notebook(self.workframe);self.nb.pack(fill='both',expand=True,padx=12,pady=(7,5))
        self.collect_tab=ttk.Frame(self.nb)
        # Construct spread first so MutationViewer can jump directly into archive search.
        self.spread_tab=PoolSpreadAnalyzer(self.nb,BASE,DB,CFG)
        self.mut_tab=MutationViewer(self.nb,BASE,DB,CFG,archive_callback=self.open_variant_archive,on_change=self.data_changed)
        self.ai_tab=AIPackageExporter(self.nb,BASE,DB,CFG,reindex_callback=self.spread_tab.reindex)
        self.review_tab=AIReviewIGV(self.nb,BASE,DB,CFG,browser_submit=lambda command,**kw:self._submit(command,**kw))
        self.nb.add(self.collect_tab,text='  Ana / Toplama  ')
        self.nb.add(self.mut_tab,text='  Mutasyonlar  ')
        self.nb.add(self.spread_tab,text='  POOL Yayılım Analizi  ')
        self.nb.add(self.ai_tab,text='  YZ Paketi  ')
        self.nb.add(self.review_tab,text='  YZ Değerlendirme / IGV  ')
        self._build_workflow()
        self._build_collect_tab()

        bot=ttk.Frame(self.collect_tab,padding=(12,4,12,9));bot.pack(side='bottom',fill='x')
        ttk.Button(bot,text='Durumu Yenile',style='Small.TButton',command=self.refresh_table).pack(side='left')
        ttk.Button(bot,text='Seçili Hasta / Blok',style='Small.TButton',command=self.open_selected_metadata).pack(side='left',padx=5)
        ttk.Button(bot,text='DNA + RNA Rapor',style='Small.TButton',command=self.open_selected_dual_report).pack(side='left',padx=5)
        ttk.Button(bot,text='Tanı Paketleri',style='Small.TButton',command=self.open_diag).pack(side='left',padx=5)
        ttk.Button(bot,text='Web Arayüzü / Koklayıcı',style='Small.TButton',command=lambda:self.workspaces.select(self.web_panel)).pack(side='left',padx=5)
        ttk.Button(bot,text='YZ / Raporlama',style='Small.TButton',command=lambda:self.nb.select(self.ai_tab)).pack(side='left',padx=5)
        ttk.Button(bot,text='YZ Dönüş / IGV',style='Small.TButton',command=lambda:self.nb.select(self.review_tab)).pack(side='left',padx=5)
        ttk.Button(bot,text='Hasta / Blok / Enlil',style='Small.TButton',command=lambda:self.workspaces.select(self.qc_workspace)).pack(side='left',padx=5)
        ttk.Label(bot,text='Çift tık: örneğin mutasyonlarını aç · ✓ = uygun bütün görevler tamam',foreground='#667085').pack(side='right')
        self.archive=ArchiveWorkspace(self.workspaces,BASE,DB,CFG,
            open_metadata=self.open_record_metadata,open_mutations=self.open_record_mutations,
            open_review=self.open_review_workspace,reindex=self.spread_tab.reindex)
        self.workspaces.add(self.archive,text='  2. Arşiv / Araştırma  ')
        self.patient_workspace=PatientWorkspace(self.workspaces,BASE,DB,CFG,on_change=self.data_changed,
            open_mutations=self.open_record_mutations,open_archive=self.open_patient_archive,
            request_scan=self._scan_from_patient_workspace,
            on_choose_pool_alias=self.choose_pool_alias)
        self.qc_workspace=self.patient_workspace
        self.qc_panel=self.patient_workspace.qc
        self.meta_tab=self.patient_workspace.metadata
        self.workspaces.add(self.patient_workspace,text='  3. Hasta / Raporlama  ')
        self.web_panel=WebInterfacePanel(self.workspaces,BASE,CFG,
            submit=lambda command,**kw:self._submit(command,**kw),
            open_browser=self.start_browser,
            stop_browser=lambda:self.browser.request_stop(False),
            set_auto=lambda enabled,mode:self.browser.set_auto_inspector(enabled,mode),
            active_command=lambda:self.browser.running_command,
            request_snapshot=self.browser.request_auto_snapshot)
        self.workspaces.add(self.web_panel,text='  4. Web / Koklayıcı  ')
        self.web_recovery_panel=WebRecoveryPanel(self.workspaces,BASE,DB,
             submit=lambda command,**kw:self._submit(command,**kw),
             is_busy=lambda:self.browser.busy,cfg=CFG)
        self.workspaces.add(self.web_recovery_panel,text='  5. Web Eksikleri / Manuel  ')
        # NGS_BILGI_ATLASI_V1_BEGIN
        try:
            from ngs_bilgi_atlasi_panel import BilgiAtlasiPanel
            self.bilgi_atlasi = BilgiAtlasiPanel(self.workspaces, BASE, VERSION)
        except Exception as atlas_error:
            self.bilgi_atlasi = ttk.Frame(self.workspaces)
            ttk.Label(self.bilgi_atlasi, text='Bilgi Atlası açılamadı: ' + str(atlas_error),
                      foreground='#9d3c17', wraplength=950).pack(padx=30, pady=35)
        self.workspaces.add(self.bilgi_atlasi, text='  6. Bilgi Atlası  ')
        # NGS_BILGI_ATLASI_V1_END
        self.workspaces.bind('<<NotebookTabChanged>>',self._workspace_changed)
        self.nb.bind('<<NotebookTabChanged>>',self._working_tab_changed)
        self._update_workflow_buttons()
        last_workspace=DB.get_meta('last_workspace','collect')
        if last_workspace=='archive':self.after(200,lambda:self.open_patient_archive())
        elif last_workspace=='patient':self.after(200,lambda:self.workspaces.select(self.patient_workspace))

    def _build_header(self):
        hdr=tk.Frame(self,bg='#17324d',height=66);hdr.pack(fill='x');hdr.pack_propagate(False)
        left=tk.Frame(hdr,bg='#17324d');left.pack(side='left',fill='y',padx=16)
        tk.Label(left,text='NGS Mutasyon Havuzu',bg='#17324d',fg='white',font=('Segoe UI',16,'bold')).pack(anchor='w',pady=(9,0))
        tk.Label(left,text='NGS platformu toplama · mutasyon inceleme · POOL yayılım analizi',bg='#17324d',fg='#c9d8e6',font=('Segoe UI',9)).pack(anchor='w')
        right=tk.Frame(hdr,bg='#17324d');right.pack(side='right',fill='y',padx=16)
        self.global_job=tk.StringVar(value='Tarayıcı boşta · Arşiv yerel')
        tk.Label(right,textvariable=self.global_job,bg='#17324d',fg='#f1f5f9',font=('Segoe UI',9)).pack(anchor='e')
        self.diag_var=tk.StringVar(value='DIAGNOSTICS: kontrol ediliyor…')
        tk.Label(right,textvariable=self.diag_var,bg='#17324d',fg='#dbe7f2',font=('Segoe UI',9,'bold')).pack(anchor='e',pady=(13,1))
        tk.Label(right,text=f'v{VERSION} · Sekmeli çalışma alanı',bg='#17324d',fg='#9fb4c7',font=('Segoe UI',8)).pack(anchor='e')

    def _workflow_button(self,parent,text,command,kind='primary'):
        palette={
            'primary':('#2563eb','#1d4ed8'),
            'success':('#0f766e','#0b5f59'),
            'neutral':('#475569','#334155'),
            'danger':('#b42318','#912018')
        }
        bg,active=palette[kind]
        b=tk.Button(parent,text=text,command=command,bg=bg,fg='white',activebackground=active,activeforeground='white',
                    disabledforeground='#8b96a5',relief='flat',bd=0,font=('Segoe UI',10,'bold'),padx=13,pady=8,cursor='hand2')
        return b

    def _build_workflow(self):
        outer=tk.Frame(self.collect_tab,bg='#f3f6fa');outer.pack(fill='x',padx=8,pady=(7,4))
        card=tk.Frame(outer,bg='white',highlightbackground='#d9e2ec',highlightthickness=1);card.pack(fill='x')
        title=tk.Frame(card,bg='white');title.pack(fill='x',padx=12,pady=(8,3))
        tk.Label(title,text='Yeni POOL İş Akışı',bg='white',fg='#102a43',font=('Segoe UI',11,'bold')).pack(side='left')
        self.workflow_hint=tk.StringVar(value='Sırayla 1 → 2 → 3. Yalnız o anda kullanılabilir adım aktif olur.')
        tk.Label(title,textvariable=self.workflow_hint,bg='white',fg='#64748b',font=('Segoe UI',9)).pack(side='left',padx=12)
        self.advanced_toggle_btn=ttk.Button(title,text='Gelişmiş ▸',style='Small.TButton',command=self.toggle_advanced);self.advanced_toggle_btn.pack(side='right')

        row=tk.Frame(card,bg='white');row.pack(fill='x',padx=12,pady=(4,10))
        self.step1_btn=self._workflow_button(row,'1. Tarayıcıyı Aç / Login',self.start_browser,'primary');self.step1_btn.pack(side='left',padx=(0,7))
        self.step2_btn=self._workflow_button(row,"2. RUNS'u Tara",self.start_scan,'primary');self.step2_btn.pack(side='left',padx=7)
        self.step3_btn=self._workflow_button(row,"3. Son POOL'u Tamamla",self.start_latest,'success');self.step3_btn.pack(side='left',padx=7)
        tk.Label(row,text='│',bg='white',fg='#cbd5e1',font=('Segoe UI',16)).pack(side='left',padx=6)
        self.stop_after_btn=self._workflow_button(row,'Mevcut Adım Sonrası Durdur',lambda:self.browser.request_stop(False),'neutral');self.stop_after_btn.pack(side='left',padx=5)
        self.stop_now_btn=self._workflow_button(row,'HEMEN DURDUR',lambda:self.browser.request_stop(True),'danger');self.stop_now_btn.pack(side='left',padx=5)

        # Put the RUN alias action on its own visible row. A selected technical
        # sample is NOT required: the dialog lists all runs, including raw IDs.
        alias_row=tk.Frame(card,bg='#eef6ff');alias_row.pack(fill='x',padx=12,pady=(0,9))
        self.pool_alias_btn=self._workflow_button(alias_row,
            'POOL ADLANDIR / SON RUN SEÇ (örn. POOL 69)',self.choose_pool_alias,'success')
        self.pool_alias_btn.pack(side='left',padx=(4,12),pady=5)
        ttk.Button(alias_row,text='Otomatik Son Çalışma',style='Small.TButton',
                   command=self.clear_latest_override).pack(side='left',padx=5)
        tk.Label(alias_row,text='Teknik örnek seçmeden RUN listesinden adlandırın',
                 bg='#eef6ff',fg='#17324d',font=('Segoe UI',9)).pack(side='left',padx=10)

        self.advanced_frame=tk.Frame(card,bg='#f8fafc',highlightbackground='#e2e8f0',highlightthickness=1)
        tk.Label(self.advanced_frame,text='Gelişmiş / yalnız gerektiğinde:',bg='#f8fafc',fg='#475569',font=('Segoe UI',9,'bold')).pack(side='left',padx=(10,6),pady=7)
        self.adv_dna=ttk.Button(self.advanced_frame,text='Son Çalışma DNA Eksikleri',command=lambda:self.start_queue(True,False));self.adv_dna.pack(side='left',padx=3,pady=5)
        self.adv_rna=ttk.Button(self.advanced_frame,text='Son Çalışma RNA Eksikleri',command=lambda:self.start_queue(False,True));self.adv_rna.pack(side='left',padx=3,pady=5)
        self.adv_all=ttk.Button(self.advanced_frame,text='Son Çalışma Tüm Eksikleri',command=lambda:self.start_queue(True,True));self.adv_all.pack(side='left',padx=3,pady=5)
        self.adv_clinical=ttk.Button(self.advanced_frame,text='Son Çalışma Clinical Bağlamını Zorla Yenile',command=self.start_clinical_refresh);self.adv_clinical.pack(side='left',padx=9,pady=5)
        self.adv_hrd=ttk.Button(self.advanced_frame,text='Son Çalışma HRD Skorlarını Yenile',command=self.start_hrd_refresh);self.adv_hrd.pack(side='left',padx=3,pady=5)
        self.adv_selection=ttk.Button(self.advanced_frame,text='DNA Selection (15)',command=self.start_selection_backfill)
        self.adv_selection.pack(side='left',padx=3,pady=5)
        self.adv_fusion=ttk.Button(self.advanced_frame,text='RNA Fusion Selection (5)',command=self.start_fusion_backfill)
        self.adv_fusion.pack(side='left',padx=3,pady=5)
        ttk.Button(self.advanced_frame,text='Arşiv Geçmiş Eksik/Hata',command=lambda:self.show_missing('all','archive')).pack(side='right',padx=3,pady=5)
        ttk.Button(self.advanced_frame,text='DIAGNOSTICS Klasörü',command=self.open_diag).pack(side='right',padx=10,pady=5)

    def _build_banner(self):
        self.banner_host=ttk.Frame(self.workframe);self.banner_host.pack(fill='x')
        self.banner_var=tk.StringVar(value='HAZIR · 1. adımdan başlayın')
        self.banner=tk.Label(self.banner_host,textvariable=self.banner_var,anchor='w',font=('Segoe UI',11,'bold'),padx=14,pady=7,bg='#e8eef7',fg='#1f3b57')
        self.banner.pack(fill='x',padx=12,pady=(2,1))
        self.status=tk.StringVar(value='Hazır')
        ttk.Label(self.banner_host,textvariable=self.status,padding=(14,2),foreground='#52606d').pack(fill='x')

    def toggle_advanced(self):
        self.advanced_visible=not self.advanced_visible
        if self.advanced_visible:
            self.advanced_frame.pack(fill='x',padx=12,pady=(0,9));self.advanced_toggle_btn.configure(text='Gelişmiş ▾')
        else:
            self.advanced_frame.pack_forget();self.advanced_toggle_btn.configure(text='Gelişmiş ▸')

    def _card(self,parent,title,var,bg='#eef4fb',fg='#17324d',command=None):
        f=tk.Frame(parent,bg=bg,highlightbackground='#d9e2ec',highlightthickness=1);f.pack(side='left',fill='x',expand=True,padx=4)
        l1=tk.Label(f,text=title,bg=bg,fg='#667085',font=('Segoe UI',8,'bold'))
        l1.pack(anchor='w',padx=9,pady=(6,0))
        l2=tk.Label(f,textvariable=var,bg=bg,fg=fg,font=('Segoe UI',14,'bold'))
        l2.pack(anchor='w',padx=9,pady=(0,7))
        if command:
            for w in (f,l1,l2):
                w.configure(cursor='hand2');w.bind('<Button-1>',lambda e:command())
        return f

    def _build_collect_tab(self):
        stats=tk.Frame(self.collect_tab,bg='#f3f6fa');stats.pack(fill='x',pady=(6,7),padx=4)
        self.stat_total=tk.StringVar(value='0');self.stat_complete=tk.StringVar(value='0');self.stat_missing=tk.StringVar(value='0');self.stat_error=tk.StringVar(value='0');self.stat_latest=tk.StringVar(value='—')
        self._card(stats,'TEKNİK KAYIT',self.stat_total)
        self._card(stats,'ARŞİV TAMAM',self.stat_complete,'#eaf7ef','#17643a')
        self._card(stats,'SON ÇALIŞMA BEKLEYEN',self.stat_missing,'#eef5ff','#174a7a',lambda:self.show_missing('missing','latest'))
        self._card(stats,'SON ÇALIŞMA KONTROL / HATA',self.stat_error,'#fff4d6','#8a5a00',lambda:self.show_missing('error','latest'))
        self._card(stats,'SON ÇALIŞMA',self.stat_latest,'#eef4fb','#17324d')

        pan=ttk.Panedwindow(self.collect_tab,orient='vertical');pan.pack(fill='both',expand=True,padx=4,pady=(0,4))
        f1=ttk.LabelFrame(pan,text='Teknik örnekler');f2=ttk.LabelFrame(pan,text='İşlem günlüğü');pan.add(f1,weight=4);pan.add(f2,weight=1)
        cols=('overall','pool','name','mp','other_mp','family','type','run','clinical','snv','cnv','qc','fusion','gennext')
        self.tree=ttk.Treeview(f1,columns=cols,show='headings',height=18,selectmode='browse')
        widths={'overall':70,'pool':135,'name':220,'mp':120,'other_mp':150,'family':120,'type':72,'run':62,'clinical':80,'snv':60,'cnv':60,'qc':60,'fusion':70,'gennext':155}
        labels={'overall':'DURUM','pool':'POOL','name':'AD SOYAD','mp':'ÖRNEK','other_mp':'AYNI HASTA / DİĞER MP','family':'VAKA AİLESİ','type':'TİP','run':'RUN','clinical':'CLINICAL','snv':'SNV','cnv':'CNV','qc':'QC','fusion':'FUSION','gennext':'GENNEXT SELECTION'}
        centered={'overall','run','clinical','snv','cnv','qc','fusion','type'}
        for c in cols:
            self.tree.heading(c,text=labels.get(c,c.upper()),anchor='w')
            self.tree.column(c,width=widths[c],anchor='center' if c in centered else 'w')
        ys=ttk.Scrollbar(f1,orient='vertical',command=self.tree.yview);xs=ttk.Scrollbar(f1,orient='horizontal',command=self.tree.xview)
        self.tree.configure(yscrollcommand=ys.set,xscrollcommand=xs.set)
        self.tree.grid(row=0,column=0,sticky='nsew');ys.grid(row=0,column=1,sticky='ns');xs.grid(row=1,column=0,sticky='ew')
        f1.rowconfigure(0,weight=1);f1.columnconfigure(0,weight=1)
        self.tree.bind('<Double-1>',self.open_selected_mutations)
        self.tree.tag_configure('complete',background='#e7f6ec');self.tree.tag_configure('error',background='#fdeaea');self.tree.tag_configure('waiting',background='#eef5ff');self.tree.tag_configure('review',background='#fff4d6');self.tree.tag_configure('missing',background='#fffdf6')
        self.log=tk.Text(f2,wrap='none',height=10,bg='#0f172a',fg='#dbeafe',insertbackground='white',font=('Consolas',9),relief='flat')
        lys=ttk.Scrollbar(f2,orient='vertical',command=self.log.yview);self.log.configure(yscrollcommand=lys.set)
        self.log.pack(side='left',fill='both',expand=True);lys.pack(side='right',fill='y')

    def emit_event(self,typ,msg):self.q.put((typ,msg))

    def _submit(self,command,**kwargs):
        if self.browser.submit(command,**kwargs):
            self.current_command=command;self.close_safe=False;return True
        return False

    def start_browser(self):
        if not config_ready(CFG):
            messagebox.showwarning('Platform ayarı gerekli', 'Önce config.example.json dosyasını config.local.json olarak kopyalayın ve kurum içi Runs URL adresini girin.\n\nBu özel adres GitHub’a gönderilmez.')
            return
        self._submit('open_browser')

    def start_browser_sniffer(self):
        # BrowserWorker is the only owner of Playwright objects. Do not inspect
        # Chromium directly from the Tk UI thread.
        if self.job_busy:
            messagebox.showinfo('Tarayıcı Koklayıcı','Tarayıcı meşgul. Mevcut toplama bitince tekrar deneyin.')
            return
        if not config_ready(CFG):
            messagebox.showwarning('Tarayıcı Koklayıcı','Önce GenNext platform bağlantısını yapılandırın.')
            return
        sample=''
        try:
            selected=self.tree.selection()
            if selected:
                vals=self.tree.item(selected[0],'values')
                sample=str(vals[3]) if len(vals)>3 else ''
        except Exception:
            sample=''
        self._submit('sniff_browser',sample_id=sample)

    def start_scan(self):
        if self._submit('scan_runs'):
            self.runs_scanned=False

    def start_latest(self):
        self._submit('complete_latest')

    def _preferred_run_for_alias(self):
        """Use an active selection only as a hint; never require a sample row."""
        if hasattr(self,'patient_workspace') and self.workspaces.select()==str(self.patient_workspace):
            ws=self.patient_workspace
            if hasattr(ws,'pool_tree') and ws.notebook.select()==str(ws.pool_tab):
                picked=ws.pool_tree.selection()
                if picked:
                    key=picked[0]
                    if key.startswith('RUN::'):
                        return key[len('RUN::'):]
                    row=DB.get_sample(key)
                    if row:return str(row.get('run_uuid') or '')
        if self.workspaces.select()==str(self.workframe) and self.nb.select()==str(self.collect_tab):
            if hasattr(self,'tree'):
                picked=self.tree.selection()
                if picked:
                    row=DB.get_sample(picked[0])
                    if row:return str(row.get('run_uuid') or '')
        return ''

    def choose_pool_alias(self):
        """Pick from actual RUNs without selecting a sample in a hidden tab."""
        if self.job_busy or self.browser.busy:
            messagebox.showwarning('POOL seçimi','Tarayıcı işi bittikten sonra yeniden deneyin.')
            return
        from pool_alias_ui import ask_run_alias
        chosen=ask_run_alias(self,DB,preferred_run=self._preferred_run_for_alias())
        if chosen is None:
            return
        run_uuid,canonical=chosen
        try:
            clashes=[]
            for other in DB.cx.execute('SELECT run_uuid,pool_name FROM pools WHERE run_uuid<>?',
                                       (run_uuid,)).fetchall():
                label=DB.display_pool_name(other['run_uuid'],other['pool_name'])
                if label==canonical:
                    clashes.append(other['run_uuid'])
            if clashes and not messagebox.askyesno('Başka RUN da aynı POOL adında',
                    canonical+' etiketi başka '+str(len(set(clashes)))+
                    ' RUN için de kullanılıyor. Teknik kayıtlar birleşmeyecek. Devam edilsin mi?'):
                return
            canonical=DB.set_run_alias_and_latest(run_uuid,canonical)
        except ValueError as exc:
            messagebox.showerror('POOL adı kaydedilemedi',str(exc))
            return
        self.refresh_table()
        self.spread_tab.refresh_pools(prefer_latest=True)
        self.ai_tab.refresh_pools(prefer_latest=True)
        if hasattr(self,'patient_workspace'):
            self.patient_workspace.refresh()
            self.patient_workspace.qc.refresh_report_summary()
        self.status.set(canonical+' yerel olarak kaydedildi (RUN '+str(run_uuid)[:8]+').')
        self.banner_var.set('SON ÇALIŞMA SEÇİLDİ: '+canonical+' · 3. ADIM BU RUN İÇİN ÇALIŞIR')
        self.banner.configure(bg='#e8f2ff',fg='#174a7a')
        messagebox.showinfo('Yerel POOL seçildi',canonical+
             ' adı kaydedildi. Bu RUN son POOL olarak seçildi. '
             'GenNext sistemindeki isim ve eski POOL 68 verileri değişmedi.\n\n'
             '1. POOL / YZ > Ana / Toplama bölümünde RUNS taramasını doğrulayın; '
             'ardından 3. Son POOL’u Tamamla düğmesini kullanın.')

    def clear_latest_override(self):
        if self.job_busy or self.browser.busy:
            messagebox.showwarning('POOL secimi','Tarayici gorevi bitince tekrar deneyin.');return
        if not DB.manual_latest_run_uuid():return
        if messagebox.askyesno('Otomatik Son Calisma',
                'Yerel POOL adlari korunacak; sadece sabitlenmis son RUN secimi kaldirilacak. Emin misiniz?'):
            DB.clear_manual_latest_run()
            self.refresh_table()
            self.status.set('Otomatik son RUN secimine geri donuldu.')

    def _latest_record_keys(self):
        try:return DB.latest_record_keys()
        except Exception:return []

    def start_queue(self,dna,rna):
        keys=self._latest_record_keys()
        if not keys:
            messagebox.showwarning('Son çalışma','Önce RUNS taraması yapın; son çalışmanın teknik örnekleri bulunamadı.')
            return
        self._submit('run_queue',dna=dna,rna=rna,only_missing=True,record_keys=keys)

    def start_clinical_refresh(self):
        """Force-refresh only Clinical for the latest POOL, even when task status is done."""
        keys=self._latest_record_keys()
        if not keys:
            messagebox.showwarning('Clinical bağlam yenileme','Son çalışma bulunamadı. Önce RUNS taraması yapın.')
            return
        self._submit('run_queue',dna=True,rna=True,only_missing=False,record_keys=keys,
                     task_types=['clinical'],preserve_previous_done=True)

    def start_hrd_refresh(self):
        """Force-refresh Run Detail's authoritative HRD Score for latest DNA POOL."""
        keys=self._latest_record_keys()
        if not keys:
            messagebox.showwarning('HRD skoru yenileme','Son çalışma bulunamadı. Önce RUNS taraması yapın.')
            return
        self._submit('run_queue',dna=True,rna=False,only_missing=False,record_keys=keys,
                     task_types=['run'],preserve_previous_done=True)

    def start_selection_backfill(self):
        if not config_ready(CFG):
            messagebox.showwarning('GenNext Selection','Önce GenNext platform bağlantısını yapılandırın.')
            return
        if not DB.samples():
            messagebox.showwarning('GenNext Selection','Teknik kayıt yok. Önce RUNS taraması yapın.')
            return
        # 15 distinct records per invocation; checkpoints and retries are in SQLite.
        # Repeated clicks continue backwards without wiping previously reported data.
        self._submit('scan_report_selections',max_samples=15)

    def start_fusion_backfill(self):
        if not config_ready(CFG):
            messagebox.showwarning('RNA Fusion Selection','GenNext baglantisini yapilandirin.')
            return
        if not any((s.get('sample_type') or '').upper() in ('RNA','DNA+RNA') for s in DB.samples()):
            messagebox.showwarning('RNA Fusion Selection','RNA teknik kaydi yok. Once RUNS tarayin.')
            return
        self._submit('scan_fusion_selections',max_samples=5)

    def _update_workflow_buttons(self):
        busy=self.job_busy
        def state(widget,enabled):
            try:widget.configure(state='normal' if enabled else 'disabled',cursor='hand2' if enabled else 'arrow')
            except Exception:pass
        state(self.step1_btn,not busy and not self.browser_ready)
        state(self.step2_btn,not busy and self.browser_ready)
        state(self.step3_btn,not busy and self.runs_scanned)
        state(self.pool_alias_btn,not busy)
        for b in (self.adv_dna,self.adv_rna,self.adv_all,self.adv_clinical,self.adv_hrd,self.adv_selection,self.adv_fusion):
            try:b.configure(state='normal' if (not busy and self.runs_scanned) else 'disabled')
            except Exception:pass
        state(self.stop_after_btn,busy);state(self.stop_now_btn,busy)
        self.step1_btn.configure(text='✓ 1. Tarayıcı Açık' if self.browser_ready else '1. Tarayıcıyı Aç / Login')
        self.step2_btn.configure(text="✓ 2. RUNS Tarandı" if self.runs_scanned else "2. RUNS'u Tara")

    def _set_busy_ui(self,busy):
        self.job_busy=bool(busy);self._update_workflow_buttons()
        self.global_job.set('Tarayıcı işi sürüyor · Durdur: Toplama bölümü' if busy else 'Tarayıcı boşta · Arşiv yerel')
        if busy:
            names={'open_browser':'Tarayıcı açılıyor…','scan_runs':'RUNS taranıyor…','complete_latest':'Son POOL tamamlanıyor…','run_queue':'Eksikler / zorlanan adımlar toplanıyor…','scan_report_selections':'GenNext Selection geçmişi vaka vaka inceleniyor…','scan_fusion_selections':'RNA Fusion Selection vaka vaka inceleniyor…','igv_open':'IGV açılıyor…','igv_capture':'IGV görüntüsü alınıyor…','igv_batch_capture':'IGV kuyruğu otomatik hazırlanıyor…','sniff_browser':'Tarayıcı yapısı inceleniyor…'}
            self.banner_var.set('ÇALIŞIYOR · '+names.get(self.current_command,'İşlem sürüyor…'))
            self.banner.configure(bg='#fff3cd',fg='#664d03')

    def _notify(self,title,msg,kind='info'):
        try:
            import winsound;winsound.MessageBeep(winsound.MB_ICONASTERISK if kind=='info' else winsound.MB_ICONEXCLAMATION)
        except Exception:pass
        if CFG.get('completion_popup',True):
            try:messagebox.showinfo(title,msg)
            except Exception:pass

    def _handle_job_done(self,info):
        # NGS_LOGIN_V6: an open browser is not proof of a completed scan.
        if (info or {}).get('login_required'):
            self.refresh_table()
            self.browser_ready = bool(info.get('browser_ready'))
            self.close_safe = True
            if info.get('command') in ('open_browser', 'scan_runs'):
                self.runs_scanned = False
            self.banner_var.set('OTURUM BEKLENİYOR · GİRİŞ / KAYNAK DOĞRULANMADI')
            self.banner.configure(bg='#fff3cd', fg='#664d03')
            self.status.set(info.get('message') or 'GenNext girişini tamamlayın, ardından ilgili adımı yeniden deneyin.')
            self._update_workflow_buttons()
            return
        info=info or {};st=info.get('status');cmd=info.get('command');text=info.get('message') or ''
        self.refresh_table()
        if cmd in ('recovery_retry','recovery_open_watch'):
            try:self.web_recovery_panel.refresh()
            except Exception:pass
            if st in ('error','stopped'):
                self.banner_var.set('WEB KURTARMA HATASI / DURDURULDU · EKSİK KAYNAK AÇIK')
                self.banner.configure(bg='#f8d7da',fg='#842029')
                self.status.set(text or 'Web kurtarma tamamlanamadı; açık görevleri kontrol edin.')
                self._update_workflow_buttons()
                return
            self.browser_ready=True
            left=int(info.get('remaining',0) or 0)
            if left or info.get('manual'):
                self.banner_var.set('WEB EKSİKLERİ · KAYNAK / KİMLİK KONTROLÜ DEVAM EDİYOR')
                self.banner.configure(bg='#fff3cd',fg='#664d03')
            else:
                self.banner_var.set('WEB EKSİK KAYNAK İŞLEMİ TAMAMLANDI · DURUMU KONTROL EDİN')
                self.banner.configure(bg='#e8f2ff',fg='#174a7a')
            self.status.set(text)
            self._update_workflow_buttons()
            return
        if cmd in ('web_inspect_all','web_record_session'):
            self.close_safe=True
            try:self.web_panel.on_browser_result(info)
            except Exception:pass
            self.banner_var.set('WEB KOKLAYICI · '+str(info.get('pages',0))+' YAPISAL SAYFA KAYDEDİLDİ')
            self.banner.configure(bg='#e8f2ff' if st!='error' else '#fff3cd',
                                  fg='#174a7a' if st!='error' else '#664d03')
            self.status.set(text or 'Web inceleme tamamlandı.')
            self._update_workflow_buttons()
            return
        if st=='stopped':
            self.close_safe=True
            self.banner_var.set('■ DURDURULDU · GÜVENLE KAPATILABİLİR')
            self.banner.configure(bg='#dbeafe',fg='#1e3a5f');self.status.set(text or 'Durduruldu.')
            self._notify('NGS Mutasyon Havuzu','İşlem durduruldu.\n\nGüvenle kapatabilirsiniz.','warning')
            return
        if st=='error':
            self.close_safe=False
            names={'scan_runs':'RUNS TARAMASI','complete_latest':'SON POOL',
                'scan_report_selections':'DNA SELECTION',
                'scan_fusion_selections':'RNA SELECTION','run_queue':'KAYNAK KUYRUGU'}
            which=names.get(cmd,str(cmd or 'WEB ISLEMI').upper())
            error_kind=str(info.get('error_type') or 'beklenmeyen hata')
            # Local UI only. Never copy raw exception messages into the
            # anonymized V05 diagnostics ZIP.
            self.banner_var.set(f'HATA VAR · {which} DURDU · {error_kind} · EKSİKLER KORUNDU')
            self.banner.configure(bg='#f8d7da',fg='#842029')
            self.status.set((str(text)[:280] if text else 'Beklenmeyen tarayici hatasi')+
                ' · Ayrinti icin hata penceresi / islem gunlugu. Eksikler negatif sayilmaz.')
            self._update_workflow_buttons()
            return
        if cmd=='sniff_browser':
            self.close_safe=True
            kind='Fusion' if info.get('page_type')=='fusion' else 'RUNS/diğer'
            self.banner_var.set(f'✓ TARAYICI KOKLAYICI · {kind} yapısı kaydedildi')
            self.banner.configure(bg='#e8f2ff',fg='#174a7a')
            self.status.set('Tanı dosyası (özel veri yok): '+str(info.get('report_path') or 'DIAGNOSTICS'))
            self.browser_ready=True
            return
        if cmd=='open_browser':
            self.browser_ready=True;self.runs_scanned=False
            self.banner_var.set("✓ TARAYICI HAZIR · ŞİMDİ 2. RUNS'u TARA")
            self.banner.configure(bg='#e8f2ff',fg='#174a7a');self.status.set('Analiz platformu login durumunu kontrol edin; sonra RUNS taramasına geçin.')
        elif cmd=='scan_runs':
            self.browser_ready=True;self.runs_scanned=True
            n=info.get('technical_records')
            grp=DB.latest_pool_group()
            base=DB.pool_group_name(grp[0].get('pool_name')) if grp else 'SON POOL'
            states=', '.join(f"{r.get('pool_name')}={r.get('run_status') or '?'}" for r in grp)
            run_uuids=[r.get('run_uuid') or '' for r in grp]
            keys=[]
            for ru in run_uuids:keys.extend(x['record_key'] for x in DB.samples_for_run(ru))
            keys=list(dict.fromkeys(keys))
            unfinished=DB.unfinished_tasks(keys) if keys else []
            waiting=sum(1 for r in unfinished if r['status']=='waiting')
            review=sum(1 for r in unfinished if r['status']=='review')
            errors=sum(1 for r in unfinished if r['status'] in ('error','blocked'))
            incomplete=[r for r in grp if (r.get('run_status') or '').upper() not in ('','COMPLETED')]
            if incomplete or waiting:
                self.banner_var.set(f"✓ RUNS TARAMASI BİTTİ · {base} DEVAM EDİYOR · 3. ADIM PROCESSING OLSA BİLE HAZIR SAYFALARI DENER")
                self.banner.configure(bg='#e8f2ff',fg='#174a7a');self.status.set((states or '')+(f' · {waiting} sayfa/çıktı daha önce hazır değildi' if waiting else ''))
            elif review or errors:
                self.banner_var.set(f"✓ RUNS TARAMASI BİTTİ · {base} · KONTROL {review} / HATA {errors} · 3. ADIM EKSİKLERİ TAMAMLAR")
                self.banner.configure(bg='#fff3cd',fg='#664d03');self.status.set(states)
            else:
                self.banner_var.set(f"✓ RUNS TARAMASI BİTTİ · {base}{' · '+str(n)+' TEKNİK KAYIT' if n is not None else ''} · ŞİMDİ 3. SON POOL'U TAMAMLA")
                self.banner.configure(bg='#e8f2ff',fg='#174a7a');self.status.set(text)
        elif cmd=='scan_fusion_selections':
            self.browser_ready=True
            issues=int(info.get('review',0) or 0)+int(info.get('error',0) or 0)
            self.close_safe=issues==0
            self.banner_var.set(f"RNA Fusion Selection: {info.get('processed',0)} incelendi | rapora secili fusion {info.get('positive_fusion',0)} | 0 fusion secimi {info.get('no_fusion_selection',0)} | analiz suruyor {info.get('incomplete',0)} | kontrol {issues}")
            self.banner.configure(bg='#fff3cd' if issues else '#d1e7dd',fg='#664d03' if issues else '#0f5132')
            self.status.set('RNA Fusion arşivi güncellendi. 0 secim = RNA panel negatif degil.')
        elif cmd=='scan_report_selections':
            self.browser_ready=True
            issues=int(info.get('error',0) or 0)+int(info.get('review',0) or 0)
            self.close_safe=issues==0
            self.banner_var.set(f"GenNext Selection: {info.get('processed',0)} incelendi | secili pozitif {info.get('positive',0)} | SNV 0 secim {info.get('negative_snv',0)} | analiz suruyor {info.get('incomplete',0)} | kontrol {issues}")
            self.banner.configure(bg='#fff3cd' if issues else '#d1e7dd',fg='#664d03' if issues else '#0f5132')
            self.status.set(text or 'Geriye donuk Selection taramasi tamamlandi. Arsivi yenileyin.')
        elif cmd in ('igv_open','igv_capture','igv_batch_capture'):
            try:self.review_tab.on_browser_result(info)
            except Exception:pass
            self.close_safe=True
            if cmd=='igv_batch_capture':
                ready=int(info.get('ai_ready',0) or 0);review=int(info.get('review',0) or 0);err=int(info.get('error',0) or 0)
                self.banner_var.set(f'✓ IGV OTOMATİK HAZIRLAMA BİTTİ · AI-READY {ready} · AYAR KONTROL {review} · HATA {err}')
                self.banner.configure(bg='#d1e7dd' if err==0 and review==0 else '#fff3cd',fg='#0f5132' if err==0 and review==0 else '#664d03')
            elif cmd=='igv_capture' and info.get('screenshot_path'):
                self.banner_var.set('✓ IGV GÖRÜNTÜSÜ KAYDEDİLDİ · '+('AI-READY' if info.get('ai_ready') else 'AYAR KONTROL GEREKLİ'))
                self.banner.configure(bg='#d1e7dd' if info.get('ai_ready') else '#fff3cd',fg='#0f5132' if info.get('ai_ready') else '#664d03')
            else:
                self.banner_var.set('✓ IGV AÇILDI · DUPLICATE / SORT AYARLARI UYGULANDI' if info.get('ai_ready') else '✓ IGV AÇILDI · MANUEL AYAR KONTROLÜ GEREKEBİLİR')
                self.banner.configure(bg='#e8f2ff',fg='#174a7a')
            self.status.set(text or 'IGV işlemi tamamlandı.')
        elif cmd in ('complete_latest','run_queue'):
            all_complete=bool(info.get('all_complete'))
            err=int(info.get('error',0) or 0);remaining=int(info.get('remaining',0) or 0)
            waiting=int(info.get('waiting_remaining',info.get('waiting',0)) or 0)
            blocked=int(info.get('blocked_remaining',info.get('blocked',0)) or 0)
            review=int(info.get('review_remaining',info.get('review',0)) or 0)
            pending=int(info.get('pending_remaining',0) or 0)
            archive_remaining=len(DB.unfinished_tasks())
            selection_unresolved=int(info.get('selection_unresolved',0) or 0)
            if cmd=='complete_latest' and selection_unresolved:
                self.close_safe=False
                self.banner_var.set(f"⚠ {info.get('pool_name') or 'SON CALISMA'} · {selection_unresolved} SELECTION DURUMU DOGRULANAMADI")
                self.banner.configure(bg='#fff3cd',fg='#664d03')
                self.status.set((text or '')+' · SNV/RNA Selection belirsiz; otomatik negatif veya raporlandi kabul edilmez.')
            elif cmd=='complete_latest' and waiting>0 and err==0 and blocked==0 and pending==0:
                self.close_safe=True
                self.banner_var.set(f"⏳ {info.get('pool_name') or 'SON ÇALIŞMA'} · {waiting} SAYFA/ÇIKTI HENÜZ HAZIR DEĞİL · HAZIR OLANLAR ALINDI")
                self.banner.configure(bg='#fff3cd',fg='#664d03');self.status.set((info.get('run_states') or text or '')+' · PROCESSING etiketi tek başına engel değildir; ilgili sayfa hazır olunca yeniden deneyin.')
                self._notify('NGS Mutasyon Havuzu','Hazır olan sayfalar alındı. Bazı sayfa/çıktılar henüz hazır değil; bunlar hata sayılmadı.\n\nGüvenle kapatabilir, sonra Son Çalışma Eksiklerini yeniden deneyebilirsiniz.')
            elif all_complete and err==0 and review==0 and remaining==0:
                self.close_safe=True
                if archive_remaining==0:
                    self.banner_var.set('✓ TÜM TARAMALAR BİTTİ · BİLGİLERİN TAMAMI ALINDI · GÜVENLE KAPATILABİLİR')
                    self.banner.configure(bg='#d1e7dd',fg='#0f5132');self.status.set(text or 'Tamamlandı.')
                    self._notify('NGS Mutasyon Havuzu','Tüm taramalar bitti.\nBilgilerin tamamı alındı.\n\nGüvenle kapatabilirsiniz.')
                else:
                    scope='SON ÇALIŞMA TAMAM' if cmd=='complete_latest' else 'ÇALIŞTIRILAN KUYRUK TAMAM'
                    self.banner_var.set(f'✓ {scope} · GÜVENLE KAPATILABİLİR')
                    self.banner.configure(bg='#d1e7dd',fg='#0f5132');self.status.set((text or 'İşlem tamamlandı.')+f' · Arşiv geçmişinde {archive_remaining} eski eksik/kontrol kaydı Gelişmiş bölümünde saklı.')
            else:
                self.close_safe=False
                self.banner_var.set(f'⚠ İŞLEM BİTTİ · BU KUYRUKTA KALAN {remaining} · KONTROL {review} · HATA {err} · İLGİLİ KARTA TIKLAYIN')
                self.banner.configure(bg='#fff3cd',fg='#664d03');self.status.set(text)
        self._update_workflow_buttons()

    def pump(self):
        try:
            while True:
                typ,msg=self.q.get_nowait()
                if typ=='log':self.log.insert('end',str(msg)+'\n');self.log.see('end')
                elif typ=='status':self.status.set(str(msg))
                elif typ=='refresh':
                    self.refresh_table()
                    try:self.web_recovery_panel.refresh()
                    except Exception:pass
                elif typ=='error':
                    self.close_safe=False;self.banner_var.set('HATA VAR · DIAGNOSTICS KONTROL EDİN');self.banner.configure(bg='#f8d7da',fg='#842029')
                    if not (hasattr(self,'remote_bridge') and self.remote_bridge.suppress_modal_error()):messagebox.showerror('Hata',str(msg))
                    else:self.log.insert('end','Uzaktan RUNS işleminde hata oluştu; Telegram tarafına hasta verisi içermeyen genel hata durumu gönderildi.\n')
                elif typ=='warning':
                    if not (hasattr(self,'remote_bridge') and self.remote_bridge.suppress_modal_error()):messagebox.showwarning('Çalışıyor',str(msg))
                    else:self.log.insert('end','Uzaktan RUNS komutu sırasında tarayıcı meşgul uyarısı oluştu.\n')
                elif typ=='auto_web_result':
                    try:
                        self.web_panel.refresh(highlight=Path(msg.get('session_dir') or ''))
                        self.web_panel.info.set('Otomatik WEB tanısı kaydedildi: '+
                              str(msg.get('pages',0))+' hafif sayfa görüntüsü / '+
                              str(msg.get('issues',0))+' uyarı. İsterseniz YZ Tanı ZIP oluşturun.')
                    except Exception:pass
                elif typ=='busy':self._set_busy_ui(bool(msg))
                elif typ=='job_done':
                    self._handle_job_done(msg)
                    if hasattr(self,'remote_bridge'):self.remote_bridge.on_job_done(msg)
        except queue.Empty:pass
        self.after(200,self.pump)

    def _auto_web_recovery_tick(self):
        """Idle-only, bounded source recovery; never interrupts a browser job.

        One small pass every eight minutes, strictly subject to backoff and
        three attempts per source. Reopening the application preserves its queue.
        """
        try:
            if (CFG.get('web_recovery_auto',True) and self.browser_ready
                    and self.runs_scanned and not self.job_busy and not self.browser.busy):
                store=RecoveryStore(BASE)
                try:
                    keys=DB.latest_record_keys()
                    store.synchronize(DB,keys)
                    due=store.due(keys,limit=1)
                finally:store.close()
                if due:self._submit('recovery_retry',limit=4,force=False)
        except Exception as exc:
            try:self.log.insert('end','Web eksikleri otomatik kontrol: '+type(exc).__name__+'\n')
            except Exception:pass
        finally:
            self.after(8*60*1000,self._auto_web_recovery_tick)

    def _diag_count(self):
        p=(BASE/CFG['diagnostics_dir']).resolve();p.mkdir(parents=True,exist_ok=True)
        try:return sum(1 for x in p.iterdir() if x.is_dir() and any(x.iterdir()))
        except Exception:return 0

    def refresh_table(self):
        if not hasattr(self,'tree'):return
        current=self.tree.selection()[0] if self.tree.selection() else None
        for x in self.tree.get_children():self.tree.delete(x)
        total=complete=errors=missing=0
        try:
            rows=DB.samples();total=len(rows)
            for r in rows:
                r=dict(r)
                rk=r['record_key'];ov=DB.overall_status(rk)
                if ov=='✓':complete+=1
                elif ov=='HATA':errors+=1
                else:missing+=1
                family=r['case_family'] or r['patient_id'] or ''
                cm=DB.get_case_metadata(family)
                other_mp=', '.join(DB.related_patient_ids_for_case(family)) or '—'
                vals=[ov,DB.display_pool_name(r['run_uuid'],r['pool_name']),cm.get('full_name','') or '',r['patient_id'],other_mp,family,r['sample_type']]
                for tp in ['run','clinical','snv','cnv','qc','fusion']:
                    st=DB.task_status(rk,tp);vals.append({'done':'✓','error':'HATA','blocked':'RUN HATASI','waiting':'BEKLİYOR','review':'KONTROL','pending':'○','not_available':'—',None:'—'}.get(st,st or '—'))
                from gennext_selection_store import current_status
                gen=current_status(DB,rk) or {}
                state=gen.get('result_status')
                label={'selected_positive':f"✓ {gen.get('included_count',0)} secili",
                       'no_snv_selection':'✓ SNV secim 0',
                       'analysis_incomplete':f"DEVAM · {gen.get('included_count',0)} on secim",
                       'analysis_incomplete_unread':'DEVAM',
                       'needs_selection_refresh':'KONTROL / DEGISIK',
                       'awaiting_selection':'KONTROL / BEKLIYOR'}.get(state,'—')
                if (r.get('sample_type') or '').upper()=='RNA':
                    from gennext_fusion_selection_store import current_fusion_status
                    rn=current_fusion_status(DB,rk) or {}
                    state=rn.get('result_status')
                    label={'fusion_selected_positive':f"✓ Fusion {rn.get('included_count',0)} secili",
                           'no_fusion_selection':'✓ Fusion secim 0',
                           'fusion_analysis_incomplete':'DEVAM · Fusion',
                           'fusion_analysis_incomplete_unread':'DEVAM · Fusion',
                           'needs_fusion_refresh':'RNA FUSION KONTROL',
                           'fusion_identity_review':'RNA FUSION MP KONTROL',
                           'awaiting_fusion_selection':'RNA FUSION BEKLIYOR'}.get(state,'—')
                    if rn.get('last_error'):label+=' !'
                elif gen.get('last_error'):label+=' !'
                vals.append(label)
                tags=('complete',) if ov=='✓' else ('error',) if ov=='HATA' else ('review',) if ov=='KONTROL' else ('waiting',) if ov=='BEKLİYOR' else ('missing',)
                self.tree.insert('', 'end',iid=rk,values=vals,tags=tags)
            if current and self.tree.exists(current):self.tree.selection_set(current)
        except Exception as e:self.log.insert('end',f'UI veritabanı yenileme uyarısı: {e}\n')
        latest=DB.latest_pool();latest_name=(latest.get('pool_name') if latest else '—')
        try:
            grp=DB.latest_pool_group()
            if grp:
                base=DB.display_pool_name(grp[0].get('run_uuid'),grp[0].get('pool_name'))
                states='/'.join(sorted({(r.get('run_type') or '?')+':'+(r.get('run_status') or '?') for r in grp}))
                keys=[]
                for r in grp:keys.extend(x['record_key'] for x in DB.samples_for_run(r.get('run_uuid') or ''))
                unfinished=DB.unfinished_tasks(list(dict.fromkeys(keys))) if keys else []
                wait=sum(1 for x in unfinished if x['status']=='waiting')
                review=sum(1 for x in unfinished if x['status']=='review')
                bad=sum(1 for x in unfinished if x['status'] in ('error','blocked'))
                suffix=[]
                if wait:suffix.append(f'{wait} bekliyor')
                if review:suffix.append(f'{review} kontrol')
                if bad:suffix.append(f'{bad} hata')
                latest_name=f'{base} · {states}'+((' · '+', '.join(suffix)) if suffix else '')
        except Exception:
            pass
        latest_keys=self._latest_record_keys()
        latest_unfinished=list(DB.unfinished_tasks(latest_keys)) if latest_keys else []
        latest_wait=[x for x in latest_unfinished if x['status'] in ('pending','waiting')]
        latest_review=[x for x in latest_unfinished if x['status']=='review']
        latest_bad=[x for x in latest_unfinished if x['status'] in ('error','blocked')]
        wait_samples=len({x['record_key'] for x in latest_wait});issue_samples=len({x['record_key'] for x in latest_review+latest_bad})
        self.stat_total.set(str(total));self.stat_complete.set(str(complete))
        self.stat_missing.set(f'{wait_samples} örnek / {len(latest_wait)} adım')
        self.stat_error.set(f'{issue_samples} örnek · kontrol {len(latest_review)} / hata {len(latest_bad)}')
        self.stat_latest.set(latest_name or '—')
        n=self._diag_count();self.diag_var.set('DIAGNOSTICS: boş ✓' if n==0 else f'DIAGNOSTICS: {n} geçmiş tanı paketi')
        for obj,method in ((self.mut_tab,'refresh_samples'),
                           (self.spread_tab,'refresh_pools'),
                           (self.patient_workspace,'refresh'),
                           (self.ai_tab,'refresh_pools'),
                           (self.review_tab,'refresh')):
            try:getattr(obj,method)()
            except Exception as exc:
                self.log.insert('end',f'UI yenileme uyarısı ({method}): {exc}\n')
        self.data_changed()

    def _latest_task_file(self,record_key,task_type):
        for row in DB.files_for_record(record_key,task_type):
            p=Path(row['path']);p=p if p.is_absolute() else BASE/p
            if p.is_file():return p
        return None

    def _retry_record_missing(self,row,window=None):
        if self.job_busy:
            messagebox.showwarning('Çalışıyor','Başka bir tarama sürüyor.',parent=window or self);return
        rk=row['record_key'];typ=(row['sample_type'] or '').upper();dna=typ in ('DNA','DNA+RNA');rna=typ in ('RNA','DNA+RNA')
        if not config_ready(CFG):
            messagebox.showwarning('Platform ayarı gerekli','Önce kurum içi Runs URL ayarını kontrol edin.',parent=window or self);return
        if self._submit('run_queue',dna=dna,rna=rna,only_missing=True,record_keys=[rk]):
            if window:window.destroy()

    def _manual_attach_task_file(self,row,window=None):
        rk=row['record_key'];tp=row['task_type'];sample=DB.get_sample(rk) or {}
        filename=filedialog.askopenfilename(title=f"{sample.get('patient_id') or ''} · {tp.upper()} için manuel kaynak dosyası")
        if not filename:return
        src=Path(filename)
        if tp=='qc':
            ok,reason,_info=validate_qc_source(src,sample.get('patient_id') or '')
            if not ok:
                messagebox.showerror('QC kaynağı doğrulanamadı',reason+'\n\nQC adımı tamamlanmadı.',parent=window or self);return
        if tp=='fusion':
            ok,reason,info=validate_fusion_source(src)
            if not ok:
                messagebox.showerror('Fusion kaynağı doğrulanamadı',reason+'\n\nFusion adımı tamamlanmadı. Negatiflik için özel Fusion kontrol penceresini kullanın.',parent=window or self);return
        safe=lambda x:re.sub(r'[^A-Za-z0-9._-]+','_',str(x or ''))[:100] or 'X'
        out=BASE/CFG.get('output_dir','OUTPUT')/'MANUAL'/safe(sample.get('pool_name'))/safe(sample.get('patient_id'))/safe(sample.get('sample_uuid'))
        out.mkdir(parents=True,exist_ok=True);dest=out/(time.strftime('%Y%m%d_%H%M%S')+'_'+safe(src.name))
        shutil.copy2(src,dest);raw=dest.read_bytes();sha=hashlib.sha256(raw).hexdigest()
        DB.add_file({'record_key':rk,'sample_uuid':sample.get('sample_uuid'),'pool_name':sample.get('pool_name'),'patient_id':sample.get('patient_id'),'file_type':tp,'path':str(dest.relative_to(BASE)),'sha256':sha,'size':len(raw)})
        DB.set_task(rk,tp,'done',f'Manuel kaynak dosyası eklendi: {dest.name}')
        self.data_changed();self.refresh_table()
        messagebox.showinfo('Manuel kaynak',f'Dosya arşive eklendi ve {tp.upper()} adımı tamamlandı.\n\n{dest}',parent=window or self)
        if window:window.destroy()

    def _manual_complete_task(self,row,window=None):
        rk=row['record_key'];tp=row['task_type'];sample=DB.get_sample(rk) or {}
        if tp=='qc':
            messagebox.showwarning('QC manuel tamamlama','QC görevi yalnız not yazarak tamamlanamaz.\n\nGerçek MultiQC ZIP/TSV/JSON kaynağını “kaynak dosyası ekle” ile ekleyin veya yeniden indirin.',parent=window or self);return
        if tp=='fusion':
            self._fusion_review_window(row,window or self);return
        note=simpledialog.askstring('Manuel tamamlama',f"{sample.get('patient_id') or ''} · {tp.upper()} için neden/neyi kontrol ettiğinizi yazın:",parent=window or self)
        if note is None:return
        note=note.strip()
        if len(note)<5:
            messagebox.showwarning('Manuel tamamlama','En az birkaç kelimelik kontrol notu yazın.',parent=window or self);return
        if not messagebox.askyesno('Manuel tamamlama','Bu adımı otomatik kaynak toplama yerine MANUEL KONTROL ile tamamlandı olarak işaretlemek istiyor musunuz?\n\nNot ve denetim kaydı saklanacaktır.',parent=window or self):return
        safe=lambda x:re.sub(r'[^A-Za-z0-9._-]+','_',str(x or ''))[:100] or 'X'
        out=BASE/CFG.get('output_dir','OUTPUT')/'MANUAL'/safe(sample.get('pool_name'))/safe(sample.get('patient_id'))/safe(sample.get('sample_uuid'));out.mkdir(parents=True,exist_ok=True)
        dest=out/(time.strftime('%Y%m%d_%H%M%S')+f'_{safe(tp.upper())}_MANUAL_COMPLETE.json')
        payload={'manual_completion':True,'record_key':rk,'task_type':tp,'note':note,'created_at':time.strftime('%Y-%m-%dT%H:%M:%S')}
        dest.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8');raw=dest.read_bytes()
        DB.add_file({'record_key':rk,'sample_uuid':sample.get('sample_uuid'),'pool_name':sample.get('pool_name'),'patient_id':sample.get('patient_id'),'file_type':tp,'path':str(dest.relative_to(BASE)),'sha256':hashlib.sha256(raw).hexdigest(),'size':len(raw)})
        DB.set_task(rk,tp,'done','Manuel kontrol ile tamamlandı: '+note)
        self.data_changed();self.refresh_table();messagebox.showinfo('Manuel tamamlama',f'{tp.upper()} adımı manuel kontrol ile tamamlandı.\n\n{dest.name}',parent=window or self)
        if window:window.destroy()

    def _fusion_review_window(self,row,parent=None):
        rk=row['record_key'];sample=DB.get_sample(rk) or {};path=self._latest_task_file(rk,'fusion')
        obj={};visible=''
        if path and path.suffix.lower()=='.json':
            try:obj=json.loads(path.read_text(encoding='utf-8-sig',errors='replace'));visible=str(obj.get('visible_text') or '')
            except Exception:pass
        win=tk.Toplevel(parent or self);win.title(f"Fusion kontrolü · {sample.get('patient_id') or ''}");win.geometry('1180x760');win.transient(parent or self)
        top=ttk.Frame(win,padding=10);top.pack(fill='x')
        ttk.Label(top,text=f"{sample.get('patient_id') or ''} · {sample.get('sample_type') or ''} · FUSION",font=('Segoe UI',12,'bold')).pack(anchor='w')
        ttk.Label(top,text='Tercih edilen Fusion TSV/CSV alınamadı. Aşağıdaki metin yalnız sayfadaki görünür yedektir; otomatik NEGATİF sayılmaz.',foreground='#8a5a00',wraplength=1100).pack(anchor='w',pady=(4,0))
        if obj.get('export_error'):ttk.Label(top,text='Export hatası: '+str(obj.get('export_error')),foreground='#9b1c1c',wraplength=1100).pack(anchor='w',pady=(4,0))
        area=ttk.Frame(win);area.pack(fill='both',expand=True,padx=10,pady=5)
        txt=tk.Text(area,wrap='word',font=('Consolas',9));ys=ttk.Scrollbar(area,orient='vertical',command=txt.yview);txt.configure(yscrollcommand=ys.set)
        txt.pack(side='left',fill='both',expand=True);ys.pack(side='right',fill='y');txt.insert('1.0',visible or '[Görünür Fusion metni bulunamadı.]');txt.configure(state='disabled')
        foot=ttk.Frame(win,padding=10);foot.pack(fill='x')
        ttk.Button(foot,text='Fusion exportu yeniden dene',command=lambda:self._retry_record_missing(row,win)).pack(side='left')
        ttk.Button(foot,text='Fusion dosyası ekle → tamamla',command=lambda:self._manual_attach_task_file(row,win)).pack(side='left',padx=6)
        def negative():
            if (sample.get('sample_type') or '').upper() not in ('RNA','DNA+RNA'):
                messagebox.showwarning('Negatif RNA','Bu seçenek RNA teknik örneği içindir.',parent=win);return
            if not messagebox.askyesno('Manuel RNA negatif onayı','Görünür Fusion sonuçlarını manuel olarak kontrol ettiniz ve raporlanabilir füzyon/değişiklik OLMADIĞINI onaylıyor musunuz?\n\nBu işlem otomatik çıkarım değildir; sizin manuel kararınız olarak kaydedilir.',parent=win):return
            try:self.archive.store.set_assay_result(rk,'negative','Fusion export alınamadı; sayfa görünür metni kullanıcı tarafından manuel kontrol edilerek RNA negatif olarak onaylandı.')
            except Exception as e:messagebox.showerror('RNA negatif',str(e),parent=win);return
            # Preserve an explicit manual evidence file instead of silently treating the fallback as a full export.
            safe=lambda x:re.sub(r'[^A-Za-z0-9._-]+','_',str(x or ''))[:100] or 'X'
            out=BASE/CFG.get('output_dir','OUTPUT')/'MANUAL'/safe(sample.get('pool_name'))/safe(sample.get('patient_id'))/safe(sample.get('sample_uuid'));out.mkdir(parents=True,exist_ok=True)
            dest=out/(time.strftime('%Y%m%d_%H%M%S')+'_FUSION_MANUAL_NEGATIVE.json')
            payload={'manual_review':True,'result':'negative','record_key':rk,'source_fallback':str(path or ''),'visible_text_sha256':hashlib.sha256(visible.encode('utf-8')).hexdigest(),'created_at':time.strftime('%Y-%m-%dT%H:%M:%S')}
            dest.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8');raw=dest.read_bytes()
            DB.add_file({'record_key':rk,'sample_uuid':sample.get('sample_uuid'),'pool_name':sample.get('pool_name'),'patient_id':sample.get('patient_id'),'file_type':'fusion','path':str(dest.relative_to(BASE)),'sha256':hashlib.sha256(raw).hexdigest(),'size':len(raw)})
            DB.set_task(rk,'fusion','done','Manuel RNA negatif kontrolü ile tamamlandı; tam Fusion exportu alınmadı.')
            self.data_changed();self.refresh_table();messagebox.showinfo('RNA negatif','Manuel negatif sonuç ve denetim kaydı saklandı. Fusion adımı tamamlandı.',parent=win);win.destroy()
        ttk.Button(foot,text='MANUEL RNA NEGATİF → tamamla',command=negative).pack(side='left',padx=6)
        ttk.Button(foot,text='Arşivde hastayı aç / manuel füzyon ekle',command=lambda:(win.destroy(),self.open_patient_archive(rk))).pack(side='left',padx=6)
        ttk.Button(foot,text='Kapat',command=win.destroy).pack(side='right')

    def show_missing(self,mode='all',scope='archive'):
        keys=self._latest_record_keys() if scope=='latest' else None
        rows=list(DB.unfinished_task_details(keys)) if keys else ([] if scope=='latest' else list(DB.unfinished_task_details()))
        if mode=='error':rows=[r for r in rows if r['status'] in (('review','error','blocked') if scope=='latest' else ('error','blocked'))]
        elif mode=='missing':rows=[r for r in rows if r['status'] in ('pending','waiting','review')]
        if scope=='latest':
            title={'error':'Son Çalışma · Kontrol / Hata','missing':'Son Çalışma · Bekleyen / Eksik'}.get(mode,'Son Çalışma · Eksikler')
        else:
            title={'error':'Arşiv Geçmiş Hataları','missing':'Arşiv Geçmiş Eksikleri / Kontrol'}.get(mode,'Arşiv Geçmiş Eksik / Hata')
        win=tk.Toplevel(self);win.title(title);win.geometry('1320x650');win.minsize(950,440)
        head=ttk.Frame(win,padding=10);head.pack(fill='x')
        ttk.Label(head,text=f'{title}: {len(rows)} adım',font=('Segoe UI',12,'bold')).pack(side='left')
        ttk.Label(head,text='BEKLİYOR = ilgili sayfa/çıktı henüz hazır değil · KONTROL = kaynak var ama doğrulama gerekiyor · HATA = gerçek toplama hatası. PROCESSING tek başına hata değildir.',foreground='#667085').pack(side='left',padx=14)
        cols=('pool','name','mp','type','task','status','attempts','error','updated')
        area=ttk.Frame(win);area.pack(fill='both',expand=True,padx=10)
        tr=ttk.Treeview(area,columns=cols,show='headings',selectmode='extended')
        widths={'pool':140,'name':180,'mp':115,'type':65,'task':90,'status':90,'attempts':70,'error':470,'updated':145}
        labels={'pool':'POOL','name':'AD SOYAD','mp':'ÖRNEK','type':'TİP','task':'ADIM','status':'DURUM','attempts':'DENEME','error':'SON HATA / AÇIKLAMA','updated':'SON GÜNCELLEME'}
        rowmap={}
        for c in cols:tr.heading(c,text=labels[c],anchor='w');tr.column(c,width=widths[c],anchor='center' if c in ('type','task','status','attempts') else 'w')
        for i,r0 in enumerate(rows):
            r=dict(r0);rowmap[str(i)]=r
            tr.insert('', 'end',iid=str(i),values=(r['pool_name'] or '',r['full_name'] or '',r['patient_id'] or '',r['sample_type'] or '',r['task_type'] or '',r['status'] or '',r['attempts'] or 0,r['last_error'] or '',r['updated_at'] or ''))
        ys=ttk.Scrollbar(area,orient='vertical',command=tr.yview);xs=ttk.Scrollbar(area,orient='horizontal',command=tr.xview);tr.configure(yscrollcommand=ys.set,xscrollcommand=xs.set)
        tr.grid(row=0,column=0,sticky='nsew');ys.grid(row=0,column=1,sticky='ns');xs.grid(row=1,column=0,sticky='ew');area.rowconfigure(0,weight=1);area.columnconfigure(0,weight=1)
        def selected_rows():return [rowmap[x] for x in tr.selection() if x in rowmap]
        def selected():
            arr=selected_rows();return arr[0] if arr else None
        def retry():
            arr=selected_rows()
            if not arr:return
            if self.job_busy:messagebox.showwarning('Çalışıyor','Başka bir tarama sürüyor.',parent=win);return
            keys=list(dict.fromkeys(r['record_key'] for r in arr));types={(r['sample_type'] or '').upper() for r in arr}
            dna=bool(types.intersection({'DNA','DNA+RNA'}));rna=bool(types.intersection({'RNA','DNA+RNA'}))
            if self._submit('run_queue',dna=dna,rna=rna,only_missing=True,record_keys=keys):win.destroy()
        def attach():
            r=selected()
            if r:self._manual_attach_task_file(r,win)
        def inspect():
            r=selected()
            if not r:return
            if r['task_type']=='fusion':self._fusion_review_window(r,win)
            else:messagebox.showinfo('Manuel kontrol','Bu ayrıntılı kontrol penceresi Fusion için hazırlanmıştır. Diğer adımlarda yeniden dene veya kaynak dosya ekle kullanın.',parent=win)
        def archive():
            r=selected()
            if r:win.destroy();self.open_patient_archive(r['record_key'])
        tr.bind('<Double-1>',lambda e:inspect())
        def retry_all():
            if not rows:return
            if self.job_busy:messagebox.showwarning('Çalışıyor','Başka bir tarama sürüyor.',parent=win);return
            if not config_ready(CFG):messagebox.showwarning('Platform ayarı gerekli','Kurum içi Runs URL ayarını kontrol edin.',parent=win);return
            keys=list(dict.fromkeys(r['record_key'] for r in rows));types={(r['sample_type'] or '').upper() for r in rows}
            dna=bool(types.intersection({'DNA','DNA+RNA'}));rna=bool(types.intersection({'RNA','DNA+RNA'}))
            if messagebox.askyesno('Toplu yeniden dene',f'{len(keys)} teknik örneğin bu ekrandaki eksik/kontrol adımları yeniden denensin mi? Hazır sayfalar alınır; hazır olmayanlar BEKLİYOR kalır.',parent=win):
                if self._submit('run_queue',dna=dna,rna=rna,only_missing=True,record_keys=keys):win.destroy()
        foot=ttk.Frame(win,padding=10);foot.pack(fill='x')
        ttk.Button(foot,text='Seçili örnek(ler)in eksiklerini yeniden dene',command=retry).pack(side='left')
        ttk.Button(foot,text='BU LİSTEDEKİ KONTROL/EKSİKLERİ yeniden dene',command=retry_all).pack(side='left',padx=6)
        ttk.Button(foot,text='Seçili adıma kaynak dosyası ekle → tamamla',command=attach).pack(side='left',padx=6)
        ttk.Button(foot,text='Manuel doğrula → tamamla',command=lambda:(self._manual_complete_task(selected(),win) if selected() else None)).pack(side='left',padx=6)
        ttk.Button(foot,text='Seçili adımı incele',command=inspect).pack(side='left',padx=6)
        ttk.Button(foot,text='Arşivde hastayı aç',command=archive).pack(side='left',padx=6)
        ttk.Button(foot,text='Kapat',command=win.destroy).pack(side='right')
        if not rows:ttk.Label(foot,text='✓ Bu grupta kayıt yok.',foreground='#17643a',font=('Segoe UI',10,'bold')).pack(side='left')

    def open_variant_archive(self,variant_key,gene='',change=''):
        self.workspaces.select(self.archive)
        self.archive.open_variant(variant_key,gene,change)

    def open_selected_mutations(self,event=None):
        sel=self.tree.selection()
        if not sel:return
        self.open_record_mutations(sel[0])

    def open_selected_metadata(self):
        sel=self.tree.selection()
        if not sel:return
        self.open_record_metadata(sel[0])

    def open_selected_dual_report(self):
        sel=self.tree.selection()
        if not sel:return
        if self.patient_workspace.select_record(sel[0],'dual'):
            self.workspaces.select(self.patient_workspace)

    def _ai_reviews_changed(self,event=None):
        self.data_changed()
        if hasattr(self,'mut_tab') and self.mut_tab.current_record:
            self.mut_tab.load()
        if hasattr(self,'patient_workspace') and hasattr(self.patient_workspace,'ai_panel'):
            panel=self.patient_workspace.ai_panel
            if panel.record_key:panel.load_record(panel.record_key)

    def data_changed(self):
        if hasattr(self,'archive'):
            self.archive.invalidate()
            if self.workspaces.select()==str(self.archive):self.archive.refresh()

    def _metadata_changed(self):
        if hasattr(self,'patient_workspace'):self.patient_workspace.metadata_changed()
        self.data_changed()

    def _qc_metadata_changed(self):
        self._metadata_changed()

    def _workspace_changed(self,event=None):
        if event is not None and event.widget is not self.workspaces:return
        if hasattr(self,'archive'):
            selected=self.workspaces.select()
            is_archive=selected==str(self.archive)
            DB.set_meta('last_workspace','archive' if is_archive else 'patient' if selected==str(self.patient_workspace) else 'collect')
            if is_archive:self.archive.activate()
            if hasattr(self,'qc_workspace') and selected==str(self.qc_workspace):
                self.qc_panel.auto_sync_once()
                try:self.qc_panel.refresh_shared_metadata(quiet=True)
                except Exception:pass

    def _working_tab_changed(self,event=None):
        if event is not None and event.widget is not self.nb:return
        if self.nb.select()==str(self.collect_tab):self.banner_host.pack(fill='x',before=self.nb)
        else:self.banner_host.pack_forget()
        if self.nb.select()==str(self.mut_tab) and self.mut_tab.current_record:self.mut_tab.load()
        elif self.nb.select()==str(self.spread_tab):self.spread_tab.refresh_pools()
        elif self.nb.select()==str(self.ai_tab):self.ai_tab.refresh_pools()
        elif self.nb.select()==str(self.review_tab):self.review_tab.refresh()

    def open_patient_archive(self,record_key=None):
        self.workspaces.select(self.archive)
        if record_key:self.archive.select_record(record_key)
        else:self.archive.activate()

    def open_record_metadata(self,record_key):
        if self.patient_workspace.select_record(record_key,'metadata'):
            self.workspaces.select(self.patient_workspace)

    def _scan_from_patient_workspace(self):
        # Scanning remains on the single existing browser worker.
        self.workspaces.select(self.workframe)
        self.nb.select(self.collect_tab)
        self.start_scan()

    def open_record_mutations(self,record_key,report_item=None):
        self.workspaces.select(self.workframe)
        self.nb.select(self.mut_tab)
        if report_item is not None:
            self.mut_tab.focus_report_variant(record_key,report_item)
        else:self.mut_tab.select_record(record_key)

    def open_review_workspace(self):
        self.workspaces.select(self.workframe);self.nb.select(self.review_tab)

    def open_diag(self):
        import os
        p=(BASE/CFG['diagnostics_dir']).resolve();p.mkdir(parents=True,exist_ok=True)
        try:os.startfile(str(p))
        except Exception:pass

    def on_close(self):
        if not self.meta_tab.confirm_leave():return
        try:
            self.review_tab.patient_panel.flush_note()
            self.patient_workspace.ai_panel.flush_note()
        except Exception as exc:
            messagebox.showerror('Not kaydedilemedi',str(exc),parent=self);return
        if self.ai_tab._generation_busy:
            if not messagebox.askyesno('YZ paketi sürüyor','YZ paketi henüz bitmedi. Üretimi iptal edip kapatmak istiyor musunuz?',parent=self):return
            self.ai_tab.cancel_generation()
        if self.job_busy and not messagebox.askyesno('İşlem sürüyor','Bir tarama hâlâ çalışıyor. HEMEN DURDUR ve programı kapat?'):
            return
        try:
            if hasattr(self,'remote_bridge'):self.remote_bridge.close()
        except Exception:pass
        try:
            if self.job_busy:self.browser.request_stop(True)
            self.browser.shutdown()
        except Exception:pass
        try:self.qc_panel.cleanup(show_errors=False)
        except Exception:pass
        try:self.meta_tab.shutdown_enlil()
        except Exception:pass
        try:DB.cx.close()
        except Exception:pass
        self.destroy()


if __name__=='__main__':App().mainloop()

# === NGS_WEB_IGV_APP_REFRESH_V0435 ===
_ngs_app_job_done_v0435_prev = App._handle_job_done


def _ngs_app_job_done_v0435(self, info):
    info = info or {}
    if info.get('status') == 'stopped' and info.get('command') in ('igv_capture','igv_batch_capture'):
        try:self.review_tab.on_browser_result(info)
        except Exception:pass
    result = _ngs_app_job_done_v0435_prev(self, info)
    if info.get('command') == 'igv_batch_capture' and info.get('status') != 'stopped':
        try:
            ready=int(info.get('session_ready',0) or info.get('ai_ready',0) or 0);total=int(info.get('session_total',0) or info.get('requested',0) or 0);err=int(info.get('session_error',0) or info.get('error',0) or 0)
            self.banner_var.set(f'✓ WEB IGV VERİ TOPLAMA BİTTİ · TAMAM {ready}/{total} · HATA {err}')
            self.banner.configure(bg='#d1e7dd' if err==0 else '#fff3cd',fg='#0f5132' if err==0 else '#664d03')
        except Exception:pass
    return result

App._handle_job_done = _ngs_app_job_done_v0435
