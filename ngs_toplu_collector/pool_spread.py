from __future__ import annotations
import csv, statistics, re
from collections import Counter
from collections import defaultdict
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

from variant_links import oncokb_url, franklin_url, open_url, search_text


def _pick(d,*names):
    for n in names:
        if n in d and str(d.get(n) or '').strip()!='':
            return str(d.get(n) or '').strip()
    return ''

def _float(v):
    if v is None:return None
    s=str(v).strip().replace('%','').replace(',','.')
    if not s:return None
    try:
        x=float(s)
        if '%' in str(v):x/=100.0
        return x
    except Exception:return None

def _norm_chrom(v):
    s=str(v or '').strip()
    if not s:return ''
    s=s[3:] if s.lower().startswith('chr') else s
    return 'chr'+s.upper()

def _onc(v):
    s=' '.join(str(v or '').strip().replace('_',' ').replace('-',' ').split()).lower()
    if s=='oncogenic':return 'Oncogenic'
    if s=='likely oncogenic':return 'Likely Oncogenic'
    return ''

def _variant_key(row):
    return '|'.join([_norm_chrom(_pick(row,'Chrom','Chr')),_pick(row,'Start','Pos'),_pick(row,'Ref'),_pick(row,'Alt')])

def _fmt_af(x):
    if x is None:return ''
    return f'{100*x:.3g}%'

def _ratio(a,b):
    if a is None or b is None or b<=0:return None
    return a/b

def _fmt_ratio(x):
    if x is None:return ''
    if x>=100:return f'{x:.0f}x'
    if x>=10:return f'{x:.1f}x'
    return f'{x:.2f}x'

def _median(vals):
    v=[x for x in vals if x is not None]
    return statistics.median(v) if v else None

def _class_rank(v):
    return 0 if str(v)=='Oncogenic' else 1 if str(v)=='Likely Oncogenic' else 2

def _numish(v):
    s=str(v or '').strip().replace('%','').replace('x','').replace(',','.')
    try:return float(s)
    except Exception:return None


def spread_run_choices(db):
    """Return (display label, RUN UUID) choices, never merging different RUNs.

    Latest selection follows the collector's exact RUN UUID. Invalid/deleted
    temporary upload labels (e.g. POOL 8CECE74B) cannot displace real POOLs.
    A locally named RUN is visible under its saved POOL alias without changing
    the underlying immutable technical records.
    """
    rows = db.cx.execute("SELECT run_uuid,pool_name,run_type,run_date,id FROM pools "
                         "WHERE TRIM(COALESCE(run_uuid,''))<>'' ORDER BY id DESC").fetchall()
    unique = {}
    for raw in rows:
        row = dict(raw)
        ru = str(row.get('run_uuid') or '').strip()
        if not ru or ru in unique:
            continue
        name = db.pool_group_name(db.display_pool_name(ru, row.get('pool_name')))
        if not re.fullmatch(r'POOL [1-9][0-9]{0,4}', name):
            continue
        row['canonical_name'] = name
        unique[ru] = row
    last = [str(r.get('run_uuid') or '') for r in (db.latest_scan_group() or [])]
    latest_pos = {ru: pos for pos, ru in enumerate(last)}
    ordered = sorted(unique.values(), key=lambda r: (
        1 if r['run_uuid'] in latest_pos else 0,
        -latest_pos[r['run_uuid']] if r['run_uuid'] in latest_pos else 0,
        r.get('run_date') or '', int(r.get('id') or 0)), reverse=True)
    counts = Counter(r['canonical_name'] for r in ordered)
    out = []
    for r in ordered:
        label = r['canonical_name']
        if counts[label] > 1:
            assay = str(r.get('run_type') or 'RUN').upper()
            label += f" · {assay} · {r['run_uuid'][:8]}"
        out.append((label, r['run_uuid']))
    return out


