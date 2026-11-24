from __future__ import annotations
import csv, json
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox

from variant_links import oncokb_url, franklin_url, open_url, search_text
from report_variant_locator import rank_variant


def _pick(d,*names):
    for n in names:
        if n in d and str(d.get(n) or '').strip()!='': return str(d.get(n) or '').strip()
    return ''


def _vkey(source,row):
    if source=='SNV':
        return '|'.join([source,_pick(row,'Chrom','Chr'),_pick(row,'Start','Pos'),_pick(row,'Ref'),_pick(row,'Alt')])
    if source=='CNV':
        return '|'.join([source,_pick(row,'Chrom','Chr'),_pick(row,'Start'),_pick(row,'End'),_pick(row,'SVType'),_pick(row,'GeneName','Gene')])
    return '|'.join([source,_pick(row,'gene'),_pick(row,'text')[:250]])


def _spread_key(source,row):
    if source!='SNV': return ''
    chrom=_pick(row,'Chrom','Chr')
    if chrom and not chrom.lower().startswith('chr'): chrom='chr'+chrom
    if chrom: chrom='chr'+chrom[3:].upper() if chrom.lower().startswith('chr') else chrom
    return '|'.join([chrom,_pick(row,'Start','Pos'),_pick(row,'Ref'),_pick(row,'Alt')])


