"""Small Tk views for missing clinical fields and per-field source choices."""
from __future__ import annotations
import tkinter as tk
from tkinter import ttk, messagebox
from reporting_context import build_record_context, save_manual_fields, LABELS, normalize_field

COLUMNS = [('age_at_reference','Yaş',7), ('sex','Cinsiyet',12),
           ('reporting_diagnosis','Tümör tanısı',40), ('block_id','Blok',18),
           ('tumor_percentage','Tümör %',10)]
VALUE_KEYS = {'age_at_reference':'age','sex':'sex','reporting_diagnosis':'tumor_diagnosis',
              'block_id':'block_no','tumor_percentage':'tumor_percent'}


def display(value):
    return '' if value is None else str(value)


def matching_records(db, pool_group=None, sample_scope='ALL', record_keys=None):
    wanted = set(record_keys) if record_keys is not None else None
    result = []
    for row in db.samples():
        r = dict(row)
        if wanted is not None and r['record_key'] not in wanted: continue
        # Explicit source RUN/record selection supersedes a mutable POOL label.
        if wanted is None and pool_group and db.pool_group_name(r.get('pool_name')) != db.pool_group_name(pool_group): continue
        scope = str(sample_scope or 'ALL').upper()
        if scope in {'DNA','RNA'} and str(r.get('sample_type')).upper() != scope: continue
        result.append(r)
    return result


