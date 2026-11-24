"""Unified patient/block/standards workspace. Existing ENLIL worker is unchanged."""
from __future__ import annotations
import copy
import json
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

from metadata_editor import MetadataEditor
from patient_dual_report_panel import DualReportPanel
from patient_ngs_history import find_previous_ngs, ui_summary
from qc_enlil import NGSQCPanel, SampleMetrics
from rna_qc_bundle import analyze_bundle, import_bundle
from patient_workspace_model import (ordered_runs, case_key, is_repeat, related_records,
    load_record_metrics, latest_path, read_observations, compare_observations)
from prepool_workspace import PrePoolPanel


def table(parent, cols, labels, widths, height=12, multiple=False, tree=False):
    host=ttk.Frame(parent);host.pack(fill='both',expand=True)
    host.rowconfigure(0,weight=1);host.columnconfigure(0,weight=1)
    tv=ttk.Treeview(host,columns=cols,show='tree headings' if tree else 'headings',
        height=height,selectmode='extended' if multiple else 'browse')
    for c,label,width in zip(cols,labels,widths):
        tv.heading(c,text=label,anchor='w');tv.column(c,width=width,minwidth=55,anchor='w')
    y=ttk.Scrollbar(host,orient='vertical',command=tv.yview)
    x=ttk.Scrollbar(host,orient='horizontal',command=tv.xview)
    tv.configure(yscrollcommand=y.set,xscrollcommand=x.set)
    tv.grid(row=0,column=0,sticky='nsew');y.grid(row=0,column=1,sticky='ns');x.grid(row=1,column=0,sticky='ew')
    return tv