class MutationViewer(ttk.Frame):
    def __init__(self,parent,base,db,cfg,archive_callback=None,on_change=None):
        super().__init__(parent,padding=8)
        self.base=Path(base); self.db=db; self.cfg=cfg; self.archive_callback=archive_callback
        self.on_change=on_change
        self.sample_map={}; self.current_record=None; self.row_map={}
        self._link_columns={'gene','change','location'}
        self._build(); self.refresh_samples()

    def _build(self):
        head=ttk.Frame(self);head.pack(fill='x',pady=(0,5))
        ttk.Label(head,text='Mutasyon İnceleme',font=('Segoe UI',13,'bold')).pack(side='left')
        ttk.Label(head,text='  Mutasyon alanına çift tık → tüm arşivde hangi hastalarda?',foreground='#52606d').pack(side='left')

        bar=ttk.LabelFrame(self,text='Örnek ve filtre',padding=(8,6)); bar.pack(fill='x')
        ttk.Label(bar,text='Hasta / teknik örnek:').pack(side='left')
        self.sample_var=tk.StringVar(); self.sample_box=ttk.Combobox(bar,textvariable=self.sample_var,state='readonly',width=54)
        self.sample_box.pack(side='left',padx=5); self.sample_box.bind('<<ComboboxSelected>>',lambda e:self.load())
        ttk.Label(bar,text='Kaynak:').pack(side='left',padx=(10,0))
        self.source=tk.StringVar(value='SNV'); cb=ttk.Combobox(bar,textvariable=self.source,state='readonly',values=['SNV','CNV','Clinical','Tümü'],width=10);cb.pack(side='left',padx=4);cb.bind('<<ComboboxSelected>>',lambda e:self.load())
        ttk.Label(bar,text='Filtre:').pack(side='left')
        self.filt=tk.StringVar(value='PASS'); fb=ttk.Combobox(bar,textvariable=self.filt,state='readonly',values=['PASS','Tümü','Onkojenik/Level'],width=15);fb.pack(side='left',padx=4);fb.bind('<<ComboboxSelected>>',lambda e:self.load())
        ttk.Label(bar,text='Ara:').pack(side='left',padx=(8,0))
        self.search=tk.StringVar(); ent=ttk.Entry(bar,textvariable=self.search,width=21);ent.pack(side='left',padx=4);ent.bind('<Return>',lambda e:self.load())
        ttk.Button(bar,text='Ara / Yenile',command=self.load).pack(side='left',padx=3)

        self.info=tk.StringVar(value='Bir örnek seçin.'); ttk.Label(self,textvariable=self.info,padding=(2,7),font=('Segoe UI',9,'bold')).pack(fill='x')
        cols=('report','source','gene','change','vaf','dp','filter','oncogenic','tier','location','detail')
        fr=ttk.Frame(self);fr.pack(fill='both',expand=True)
        self.tree=ttk.Treeview(fr,columns=cols,show='headings',height=23,selectmode='extended')
        widths={'report':85,'source':65,'gene':95,'change':205,'vaf':80,'dp':70,'filter':100,'oncogenic':135,'tier':110,'location':190,'detail':360}
        labels={'gene':'GEN 🔎','change':'DEĞİŞİKLİK 🔎','location':'VARYANT 🔎'}
        numeric={'vaf','dp'}
        for c in cols:
            self.tree.heading(c,text=labels.get(c,c.upper()),anchor='w',command=lambda cc=c:self._sort_tree(cc,False))
            self.tree.column(c,width=widths[c],anchor='center' if c in numeric else 'w')
        self.tree.pack(side='left',fill='both',expand=True)
        ys=ttk.Scrollbar(fr,orient='vertical',command=self.tree.yview);ys.pack(side='right',fill='y')
        xs=ttk.Scrollbar(self,orient='horizontal',command=self.tree.xview);xs.pack(fill='x')
        self.tree.configure(yscrollcommand=ys.set,xscrollcommand=xs.set)
        self.tree.bind('<Double-1>',self._double_click)
        self.tree.bind('<Button-3>',self._context_menu)
        self.tree.bind('<Motion>',self._motion)
        self.tree.bind('<Leave>',lambda e:self.tree.configure(cursor=''))

        bot=ttk.Frame(self);bot.pack(fill='x',pady=(7,0))
        ttk.Button(bot,text='🔎 Tüm Hastalarda Göster',command=self.show_all_patients).pack(side='left')
        ttk.Button(bot,text='OncoKB ↗',command=self.open_oncokb).pack(side='left',padx=(8,3))
        ttk.Button(bot,text='Franklin ↗',command=self.open_franklin).pack(side='left',padx=3)
        ttk.Separator(bot,orient='vertical').pack(side='left',fill='y',padx=8)
        ttk.Button(bot,text='Raporlandı',command=lambda:self.set_decision('reported')).pack(side='left')
        ttk.Button(bot,text='Raporlanmadı',command=lambda:self.set_decision('not_reported')).pack(side='left',padx=4)
        ttk.Button(bot,text='Artefakt',command=lambda:self.set_decision('artifact')).pack(side='left',padx=4)
        ttk.Button(bot,text='Kararı Temizle',command=lambda:self.set_decision(None)).pack(side='left')
        ttk.Button(bot,text='Ham Ayrıntı',command=self.details).pack(side='left',padx=8)
        ttk.Label(bot,text='Sağ tık: Tüm hastalar / OncoKB / Franklin',foreground='#667085').pack(side='right')

    def _sort_tree(self,col,reverse):
        def key(iid):
            v=self.tree.set(iid,col);s=str(v or '').strip().replace('%','').replace(',','.')
            try:return (0,float(s))
            except Exception:return (1,s.lower())
        items=sorted(self.tree.get_children(''),key=key,reverse=reverse)
        for i,iid in enumerate(items):self.tree.move(iid,'',i)
        self.tree.heading(col,command=lambda:self._sort_tree(col,not reverse))

    def _column_name(self,event):
        cid=self.tree.identify_column(event.x)
        try:
            idx=int(cid[1:])-1
            return self.tree['columns'][idx]
        except Exception:return ''

    def _motion(self,event):
        iid=self.tree.identify_row(event.y);col=self._column_name(event)
        item=self.row_map.get(iid)
        self.tree.configure(cursor='hand2' if item and item[1]=='SNV' and col in self._link_columns else '')

    def _double_click(self,event):
        iid=self.tree.identify_row(event.y)
        if iid:self.tree.selection_set(iid)
        col=self._column_name(event)
        item=self.row_map.get(iid)
        if item and item[1]=='SNV' and col in self._link_columns:
            self.show_all_patients();return 'break'
        self.details()

    def _context_menu(self,event):
        iid=self.tree.identify_row(event.y)
        if not iid:return
        self.tree.selection_set(iid)
        item=self.row_map.get(iid)
        if not item:return
        m=tk.Menu(self,tearoff=0)
        if item[1]=='SNV':
            m.add_command(label='🔎 Bu mutasyonu tüm hastalarda göster',command=self.show_all_patients)
            m.add_separator();m.add_command(label="OncoKB'de aç",command=self.open_oncokb);m.add_command(label="Franklin'de aç",command=self.open_franklin)
            m.add_command(label='Arama metnini kopyala',command=self.copy_variant_query)
            m.add_separator()
        m.add_command(label='Ham satır ayrıntısı',command=self.details)
        m.tk_popup(event.x_root,event.y_root)

    def refresh_samples(self):
        vals=[]; self.sample_map={}
        for r in self.db.samples():
            family=r['case_family'] or r['patient_id'] or ''
            cm=self.db.get_case_metadata(family)
            name=(cm.get('full_name') or '').strip()
            who=(name+' · ' if name else '')+(r['patient_id'] or '(MP yok)')
            label=f"{who} | {r['pool_name']} | {r['sample_type']}"
            vals.append(label); self.sample_map[label]=r['record_key']
        self.sample_box['values']=vals
        if vals and self.sample_var.get() not in vals:self.sample_var.set(vals[0])

    def select_record(self,record_key):
        self.refresh_samples()
        for label,rk in self.sample_map.items():
            if rk==record_key:
                self.sample_var.set(label);self.load();return

    def _path(self,row):
        p=Path(row['path']); return p if p.is_absolute() else self.base/p

    def _latest_file(self,rk,tp):
        rows=self.db.files_for_record(rk,tp.lower())
        for r in rows:
            p=self._path(r)
            if p.exists():return p
        return None

    def _match(self,row,source):
        filt=self.filt.get(); q=self.search.get().strip().lower()
        if source=='SNV':
            if filt=='PASS' and _pick(row,'Filter').upper()!='PASS':return False
            if filt=='Onkojenik/Level':
                onc=_pick(row,'Oncogenic');lev=_pick(row,'HighestLevel','HighestSensitiveLevel','HighestResistanceLevel')
                if (not onc or onc.lower() in ('unknown','false','0','na','n/a')) and not lev:return False
        elif filt=='Onkojenik/Level' and source=='CNV':
            onc=_pick(row,'Oncogenic');lev=_pick(row,'HighestLevel','HighestSensitiveLevel','HighestResistanceLevel')
            if (not onc or onc.lower() in ('unknown','false','0','na','n/a')) and not lev:return False
        if q and q not in ' '.join(str(v) for v in row.values()).lower():return False
        return True

    def _snv_rows(self,p):
        with open(p,'r',encoding='utf-8-sig',errors='replace',newline='') as f:
            for row in csv.DictReader(f,delimiter='\t'):
                if self._match(row,'SNV'):yield row

    def _cnv_rows(self,p):
        with open(p,'r',encoding='utf-8-sig',errors='replace',newline='') as f:
            for row in csv.DictReader(f,delimiter='\t'):
                if self._match(row,'CNV'):yield row

    def _clinical_rows(self,p):
        data=json.loads(p.read_text(encoding='utf-8'))
        for g in data.get('genes',[]):
            gene=g.get('gene') or g.get('label') or ''
            for vr in g.get('variant_rows') or []:
                row={'gene':gene,'text':vr.get('text') or '','cells':vr.get('cells') or []}
                q=self.search.get().strip().lower()
                if q and q not in (gene+' '+row['text']).lower():continue
                yield row

    def _display(self,source,row,decision):
        if source=='SNV':
            gene=_pick(row,'Gene_refGene','Gene'); change=_pick(row,'aaChange','Protein_Change','AAChange'); vaf=_pick(row,'AF','VAF'); dp=_pick(row,'DP'); fil=_pick(row,'Filter'); onc=_pick(row,'Oncogenic'); tier=_pick(row,'HighestLevel','CancerVar_Tier'); loc=f"{_pick(row,'Chrom')}:{_pick(row,'Start')} {_pick(row,'Ref')}>{_pick(row,'Alt')}";detail=' | '.join(x for x in [_pick(row,'tx'),_pick(row,'txChange'),_pick(row,'Clinvar'),_pick(row,'cosmic91')] if x)
        elif source=='CNV':
            gene=_pick(row,'GeneName','Gene'); change=' '.join(x for x in [_pick(row,'SVType'),('FC '+_pick(row,'FoldChange')) if _pick(row,'FoldChange') else ''] if x); vaf='';dp=_pick(row,'DP');fil='';onc=_pick(row,'Oncogenic');tier=_pick(row,'HighestLevel');loc=f"{_pick(row,'Chrom')}:{_pick(row,'Start')}-{_pick(row,'End')}";detail=' | '.join(x for x in [('Probes '+_pick(row,'Probes')) if _pick(row,'Probes') else '',('Sigma '+_pick(row,'SigmaLevel')) if _pick(row,'SigmaLevel') else '',_pick(row,'CytoBand')] if x)
        else:
            gene=row.get('gene','');change='Clinical';vaf=dp=fil=onc=tier=loc='';detail=row.get('text','')[:800]
        rep={'reported':'RAPORLANDI','not_reported':'RAPORLANMADI','artifact':'ARTEFAKT'}.get(decision,'')
        return (rep,source,gene,change,vaf,dp,fil,onc,tier,loc,detail)

    def load(self):
        for x in self.tree.get_children():self.tree.delete(x)
        self.row_map={}
        label=self.sample_var.get(); rk=self.sample_map.get(label)
        if not rk:self.info.set('Örnek seçilmedi.');return
        self.current_record=rk; decisions=self.db.variant_decisions(rk)
        sources=['SNV','CNV','Clinical'] if self.source.get()=='Tümü' else [self.source.get()]
        limit=int(self.cfg.get('mutation_view_limit',5000)); shown=0; matched=0; missing=[]
        try:
            for src in sources:
                tp=src.lower(); p=self._latest_file(rk,tp)
                if not p:missing.append(src);continue
                iterator=self._snv_rows(p) if src=='SNV' else self._cnv_rows(p) if src=='CNV' else self._clinical_rows(p)
                for row in iterator:
                    matched+=1
                    if shown>=limit:continue
                    key=_vkey(src,row); iid=f'v{shown}'
                    self.tree.insert('', 'end',iid=iid,values=self._display(src,row,decisions.get(key)))
                    self.row_map[iid]=(key,src,row);shown+=1
            miss=f" · dosya yok: {', '.join(missing)}" if missing else ''
            cap=f" · ilk {limit} gösteriliyor" if matched>limit else ''
            self.info.set(f'{label} · eşleşen {matched} · gösterilen {shown}{cap}{miss}')
        except Exception as e:
            self.info.set(f'Mutasyon okuma hatası: {e}');messagebox.showerror('Mutasyonlar',str(e))

    def focus_report_variant(self,record_key,item):
        """Jump to the real local TSV row, or show gene-only without claiming exact."""
        if not record_key or not self.db.get_sample(record_key):return 'Kayıt yok'
        self.select_record(record_key)
        gene=str(item.get('gene') or '').strip().upper()
        if not gene:return 'Gen belirtilmedi'
        assay='CNV' if str(item.get('type') or '').upper()=='CNV' else 'SNV'
        self.source.set(assay)
        self.filt.set('Tümü')
        self.search.set(gene)
        self.load()
        candidates=[]
        for iid, payload in self.row_map.items():
            key,kind,row=payload
            if kind!=assay:continue
            score,reason=rank_variant(item,row,assay)
            if score:candidates.append((score,reason,iid,key))
        if not candidates:
            message='Rapor genine ait ham '+assay+' satırı bulunamadı. Bu, negatif sonuç değildir.'
            self.info.set(message);return message
        candidates.sort(reverse=True)
        best=candidates[0]
        ties=[x for x in candidates if x[0]==best[0]]
        self.tree.selection_set(best[2]);self.tree.focus(best[2]);self.tree.see(best[2])
        if best[0]==4 and len(ties)==1:
            message='TAM HGVS+TRANSKRİPT ADAYI: '+gene+' · '+best[3]+' · koordinatlı TSV satırı işaretlendi; klinik/IGV onayı gerekir.'
        elif best[0] in (2,3):
            message='KISMİ MOLEKÜLER UYUM: '+gene+' · '+best[1]+'; koordinat kesin eşleşme sayılmaz.'
        else:
            message='YALNIZ GEN EŞLEŞMESİ: '+gene+' · '+str(len(candidates))+' aday. Tam varyant tespit edilmedi.'
        if len(ties)>1:message+=' Aynı puanlı aday sayısı: '+str(len(ties))+'.'
        self.info.set(message)
        return message

    def _selected(self):
        sel=self.tree.selection()
        if not sel:return None
        return self.row_map.get(sel[0])

    def show_all_patients(self):
        item=self._selected()
        if not item:return
        _,src,row=item
        if src!='SNV':
            messagebox.showinfo('Tüm hastalar','Tüm arşiv yayılımı şu anda SNV için kullanılabilir.');return
        vk=_spread_key(src,row);gene=_pick(row,'Gene_refGene','Gene');change=_pick(row,'aaChange','Protein_Change','AAChange')
        if self.archive_callback:
            self.archive_callback(vk,gene,change)
        else:
            self.info.set(f'Arşiv bağlantısı: {gene} {change} · {vk}')

    def copy_variant_query(self):
        item=self._selected()
        if not item:return
        _,src,row=item
        if src!='SNV':return
        q=search_text(_pick(row,'Gene_refGene','Gene'),_pick(row,'aaChange','Protein_Change','AAChange'),_spread_key(src,row))
        self.clipboard_clear();self.clipboard_append(q);self.info.set(f'Panoya kopyalandı: {q}')

    def open_oncokb(self):
        item=self._selected()
        if not item:return
        _,src,row=item
        if src!='SNV':return
        gene=_pick(row,'Gene_refGene','Gene');change=_pick(row,'aaChange','Protein_Change','AAChange');vk=_spread_key(src,row)
        open_url(oncokb_url(gene,change,vk));self.info.set(f'OncoKB açıldı: {search_text(gene,change,vk)}')

    def open_franklin(self):
        item=self._selected()
        if not item:return
        _,src,row=item
        if src!='SNV':return
        gene=_pick(row,'Gene_refGene','Gene');change=_pick(row,'aaChange','Protein_Change','AAChange');vk=_spread_key(src,row)
        q=search_text(gene,change,vk);self.clipboard_clear();self.clipboard_append(q)
        ref=self.cfg.get('franklin_reference','auto');open_url(franklin_url(vk,ref))
        self.info.set(f'Franklin açıldı · arama metni panoda: {q}')

    def set_decision(self,status):
        sel=self.tree.selection()
        if not sel or not self.current_record:return
        for iid in sel:
            item=self.row_map.get(iid)
            if not item:continue
            self.db.set_variant_decision(self.current_record,item[0],status)
            if status=='reported':
                key,source,row=item
                self.db.add_reported_variant(self.current_record,key,source,
                    _pick(row,'Gene_refGene','GeneName','Gene','gene'),
                    _pick(row,'tx','Tx','transcript'),_pick(row,'txChange','cdna'),
                    _pick(row,'aaChange','protein','change'),source='mutation_viewer',
                    source_text=_pick(row,'text'),confirmed=True)
        if self.on_change:self.on_change()
        self.load()

    def details(self,event=None):
        item=self._selected()
        if not item:return
        key,src,row=item
        win=tk.Toplevel(self);win.title(f'{src} ayrıntı');win.geometry('900x650')
        txt=tk.Text(win,wrap='word',font=('Consolas',10));txt.pack(fill='both',expand=True)
        txt.insert('1.0',json.dumps({'variant_key':key,'source':src,'data':row},ensure_ascii=False,indent=2));txt.configure(state='disabled')