class ClinicalCompletionDialog(tk.Toplevel):
    def __init__(self, parent, db, records, *, missing_only=False, allow_technical=False):
        super().__init__(parent)
        self.db=db;self.rows=[];self.result=False;self.allow_technical=allow_technical
        self.title('POOL klinik bilgileri' if not missing_only else 'Eksik klinik bilgileri tamamla')
        self.geometry('1140x520');self.minsize(880,360);self.transient(parent.winfo_toplevel())
        self.status=tk.StringVar(value='Her satır kendi DNA/RNA çalışmasına kaydedilir.')
        ttk.Label(self,text='Yaş, cinsiyet, tanı, blok ve tümör yüzdesi',font=('Segoe UI',12,'bold'),padding=12).pack(anchor='w')
        ttk.Label(self,text='Boş alan eksiktir. 0 ve 100 geçerlidir. Kaynak veya hasta benzerliğinden değer tahmin edilmez.',padding=(12,0,12,8)).pack(anchor='w')
        host=ttk.Frame(self);host.pack(fill='both',expand=True,padx=12)
        canvas=tk.Canvas(host,highlightthickness=0);ys=ttk.Scrollbar(host,command=canvas.yview)
        canvas.configure(yscrollcommand=ys.set);canvas.pack(side='left',fill='both',expand=True);ys.pack(side='right',fill='y')
        body=ttk.Frame(canvas);window=canvas.create_window((0,0),window=body,anchor='nw')
        body.bind('<Configure>',lambda e:canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind('<Configure>',lambda e:canvas.itemconfigure(window,width=max(e.width,1020)))
        ttk.Label(body,text='MP / çalışma',font=('Segoe UI',9,'bold')).grid(row=0,column=0,sticky='w',pady=6)
        for col,(_,label,_) in enumerate(COLUMNS,1):ttk.Label(body,text=label,font=('Segoe UI',9,'bold')).grid(row=0,column=col,sticky='w',padx=4)
        for record in records:
            context=build_record_context(db,record)
            if missing_only and not context['missing_fields']:continue
            i=len(self.rows)+1;variables={};initial={}
            label=f"{record.get('patient_id')} · {record.get('sample_type')}\n{record.get('pool_name')}"
            ttk.Label(body,text=label,width=21).grid(row=i,column=0,sticky='w',padx=(0,6),pady=5)
            for col,(name,_,width) in enumerate(COLUMNS,1):
                value=context['clinical'][VALUE_KEYS[name]];initial[name]=value
                var=tk.StringVar(value=display(value));variables[name]=var
                editable=not missing_only or name in context['missing_fields']
                if name=='sex':
                    entry=ttk.Combobox(body,textvariable=var,values=('','Erkek','Kadın','Bilinmiyor'),width=width,state='readonly' if editable else 'disabled')
                else:entry=ttk.Entry(body,textvariable=var,width=width,state='normal' if editable else 'readonly')
                entry.grid(row=i,column=col,sticky='ew',padx=4,pady=5)
                if editable:var.trace_add('write',lambda *_:self.status.set('Kaydedilmedi · değişiklikleri kaydedin.'))
            self.rows.append({'record':record,'context':context,'vars':variables,'initial':initial})
        body.columnconfigure(3,weight=1)
        ttk.Label(self,textvariable=self.status,padding=10,foreground='#8a5a00').pack(fill='x')
        bar=ttk.Frame(self,padding=(12,0,12,12));bar.pack(fill='x')
        ttk.Button(bar,text='Kaydet',command=self.save).pack(side='left')
        ttk.Button(bar,text='Kaydet ve devam et',command=self.save_and_close).pack(side='left',padx=8)
        if allow_technical:ttk.Button(bar,text='Teknik ön inceleme olarak devam et',command=self.technical_continue).pack(side='left',padx=8)
        ttk.Button(bar,text='Kapat',command=self.destroy).pack(side='right')
        self.grab_set()

    def save(self):
        pending=[]
        try:
            for row in self.rows:
                updates={name:normalize_field(name,var.get()) for name,var in row['vars'].items()}
                updates={name:value for name,value in updates.items() if value!=row['initial'][name]}
                if updates:pending.append((row,updates))
        except ValueError as exc:
            self.status.set('Kaydedilmedi');messagebox.showerror('Klinik bilgiler',str(exc),parent=self);return False
        saved=0
        try:
            for row,updates in pending:
                ctx=save_manual_fields(self.db,row['record']['record_key'],updates,expected_revision=row['context']['revision'])
                row['context']=ctx
                for name in updates:row['initial'][name]=ctx['clinical'][VALUE_KEYS[name]]
                saved+=1
        except Exception as exc:
            self.status.set(f'{saved} satır kaydedildi; kalan değişiklikler kaydedilmedi.')
            messagebox.showerror('Kayıt tamamlanamadı',str(exc),parent=self);return False
        self.status.set(f'Kaydedildi · {saved} çalışma güncellendi.');return True

    def save_and_close(self):
        if not self.save():return
        missing=[row['record']['patient_id'] for row in self.rows if build_record_context(self.db,row['record'])['missing_fields']]
        if missing and self.allow_technical:
            self.status.set('Eksik bilgiler var: '+', '.join(missing)+'. Teknik ön inceleme olarak devam edebilirsiniz.');return
        self.result=True;self.destroy()

    def technical_continue(self):
        if not self.save():return
        self.result=True;self.destroy()


def open_pool_completion(parent, db, pool_group=None, record_keys=None, sample_scope='ALL'):
    records=matching_records(db,pool_group,sample_scope,record_keys)
    if not records:
        messagebox.showinfo('Klinik bilgiler','Bu kapsamda teknik çalışma bulunamadı.',parent=parent);return False
    win=ClinicalCompletionDialog(parent,db,records)
    parent.wait_window(win);return win.result


def complete_before_export(parent, db, pool_group, sample_scope='ALL', record_keys=None):
    records=matching_records(db,pool_group,sample_scope,record_keys=record_keys)
    if not any(build_record_context(db,row)['missing_fields'] for row in records):return True
    win=ClinicalCompletionDialog(parent,db,records,missing_only=True,allow_technical=True)
    parent.wait_window(win);return win.result


def show_field_sources(parent, db, record_key, on_change=None):
    win=tk.Toplevel(parent);win.title('Alan değerleri ve kaynakları');win.geometry('1000x470');win.transient(parent.winfo_toplevel())
    context=build_record_context(db,record_key)
    sample=db.get_sample(record_key)
    ttk.Label(win,text=f"{sample.get('patient_id')} · {sample.get('sample_type')} · {sample.get('pool_name')}",padding=10,font=('Segoe UI',11,'bold')).pack(anchor='w')
    columns=('field','effective','source','status','time')
    tree=ttk.Treeview(win,columns=columns,show='headings',height=12)
    for col,label,width in zip(columns,('Alan','Kullanılan değer','Kaynak değeri','Durum','Kaynak zamanı'),(155,230,230,180,160)):
        tree.heading(col,text=label);tree.column(col,width=width,minwidth=80)
    tree.pack(fill='both',expand=True,padx=10)
    def refresh():
        nonlocal context
        context=build_record_context(db,record_key);tree.delete(*tree.get_children())
        for name,field in context['field_sources'].items():
            if name in {'source_analyst','reporting_signer'}:continue
            manual=field['manual_override']['present']
            status='Kaynakta yeni değer var' if field['source_conflict'] else 'Siz düzelttiniz' if manual else 'Web tanı önerisi' if field.get('effective_origin')=='source_proposal' else 'Önceki kayıttan' if field.get('effective_origin')=='legacy_source' else 'Webden alındı'
            source=field['source']
            if field.get('last_observation',{}).get('manual_import_conflict'):source=field['last_observation']
            availability=source.get('availability')
            source_text='N/A' if availability=='not_available' else display(source.get('value')) or 'Alınamadı'
            reference=source.get('source_reference') or {}
            stamp=source.get('observed_at') or reference.get('reported_at') or reference.get('reported_date') or ''
            tree.insert('','end',iid=name,values=(LABELS.get(name,name),display(field['effective_value']),source_text,status,stamp))
    def use_source():
        selected=tree.selection()
        if not selected:return
        try:
            updates={};reset=[];references={}
            for name in selected:
                field=context['field_sources'][name];last=field.get('last_observation') or {}
                if last.get('manual_import_conflict'):
                    updates[name]=last.get('value');references[name]=last.get('source_reference')
                elif field.get('source',{}).get('availability') in {'observed','not_available'}:
                    reset.append(name)
                else:
                    messagebox.showinfo('Kaynak değeri','Bu alan için kullanılabilir, doğrulanmış bir kaynak değeri yok.',parent=win);return
            save_manual_fields(db,record_key,updates,expected_revision=context['revision'],use_source=reset,
                origin='user_accepted_return',source_details=references)
            refresh()
            if on_change:on_change()
        except Exception as exc:messagebox.showerror('Kaynak değeri',str(exc),parent=win)
    def details(_event=None):
        selected=tree.selection()
        if not selected:return
        name=selected[0];field=context['field_sources'][name]
        source=field.get('source') or {};manual=field.get('manual_override') or {};last=field.get('last_observation') or {}
        rows=[LABELS.get(name,name),'', 'Kullanılan değer: '+display(field.get('effective_value')),
              'Son geçerli kaynak: '+display(source.get('raw',source.get('value'))),
              'Kaynak zamanı: '+display(source.get('observed_at')),
              'Elle girilen değer: '+(display(manual.get('raw',manual.get('value'))) if manual.get('present') else 'Yok'),
              'Kullanıcı kaydı zamanı: '+display(manual.get('updated_at'))]
        if last:
            status={'observed':'Okundu','missing':'Bu kaynaktan alınamadı','invalid':'Geçersiz okuma','not_available':'N/A'}.get(last.get('availability'),display(last.get('availability')))
            rows += ['', 'Son okuma: '+status, 'Son okuma değeri: '+display(last.get('raw',last.get('value'))),
                     'Son okuma zamanı: '+display(last.get('observed_at') or last.get('ingested_at'))]
        messagebox.showinfo('Alan kaynağı', '\n'.join(rows),parent=win)
    tree.bind('<Double-1>',details)
    refresh();bar=ttk.Frame(win,padding=10);bar.pack(fill='x')
    ttk.Button(bar,text='Seçili alanda kaynak değerini kullan',command=use_source).pack(side='left')
    ttk.Button(bar,text='Yenile',command=refresh).pack(side='left',padx=8)
    ttk.Button(bar,text='Seçili alanın ayrıntısı',command=details).pack(side='left',padx=8)
    ttk.Button(bar,text='Kapat',command=win.destroy).pack(side='right')
    return win