class WorkspaceQCPanel(NGSQCPanel):
    """Keep the existing standards generator; use exact record keys for drafts/QC."""
    def __init__(self,parent,base,db,owner):
        self.owner=owner;self.selected_record_key='';self._selecting=False;self._external_window=None;self._external_panel=None
        self.report_section_overrides={}
        self._overrides_owner_key=''
        self._section_editor_rendering=False
        self.section_editors={}
        self.report_summary_vars={}
        self.hrd_component_vars={}
        super().__init__(parent,base,db,on_metadata_change=owner.metadata_changed,
                         open_shared_metadata=lambda rk:owner.select_record(rk,'metadata'))

    def _build_ui(self):
        self.notebook=ttk.Notebook(self);self.notebook.pack(fill='both',expand=True)
        self.standard_host=ttk.Frame(self.notebook)
        self.qc_tab=ttk.Frame(self.notebook,padding=12)
        self.source_tab=ttk.Frame(self.notebook,padding=12)
        for frame,name in ((self.standard_host,'Raporlama'),(self.qc_tab,'QC Metni'),(self.source_tab,'QC Kaynakları')):
            self.notebook.add(frame,text='  '+name+'  ')
        self.report_layout_tabs=ttk.Notebook(self.standard_host)
        self.report_layout_tabs.pack(fill='both',expand=True)
        self.report_edit_page=ttk.Frame(self.report_layout_tabs)
        self.report_preview_page=ttk.Frame(self.report_layout_tabs)
        self.report_layout_tabs.add(self.report_edit_page,text='  Rapor Düzenleme  ')
        self.report_layout_tabs.add(self.report_preview_page,text='  Tam Rapor Önizleme / Onay  ')
        split=ttk.Panedwindow(self.report_edit_page,orient='horizontal')
        split.pack(fill='both',expand=True)
        form_host=ttk.Frame(split)
        self.standard_preview_parent=ttk.Frame(self.report_preview_page,padding=(12,10))
        self.standard_preview_parent.pack(fill='both',expand=True)
        split.add(form_host,weight=1)
        self._standard_split=split;self._split_initialized=True
        split.bind('<Configure>',self._initial_split)
        self.standard_canvas=tk.Canvas(form_host,highlightthickness=0)
        sy=ttk.Scrollbar(form_host,orient='vertical',command=self.standard_canvas.yview)
        self.standard_canvas.configure(yscrollcommand=sy.set)
        self.standard_canvas.pack(side='left',fill='both',expand=True);sy.pack(side='right',fill='y')
        content=ttk.Frame(self.standard_canvas,padding=14)
        window=self.standard_canvas.create_window((0,0),window=content,anchor='nw')
        content.bind('<Configure>',lambda e:self.standard_canvas.configure(scrollregion=self.standard_canvas.bbox('all')))
        self._standard_form=content
        self.standard_canvas.bind('<Configure>',lambda e:(
            self.standard_canvas.itemconfigure(window,width=e.width,
                height=max(e.height,content.winfo_reqheight())),
            self._fit_labels(content,e.width)))
        self.standard_canvas.bind('<MouseWheel>',lambda e:self.standard_canvas.yview_scroll(-1 if e.delta>0 else 1,'units'))
        self._build_report_summary_strip()
        self._build_standard_tab(content);self.standard_output.configure(height=17)
        row=ttk.Frame(self.qc_tab);row.pack(fill='x',pady=(0,8))
        ttk.Label(row,text='Yalnız seçili teknik kaydın QC cümlesi').pack(side='left')
        ttk.Button(row,text='QC Metnini Kopyala',command=self.copy_selected).pack(side='right')
        self.output=tk.Text(self.qc_tab,wrap='word',font=('Segoe UI',11),padx=15,pady=15)
        self.output.pack(fill='both',expand=True)
        bar=ttk.Frame(self.source_tab);bar.pack(fill='x',pady=(0,10))
        ttk.Button(bar,text='Yerel Kaynakları Yenile',command=lambda:(self.owner.refresh(force_metrics=True),self.owner.refresh_source_rows(force=True))).pack(side='left')
        ttk.Button(bar,text='RNA QC ZIP Ice Aktar',command=self.import_rna_qc_zip).pack(side='left',padx=8)
        ttk.Button(bar,text='Dış QC / ZIP İçe Aktarma Aracı',command=self.open_external_import).pack(side='left',padx=8)
        ttk.Label(self.source_tab,text='Eski sol tablo burada. Her satır ayrı run / örnek / DNA-RNA kaydidir; tekrarlar birleştirilmez.',foreground='#52657a').pack(anchor='w',pady=(0,8))
        self.tree=table(self.source_tab,('pool','patient','type','pairs','q30','coverage','status'),
            ('POOL / ÇALIŞMA','ÖZGÜN MP','TÜR','OKUMA ÇİFTİ (M)','Q30 (%)','ÖRTÜLME','KAYNAK'),
            (180,145,75,140,85,120,480))
        self.tree.bind('<<TreeviewSelect>>',self._source_selected)
        self.tree.bind('<Double-1>',lambda e:self.owner.show_tab('standard'))
        ttk.Label(self,textvariable=self.status_var,relief='sunken',padding=(10,5)).pack(fill='x',side='bottom')
        self.notebook.select(self.standard_host)

    def _initial_split(self,event):
        if not self._split_initialized and event.width>800:
            self._split_initialized=True
            self._standard_split.sashpos(0,int(event.width*.52))

    def _fit_labels(self,parent,width):
        for child in parent.winfo_children():
            if isinstance(child,(tk.Label,ttk.Label)):
                try:
                    if len(str(child.cget('text')))>60 or child.cget('textvariable'):
                        child.configure(wraplength=max(250,width-62))
                except tk.TclError:pass
            self._fit_labels(child,width)

    def _build_report_summary_strip(self):
        """Always-visible, exact-record context; not a second copy of case data."""
        box=ttk.LabelFrame(self.standard_host,text='AKTİF ÇALIŞMA · RAPOR ÖNCESİ KONTROL',padding=(10,6))
        box.pack(side='top',fill='x',before=self._standard_split,padx=(8,8),pady=(4,3))
        specs=(('record','Teknik kayıt'),('diagnosis','Tümör tanısı'),('block','Blok / tümör oranı'),
               ('markers','DNA: TMB / MSI / HRD'),('physician','İstem / raporlayan'),('signature','İmza sahibi'))
        for i,(key,label) in enumerate(specs):
            r,c=divmod(i,2)
            holder=ttk.Frame(box);holder.grid(row=r,column=c,sticky='ew',padx=(4,10),pady=1)
            holder.columnconfigure(1,weight=1)
            ttk.Label(holder,text=label+':',foreground='#33556d',font=('Segoe UI',9,'bold')).grid(row=0,column=0,sticky='w')
            value=tk.StringVar(value='—');self.report_summary_vars[key]=value
            ttk.Label(holder,textvariable=value,wraplength=425,justify='left').grid(row=0,column=1,sticky='w',padx=(7,0))
        box.columnconfigure(0,weight=1);box.columnconfigure(1,weight=1)
        bar=ttk.Frame(box);bar.grid(row=3,column=0,columnspan=2,sticky='ew',pady=(5,1))
        ttk.Button(bar,text='Mutasyon / Yorum',command=lambda:self.show_report_section('report')).pack(side='left',padx=(0,5))
        ttk.Button(bar,text='TMB / MSI / HRD',command=lambda:self.show_report_section('biomarkers')).pack(side='left',padx=5)
        ttk.Button(bar,text='Blok / Tanı',command=lambda:self.show_report_section('patient')).pack(side='left',padx=5)
        ttk.Button(bar,text='Doktor / İmza',command=lambda:self.show_report_section('doctors')).pack(side='left',padx=5)
        ttk.Button(bar,text='Eski DNA / RNA',command=lambda:self.show_report_section('history')).pack(side='left',padx=5)
        ttk.Button(bar,text='Genel / Makro',command=lambda:self.show_report_section('texts')).pack(side='left',padx=5)
        bar.grid_remove()  # The same destinations already have full-width tabs.
        self.report_form_status_var=tk.StringVar(value='Seçili teknik çalışma için rapor taslağı.')
        ttk.Label(box,textvariable=self.report_form_status_var,foreground='#8a490d').grid(row=4,column=0,columnspan=2,sticky='w',pady=(4,0))

    def _scrollable_report_page(self,notebook,title):
        host=ttk.Frame(notebook)
        notebook.add(host,text='  '+title+'  ')
        canvas=tk.Canvas(host,highlightthickness=0)
        bar=ttk.Scrollbar(host,orient='vertical',command=canvas.yview)
        canvas.configure(yscrollcommand=bar.set)
        canvas.pack(side='left',fill='both',expand=True)
        bar.pack(side='right',fill='y')
        content=ttk.Frame(canvas,padding=(6,8))
        item=canvas.create_window((0,0),window=content,anchor='nw')
        content.bind('<Configure>',lambda e:canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind('<Configure>',lambda e:canvas.itemconfigure(item,width=e.width))
        canvas.bind('<MouseWheel>',lambda e:canvas.yview_scroll(-1 if e.delta>0 else 1,'units'))
        return host,content

    def show_report_section(self,key):
        if getattr(self,'report_editor_tabs',None) and key in self.report_page_hosts:
            self.owner.show_tab('standard')
            self.report_layout_tabs.select(self.report_edit_page)
            self.report_editor_tabs.select(self.report_page_hosts[key])

    def _build_standard_tab(self,parent):
        self.report_editor_tabs=ttk.Notebook(parent)
        self.report_editor_tabs.pack(fill='both',expand=True)
        pages=(('report','Mutasyon / Yorum'),('biomarkers','TMB / MSI / HRD'),
               ('patient','Hasta / Blok'),('doctors','Doktor / İmza'),
               ('texts','Genel / Makro'),('history','Geçmiş DNA/RNA'))
        self.report_page_hosts={}
        self._report_section_hosts={}
        for key,name in pages:
            host,content=self._scrollable_report_page(self.report_editor_tabs,name)
            self.report_page_hosts[key]=host
            self._report_section_hosts[key]=content
        self._report_section_hosts['type']=self._report_section_hosts['report']
        super()._build_standard_tab(parent)

        # Build on the original Tk widgets/variables; do not duplicate report logic.
        children=self.dna_positive_frame.winfo_children()
        for child in children:child.pack_forget()
        children[0].grid(row=0,column=0,columnspan=6,sticky='w',pady=(0,5))
        for i,child in enumerate(self.dna_positive_widgets):child.grid(row=1,column=i,sticky='w',padx=(0,8))
        children[-1].grid(row=2,column=0,columnspan=6,sticky='e',pady=(5,0))
        action=self.standard_preview_parent.winfo_children()[0]
        buttons=action.winfo_children()
        for child in buttons:child.pack_forget()
        buttons[0].grid(row=0,column=0,columnspan=2,sticky='w',pady=(0,6))
        for i,child in enumerate(buttons[1:]):
            child.grid(row=1,column=i,sticky='ew',padx=(0,5))
            action.columnconfigure(i,weight=1)
        ttk.Button(action,text='Raporu Onayla',command=self.approve_current_report).grid(row=0,column=2,sticky='ew',padx=5,pady=(0,6))
        ttk.Button(action,text='Taslağı Kaydet',command=self.save_report_draft).grid(row=0,column=3,columnspan=2,sticky='e')
        self.standard_preview_parent.bind('<Configure>',lambda e:self._fit_labels(self.standard_preview_parent,e.width))
        for frame in self._report_section_hosts['doctors'].winfo_children():
            if isinstance(frame,ttk.LabelFrame):
                for child in frame.winfo_children():
                    if isinstance(child,tk.Entry):child.configure(width=27)
        for frame in self._report_section_hosts['report'].winfo_children():
            if isinstance(frame,ttk.LabelFrame):
                for child in frame.winfo_children():
                    if isinstance(child,ttk.Label) and 'Bu alan hasta' in str(child.cget('text')):
                        child.configure(text='Pozitif mutasyon / rapor yorumu yalnız seçili teknik çalışmaya kaydedilir.')
        self.diagnosis_comment_text.configure(height=11,undo=True)
        # The actual report-writing field must be visible immediately, before
        # auxiliary gene checkboxes or the readonly assay-mode explanation.
        report_frames=[child for child in self._report_section_hosts['report'].winfo_children()
                       if isinstance(child,ttk.LabelFrame)]
        diagnosis=next(w for w in report_frames if w.cget('text')=='Tanı / Yorum')
        finding=next(w for w in report_frames if 'Pozitif mutasyon' in w.cget('text'))
        mode=next(w for w in report_frames if w.cget('text')=='Metin türü')
        diagnosis.pack_forget();diagnosis.pack(fill='x',pady=(0,8),before=finding)
        mode.pack_forget();mode.pack(fill='x',pady=(0,8))
        self._build_report_shortcuts()
        self._build_section_text_editors()
        self._build_report_history()
        self.report_editor_tabs.bind('<<NotebookTabChanged>>',self._on_report_editor_tab,add='+')
        self._wrap_long_labels(parent)

    def _build_report_shortcuts(self):
        report=self._report_section_hosts['report']
        top=ttk.Frame(report,padding=(4,4));top.pack(fill='x',before=report.winfo_children()[0])
        ttk.Label(top,text='RAPORA GİRECEK POZİTİF MUTASYON / FÜZYON VE YORUM',
                  font=('Segoe UI',11,'bold'),foreground='#075985').pack(anchor='w')
        ttk.Label(top,text='Tam Rapor Önizleme / Onay ayrı geniş sekmededir. Metin yalnız bu teknik çalışma içindir.',
                  wraplength=600).pack(anchor='w',pady=(2,5))
        ttk.Button(top,text='Seçili rapor taslağını kaydet',command=self.save_report_draft).pack(anchor='w')
        ttk.Button(top,text='Tam raporu önizle / onayla',command=lambda:self.report_layout_tabs.select(self.report_preview_page)).pack(anchor='w',pady=(6,0))
        patient=self._report_section_hosts['patient']
        ttk.Label(patient,text='Bu kayıt tek teknik DNA/RNA örneğidir. Aynı hastadaki farklı tümörler ayrı kalır.',
                  wraplength=590,foreground='#765828').pack(anchor='w',pady=(4,6))
        p=ttk.LabelFrame(patient,text='Hızlı düzenleme · gerçek Hasta / Blok kaydı',padding=8)
        p.pack(fill='x',before=next(c for c in patient.winfo_children() if isinstance(c,ttk.LabelFrame)))
        self.patient_quick_host=p
        doctor=self._report_section_hosts['doctors']
        d=ttk.LabelFrame(doctor,text='İstem / raporlama doktoru · Hasta / Blok kaydına yazılır',padding=8)
        d.pack(fill='x',before=next(c for c in doctor.winfo_children() if isinstance(c,ttk.LabelFrame)))
        self.doctor_quick_host=d
        biomarker=self._report_section_hosts['biomarkers']
        component_box=ttk.LabelFrame(biomarker,text='HRD bileşenleri',padding=8)
        component_box.pack(fill='x',pady=(4,8))
        for index,(key,label) in enumerate((('hrd_loh','HRD-LOH'),('hrd_tai','Telomeric AI'),('hrd_lst','LST'))):
            self.hrd_component_vars[key]=tk.StringVar()
            ttk.Label(component_box,text=label).grid(row=0,column=index*2,sticky='w',padx=5)
            ttk.Entry(component_box,textvariable=self.hrd_component_vars[key],width=8).grid(row=0,column=index*2+1,padx=(0,12))
        ttk.Button(component_box,text='Alanların kaynakları',command=self.show_reporting_sources).grid(row=1,column=0,columnspan=6,sticky='w',pady=(8,0))
        ttk.Label(biomarker,text='TMB, MSI ve HRD değerini düzelttikten sonra Biyobelirteçleri Kaydet düğmesine basın. '
                  'Kaydetme ilgili DNA teknik kaydına yapılır.',foreground='#84551e',wraplength=600).pack(anchor='w',pady=(3,6))

    def attach_metadata_quick_editors(self):
        """Called after the owning MetadataEditor exists. Shared StringVars keep one source of truth."""
        if getattr(self,'_quick_edit_built',False):return
        self._quick_edit_built=True
        meta=self.owner.metadata
        fields=(('Tümör tanısı','diagnosis'),('Blok kaynağı','block_source'),
                ('Blok bilgisi','block_id'),('Tümör oranı (%)','tumor_percentage'))
        for r,(label,key) in enumerate(fields):
            ttk.Label(self.patient_quick_host,text=label).grid(row=r,column=0,sticky='w',pady=4)
            ttk.Entry(self.patient_quick_host,textvariable=meta.vars[key],width=47).grid(
                row=r,column=1,sticky='ew',padx=(8,0),pady=4)
        self.patient_quick_host.columnconfigure(1,weight=1)
        controls=ttk.Frame(self.patient_quick_host)
        controls.grid(row=len(fields),column=0,columnspan=2,sticky='ew',pady=(5,0))
        ttk.Button(controls,text='Hasta / Blok Değişikliklerini Kaydet',command=self.save_metadata_from_report).pack(side='left')
        ttk.Button(controls,text='Raporu Onayla',command=self.approve_current_report).pack(side='left',padx=8)
        ttk.Button(controls,text='Tam Hasta / Blok / Enlil Ekranı',
                   command=lambda:self.owner.show_tab('metadata')).pack(side='left',padx=8)
        for r,(label,key) in enumerate((('İsteyen doktor','requesting_physician'),('Raporlayan doktor','reporting_physician'))):
            ttk.Label(self.doctor_quick_host,text=label).grid(row=r,column=0,sticky='w',pady=4)
            ttk.Entry(self.doctor_quick_host,textvariable=meta.vars[key],width=45).grid(
                row=r,column=1,sticky='ew',padx=(8,0),pady=4)
        self.doctor_quick_host.columnconfigure(1,weight=1)
        ttk.Button(self.doctor_quick_host,text='Doktor Bilgilerini Kaydet',
                   command=self.save_metadata_from_report).grid(row=2,column=0,columnspan=2,sticky='w',pady=(6,2))
        ttk.Label(self.doctor_quick_host,text='İmza sahibi aşağıdan ayrıca seçilir. Seçilmezse rapor kopyalanmaz.',
                  foreground='#9a3412').grid(row=3,column=0,columnspan=2,sticky='w')

    def save_metadata_from_report(self):
        if not self.selected_record_key:
            messagebox.showwarning('Teknik kayıt','Önce DNA/RNA teknik kaydı seçin.',parent=self);return False
        if self.owner.metadata.current_record!=self.selected_record_key:
            messagebox.showwarning('Kayıt eşleşmesi','Hasta / Blok kaydı seçili teknik örnekle eşleşmiyor.',parent=self);return False
        result=self.owner.metadata.save(notify=False)
        if result:self.report_form_status_var.set('✓ Hasta / Blok değişiklikleri kaydedildi.')
        self.refresh_shared_metadata()
        self.refresh_report_summary()
        return result

    def save_report_draft(self):
        if not self.selected_record_key:return False
        try:
            self.store_current_form_state();self._persist_forms()
        except OSError as exc:
            messagebox.showerror('Rapor taslağı kaydedilemedi',str(exc),parent=self);return False
        self.report_form_status_var.set('✓ Seçili teknik çalışma için rapor taslağı kaydedildi.')
        return True

    def show_reporting_sources(self):
        if not self.selected_record_key:return
        from clinical_completion import show_field_sources
        show_field_sources(self,self.db,self.selected_record_key,on_change=self.refresh_shared_metadata)

    def load_form_for_selection(self):
        super().load_form_for_selection()
        rk=self.selected_record_key
        if rk and self.hrd_component_vars:
            from reporting_context import build_record_context
            from clinical_completion import display
            context=build_record_context(self.db,rk)
            self._biomarker_context_revision=context['revision']
            for name,var in self.hrd_component_vars.items():
                field=context['field_sources'].get(name,{})
                payload=field.get('manual_override') if field.get('manual_override',{}).get('present') else field.get('source',{})
                var.set('N/A' if payload and payload.get('availability')=='not_available' else display(field.get('effective_value')))

    def save_dna_biomarkers(self):
        rk=self.selected_record_key
        if not rk or self.active_report_type()!='DNA':
            messagebox.showwarning('Biyobelirteçler','Bir DNA çalışması seçin.',parent=self);return False
        from reporting_context import save_manual_fields
        fields={'tmb_value':self.tmb_var.get(),'msi_percentage':self.msi_pct_var.get(),'hrd_score':self.hrd_score_var.get()}
        fields.update({key:var.get() for key,var in self.hrd_component_vars.items()})
        try:
            context=save_manual_fields(self.db,rk,fields,expected_revision=self._biomarker_context_revision)
            self._biomarker_context_revision=context['revision']
            self.store_current_form_state();self._persist_forms()
            self.biomarker_save_status_var.set('Kaydedildi · TMB, MSI, HRD ve bileşenleri bu DNA çalışmasına ait.')
            self.report_form_status_var.set('Biyobelirteçler kaydedildi; rapor yeniden onay bekliyor.')
            self.refresh_standard_sections()
            return True
        except Exception as exc:
            self.biomarker_save_status_var.set('Kaydedilmedi')
            messagebox.showerror('Biyobelirteçler kaydedilmedi',str(exc),parent=self);return False

    def _current_signer(self):
        try:return self.current_doctor_names()[int(self.signature_owner_var.get())] if int(self.signature_owner_var.get())>=0 else ''
        except (ValueError,IndexError):return ''

    def approve_current_report(self):
        if not self._signature_copy_allowed():return False
        if not self.selected_record_key:return False
        from reporting_context import approve_report, build_record_context, report_fingerprint, LABELS, normalize_field, ContextConflict
        context=build_record_context(self.db,self.selected_record_key)
        if self.owner.metadata.is_dirty():
            messagebox.showwarning('Kaydedilmemiş bilgi','Hasta / Blok değişikliklerini önce kaydedin.',parent=self);return False
        shown_clinical={'diagnosis':'reporting_diagnosis','age':'age_at_reference','sex':'sex',
                        'block_id':'block_id','block_source':'block_source','tumor_percentage':'tumor_percentage',
                        'requesting_physician':'requesting_physician'}
        try:
            stale=[target for key,target in shown_clinical.items()
                   if normalize_field(target,self.owner.metadata.vars[key].get())!=context['field_sources'].get(target,{}).get('effective_value')]
        except ValueError as exc:
            messagebox.showwarning('Klinik bilgiler',str(exc),parent=self);return False
        if stale:
            messagebox.showwarning('Klinik bilgiler güncellendi','Ekrandaki bilgiler son kayıttan farklı. Hasta / Blok kaydını yeniden açıp karşılaştırın.',parent=self);return False
        if self.active_report_type()=='DNA':
            displayed={'tmb_value':self.tmb_var.get(),'msi_percentage':self.msi_pct_var.get(),'hrd_score':self.hrd_score_var.get()}
            displayed.update({name:var.get() for name,var in self.hrd_component_vars.items()})
            try:
                pending=[name for name,value in displayed.items() if normalize_field(name,value)!=context['field_sources'].get(name,{}).get('effective_value')]
            except ValueError as exc:
                messagebox.showwarning('Biyobelirteçler',str(exc),parent=self);return False
            if pending:
                messagebox.showwarning('Kaydedilmemiş biyobelirteç','TMB / MSI / HRD değişikliklerini önce kaydedin.',parent=self);return False
        if context['missing_fields']:
            from clinical_completion import ClinicalCompletionDialog
            win=ClinicalCompletionDialog(self,self.db,[self.db.get_sample(self.selected_record_key)],missing_only=True)
            self.wait_window(win);self.refresh_shared_metadata()
            context=build_record_context(self.db,self.selected_record_key)
            if context['missing_fields']:
                messagebox.showwarning('Klinik bilgiler eksik',', '.join(LABELS.get(n,n) for n in context['missing_fields']),parent=self);return False
        try:
            # Approval belongs to the explicitly reviewed local text and signer.
            # It does not change GenNext Selection or release a report remotely.
            reviewed_record=self.selected_record_key
            reviewed_text=self.full_standard_text();reviewed_signer=self._current_signer()
            reviewed_fingerprint,_,reviewed_context=report_fingerprint(self.db,reviewed_record,reviewed_text,reviewed_signer)
            if reviewed_context['revision'] != context['revision']:
                raise ContextConflict('Rapor bilgileri inceleme sırasında değişti; güncel kaydı yeniden açın.')
            if not messagebox.askyesno('Rapor içeriği onayı',
                'Ekrandaki rapor metnini, blok bilgisini, biyobelirteçleri ve rapora alınacak seçimleri kontrol ettiniz mi?\n\n'
                'Onay bu içerik ve seçili imzacı için saklanır. Sonraki değişiklikler yeniden onay gerektirir.',parent=self):return False
            if self.selected_record_key!=reviewed_record or self.full_standard_text()!=reviewed_text or self._current_signer()!=reviewed_signer:
                raise ContextConflict('Onay penceresi açıkken rapor metni veya imzacı değişti; güncel içeriği yeniden değerlendirin.')
            approve_report(self.db,reviewed_record,reviewed_text,reviewed_signer,
                expected_fingerprint=reviewed_fingerprint,expected_revision=reviewed_context['revision'])
            self.report_form_status_var.set('Bu rapor içeriği ve imzacı onaylandı.');return True
        except Exception as exc:
            messagebox.showerror('Rapor onayı',str(exc),parent=self);return False

    def _copy_approved_report(self):
        if not self._signature_copy_allowed():return
        from reporting_context import report_is_approved
        if not self.selected_record_key:return
        text=self.full_standard_text()
        if not report_is_approved(self.db,self.selected_record_key,text,self._current_signer()):
            self.report_form_status_var.set('Rapor içeriği değişti veya onaylanmadı. Raporu Onayla düğmesini kullanın.')
            messagebox.showwarning('Rapor onayı gerekli','Güncel raporu kontrol edip Raporu Onayla düğmesine basın.',parent=self);return
        self.copy_text(text)

    def _build_section_text_editors(self):
        host=self._report_section_hosts['texts']
        ttk.Label(host,text='Genel, Makroskopi ve Boyama metinleri aşağıda ayrı görünür. '
                  'Varsayılan metinler hasta/blok ve QC bilgileriyle otomatik yenilenir. '
                  'Elle değiştirilen metin yalnız bu teknik çalışma için saklanır.',
                  wraplength=640,foreground='#84551e').pack(fill='x',pady=(2,9))
        for key,title in (('general','Genel / Yöntem'),('macro','Makroskopi / Blok'),
                          ('stain','Boyama / QC / Panel kapsamı')):
            box=ttk.LabelFrame(host,text=title,padding=6);box.pack(fill='x',pady=(0,8))
            text=tk.Text(box,height=6 if key!='stain' else 9,wrap='word',font=('Segoe UI',10),undo=True)
            y=ttk.Scrollbar(box,orient='vertical',command=text.yview)
            text.configure(yscrollcommand=y.set)
            text.pack(side='left',fill='both',expand=True);y.pack(side='right',fill='y')
            actions=ttk.Frame(host);actions.pack(fill='x',pady=(0,10))
            ttk.Button(actions,text='Otomatik metne dön',command=lambda k=key:self.reset_report_section(k)).pack(side='left')
            ttk.Button(actions,text='Bu metni kopyala',command=lambda k=key:self.copy_section(k)).pack(side='left',padx=6)
            self.section_editors[key]=text
            text.bind('<KeyRelease>',lambda e,k=key:self._section_edited(k))
            text.bind('<<Paste>>',lambda e,k=key:self.after_idle(lambda:self._section_edited(k)))
            text.bind('<<Cut>>',lambda e,k=key:self.after_idle(lambda:self._section_edited(k)))
        ttk.Button(host,text='Düzenlenmiş Enlil Metinlerini Kaydet',command=self.save_report_draft).pack(anchor='w',pady=(2,4))

    def _section_generated(self,key):
        return super().section_copy_text(key)

    def _section_edited(self,key):
        if self._section_editor_rendering or self._loading_form or not self.selected_record_key:return
        self.report_section_overrides[key]=self.section_editors[key].get('1.0','end-1c')
        self.store_current_form_state()
        self.report_form_status_var.set('Düzenlenen metin henüz diske kaydedilmedi. Taslağı Kaydet düğmesine basın.')
        self.refresh_standard_sections()

    def reset_report_section(self,key):
        self.report_section_overrides.pop(key,None)
        self._update_section_text_editors()
        self.store_current_form_state()
        self.refresh_standard_sections()
        self.report_form_status_var.set('Otomatik metne dönüldü. Taslağı Kaydet düğmesine basın.')

    def _update_section_text_editors(self):
        if not self.section_editors or self._section_editor_rendering:return
        self._section_editor_rendering=True
        try:
            for key,widget in self.section_editors.items():
                desired=self.report_section_overrides.get(key,self._section_generated(key))
                if widget.get('1.0','end-1c')!=desired:
                    widget.delete('1.0','end');widget.insert('1.0',desired)
        finally:self._section_editor_rendering=False

    def section_copy_text(self,section):
        if section in ('general','macro','stain') and section in self.report_section_overrides:
            return self.report_section_overrides[section]
        return super().section_copy_text(section)

    def build_standard_sections(self):
        sections=super().build_standard_sections()
        if not self.report_section_overrides:return sections
        output=[];stain_done=False
        for title,text in sections:
            if 'paneli — yöntem' in title and 'general' in self.report_section_overrides:
                text=self.report_section_overrides['general']
            if title=='Parafin blok ve tümör oranı' and 'macro' in self.report_section_overrides:
                text=self.report_section_overrides['macro']
            is_stain=(title=='QC metni' or title=='MSI — değerlendirme yöntemi' or
                      title.endswith('— incelenen genler') or title=='DNA paneli — CNV kapsamı')
            if is_stain and 'stain' in self.report_section_overrides:
                if not stain_done:output.append(('Boyama / QC / Panel',self.report_section_overrides['stain']));stain_done=True
                continue
            output.append((title,text))
        return output

    def store_current_form_state(self):
        super().store_current_form_state()
        key=getattr(self,'active_form_key','')
        if key and key==self._overrides_owner_key and not self._loading_form and not key.startswith('__') and key in self.patient_form_data:
            self.patient_form_data[key]['report_section_overrides']=dict(self.report_section_overrides)

    def load_form_for_selection(self):
        # Base loader rebuilds its legacy form dict after switching the technical
        # key; capture custom sections BEFORE that reset or they would disappear.
        requested_key=self.current_form_key()
        cached=self.patient_form_data.get(requested_key,{})
        prior=cached.get('report_section_overrides',{})
        overrides=copy.deepcopy(prior) if isinstance(prior,dict) else {}
        super().load_form_for_selection()
        if requested_key==self.active_form_key and self.active_form_key in self.patient_form_data:
            self.patient_form_data[self.active_form_key]['report_section_overrides']=copy.deepcopy(overrides)
        self.report_section_overrides={k:str(v) for k,v in overrides.items()
                                       if k in ('general','macro','stain') and isinstance(v,str)} if isinstance(overrides,dict) else {}
        self._overrides_owner_key=self.active_form_key
        self._update_section_text_editors()
        self.refresh_standard_sections()

    def refresh_standard_sections(self):
        super().refresh_standard_sections()
        if hasattr(self,'section_editors'):
            self._update_section_text_editors()
        if hasattr(self,'report_summary_vars'):
            self.refresh_report_summary()

    def refresh_report_summary(self):
        if not self.report_summary_vars:return
        meta=getattr(self.owner,'metadata',None)
        vals=meta.vars if meta and getattr(meta,'current_record',None)==self.selected_record_key else {}
        get=lambda key,fallback='': vals[key].get().strip() if key in vals else fallback
        record=(self.db.get_sample(self.selected_record_key) or {}) if self.selected_record_key else {}
        rows={
            'record':f"{self.db.display_pool_name(record.get('run_uuid'),record.get('pool_name')) or '—'} · {record.get('patient_id') or '—'} · {record.get('sample_type') or '—'}" if record else 'DNA/RNA çalışması seçin',
            'diagnosis':get('diagnosis',self.source_diagnosis_var.get().strip()) or '[Tanı girilmedi]',
            'block':(get('block_source',self.block_source_var.get())+' / '+get('block_id',self.block_detail_var.get())+
                     '  |  Tümör %'+(get('tumor_percentage',self.tumor_pct_var.get()) or '?')),
            'markers':(f"TMB {self.tmb_var.get() or '—'}  |  MSI %{self.msi_pct_var.get() or '—'}  |  HRD {self.hrd_score_var.get() or '—'}"
                       if self.active_report_type()=='DNA' else 'RNA çalışması · DNA biyobelirteçleri düzenlenemez'),
            'physician':f"İsteyen: {get('requesting_physician',self.requesting_physician_var.get()) or '—'} / Raporlayan: {get('reporting_physician',self.reporting_physician_var.get()) or '—'}",
            'signature':(self.current_doctor_names()[self.signature_owner_var.get()]
                         if 0<=self.signature_owner_var.get()<4 else 'SEÇİLMEDİ · kopyalama engelli')}
        for key,value in rows.items():self.report_summary_vars[key].set(value)

    def _build_report_history(self):
        host=self._report_section_hosts['history']
        ttk.Label(host,text='Önceki çalışmalar DNA/RNA, POOL ve MP bazında listelenir. '
                  'Farklı tümöre ait tanı/blok otomatik aktarılmaz; önce kaynağı doğrulayın.',
                  foreground='#84551e',wraplength=650).pack(fill='x',pady=(3,7))
        self.report_history_views={}
        for key,label,columns,widths in (
            ('block','Önceki teknik örnek / blok',('Çalışma','MP','Tür','Blok kaynağı','Blok','Tümör %'),(160,110,60,160,200,80)),
            ('diagnosis','Önceki tümör tanıları',('Çalışma','MP','Tür','Tümör tanısı','Kaynak'),(160,110,60,370,170))):
            ttk.Label(host,text=label,font=('Segoe UI',10,'bold')).pack(anchor='w',pady=(6,3))
            tv=ttk.Treeview(host,columns=[f'{key}_{i}' for i in range(len(columns))],show='headings',height=6)
            for c,title,width in zip(tv['columns'],columns,widths):
                tv.heading(c,text=title);tv.column(c,width=width,minwidth=55)
            tv.pack(fill='x')
            tv.bind('<Double-1>',lambda e,k=key:self.open_history_editor(k))
            self.report_history_views[key]=tv
            ttk.Button(host,text='Seçili kaydı Hasta / Blok ekranında aç',
                       command=lambda k=key:self.open_history_editor(k)).pack(anchor='e',pady=(3,5))
        ttk.Button(host,text='DNA + RNA / Eski NGS Raporlarını Aç',
                   command=lambda:self.owner.show_tab('dual')).pack(anchor='w',pady=(6,0))

    def _on_report_editor_tab(self,event=None):
        if self.report_editor_tabs.select()==str(self.report_page_hosts['history']):
            self.refresh_report_history()

    def refresh_report_history(self):
        if not self.report_history_views:return
        meta=getattr(self.owner,'metadata',None)
        if not meta or not self.selected_record_key:return
        meta.refresh_metadata_history()
        for key,original in (('block',meta.block_history_tree),('diagnosis',meta.diagnosis_history_tree)):
            target=self.report_history_views[key]
            target.delete(*target.get_children())
            for item in original.get_children():
                target.insert('','end',iid=item,values=original.item(item,'values'))

    def open_history_editor(self,key):
        tv=self.report_history_views[key]
        selected=tv.selection()
        if not selected:
            messagebox.showinfo('Eski çalışma','Önce kaynağını görmek istediğiniz çalışma satırını seçin.',parent=self)
            return
        meta=self.owner.metadata
        target=meta.block_history_tree if key=='block' else meta.diagnosis_history_tree
        meta.refresh_metadata_history()
        if target.exists(selected[0]):target.selection_set(selected[0]);target.see(selected[0])
        if self.owner.show_tab('metadata'):
            # History tables live near the lower part of the full metadata form.
            # Navigate directly to them instead of opening an apparently empty top.
            meta.after_idle(lambda:meta.form_canvas.yview_moveto(1.0))

    def _wrap_long_labels(self,parent):
        for child in parent.winfo_children():
            if isinstance(child,(ttk.Label,tk.Label)):
                try:
                    if len(str(child.cget('text')))>85:child.configure(wraplength=740)
                except tk.TclError:pass
            self._wrap_long_labels(child)

    def current_form_key(self):
        selected=self.tree.selection() if hasattr(self,'tree') else ()
        if selected and selected[0] in self.samples:
            m=self.samples[selected[0]]
            if getattr(m,'collector_exact',False):return 'TECH::'+m.collector_record_key
        return '__NO_TECHNICAL_RECORD__'

    def active_report_type(self):
        selected=self.tree.selection() if hasattr(self,'tree') else ()
        if selected and selected[0] in self.samples:return self.samples[selected[0]].sample_type
        return 'DNA'  # Empty template is not copyable.

    def refresh_standard_sections(self):
        super().refresh_standard_sections()
        if hasattr(self,'summary_text'):
            parts=[self.patient_name_var.get(),self.sex_var.get(),self.source_diagnosis_var.get(),
                'Blok: '+(self.block_detail_var.get() or '-'),
                'Tümör: '+('%'+self.tumor_pct_var.get() if self.tumor_pct_var.get() else 'girilmedi')]
            self.summary_text.set('  |  '.join(x for x in parts if x) if self.selected_record_key else 'Bir teknik kayıt seçin.')

    def set_records(self,rows):
        previous=self.selected_record_key;self._selecting=True
        try:
            self.tree.delete(*self.tree.get_children())
            for r in rows:
                rk=r['record_key']
                if rk not in self.samples:
                    self.samples[rk]=SampleMetrics(sample=rk,sample_type=r.get('sample_type') or '',
                        collector_record_key=rk,collector_exact=True,collector_patient_id=r.get('patient_id') or '',
                        patient_protocol=r.get('patient_id') or '',report_protocol=r.get('patient_id') or '')
                m=self.samples[rk]
                self.tree.insert('','end',iid=rk,values=(r.get('pool_name') or '',r.get('patient_id') or '',r.get('sample_type') or '',
                    m.total_reads_m if m.total_reads_m is not None else '-',m.q30_pct if m.q30_pct is not None else '-',
                    m.mean_coverage_x if m.mean_coverage_x is not None else '-',m.collector_source_note or 'Seçildiğinde yerel QC okunur'))
            if previous and self.tree.exists(previous):self.tree.selection_set(previous)
        finally:self._selecting=False

    def update_source_row(self, row, metrics):
        """Fill an already visible tree row without changing patient selection."""
        rk = row['record_key']
        self.samples[rk] = metrics
        if not self.tree.exists(rk):
            return
        self.tree.item(rk, values=(
            row.get('pool_name') or '', row.get('patient_id') or '',
            row.get('sample_type') or '',
            round(metrics.total_reads_m, 6) if metrics.total_reads_m is not None else '-',
            round(metrics.q30_pct, 3) if metrics.q30_pct is not None else '-',
            round(metrics.mean_coverage_x, 2) if metrics.mean_coverage_x is not None else '-',
            metrics.collector_source_note or 'QC kaynagi yok',
        ))

    def import_rna_qc_zip(self):
        selected = self.owner.db.get_sample(self.owner.current_record) if self.owner.current_record else None
        if not selected or str(selected.get('sample_type') or '').upper() != 'RNA':
            messagebox.showwarning('RNA teknik kaydi',
                'Once hedef POOL icinden bir RNA teknik kaydi secin. DNA kaynaklarina dokunulmaz.',parent=self)
            return
        selected_run = selected.get('run_uuid') or ''
        selected_pool = selected.get('pool_name') or ''
        candidates = [dict(r) for r in self.owner.rows if
                      (r.get('run_uuid') or '') == selected_run and
                      (r.get('pool_name') or '') == selected_pool and
                      str(r.get('sample_type') or '').upper() == 'RNA']
        if not candidates:
            messagebox.showwarning('RNA teknik kaydi','Secili run icinde RNA kaydi bulunamadi.',parent=self)
            return
        chosen = filedialog.askopenfilename(parent=self,
            title='RNA QC: MP... trimming ve fqStat dosyalarini iceren ZIP secin',
            filetypes=[('ZIP QC arsivi','*.zip'),('Tum dosyalar','*.*')])
        if not chosen:
            return
        try:
            plan = analyze_bundle(chosen, candidates)
        except Exception as exc:
            messagebox.showerror('RNA QC ZIP okunamadi',str(exc),parent=self)
            return
        win = tk.Toplevel(self)
        win.title('RNA QC onizleme - hasta/yil/run kontrolu')
        win.geometry('1150x650')
        win.transient(self.winfo_toplevel())
        caption = (f"Secili POOL: {selected_pool} | Run: {selected_run[:12]} | "
                   f"Hedef RNA: {plan['target_count']} | "
                   f"Aktarilabilir: {len(plan['ready'])} | "
                   f"Eslesmeyen/kontrol: {len(plan['items'])-len(plan['ready'])}")
        ttk.Label(win,text=caption,padding=12,wraplength=1080).pack(fill='x')
        host = ttk.Frame(win,padding=(10,0,10,5))
        host.pack(fill='both',expand=True)
        host.rowconfigure(0,weight=1);host.columnconfigure(0,weight=1)
        columns=('mp','pairs','q30','status','note')
        tv=ttk.Treeview(host,columns=columns,show='headings',height=21)
        for key,title,width in (
            ('mp','ZIP icindeki MP',140),('pairs','Okuma cifti (M)',145),
            ('q30','Q30 (%)',100),('status','Durum',130),('note','Aciklama',580)):
            tv.heading(key,text=title,anchor='w')
            tv.column(key,width=width,anchor='w')
        tv.tag_configure('problem',background='#fff0ee')
        scrollbar=ttk.Scrollbar(host,orient='vertical',command=tv.yview)
        tv.configure(yscrollcommand=scrollbar.set)
        tv.grid(row=0,column=0,sticky='nsew');scrollbar.grid(row=0,column=1,sticky='ns')
        for i,item in enumerate(plan['items']):
            m=item['metric']
            tv.insert('','end',iid=str(i),values=(item['mp'],
                f'{m.total_reads_m:.6f}' if m and m.total_reads_m is not None else '-',
                f'{m.q30_pct:.3f}' if m and m.q30_pct is not None else '-',
                item['status'],'; '.join(item['notes'])),
                tags=('problem',) if item['status'] != 'HAZIR' else ())
        if plan['not_found']:
            ttk.Label(win,text='ZIP icinde bulunmayan hedefler: '+', '.join(plan['not_found']),
                      padding=(12,7),wraplength=1080).pack(anchor='w')
        ttk.Label(win,text='Sadece birebir MP + yil + RNA + secili run eslesenler baglanir. '
                  'Mevcut hasta raporlari ve kalite onaylari degistirilmez.',
                  padding=(12,5),wraplength=1050).pack(anchor='w')
        actions=ttk.Frame(win,padding=10);actions.pack(fill='x')
        ttk.Button(actions,text='Vazgec',command=win.destroy).pack(side='right',padx=5)
        def apply():
            try:
                result=import_bundle(self.base,self.db,plan)
            except Exception as exc:
                messagebox.showerror('QC ice aktarma hatasi',str(exc),parent=win)
                return
            win.destroy()
            self.owner.refresh(force_metrics=True)
            self.owner.refresh_source_rows(force=True)
            messagebox.showinfo('RNA QC import',
                f"Kaydedilen RNA: {result['imported']} | Eslesmeyen/atlanan: {result['skipped']}\n"
                'Yil uyusmazligi veya belirsiz eslesmeler otomatik baglanmadi.\n'
                'Ortum derinligi ham QC kaynaklarindan hesaplanmaz.',parent=self)
        ttk.Button(actions,text=f"{len(plan['ready'])} RNA QC Kaydini Aktar",
                   command=apply,state='normal' if plan['ready'] else 'disabled').pack(side='right')

    def select_exact(self,row,metrics):
        rk=row['record_key'];self.store_current_form_state();self._selecting=True
        try:
            self.samples[rk]=metrics;self.selected_record_key=rk
            if not self.tree.exists(rk):self.tree.insert('','end',iid=rk)
            self.tree.selection_set(rk);self.tree.focus(rk);self.tree.see(rk)
            self.update_source_row(row,metrics)
            self.load_form_for_selection()
            self._loading_form=True;self.report_type_var.set(metrics.sample_type);self._loading_form=False
            self.output.delete('1.0','end')
            self.output.insert('1.0',f"{row.get('pool_name')} | {row.get('patient_id')} | {metrics.sample_type}\n\n"+metrics.report_sentence())
            missing=metrics.missing_required()
            if missing:self.output.insert('end','\n\nEksik metrik: '+', '.join(missing))
            self.output.insert('end','\n\n'+metrics.collector_source_note)
            self.status_var.set(metrics.collector_source_note);self.refresh_standard_sections()
        finally:self._selecting=False

    def _source_selected(self,event=None):
        if self._selecting or not getattr(self.owner,'_ready',False):return
        sel=self.tree.selection()
        if sel and sel[0]!=self.owner.current_record:
            if not self.owner.select_record(sel[0]):
                if self.selected_record_key and self.tree.exists(self.selected_record_key):self.tree.selection_set(self.selected_record_key)

    def refresh_shared_metadata(self,quiet=True):
        if self.selected_record_key:
            super().refresh_shared_metadata(quiet=quiet);self.refresh_standard_sections()

    def auto_sync_once(self):self.owner.refresh()
    def sync_from_collector(self,include_external=True,quiet=False):self.owner.refresh(force_metrics=True)

    def _copy_allowed(self):
        if not self.selected_record_key or self.active_report_type() not in ('DNA','RNA'):
            messagebox.showwarning('Teknik kayıt','DNA veya RNA türü belirli bir teknik kayıt seçin.',parent=self);return False
        return self.owner.metadata.confirm_leave()

    def copy_standard_all(self):
        if self._copy_allowed():self._copy_approved_report()
    def copy_section(self,section):
        if self._copy_allowed():super().copy_section(section)
    def copy_selected(self):
        if self._copy_allowed():self.copy_text(self.output.get('1.0','end'))

    def import_legacy_form(self):
        if not self.selected_record_key:return
        row=self.db.get_sample(self.selected_record_key) or {};family=case_key(row)
        candidates={k:v for k,v in self.patient_form_data.items() if not k.startswith(('TECH::','__'))
                    and family and case_key({'patient_id':k})==family}
        if not candidates:
            messagebox.showinfo('Eski taslak','Bu MP için eski ortak form taslağı bulunamadı. Eski kayıtlar silinmedi.',parent=self);return
        win=tk.Toplevel(self);win.title('Eski ortak taslak - onaylı aktarım');win.geometry('960x620')
        ttk.Label(win,text='Eski formlar DNA/RNA/tekrar bazında ayrı değildi. Yalnız metin ve imza aktarılır; QC, biyobelirteç ve blok aktarılmaz.',wraplength=910,padding=12).pack(fill='x')
        key_var=tk.StringVar(value=next(iter(candidates)))
        choice=ttk.Combobox(win,textvariable=key_var,values=list(candidates),state='readonly');choice.pack(fill='x',padx=12)
        text=tk.Text(win,wrap='word',padx=12,pady=12);text.pack(fill='both',expand=True,padx=12,pady=12)
        def show(e=None):
            text.delete('1.0','end');text.insert('1.0',json.dumps(candidates[key_var.get()],ensure_ascii=False,indent=2))
        choice.bind('<<ComboboxSelected>>',show);show();target=self.selected_record_key
        def apply():
            if self.selected_record_key!=target:win.destroy();return
            if not messagebox.askyesno('Taslak aktarımı','Gösterilen yorum ve imza sahibi seçili teknik kayda aktarılsın mi?',parent=win):return
            state=candidates[key_var.get()];self.store_current_form_state()
            new=copy.deepcopy(self.patient_form_data.get('TECH::'+target,{}))
            for k in ('diagnosis_comment','signature_owner'):
                if k in state:new[k]=copy.deepcopy(state[k])
            self.patient_form_data['TECH::'+target]=new;self.active_form_key=''
            self.load_form_for_selection();self.refresh_standard_sections();win.destroy()
        ttk.Button(win,text='Bu teknik kayda aktarımı onayla',command=apply).pack(pady=10)

    def open_external_import(self):
        if self._external_window is not None and self._external_window.winfo_exists():self._external_window.lift();return
        win=tk.Toplevel(self);self._external_window=win
        win.title('Dış QC Kaynak Aracı - ana kayıtlar ayrı kalır');win.geometry('1500x900')
        panel=NGSQCPanel(win,self.base,self.db,open_shared_metadata=lambda rk:self.owner.select_record(rk,'metadata'))
        self._external_panel=panel;panel.pack(fill='both',expand=True)
        def close():
            panel.cleanup();win.destroy();self._external_window=None;self._external_panel=None
        win.protocol('WM_DELETE_WINDOW',close)

    def cleanup(self,show_errors=False):
        if self._external_panel is not None:
            self._external_panel.cleanup(show_errors=show_errors)
            self._external_panel=None
        if self._external_window is not None:
            try:self._external_window.destroy()
            except tk.TclError:pass
            self._external_window=None
        super().cleanup(show_errors=show_errors)


class PatientWorkspace(ttk.Frame):
    def __init__(self,parent,base,db,cfg=None,on_change=None,open_mutations=None,open_archive=None,request_scan=None,
                 on_choose_pool_alias=None):
        super().__init__(parent,padding=(8,6))
        self.base,self.db,self.cfg=Path(base),db,cfg or {}
        self.on_change,self.open_mutations,self.open_archive,self.request_scan=on_change,open_mutations,open_archive,request_scan
        self.on_choose_pool_alias=on_choose_pool_alias
        self._ready=False;self._switching=False;self._refreshing=False;self._tab_guard=False
        self.current_record='';self.rows=[];self.cards=[];self._metrics_cache={}
        self._comparison=[];self._display_comparison={};self._return_record=''
        self._source_refresh_generation=0
        self.header_var=tk.StringVar(value='POOL / Örnekler: bir çalışma seçin')
        self.detail_var=tk.StringVar(value='Tek hasta formu / ayrı teknik çalışmalar')
        self.order_var=tk.StringVar();self.search_var=tk.StringVar();self.filter_type_var=tk.StringVar(value='Tümü')
        self._build_header()
        # The outer navigation remains accessible even when the POOL notebook
        # tab is off-screen or the large RUN table is horizontally scrolled.
        alias_strip=ttk.Frame(self,padding=(5,4))
        alias_strip.pack(fill='x',pady=(0,4))
        ttk.Button(alias_strip,text='POOL Listesini Aç',
                   command=lambda:self.show_tab('pools')).pack(side='left',padx=(0,9))
        if self.on_choose_pool_alias:
            ttk.Button(alias_strip,text='POOL ADLANDIR / SON RUN SEÇ',
                       command=self.on_choose_pool_alias).pack(side='left')
        ttk.Label(alias_strip,text='Teknik örnek seçmenize gerek yok',
                  foreground='#52657a').pack(side='left',padx=12)
        self.qc=WorkspaceQCPanel(self,self.base,self.db,self);self.qc.pack(fill='both',expand=True)
        self.notebook=self.qc.notebook
        self.metadata=MetadataEditor(self.notebook,db,self.base,on_change=self.metadata_changed,
            report_archive_callback=open_archive,cfg=self.cfg,form_only=True)
        self.metadata.on_record_selected=self._metadata_record_changed
        self.qc.attach_metadata_quick_editors()
        for _key in ('diagnosis','block_source','block_id','tumor_percentage','requesting_physician','reporting_physician'):
            self.metadata.vars[_key].trace_add('write',lambda *_:self.qc.refresh_report_summary())
        self.pool_tab=ttk.Frame(self.notebook,padding=12)
        self.comparison_tab=ttk.Frame(self.notebook,padding=12)
        self.prepool_panel=PrePoolPanel(self.notebook,self.db,on_change=self.metadata_changed,select_record=self.select_record,cfg=self.cfg)
        self.dual_panel=DualReportPanel(self.notebook,self.base,self.db,self)
        self.metadata.open_dual_reports=lambda:self.show_tab('dual')
        self.metadata.comment_preview_provider=lambda:self.qc.patient_form_data
        self.notebook.insert(1,self.metadata,text='  Hasta / Blok  ')
        self.notebook.insert(2,self.dual_panel,text='  DNA + RNA  ')
        self.notebook.insert(3,self.prepool_panel,text='  POOL Öncesi  ')
        self.notebook.insert(4,self.pool_tab,text='  POOL  ')
        self.notebook.insert(5,self.comparison_tab,text='  Karşılaştırma  ')
        from review_workspace_ui import PatientReviewPanel
        self.ai_panel=PatientReviewPanel(self.notebook,self.base,self.db,self.cfg,
            on_change=self.on_change,open_mutations=self.open_mutations)
        self.notebook.insert(0,self.ai_panel,text='  YZ Notları  ')
        self.tabs={'ai':self.ai_panel,'standard':self.qc.standard_host,'metadata':self.metadata,'dual':self.dual_panel,'prepool':self.prepool_panel,'pools':self.pool_tab,
            'compare':self.comparison_tab,'qc':self.qc.qc_tab,'sources':self.qc.source_tab}
        self.prior_ngs_brief=tk.StringVar(value='Önceki yerel NGS geçmişi: henüz hasta seçilmedi.')
        summary_box=ttk.LabelFrame(self.qc.standard_host,text='ÖNCEKİ NGS · TC / ENLİL DOSYA NO (rapora otomatik eklenmez)',padding=(10,5))
        summary_box.pack(side='top',fill='x',before=self.qc.report_layout_tabs,padx=8,pady=(0,3))
        ttk.Label(summary_box,textvariable=self.prior_ngs_brief,foreground='#0f3d62',wraplength=1350,justify='left').pack(side='left',fill='x',expand=True)
        ttk.Button(summary_box,text='Eski raporlar / karşılaştırma',command=lambda:self.show_tab('dual')).pack(side='right',padx=(9,0))
        self._build_pools();self._build_comparison()
        self._last_tab=str(self.qc.standard_host)
        self.notebook.bind('<<NotebookTabChanged>>',self._tab_changed)
        self.search_var.trace_add('write',lambda *a:self._populate_pools())
        self.filter_type_var.trace_add('write',lambda *a:self._populate_pools())
        self._ready=True;self.refresh();self.show_tab('standard')

    def _build_header(self):
        self.header=tk.Frame(self,bg='#123b55',padx=16,pady=10);self.header.pack(fill='x',pady=(0,7))
        left=tk.Frame(self.header,bg='#123b55');left.pack(side='left',fill='x',expand=True)
        self.title_label=tk.Label(left,textvariable=self.header_var,font=('Segoe UI',14,'bold'),fg='white',bg='#123b55',anchor='w')
        self.title_label.pack(fill='x')
        self.detail_label=tk.Label(left,textvariable=self.detail_var,font=('Segoe UI',9),fg='#e4edf5',bg='#123b55',anchor='w')
        self.detail_label.pack(fill='x',pady=(3,0))
        right=tk.Frame(self.header,bg='#123b55');right.pack(side='right')
        for text,cmd in [('POOL Seç',lambda:self.show_tab('pools')),('DNA',lambda:self.switch_assay('DNA')),
                         ('RNA',lambda:self.switch_assay('RNA')),('DNA + RNA Rapor',lambda:self.show_tab('dual')),
                         ('Enlil Getir',self.lookup_enlil)]:
            ttk.Button(right,text=text,command=cmd).pack(side='left',padx=3)

    def _build_pools(self):
        choose=ttk.Frame(self.pool_tab)
        choose.pack(fill='x',pady=(0,8))
        if self.on_choose_pool_alias:
            ttk.Button(choose,text='İsimsiz RUN’ı POOL 69 olarak adlandır / Son RUN seç',
                       command=self.on_choose_pool_alias).pack(side='left')
        ttk.Label(choose,text='Bu işlem, üstteki teknik örnek tablosundan seçim gerektirmez.',
                  foreground='#52657a').pack(side='left',padx=12)
        bar=ttk.Frame(self.pool_tab);bar.pack(fill='x',pady=(0,9))
        ttk.Label(bar,text='Ara (MP / hasta / pool):').pack(side='left')
        ttk.Entry(bar,textvariable=self.search_var,width=35).pack(side='left',padx=8)
        ttk.Combobox(bar,textvariable=self.filter_type_var,values=('Tümü','DNA','RNA'),state='readonly',width=9).pack(side='left')
        ttk.Button(bar,text='Yerel Listeyi Yenile',command=self.refresh).pack(side='left',padx=8)
        ttk.Button(bar,text='POOL klinik bilgileri',command=self.open_pool_clinical).pack(side='left',padx=4)
        ttk.Button(bar,text='GenNext RUNS Sırasını Tara',command=self._scan).pack(side='left')
        ttk.Button(bar,text='Seçili Örneği Aç',command=self._open_pool_selection).pack(side='right')
        ttk.Label(self.pool_tab,textvariable=self.order_var,foreground='#52657a').pack(anchor='w',pady=(0,10))
        self.pool_tree=table(self.pool_tab,('patient','assay','status','depth','repeat','date'),
            ('HASTA','TÜR','DURUM','ÖRTÜLME','ÇALIŞMA','TARIH'),(240,70,140,95,150,145),height=22,tree=True)
        self.pool_tree.heading('#0',text='GENNEXT POOL / ÖRNEK DÜZENİ',anchor='w')
        self.pool_tree.column('#0',width=360,minwidth=250)
        self.pool_tree.tag_configure('pool',background='#e5eff6',foreground='#153953',font=('Segoe UI',10,'bold'))
        self.pool_tree.tag_configure('dna',background='#f4fbfd');self.pool_tree.tag_configure('rna',background='#f7f0fc')
        self.pool_tree.tag_configure('unseen',foreground='#6b7280')
        self.pool_tree.bind('<Double-1>',self._open_pool_selection);self.pool_tree.bind('<Return>',self._open_pool_selection)
        self.pool_tree.bind('<<TreeviewSelect>>',self._pool_selected)

    def _populate_pools(self):
        if not hasattr(self,'pool_tree'):return
        old_open={i for i in self.pool_tree.get_children() if self.pool_tree.item(i,'open')}
        had_items=bool(self.pool_tree.get_children());self.pool_tree.delete(*self.pool_tree.get_children())
        query=self.search_var.get().strip().casefold();kind=self.filter_type_var.get()
        for i,card in enumerate(self.cards):
            children=[]
            for row in card['rows']:
                if kind in ('DNA','RNA') and row.get('sample_type')!=kind:continue
                display_pool=self.db.display_pool_name(row.get('run_uuid'),row.get('pool_name'))
                if query and query not in (' '.join(str(row.get(k) or '') for k in
                                           ('pool_name','patient_id','_name'))+' '+display_pool).casefold():continue
                children.append(row)
            if not children and (query or kind in ('DNA','RNA')):continue
            pid='RUN::'+str(card.get('run_uuid') or i)
            tags=('pool',) if card.get('source_order') else ('pool','unseen')
            source_name=card.get('pool_name') or 'POOL'
            alias=self.db.run_alias(card.get('run_uuid'))
            title=f"{alias or source_name}{' (yerel)' if alias else ''}  |  {len(children)} örnek"
            if card.get('run_uuid'):title+='  ['+str(card['run_uuid'])[:8]+']'
            self.pool_tree.insert('','end',iid=pid,text=title,
                values=('',card.get('run_type') or '',card.get('run_status') or '','','',card.get('run_date') or ''),
                tags=tags,open=bool(query) or pid in old_open or (not had_items and i==0))
            for row in children:
                self.pool_tree.insert(pid,'end',iid=row['record_key'],text=row.get('patient_id') or '(etiket yok)',
                    values=(row.get('_name') or '',row.get('sample_type') or '',row.get('sample_status') or row.get('run_status') or '',
                            row.get('depth') or '-','TEKRAR OKUMA' if is_repeat(row) else 'İlk / mevcut okuma',''),
                    tags=('rna' if row.get('sample_type')=='RNA' else 'dna',))
        if self.current_record and self.pool_tree.exists(self.current_record):self.pool_tree.selection_set(self.current_record)

    def _pool_selected(self,event=None):
        if self._refreshing or self._switching:return
        selected=self.pool_tree.selection()
        if selected and not selected[0].startswith('RUN::') and selected[0]!=self.current_record:
            if not self.select_record(selected[0]):
                if self.current_record and self.pool_tree.exists(self.current_record):self.pool_tree.selection_set(self.current_record)

    def _open_pool_selection(self,event=None):
        selected=self.pool_tree.selection()
        if selected and not selected[0].startswith('RUN::'):self.select_record(selected[0],'standard')
        return 'break'

    def _scan(self):
        if self.request_scan:self.request_scan()
        else:messagebox.showinfo('GenNext sırası','Toplama sekmesinde RUNS Tara işlemini çalıştırın.',parent=self)

    def refresh(self,force_metrics=False):
        if not self._ready or self._refreshing or self._switching:return
        self._refreshing=True
        try:
            self.cards,hint=ordered_runs(self.db);self.order_var.set(hint);self.rows=[];names={}
            for card in self.cards:
                for row in card['rows']:
                    family=row.get('case_family') or row.get('patient_id') or ''
                    if family not in names:names[family]=self.db.get_case_metadata(family).get('full_name') or ''
                    row['_name']=names[family];self.rows.append(row)
            self._populate_pools();self.qc.set_records(self.rows)
            if not self.current_record and self.rows:self.select_record(self.rows[0]['record_key'])
            elif self.current_record:
                row=self.db.get_sample(self.current_record)
                before=self._metrics_cache.get(self.current_record)
                if force_metrics:self._metrics_cache.pop(self.current_record,None)
                if row:
                    metrics=self.get_metrics(row)
                    after=self._metrics_cache.get(self.current_record)
                    if force_metrics or before!=after:self.qc.select_exact(row,metrics)
                self.update_header();self._refresh_related()
        finally:self._refreshing=False

    def get_metrics(self,row):
        key=row['record_key']
        paths=[]
        for f in self.db.files_for_record(key,'qc'):
            p=Path(f['path']);p=p if p.is_absolute() else self.base/p
            if p.is_file():
                stat=p.stat()
                paths.append((str(p),stat.st_mtime_ns,stat.st_size))
        sig=tuple(paths)
        old=self._metrics_cache.get(key)
        if old and old[0]==sig:return old[1]
        m,status=load_record_metrics(self.base,self.db,row);self._metrics_cache[key]=(sig,m);return m

    def refresh_source_rows(self, force=False):
        """Populate the selected run's rows in small UI-friendly batches."""
        self._source_refresh_generation+=1
        generation=self._source_refresh_generation
        selected=self.db.get_sample(self.current_record) if self.current_record else None
        if not selected:
            return
        run=selected.get('run_uuid') or ''
        pool=selected.get('pool_name') or ''
        rows=[r for r in self.rows if (r.get('run_uuid') or '')==run and
              (r.get('pool_name') or '')==pool]
        if force:
            for row in rows:
                self._metrics_cache.pop(row['record_key'],None)
        position=0
        def batch():
            nonlocal position
            if generation!=self._source_refresh_generation or not self.winfo_exists():
                return
            for row in rows[position:position+5]:
                m=self.get_metrics(row)
                self.qc.update_source_row(row,m)
            position+=5
            if position<len(rows):
                self.after(15,batch)
        self.after(1,batch)

    def select_record(self,record_key,tab=None):
        if not self._ready or self._switching:return False
        row=self.db.get_sample(record_key)
        if not row:return False
        if self.metadata._enlil_busy and record_key!=self.current_record:
            self.qc.status_var.set('Enlil sorgusu bitmeden hasta değiştirilemez.');return False
        if record_key!=self.current_record and not self.metadata.confirm_leave():return False
        if hasattr(self,'ai_panel'):self.ai_panel.flush_note()
        self._switching=True
        try:
            if self.metadata.select_record(record_key) is False:return False
            self.qc.store_current_form_state();self.current_record=record_key
            self.qc.select_exact(row,self.get_metrics(row));self.update_header();self._refresh_related()
            self._refresh_prior_ngs_brief()
            self.ai_panel.load_record(record_key)
            self.qc.refresh_report_summary()
            self.qc.refresh_report_history()
            self.metadata.refresh_comment_preview()
            if hasattr(self,'dual_panel') and self.notebook.select()==str(self.dual_panel):
                self.dual_panel.refresh(record_key)
            self._comparison=[];self._render_comparison()
            if self.pool_tree.exists(record_key):self.pool_tree.selection_set(record_key);self.pool_tree.see(record_key)
        finally:self._switching=False
        if tab:self.show_tab(tab)
        return True

    def _metadata_record_changed(self,record_key):
        if self._ready and not self._switching and record_key!=self.current_record:self.select_record(record_key)

    def _refresh_prior_ngs_brief(self):
        if not self.current_record:return
        try:self.prior_ngs_brief.set(ui_summary(find_previous_ngs(self.db,self.current_record,base=self.base,form_data=self.qc.patient_form_data),limit=3))
        except Exception:self.prior_ngs_brief.set('Önceki NGS geçmişi okunamadı; rapor kaydı değiştirilmedi.')

    def update_header(self):
        if not self.current_record:return
        row=self.db.get_sample(self.current_record) or {}
        cm=self.db.get_case_metadata(row.get('case_family') or row.get('patient_id') or '');typ=row.get('sample_type') or '?'
        self.header_var.set('  |  '.join(filter(None,[self.db.display_pool_name(row.get('run_uuid'),row.get('pool_name')),row.get('patient_id'),cm.get('full_name'),typ,
                                                   'TEKRAR OKUMA' if is_repeat(row) else ''])))
        from reporting_context import build_record_context
        clinical=build_record_context(self.db,self.current_record)['clinical']
        self.detail_var.set(f"Blok: {clinical['block_no'] or 'eksik'} | Tanı: {clinical['tumor_diagnosis'] or 'eksik'} | Tümör: {clinical['tumor_percent'] if clinical['tumor_percent'] is not None else 'eksik'}%")
        bg='#612482' if typ=='RNA' else '#123b55';self.header.configure(bg=bg)
        for child in self.header.winfo_children():
            if isinstance(child,tk.Frame):child.configure(bg=bg)
        self.title_label.configure(bg=bg);self.detail_label.configure(bg=bg)

    def open_pool_clinical(self):
        from clinical_completion import open_pool_completion
        selected=self.db.get_sample(self.current_record) if self.current_record else None
        pool=selected.get('pool_name') if selected else None
        open_pool_completion(self,self.db,pool_group=pool)
        self.refresh()

    def show_tab(self,name):
        if hasattr(self,'tabs') and name in self.tabs:
            target=str(self.tabs[name])
            if self.notebook.select()==str(self.metadata) and target!=str(self.metadata):
                if not self.metadata.confirm_leave():return False
            self.notebook.select(self.tabs[name]);return True
        return False

    def _tab_changed(self,event=None):
        if not self._ready or self._tab_guard or (event is not None and event.widget is not self.notebook):return
        selected=self.notebook.select()
        if self._last_tab==str(self.metadata) and selected!=self._last_tab and not self.metadata.confirm_leave():
            self._tab_guard=True;self.notebook.select(self.metadata)
            self.after_idle(lambda:setattr(self,'_tab_guard',False));return
        self._last_tab=selected
        if selected==str(self.qc.standard_host):
            self.qc.refresh_shared_metadata()
            self._refresh_prior_ngs_brief()
        if selected==str(self.dual_panel):self.dual_panel.refresh(self.current_record)
        if selected==str(self.qc.source_tab):self.refresh_source_rows()
        if selected==str(self.metadata):self.metadata.refresh_comment_preview()
        if selected==str(self.ai_panel):self.ai_panel.load_record(self.current_record)
        if selected==str(self.prepool_panel):self.prepool_panel.refresh()

    def metadata_changed(self):
        if not self._ready or self._switching or self._refreshing:return
        self._switching=True
        try:
            self.qc.refresh_shared_metadata()
            if not self.metadata.is_dirty() and not self.metadata._loading:self.metadata.load(force=True)
            self.update_header()
            self.qc.refresh_report_summary()
            self.qc.refresh_report_history()
            self.metadata.refresh_comment_preview()
            if self.notebook.select()==str(self.dual_panel):self.dual_panel.refresh(self.current_record)
        finally:self._switching=False
        self.refresh()
        if self.on_change:self.on_change()

    def lookup_enlil(self):
        if not self.current_record:self.show_tab('pools');return
        if self.show_tab('metadata'):self.metadata.queue_enlil_lookup()

    def switch_assay(self,assay):
        row=self.db.get_sample(self.current_record) if self.current_record else None
        if not row:self.show_tab('pools');return
        if row.get('sample_type')==assay:self.show_tab('standard');return
        options=[r for r in related_records(self.db,row,self.rows) if r.get('sample_type')==assay]
        if len(options)==1:self.select_record(options[0]['record_key'],'standard')
        elif options:
            self.show_tab('compare');self.compare_status.set(f'Bu örnekte {len(options)} {assay} kaydı var. Listeden acmak istediğiniz kaydı seçin.')
        else:self.qc.status_var.set(f'Bu MP için yerel listede {assay} kaydı yok; tür zorla değiştirilmedi.')

    def _build_comparison(self):
        self.compare_status=tk.StringVar(value='Aynı MP ailesinin teknik çalışmaları; DNA ve RNA ayrı kalır.')
        ttk.Label(self.comparison_tab,textvariable=self.compare_status,wraplength=1550,foreground='#334155').pack(anchor='w',pady=(0,8))
        self.related_tree=table(self.comparison_tab,('pool','mp','type','repeat','block','tumor'),
            ('POOL','ÖZGÜN MP','TÜR','ÇALIŞMA','BLOK','TÜMÖR (%)'),(280,190,75,150,180,110),height=5,multiple=True)
        self.related_tree.master.pack_configure(expand=False)
        self.related_tree.bind('<Double-1>',lambda e:self._open_related())
        bar=ttk.Frame(self.comparison_tab);bar.pack(fill='x',pady=8)
        actions=[('Seçili kaydı aç',self._open_related),('Önceki seçime dön',self._go_back),
                 ('İki okumayı karşılaştır',self.compare_selected),('Blok bilgisini onayla / kopyala',self.copy_block_confirmed),
                 ('Mutasyon görünümü',self._open_mutations)]
        for text,cmd in actions:ttk.Button(bar,text=text,command=cmd).pack(side='left',padx=(0,7))
        self.qc_summary=tk.StringVar(value='İki okuma seçildiğinde QC karşılaştırması burada görünür.')
        ttk.Label(self.comparison_tab,textvariable=self.qc_summary,font=('Consolas',10),padding=(10,8),
                  background='#edf3f8',justify='left').pack(fill='x',pady=(0,8))
        filterbar=ttk.Frame(self.comparison_tab);filterbar.pack(fill='x')
        self.compare_search=tk.StringVar();self.compare_filter=tk.StringVar(value='Tüm gözlemler')
        ttk.Label(filterbar,text='Gen / varyant ara:').pack(side='left')
        ttk.Entry(filterbar,textvariable=self.compare_search,width=28).pack(side='left',padx=6)
        ttk.Combobox(filterbar,textvariable=self.compare_filter,values=('Tüm gözlemler','Tek çalışmada gözlem','Oncogenic / Likely Oncogenic'),
                     state='readonly',width=28).pack(side='left')
        self.compare_count=tk.StringVar();ttk.Label(filterbar,textvariable=self.compare_count).pack(side='right')
        self.comparison_tree=table(self.comparison_tab,('kind','gene','variant','presence','class','a','adp','afilter','b','bdp','bfilter'),
            ('TÜR','GEN','VARYANT / KONUM','GÖZLEM','ONKOJENİSİTE','A: VAF/FC/DESTEK','A: DP/PROB','A: FILTRE','B: VAF/FC/DESTEK','B: DP/PROB','B: FILTRE'),
            (60,105,250,210,150,135,90,100,135,90,100),height=11)
        self.comparison_tree.bind('<Double-1>',self.show_observation_detail)
        ttk.Label(self.comparison_tab,text='Gözlem yok, negatif sonuç değildir. Konuma özgü yeterli örtülme burada doğrulanmaz. Eşleşme ham konum/allel üzerindendir; farklı indel gösterimleri ayrı kalabilir.',
                  foreground='#92400e',wraplength=1600).pack(anchor='w',pady=(7,0))
        self.compare_search.trace_add('write',lambda *a:self._render_comparison())
        self.compare_filter.trace_add('write',lambda *a:self._render_comparison())

    def _refresh_related(self):
        if not hasattr(self,'related_tree'):return
        selected=self.related_tree.selection();self.related_tree.delete(*self.related_tree.get_children())
        row=self.db.get_sample(self.current_record) if self.current_record else None
        if not row:return
        for r in related_records(self.db,row,self.rows):
            sm=self.db.get_sample_metadata(r['record_key'])
            self.related_tree.insert('','end',iid=r['record_key'],values=(r.get('pool_name'),r.get('patient_id'),r.get('sample_type'),
                'TEKRAR' if is_repeat(r) else 'İLK / MEVCUT',sm.get('block_id') or '-',sm.get('tumor_percentage') or '-'))
        selected=[k for k in selected if self.related_tree.exists(k)]
        if selected:self.related_tree.selection_set(selected)
        elif self.related_tree.exists(self.current_record):self.related_tree.selection_set(self.current_record)

    def _open_related(self):
        sel=self.related_tree.selection()
        if len(sel)!=1:return
        before=self.current_record
        if self.select_record(sel[0],'standard'):self._return_record=before

    def _go_back(self):
        if self._return_record:
            before=self.current_record
            if self.select_record(self._return_record,'standard'):self._return_record=before

    def _open_mutations(self):
        sel=self.related_tree.selection()
        if len(sel)==1 and self.open_mutations:self.open_mutations(sel[0])

    def copy_block_confirmed(self):
        sel=self.related_tree.selection()
        if len(sel)!=1 or not self.current_record or sel[0]==self.current_record:
            messagebox.showinfo('Blok aktarımı','Listeden kaynak olarak başka bir teknik kayıt seçin. Hedef, üst başlıktaki aktif kayıttır.',parent=self);return
        if not self.metadata.confirm_leave():return
        source=self.db.get_sample(sel[0]) or {};target=self.db.get_sample(self.current_record) or {}
        if case_key(source)!=case_key(target):return
        sm=self.db.get_sample_metadata(sel[0]);current=self.db.get_sample_metadata(self.current_record)
        from reporting_context import build_record_context, save_manual_fields
        current_context=build_record_context(self.db,self.current_record)
        text=f"Kaynak: {source.get('pool_name')} / {source.get('patient_id')} / {source.get('sample_type')}\nHedef: {target.get('pool_name')} / {target.get('patient_id')} / {target.get('sample_type')}\n\nBlok: {sm.get('block_id') or '-'}\nTumor: {sm.get('tumor_percentage') or '-'}\n\nYalniz blok kaynağı, blok numarası ve tumor oranı kopyalansın mi?"
        if messagebox.askyesno('Açık onayla blok aktarımı',text,parent=self):
            try:
                save_manual_fields(self.db,self.current_record,{'block_id':sm.get('block_id'),
                    'block_source':sm.get('block_source'),'tumor_percentage':sm.get('tumor_percentage')},
                    expected_revision=current_context['revision'],origin='user_selected_history',
                    source_details={name:{'record_key':sel[0]} for name in ('block_id','block_source','tumor_percentage')})
            except Exception as exc:
                messagebox.showerror('Blok aktarımı kaydedilmedi',str(exc),parent=self);return
            self.metadata.load(force=True);self.metadata_changed()

    def compare_selected(self):
        keys=self.related_tree.selection()
        if len(keys)!=2:
            messagebox.showinfo('Karşılaştırma','Ctrl ile aynı türde iki teknik kayıt seçin (DNA-DNA veya RNA-RNA).',parent=self);return
        a,b=(self.db.get_sample(k) or {} for k in keys)
        if case_key(a)!=case_key(b) or a.get('sample_type')!=b.get('sample_type') or a.get('sample_type') not in ('DNA','RNA'):
            messagebox.showwarning('Karşılaştırma','Yalnız aynı MP ailesinin aynı turdeki iki okuması karşılaştırılabilir. DNA ve RNA birbirinin tekrarı değildir.',parent=self);return
        ma,mb=self.get_metrics(a),self.get_metrics(b)
        fmt=lambda value:'-' if value is None or value=='' else str(value)
        lines=[f"A: {a.get('pool_name')} / {a.get('patient_id')} / {a.get('sample_type')}    B: {b.get('pool_name')} / {b.get('patient_id')} / {b.get('sample_type')}"]
        for label,attr in (('Q30 (%)','q30_pct'),('Ortalama örtülme','mean_coverage_x'),('>=100x (%)','coverage_100x_pct'),
                           ('Duplikasyon (%)','duplication_pct'),('FREEMIX (%)','freemix_pct'),('Okuma çifti (M)','total_reads_m')):
            lines.append(f'{label:22} A: {fmt(getattr(ma,attr)):16} B: {fmt(getattr(mb,attr))}')
        self.qc_summary.set('\n'.join(lines))
        obs_a,notes_a=read_observations(self.base,self.db,keys[0],a['sample_type'])
        obs_b,notes_b=read_observations(self.base,self.db,keys[1],b['sample_type'])
        self._comparison=compare_observations(obs_a,obs_b)
        self.compare_status.set('A: '+'; '.join(notes_a)+'    |    B: '+'; '.join(notes_b))
        self._render_comparison(clear_summary=False)

    def _render_comparison(self,clear_summary=True):
        if not hasattr(self,'comparison_tree'):return
        self.comparison_tree.delete(*self.comparison_tree.get_children());self._display_comparison={}
        query=self.compare_search.get().strip().casefold();mode=self.compare_filter.get();filtered=[]
        for r in self._comparison:
            if query and query not in ' '.join(str(r.get(k) or '') for k in ('gene','change','location','oncogenic')).casefold():continue
            if mode.startswith('Tek ') and r['a'] and r['b']:continue
            if mode.startswith('Oncogenic') and not any(x['oncogenic'].strip().casefold().replace('_',' ') in ('oncogenic','likely oncogenic') for x in r['a']+r['b']):continue
            filtered.append(r)
        for i,r in enumerate(filtered[:2000]):
            key=str(i);self._display_comparison[key]=r
            self.comparison_tree.insert('','end',iid=key,values=(r['kind'],r['gene'],r['change']+' | '+r['location'],r['presence'],r['oncogenic'],
                r['a_measure'],r['a_support'],r['a_filter'],r['b_measure'],r['b_support'],r['b_filter']))
        suffix=' (ilk 2000; aramayla daraltın)' if len(filtered)>2000 else ''
        self.compare_count.set(f'{len(filtered)} / {len(self._comparison)} gözlem{suffix}')
        if clear_summary and not self._comparison:self.qc_summary.set('İki okuma seçip karşılaştırın. Önceki karşılaştırma yeni hastaya taşınmaz.')

    def show_observation_detail(self,event=None):
        sel=self.comparison_tree.selection()
        if not sel:return
        r=self._display_comparison.get(sel[0])
        if not r:return
        win=tk.Toplevel(self);win.title('Ham gözlem A / B - rapor kararı değildir');win.geometry('1100x650')
        text=tk.Text(win,wrap='word',padx=15,pady=15);text.pack(fill='both',expand=True)
        text.insert('1.0',json.dumps({'A':r['a'],'B':r['b']},ensure_ascii=False,indent=2));text.configure(state='disabled')