class PoolSpreadAnalyzer(ttk.Frame):
    """POOL-centric and archive-wide exploratory analysis.

    Flags are prioritisation signals, not contamination diagnoses.
    """
    def __init__(self,parent,base,db,cfg):
        super().__init__(parent,padding=8)
        self.base=Path(base);self.db=db;self.cfg=cfg
        self.current_rows=[];self.variant_map={};self.pair_rows=[];self.archive_variant_map={}
        self._archive_selected_key=''
        self._pool_runs = {}
        self._follow_latest = True
        self._build();self.refresh_pools()

    def _build(self):
        bar=ttk.Frame(self);bar.pack(fill='x')
        ttk.Label(bar,text='POOL:').pack(side='left')
        self.pool_var=tk.StringVar();self.pool_box=ttk.Combobox(bar,textvariable=self.pool_var,state='readonly',width=28)
        self.pool_box.pack(side='left',padx=5);self.pool_box.bind('<<ComboboxSelected>>',self._on_pool_selected)
        ttk.Button(bar,text='Onkojenik İndeksi Güncelle',command=self.reindex).pack(side='left',padx=4)
        ttk.Button(bar,text='Analiz Et',command=self.analyze).pack(side='left',padx=4)
        ttk.Label(bar,text='Sınıf:').pack(side='left',padx=(14,0))
        self.class_filter=tk.StringVar(value='Tümü')
        ccb=ttk.Combobox(bar,textvariable=self.class_filter,state='readonly',values=['Tümü','Oncogenic','Likely Oncogenic'],width=18)
        ccb.pack(side='left',padx=4);ccb.bind('<<ComboboxSelected>>',lambda e:(self.analyze(),self.archive_search()))
        ttk.Label(bar,text='Çağrı:').pack(side='left',padx=(10,0))
        self.mode=tk.StringVar(value='Tüm çağrılar')
        mb=ttk.Combobox(bar,textvariable=self.mode,state='readonly',values=['Tüm çağrılar','Yalnız PASS'],width=14)
        mb.pack(side='left',padx=4);mb.bind('<<ComboboxSelected>>',lambda e:self.analyze())
        self.search=tk.StringVar();ent=ttk.Entry(bar,textvariable=self.search,width=18);ent.pack(side='left',padx=4);ent.bind('<Return>',lambda e:self.analyze())
        ttk.Button(bar,text='POOL Ara',command=self.analyze).pack(side='left')
        ttk.Button(bar,text='CSV Dışa Aktar',command=self.export_csv).pack(side='right')

        self.summary=tk.StringVar(value='İndeksi güncelleyin ve bir POOL seçin.')
        ttk.Label(self,textvariable=self.summary,padding=(0,7),font=('Segoe UI',10,'bold')).pack(fill='x')
        ttk.Label(self,text='UYARI: Şüpheli patern teknik inceleme önceliğidir; tek başına kontaminasyon tanısı değildir.',foreground='#8a4b08').pack(fill='x',pady=(0,6))

        self.nb=ttk.Notebook(self);self.nb.pack(fill='both',expand=True)
        self.var_tab=ttk.Frame(self.nb);self.pair_tab=ttk.Frame(self.nb);self.det_tab=ttk.Frame(self.nb);self.archive_tab=ttk.Frame(self.nb)
        self.nb.add(self.var_tab,text='Varyant Yayılımı')
        self.nb.add(self.pair_tab,text='Hasta <-> Hasta')
        self.nb.add(self.det_tab,text='Seçili Varyant')
        self.nb.add(self.archive_tab,text='Tüm Arşiv / Mutasyon Ara')
        self._build_variants();self._build_pairs();self._build_details();self._build_archive()

    def _tree(self,parent,cols,widths,numeric=None):
        numeric=set(numeric or ())
        fr=ttk.Frame(parent);fr.pack(fill='both',expand=True)
        tr=ttk.Treeview(fr,columns=cols,show='headings')
        for c in cols:
            label={'gene':'GEN 🔎','change':'DEĞİŞİKLİK 🔎','variant':'VARYANT 🔎','name':'AD SOYAD','mp':'ÖRNEK','age':'YAŞ','tumor_pct':'TÜMÖR %','classification':'SINIF'}.get(c,c.upper())
            tr.heading(c,text=label,anchor='w',command=lambda cc=c,t=tr:self._sort_tree(t,cc,False))
            tr.column(c,width=widths.get(c,100),anchor='center' if c in numeric else 'w')
        ys=ttk.Scrollbar(fr,orient='vertical',command=tr.yview);xs=ttk.Scrollbar(fr,orient='horizontal',command=tr.xview)
        tr.configure(yscrollcommand=ys.set,xscrollcommand=xs.set)
        tr.grid(row=0,column=0,sticky='nsew');ys.grid(row=0,column=1,sticky='ns');xs.grid(row=1,column=0,sticky='ew')
        fr.rowconfigure(0,weight=1);fr.columnconfigure(0,weight=1)
        return tr

    def _sort_tree(self,tr,col,reverse):
        vals=[]
        for iid in tr.get_children(''):
            v=tr.set(iid,col);n=_numish(v)
            if col in ('class','classification','oncogenic'):
                key=(_class_rank(v),v.lower())
            elif n is not None:key=(0,n)
            else:key=(1,v.lower())
            vals.append((key,iid))
        vals.sort(reverse=reverse,key=lambda x:x[0])
        for i,(_,iid) in enumerate(vals):tr.move(iid,'',i)
        tr.heading(col,command=lambda:self._sort_tree(tr,col,not reverse))

    def _build_variants(self):
        cols=('flag','gene','change','class','variant','samples','source','max_af','second_af','ratio','pools','filters')
        widths={'flag':135,'gene':100,'change':205,'class':145,'variant':225,'samples':78,'source':130,'max_af':86,'second_af':90,'ratio':78,'pools':65,'filters':230}
        self.vtree=self._tree(self.var_tab,cols,widths,{'samples','max_af','second_af','ratio','pools'})
        self.vtree.bind('<<TreeviewSelect>>',self.show_details);self.vtree.bind('<Double-1>',self._variant_double_click);self.vtree.bind('<Button-3>',self._variant_context_menu)
        self.vtree.bind('<Motion>',lambda e:self._link_motion(e,self.vtree));self.vtree.bind('<Leave>',lambda e:self.vtree.configure(cursor=''))
        self.vtree.tag_configure('highlow',background='#fff3cd');self.vtree.tag_configure('multi',background='#eef6ff')

    def _build_pairs(self):
        cols=('flag','source','recipient','shared','highlow','source_med','recipient_med','ratio','variants')
        widths={'flag':145,'source':155,'recipient':155,'shared':74,'highlow':85,'source_med':100,'recipient_med':105,'ratio':85,'variants':620}
        self.ptree=self._tree(self.pair_tab,cols,widths,{'shared','highlow','source_med','recipient_med','ratio'})
        self.ptree.tag_configure('signal',background='#fff3cd')

    def _build_details(self):
        info=ttk.LabelFrame(self.det_tab,text='Seçili varyant özeti',padding=8);info.pack(fill='x',pady=(0,6))
        info.columnconfigure(1,weight=1);info.columnconfigure(3,weight=1)
        self.det_vars={k:tk.StringVar(value='') for k in ('variant','gene','change','class','archive')}
        pairs=[('Varyant','variant',0,0),('Sınıf','class',0,2),('Gen','gene',1,0),('Değişiklik','change',1,2),('Arşiv yayılımı','archive',2,0)]
        for label,key,row,col in pairs:
            ttk.Label(info,text=label+':',anchor='w',font=('Segoe UI',9,'bold')).grid(row=row,column=col,sticky='w',padx=(0,8),pady=2)
            ttk.Label(info,textvariable=self.det_vars[key],anchor='w').grid(row=row,column=col+1,columnspan=3 if key=='archive' else 1,sticky='ew',pady=2)
        actions=ttk.Frame(self.det_tab);actions.pack(fill='x',pady=(0,6))
        ttk.Button(actions,text='🔎 Tüm Hastalarda Göster',command=self.variant_show_archive).pack(side='left')
        ttk.Button(actions,text='OncoKB ↗',command=self.variant_open_oncokb).pack(side='left',padx=(8,3))
        ttk.Button(actions,text='Franklin ↗',command=self.variant_open_franklin).pack(side='left',padx=3)
        ttk.Label(actions,text='Mutasyon satırına sağ tık ile de erişilebilir.',foreground='#667085').pack(side='right')
        cols=('name','mp','family','rerun','type','af','ad','dp','filter','depth','pool','classification')
        widths={'name':190,'mp':125,'family':125,'rerun':65,'type':80,'af':90,'ad':75,'dp':75,'filter':115,'depth':90,'pool':135,'classification':150}
        self.dtree=self._tree(self.det_tab,cols,widths,{'rerun','af','ad','dp','depth'})

    def _build_archive(self):
        top=ttk.Frame(self.archive_tab,padding=(4,4));top.pack(fill='x')
        ttk.Label(top,text='Mutasyon / gen / protein / MP ara:').pack(side='left')
        self.archive_query=tk.StringVar();e=ttk.Entry(top,textvariable=self.archive_query,width=38);e.pack(side='left',padx=6);e.bind('<Return>',lambda ev:self.archive_search())
        ttk.Button(top,text='ARA',command=self.archive_search).pack(side='left')
        ttk.Button(top,text='OncoKB ↗',command=self.archive_open_oncokb).pack(side='right',padx=3)
        ttk.Button(top,text='Franklin ↗',command=self.archive_open_franklin).pack(side='right',padx=3)
        ttk.Label(top,text='Sınıf filtresi üstteki seçimle aynıdır.',foreground='#666').pack(side='left',padx=12)
        self.archive_info=tk.StringVar(value='Onkojenik indeksi güncelledikten sonra arama yapın.')
        ttk.Label(self.archive_tab,textvariable=self.archive_info,padding=(4,5),font=('Segoe UI',9,'bold')).pack(fill='x')

        pan=ttk.Panedwindow(self.archive_tab,orient='vertical');pan.pack(fill='both',expand=True)
        upper=ttk.LabelFrame(pan,text='Mutasyon kataloğu');lower=ttk.LabelFrame(pan,text='Seçili mutasyonun tüm hastalardaki gözlemleri');pan.add(upper,weight=2);pan.add(lower,weight=3)
        vcols=('gene','change','class','variant','patients','pools','max_af','level')
        vwidth={'gene':100,'change':220,'class':150,'variant':240,'patients':85,'pools':70,'max_af':90,'level':130}
        self.atree=self._tree(upper,vcols,vwidth,{'patients','pools','max_af'});self.atree.bind('<<TreeviewSelect>>',self.archive_show_observations);self.atree.bind('<Double-1>',self.archive_show_observations);self.atree.bind('<Button-3>',self._archive_context_menu)
        self.atree.bind('<Motion>',lambda e:self._link_motion(e,self.atree));self.atree.bind('<Leave>',lambda e:self.atree.configure(cursor=''))
        ocols=('name','mp','tc','pool','diagnosis','age','block','tumor_pct','af','ad','dp','filter','depth','class','level','rerun')
        owidth={'name':190,'mp':125,'tc':115,'pool':130,'diagnosis':280,'age':70,'block':105,'tumor_pct':85,'af':82,'ad':65,'dp':65,'filter':105,'depth':85,'class':145,'level':110,'rerun':65}
        self.aotree=self._tree(lower,ocols,owidth,{'age','tumor_pct','af','ad','dp','depth','rerun'})
        self.archive_variant_summary=tk.StringVar(value='');ttk.Label(lower,textvariable=self.archive_variant_summary,padding=4).pack(fill='x')

    def _link_motion(self,event,tr):
        cid=tr.identify_column(event.x)
        try:
            idx=int(cid[1:])-1;col=tr['columns'][idx]
        except Exception:
            col=''
        tr.configure(cursor='hand2' if col in ('gene','change','variant') else '')

    def _variant_double_click(self,event=None):
        self.variant_show_archive()
        return 'break'

    def _variant_selected(self):
        sel=self.vtree.selection()
        if not sel:return None
        return self.variant_map.get(sel[0])

    def _archive_selected(self):
        sel=self.atree.selection()
        if not sel:return None
        vk=self.archive_variant_map.get(sel[0])
        if not vk:return None
        rows=self.db.oncogenic_variant_observations(vk)
        return (vk,rows) if rows else None

    def _variant_context_menu(self,event):
        iid=self.vtree.identify_row(event.y)
        if not iid:return
        self.vtree.selection_set(iid);self.show_details()
        m=tk.Menu(self,tearoff=0)
        m.add_command(label='🔎 Bu mutasyonu tüm hastalarda göster',command=self.variant_show_archive)
        m.add_separator();m.add_command(label="OncoKB'de aç",command=self.variant_open_oncokb);m.add_command(label="Franklin'de aç",command=self.variant_open_franklin)
        m.tk_popup(event.x_root,event.y_root)

    def _archive_context_menu(self,event):
        iid=self.atree.identify_row(event.y)
        if not iid:return
        self.atree.selection_set(iid);self.archive_show_observations()
        m=tk.Menu(self,tearoff=0)
        m.add_command(label='🔎 Tüm hasta gözlemlerini göster',command=self.archive_show_observations)
        m.add_separator();m.add_command(label="OncoKB'de aç",command=self.archive_open_oncokb);m.add_command(label="Franklin'de aç",command=self.archive_open_franklin)
        m.tk_popup(event.x_root,event.y_root)

    def _open_oncokb(self,vk,rows):
        if not rows:return
        r=rows[0];gene=r['gene'] or '';change=r['change'] or ''
        open_url(oncokb_url(gene,change,vk))
        self.summary.set(f"OncoKB açıldı · {search_text(gene,change,vk)}")

    def _open_franklin(self,vk,rows):
        if not rows:return
        r=rows[0];gene=r['gene'] or '';change=r['change'] or ''
        q=search_text(gene,change,vk);self.clipboard_clear();self.clipboard_append(q)
        open_url(franklin_url(vk,self.cfg.get('franklin_reference','auto')))
        self.summary.set(f"Franklin açıldı · arama metni panoda: {q}")

    def variant_open_oncokb(self):
        item=self._variant_selected()
        if item:self._open_oncokb(item[0],item[1])

    def variant_open_franklin(self):
        item=self._variant_selected()
        if item:self._open_franklin(item[0],item[1])

    def archive_open_oncokb(self):
        item=self._archive_selected()
        if item:self._open_oncokb(item[0],item[1])

    def archive_open_franklin(self):
        item=self._archive_selected()
        if item:self._open_franklin(item[0],item[1])

    def variant_show_archive(self):
        item=self._variant_selected()
        if not item:return
        vk,obs=item;top=obs[0] if obs else None
        self.open_archive_variant(vk,(top['gene'] if top else ''),(top['change'] if top else ''))

    def open_archive_variant(self,variant_key='',gene='',change=''):
        self.nb.select(self.archive_tab)
        q=variant_key or ' '.join(x for x in (gene,change) if x)
        self.archive_query.set(q);self.archive_search()
        if variant_key:
            for iid,vk in self.archive_variant_map.items():
                if vk==variant_key:
                    self.atree.selection_set(iid);self.atree.focus(iid);self.atree.see(iid);self.archive_show_observations();return
        # If the exact key is not indexed, leave a useful gene / protein search visible.
        if not self.atree.get_children() and (gene or change):
            self.archive_query.set(' '.join(x for x in (gene,change) if x));self.archive_search()

    def _on_pool_selected(self,event=None):
        self._follow_latest = False   # explicit archive selection is preserved
        self.analyze()

    def refresh_pools(self,prefer_latest=False):
        old_label=self.pool_var.get()
        old_run=self._pool_runs.get(old_label)
        choices=spread_run_choices(self.db)
        self._pool_runs=dict(choices)
        labels=[label for label,_ in choices]
        self.pool_box['values']=labels
        latest=next((ru for _,ru in choices if ru in {
            str(row.get('run_uuid') or '') for row in (self.db.latest_scan_group() or [])
        }),None)
        if prefer_latest:
            self._follow_latest=True
        if self._follow_latest or old_run not in self._pool_runs.values():
            target=latest or (choices[0][1] if choices else None)
        else:
            target=old_run
        chosen=next((label for label,ru in choices if ru==target),'')
        if chosen!=old_label:
            self.pool_var.set(chosen)
            if chosen:
                self.analyze()
            else:
                self.summary.set('Yerel RUN listesinde adlandırılmış sayısal POOL yok.')

    def _latest_snv(self,rk):
        for r in self.db.files_for_record(rk,'snv'):
            p=Path(r['path']);p=p if p.is_absolute() else self.base/p
            if p.exists() and p.suffix.lower() in ('.tsv','.txt'):
                return p,dict(r)
        return None,None

    def reindex(self):
        try:
            self.db.ensure_analysis_schema();total_files=0;total_rows=0;skipped=0
            for srow in self.db.samples():
                rk=srow['record_key'];p,fr=self._latest_snv(rk)
                if not p:continue
                sha=fr.get('sha256') or f'{p.stat().st_size}:{int(p.stat().st_mtime)}'
                old=self.db.get_indexed_file(rk,'snv_oncogenic')
                if old and old['source_sha']==sha:
                    skipped+=1;continue
                obs=[]
                with open(p,'r',encoding='utf-8-sig',errors='replace',newline='') as f:
                    for row in csv.DictReader(f,delimiter='\t'):
                        onc=_onc(_pick(row,'Oncogenic'))
                        if not onc:continue
                        key=_variant_key(row)
                        if not key or '|||' in key:continue
                        obs.append({
                            'record_key':rk,'pool_name':srow['pool_name'],'sample_uuid':srow['sample_uuid'],'patient_id':srow['patient_id'],'case_family':srow['case_family'],
                            'sample_type':srow['sample_type'],'is_rerun':int(srow['is_rerun'] or 0),'variant_key':key,'chrom':_norm_chrom(_pick(row,'Chrom','Chr')),
                            'start':_pick(row,'Start','Pos'),'ref':_pick(row,'Ref'),'alt':_pick(row,'Alt'),'gene':_pick(row,'Gene_refGene','Gene'),'change':_pick(row,'aaChange','Protein_Change','AAChange'),
                            'oncogenic':onc,'filter':_pick(row,'Filter'),'af':_float(_pick(row,'AF','VAF')),'ad':_pick(row,'AD'),'dp':_pick(row,'DP'),
                            'highest_level':_pick(row,'HighestLevel','HighestSensitiveLevel','HighestResistanceLevel'),'source_sha':sha,'source_path':str(fr.get('path') or p)
                        })
                self.db.replace_oncogenic_observations(rk,obs,sha,len(obs));total_files+=1;total_rows+=len(obs)
            self.summary.set(f'İndeks güncellendi · yeni/değişen dosya {total_files} · onkojenik gözlem {total_rows} · değişmeyen dosya {skipped}')
            self.refresh_pools();self.analyze();self.archive_search()
        except Exception as e:
            messagebox.showerror('POOL Yayılım',str(e));self.summary.set(f'İndeks hatası: {e}')

    def _rows(self,pool):
        pass_only=self.mode.get()=='Yalnız PASS'
        ru=self._pool_runs.get(pool)
        # Do not fall back to an archive-wide search if the selection vanished.
        rows=list(self.db.oncogenic_observations_for_run(ru,pass_only=pass_only))
        cf=self.class_filter.get()
        if cf!='Tümü':rows=[r for r in rows if r['oncogenic']==cf]
        q=self.search.get().strip().lower()
        if q:rows=[r for r in rows if q in ' '.join(str(r[k] or '') for k in r.keys()).lower()]
        return rows

    def analyze(self):
        pool=self.pool_var.get().strip()
        if not pool:return
        try:
            rows=self._rows(pool);self.current_rows=rows
            for t in (self.vtree,self.ptree,self.dtree):
                for x in t.get_children():t.delete(x)
            byv=defaultdict(list)
            for r in rows:byv[r['variant_key']].append(r)
            global_counts=self.db.global_variant_counts(list(byv.keys()))
            self.variant_map={};shared=0;highlow_n=0
            items=[]
            for vk,obs in byv.items():
                obs=sorted(obs,key=lambda r:(r['af'] is not None,r['af'] or -1),reverse=True)
                top=obs[0];items.append((_class_rank(top['oncogenic']),-len(obs),top['gene'] or '',vk,obs))
            for i,(_,__,___,vk,obs) in enumerate(sorted(items)):
                top=obs[0];maxaf=top['af'];second=obs[1]['af'] if len(obs)>1 else None;rat=_ratio(maxaf,second)
                flag='TEK ÖRNEK';tag=''
                if len(obs)>=2:
                    shared+=1;flag='ORTAK';tag='multi'
                    if maxaf is not None and second is not None and maxaf>=float(self.cfg.get('spread_source_min_af',0.05)) and second<=float(self.cfg.get('spread_recipient_max_af',0.05)) and rat is not None and rat>=float(self.cfg.get('spread_ratio_min',4.0)):
                        flag='UYARI YÜKSEK->DÜŞÜK';tag='highlow';highlow_n+=1
                    elif len(obs)>=3 and maxaf is not None and maxaf<=float(self.cfg.get('spread_recipient_max_af',0.05)):
                        flag='ÇOKLU DÜŞÜK'
                pools=global_counts.get(vk,{}).get('pools',0)
                filters=', '.join(sorted(set((r['filter'] or '') for r in obs if r['filter'])))
                self.vtree.insert('', 'end',iid=f'v{i}',values=(flag,top['gene'],top['change'] or '',top['oncogenic'],vk,len(obs),top['patient_id'] or '(MP yok)',_fmt_af(maxaf),_fmt_af(second),_fmt_ratio(rat),pools,filters),tags=(tag,) if tag else ())
                self.variant_map[f'v{i}']=(vk,obs)
            self._analyze_pairs(rows)
            nsamp=len(set(r['record_key'] for r in rows))
            run_uuid=self._pool_runs.get(pool)
            if run_uuid and not rows:
                n=self.db.cx.execute('SELECT COUNT(*) FROM samples WHERE run_uuid=?',(run_uuid,)).fetchone()[0]
                indexed=self.db.cx.execute('SELECT COUNT(*) FROM analysis_indexed_files a JOIN samples s ON s.record_key=a.record_key WHERE s.run_uuid=? AND a.index_type=?',(run_uuid,'snv_oncogenic')).fetchone()[0]
                if n and not indexed:
                    self.summary.set(f'{pool} · {n} teknik örnek · yerel SNV yayılım indeksi henüz oluşturulmamış. Onkojenik İndeksi Güncelle düğmesini kullanın.')
                    return
            self.summary.set(f'{pool} · sınıf {self.class_filter.get()} · teknik örnek {nsamp} · gözlem {len(rows)} · benzersiz varyant {len(byv)} · ortak {shared} · yüksek->düşük patern {highlow_n}')
        except Exception as e:
            self.summary.set(f'Analiz hatası: {e}');messagebox.showerror('POOL Yayılım',str(e))

    def _analyze_pairs(self,rows):
        bysample=defaultdict(dict);labels={}
        for r in rows:
            bysample[r['record_key']][r['variant_key']]=r
            md=self.db.metadata_for_record(r['record_key'])
            nm=(md.get('case_full_name') or '').strip();mp=r['patient_id'] or ''
            labels[r['record_key']]=f'{nm} ({mp})' if nm and mp else (nm or mp or 'Teknik örnek')
        keys=list(bysample);out=[]
        for a in keys:
            for b in keys:
                if a==b:continue
                common=set(bysample[a])&set(bysample[b])
                if not common:continue
                highlow=[]
                for vk in common:
                    ra=bysample[a][vk];rb=bysample[b][vk];aa=ra['af'];bb=rb['af']
                    if aa is not None and bb is not None and aa>=float(self.cfg.get('spread_source_min_af',0.05)) and bb<=float(self.cfg.get('spread_recipient_max_af',0.05)) and aa/max(bb,1e-9)>=float(self.cfg.get('spread_ratio_min',4.0)) and (ra['filter'] or '').upper()=='PASS':
                        highlow.append(vk)
                if not highlow:continue
                sm=_median([bysample[a][v]['af'] for v in highlow]);rm=_median([bysample[b][v]['af'] for v in highlow]);rat=_ratio(sm,rm)
                flag='UYARI ÇOKLU YAYILIM' if len(highlow)>=int(self.cfg.get('spread_pair_min_variants',2)) else 'YÜKSEK->DÜŞÜK'
                out.append((len(highlow),len(common),rat,a,b,sm,rm,highlow,flag))
        out.sort(key=lambda x:(-x[0],-(x[2] or 0),-x[1]));self.pair_rows=out
        for i,(nhi,ncommon,rat,a,b,sm,rm,vks,flag) in enumerate(out):
            genes=[]
            for vk in vks:
                r=bysample[a][vk];genes.append((r['gene'] or '')+((' '+r['change']) if r['change'] else ''))
            self.ptree.insert('', 'end',iid=f'p{i}',values=(flag,labels[a],labels[b],ncommon,nhi,_fmt_af(sm),_fmt_af(rm),_fmt_ratio(rat),'; '.join(genes[:10])),tags=('signal',) if nhi>=int(self.cfg.get('spread_pair_min_variants',2)) else ())

    def show_details(self,event=None):
        sel=self.vtree.selection()
        if not sel:return
        item=self.variant_map.get(sel[0])
        if not item:return
        vk,obs=item
        for x in self.dtree.get_children():self.dtree.delete(x)
        top=obs[0]
        for i,r in enumerate(sorted(obs,key=lambda x:(x['af'] is not None,x['af'] or -1),reverse=True)):
            depth=self.db.sample_depth(r['record_key']);md=self.db.metadata_for_record(r['record_key'])
            self.dtree.insert('', 'end',iid=f'd{i}',values=(md.get('case_full_name') or '',r['patient_id'] or '(MP yok)',r['case_family'] or '','EVET' if r['is_rerun'] else '',r['sample_type'],_fmt_af(r['af']),r['ad'] or '',r['dp'] or '',r['filter'] or '',depth or '',r['pool_name'],r['oncogenic']))
        gc=self.db.global_variant_counts([vk]).get(vk,{})
        self.det_vars['variant'].set(vk);self.det_vars['gene'].set(top['gene'] or '');self.det_vars['change'].set(top['change'] or '');self.det_vars['class'].set(top['oncogenic'] or '')
        self.det_vars['archive'].set(f"Bu POOL: {len(obs)} teknik örnek · tüm arşiv: {gc.get('samples',0)} teknik örnek / {gc.get('pools',0)} POOL")

    def archive_search(self):
        try:
            for t in (self.atree,self.aotree):
                for x in t.get_children():t.delete(x)
            q=self.archive_query.get().strip();rows=self.db.oncogenic_archive_variants(self.class_filter.get(),q)
            self.archive_variant_map={}
            for i,r in enumerate(rows):
                iid=f'a{i}';self.archive_variant_map[iid]=r['variant_key']
                self.atree.insert('', 'end',iid=iid,values=(r['gene'] or '',r['change'] or '',r['oncogenic'] or '',r['variant_key'],r['samples'],r['pools'],_fmt_af(r['max_af']),r['highest_level'] or ''))
            self.archive_info.set(f"Arama: {q or '(tümü)'} · sınıf {self.class_filter.get()} · {len(rows)} benzersiz onkojenik/likely varyant")
            self.archive_variant_summary.set('Bir mutasyon seçin; tüm hasta/teknik örnek gözlemleri aşağıda açılır.')
        except Exception as e:
            self.archive_info.set(f'Arşiv arama hatası: {e}');messagebox.showerror('Tüm Arşiv',str(e))

    def archive_show_observations(self,event=None):
        sel=self.atree.selection()
        if not sel:return
        vk=self.archive_variant_map.get(sel[0])
        if not vk:return
        self._archive_selected_key=vk
        for x in self.aotree.get_children():self.aotree.delete(x)
        rows=self.db.oncogenic_variant_observations(vk)
        for i,r in enumerate(rows):
            self.aotree.insert('', 'end',iid=f'ao{i}',values=(
                r['full_name'] or '',r['patient_id'] or '(MP yok)',r['national_id'] or '',r['pool_name'] or '',r['diagnosis'] or '',r['age'] or '',r['block_id'] or '',r['tumor_percentage'] or '',
                _fmt_af(r['af']),r['ad'] or '',r['dp'] or '',r['filter'] or '',r['depth'] or '',r['oncogenic'] or '',r['highest_level'] or '','EVET' if r['is_rerun'] else ''
            ))
        if rows:
            r=rows[0];self.archive_variant_summary.set(f"{r['gene'] or ''} {r['change'] or ''} · {r['oncogenic'] or ''} · {vk} · {len(rows)} teknik örnek / {len(set(x['pool_name'] for x in rows))} POOL")

    def export_csv(self):
        if not self.current_rows:return
        p=filedialog.asksaveasfilename(title='POOL yayılım CSV',defaultextension='.csv',filetypes=[('CSV','*.csv')],initialfile=f'{self.pool_var.get().replace(" ","_")}_oncogenic_spread.csv')
        if not p:return
        cols=['pool_name','patient_id','case_family','sample_type','is_rerun','variant_key','gene','change','oncogenic','filter','af','ad','dp','highest_level']
        with open(p,'w',encoding='utf-8-sig',newline='') as f:
            w=csv.writer(f);w.writerow(cols)
            for r in self.current_rows:w.writerow([r[c] for c in cols])
        messagebox.showinfo('POOL Yayılım',f'CSV kaydedildi:\n{p}')
