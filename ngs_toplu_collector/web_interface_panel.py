"""v0.4.61 GUI: Web Arayuzu / YZ Koklayici.

All browser operations are queued to the existing BrowserWorker. Tk never
accesses Playwright objects. ZIP export stays local until the user uploads it.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from web_interface_inspector import approve_baseline, create_ai_package, list_sessions, validate_ai_review


def is_web_ai_review_file(path):
    """Identify the Web AI review format; bound reads, never run its contents."""
    p=Path(path)
    if p.suffix.lower()!='.json':return False
    try:
        if p.stat().st_size>350000:return False
        with p.open('r',encoding='utf-8-sig') as f:
            obj=json.load(f)
        return isinstance(obj,dict) and obj.get('schema')=='ngs_web_ui_ai_review_v1'
    except (OSError,ValueError,UnicodeError):
        return False


class WebInterfacePanel(ttk.Frame):
    def __init__(self,parent,base:Path,config:dict, submit, open_browser, stop_browser, set_auto=None, active_command=None, request_snapshot=None):
        super().__init__(parent,padding=10)
        self.base=Path(base)
        self.root=self.base/'DIAGNOSTICS'/'WEB_ARAYUZ'
        self.root.mkdir(parents=True,exist_ok=True)
        self.submit=submit
        self.open_browser=open_browser
        self.stop_browser=stop_browser
        self.set_auto=set_auto or (lambda enabled,mode:None)
        self.active_command=active_command or (lambda:'')
        self.request_snapshot=request_snapshot or (lambda:None)
        self._auto_prefs=self.root/'AUTO_KOKLAYICI_AYAR.json'
        initial={'enabled':False,'mode':'errors'}
        try:
            obj=json.loads(self._auto_prefs.read_text(encoding='utf-8'))
            if isinstance(obj,dict) and type(obj.get('enabled')) is bool and obj.get('mode') in ('errors','sample'):
                initial={'enabled':obj['enabled'],'mode':obj['mode']}
        except (OSError,ValueError,UnicodeError):pass
        self.auto_enabled=tk.BooleanVar(value=initial['enabled'])
        self.auto_mode=tk.StringVar(value=initial['mode'])
        self.set_auto(self.auto_enabled.get(),self.auto_mode.get())
        self.last_zip=None
        self.info=tk.StringVar(value='Henüz bir web tanı oturumu seçilmedi.')
        self._build()
        self.refresh()

    def _build(self):
        heading=ttk.Frame(self);heading.pack(fill='x',pady=(0,8))
        ttk.Label(heading,text='Web Arayüzü / YZ Koklayıcı',font=('Segoe UI',15,'bold')).pack(anchor='w')
        ttk.Label(heading,text='Açık GenNext sayfalarının DOM/React/API yapısını ve görünmeyen hatalarını inceler. Program kodunu değiştirmez.',
                  foreground='#526579').pack(anchor='w',pady=(4,0))

        buttons=ttk.Frame(self);buttons.pack(fill='x',pady=(4,9))
        ttk.Button(buttons,text='1. Chromium Aç',command=self.open_browser).pack(side='left',padx=(0,5))
        ttk.Button(buttons,text='2. Açık Sayfaları Tara',command=self._capture).pack(side='left',padx=5)
        ttk.Button(buttons,text='3. Canlı İzle (90 sn)',command=self._record).pack(side='left',padx=5)
        ttk.Button(buttons,text='Kaydı Durdur',command=self._stop).pack(side='left',padx=5)
        ttk.Button(buttons,text='4. YZ Tanı ZIP Oluştur',command=self._package).pack(side='left',padx=5)
        ttk.Button(buttons,text='5. WEB YZ GÖRÜŞÜ İÇE AL (JSON)',command=self._import_ai).pack(side='left',padx=5)
        ttk.Button(buttons,text='Tanı Klasörü',command=self._open_root).pack(side='right')

        auto_row=ttk.Frame(self);auto_row.pack(fill='x',pady=(0,7))
        ttk.Checkbutton(auto_row,text='Otomatik taramada koklayıcı AÇIK',
                        variable=self.auto_enabled,command=self._auto_changed).pack(side='left',padx=(0,12))
        ttk.Label(auto_row,text='Kayıt modu:').pack(side='left')
        self.auto_select=ttk.Combobox(auto_row,textvariable=self.auto_mode,
                     values=('errors','sample'),state='readonly',width=10)
        self.auto_select.pack(side='left',padx=(5,12))
        self.auto_select.bind('<<ComboboxSelected>>',lambda e:self._auto_changed())
        ttk.Label(auto_row,text='errors = yalnız hata/kontrol; sample = seyrek başarılı sayfalar da',
                  foreground='#526579').pack(side='left')
        ttk.Button(auto_row,text='Sonraki görevde KAYDET',command=self._request_snapshot).pack(side='left',padx=(2,7))
        ttk.Label(auto_row,text='Kapatınca NGS durmaz',foreground='#166534').pack(side='right')
        help_row=ttk.Frame(self);help_row.pack(fill='x',pady=(0,9))
        note=('Manuel canlı izleme ayrı bir iştir. Otomatik mod toplama görevi bittikten sonra veya bir hata/kontrol görüldüğünde hafif yapısal kayıt alır. '\
              'Ziyaret edilmemiş sayfalar otomatik açılmaz. İzleme sırasında başka toplama işi başlatılamaz.')
        ttk.Label(help_row,text=note,wraplength=1460,foreground='#475569').pack(anchor='w')
        ttk.Label(help_row,text='WEB YZ görüşü için BU sekmedeki 5. düğmeyi kullanın. "YZ Dönüş / IGV" düğmesi genomik sonuçlar içindir.',
                  foreground='#92400e').pack(anchor='w',pady=(4,0))
        ttk.Label(help_row,text='Güvenlik: Hasta adları, metin/içerik, parola, cookie, token, URL ve gerçek istek gövdeleri dışa AKTARILMAZ.',
                  foreground='#92400e').pack(anchor='w',pady=(4,0))

        status=ttk.Frame(self);status.pack(fill='x',pady=(0,10))
        ttk.Label(status,textvariable=self.info,foreground='#17324d',wraplength=1480).pack(anchor='w')

        body=ttk.Panedwindow(self,orient='horizontal');body.pack(fill='both',expand=True)
        left=ttk.Frame(body);right=ttk.Frame(body)
        body.add(left,weight=2);body.add(right,weight=3)
        ttk.Label(left,text='Tanı Oturumları (en yenisi üstte)',font=('Segoe UI',10,'bold')).pack(anchor='w',pady=(0,5))
        cols=('date','mode','pages','issues','events')
        self.tree=ttk.Treeview(left,columns=cols,show='headings',height=15,selectmode='browse')
        for key,title,width in [('date','Tarih',175),('mode','Tür',82),('pages','Sayfa',70),('issues','Uyarı',70),('events','Olay',70)]:
            self.tree.heading(key,text=title);self.tree.column(key,width=width,anchor='w',stretch=(key=='date'))
        self.tree.pack(fill='both',expand=True)
        self.tree.bind('<<TreeviewSelect>>',self._show_selected)
        options=ttk.Frame(left);options.pack(fill='x',pady=(8,3))
        ttk.Button(options,text='Listeyi Yenile',command=self.refresh).pack(side='left')
        ttk.Button(options,text='WEB YZ Görüşü İçe Al',command=self._import_ai).pack(side='left',padx=(7,0))
        ttk.Button(options,text='Sağlıklı Yapıyı Onayla',command=self._approve).pack(side='left',padx=(8,0))
        ttk.Button(options,text='Tanı ZIP Yolunu Kopyala',command=self._copy_zip).pack(side='left',padx=(8,0))

        ttk.Label(right,text='Seçili oturumun YZ ön incelemesi',font=('Segoe UI',10,'bold')).pack(anchor='w',pady=(0,5))
        frame=ttk.Frame(right);frame.pack(fill='both',expand=True)
        self.detail=tk.Text(frame,wrap='word',font=('Consolas',10),background='#f8fafc',foreground='#132b3e',state='disabled')
        s=ttk.Scrollbar(frame,orient='vertical',command=self.detail.yview)
        self.detail.configure(yscrollcommand=s.set)
        self.detail.pack(side='left',fill='both',expand=True);s.pack(side='right',fill='y')
        ttk.Label(right,text='YZ dosyası bu bilgisayarda oluşturulur; ChatGPT’ye otomatik yüklenmez. ZIP’i bu sohbete siz ekleyebilirsiniz.',
                  foreground='#475569',wraplength=800).pack(anchor='w',pady=8)

    def _auto_changed(self):
        enabled=bool(self.auto_enabled.get())
        mode=self.auto_mode.get() if self.auto_mode.get() in ('errors','sample') else 'errors'
        self.set_auto(enabled,mode)
        # Persist only the user's toggle, never site state or patient data.
        try:
            temp=self._auto_prefs.with_suffix('.tmp')
            temp.write_text(json.dumps({'enabled':enabled,'mode':mode},indent=2),encoding='utf-8')
            temp.replace(self._auto_prefs)
        except OSError:pass
        self.info.set('Otomatik koklayıcı '+('AÇIK ('+mode+')' if enabled else 'KAPALI')+
                      '. Değişiklik mevcut taramada sonraki görev sınırında uygulanır.')

    def _request_snapshot(self):
        if not self.auto_enabled.get():
            self.auto_enabled.set(True)
            self._auto_changed()
        self.request_snapshot()
        self.info.set('Kayıt işaretlendi. NGS taraması kesilmeden sonraki görev sonunda güvenli yapı alınacak.')

    def _capture(self):
        if not self.submit('web_inspect_all'):
            self.info.set('Tarayıcı başka bir görev yürütüyor. İşlem bitince tekrar deneyin.')
        else:self.info.set('Açık GenNext sayfalarının yapısı taranıyor…')

    def _record(self):
        if not self.submit('web_record_session',seconds=90):
            self.info.set('Tarayıcı başka bir görev yürütüyor. İşlem bitince tekrar deneyin.')
        else:self.info.set('Canlı izleme başladı. Chromium üzerinde farklı GenNext sayfalarını açabilirsiniz; 90 sn sonra biter.')

    def _stop(self):
        # The global stop flag would otherwise interrupt an NGS collection!
        if self.active_command() in ('scan_runs','run_queue','complete_latest'):
            self.auto_enabled.set(False)
            self._auto_changed()
            self.info.set('Otomatik koklayıcı kapatıldı. NGS toplaması devam ediyor.')
            return
        self.stop_browser()
        self.info.set('Durdurma istendi; elde edilen güvenli kayıtlar saklanarak oturum kapanacak.')

    def _selected_folder(self):
        ids=self.tree.selection()
        if not ids:return None
        target=(self.root/'sessions'/ids[0]).resolve()
        return target if target.parent==(self.root/'sessions').resolve() and (target/'SESSION.json').is_file() else None

    def refresh(self,highlight=None):
        preferred=highlight.name if isinstance(highlight,Path) else (self.tree.selection()[0] if self.tree.selection() else '')
        self.tree.delete(*self.tree.get_children())
        for obj in list_sessions(self.root):
            key=obj['session_id']
            self.tree.insert('','end',iid=key,values=(obj['stamp'],obj['mode'],obj['pages'],obj['critical'],obj['events']))
        children=self.tree.get_children()
        if preferred in children:self.tree.selection_set(preferred)
        elif children:self.tree.selection_set(children[0])
        self._show_selected()

    def _show_selected(self,event=None):
        folder=self._selected_folder()
        if not folder:return
        try:
            data=json.loads((folder/'SESSION.json').read_text(encoding='utf-8'))
            issues=json.loads((folder/'ISSUES.json').read_text(encoding='utf-8')).get('issues') or []
            diffs=json.loads((folder/'BASELINE_DIFF.json').read_text(encoding='utf-8')).get('comparisons') or []
            out=[f"KAYIT: {folder.name}",f"TÜR: {data.get('mode')}",
                 f"SÜRE: {data.get('duration_sec')} sn",f"YAPISAL GÖRÜNTÜ: {len(data.get('pages') or [])}",
                 f"HATA/UYARI: {len(issues)}",f"TARAYICI OLAYI: {len(data.get('events') or [])}",
                 f"HARİCİ SEKME (YALNIZ SAYI): {data.get('excluded_external_pages')}",
                 '', 'SAYFA TÜRLERİ:']
            for p in data.get('pages') or []:
                out.append(f"  {p['file']}  {p['page_type']:12} DOM={p['nodes']}  {p.get('severity')}")
            out+=['','TESPİT EDİLEN SORUNLAR:']
            if not issues:out.append('  Tarayıcı oturumunda tanımlı teknik sorun görülmedi (görülmeyen sayfa denetlenemez).')
            for i in issues[:100]:
                out.append('  '+json.dumps(i,ensure_ascii=False,sort_keys=True))
            out+=['','ÖNCEKİ ONAYLI SÜRÜMLE FARKLAR:']
            for d in diffs[:80]:
                out.append(f"  {d['page_type']} : {d['severity']}; eksilen {d['missing_controls']}; degisen {d['changed_controls']}")
            out+=['','KAYIT ALANLARI:',
              'Tam DOM hiyerarşisi (metinsiz), rol/id ve güvenli sınıflar, React prop türleri,',
              'API JSON anahtar türleri, aktif bileşenler, konsol/network hata sınıfları.',
              'Hasta/kimlik, URL ve özel veriler bilinçli olarak dışarıda bırakılmıştır.']
            saved=folder/'WEB_AI_REVIEW.json'
            if saved.is_file():
                try:
                    obj=json.loads(saved.read_text(encoding='utf-8'))
                    previous=validate_ai_review(obj,session_id=folder.name)
                    out+=['',f'WEB YZ GÖRÜŞÜ ({len(previous)} kayıt, yerel dosyadan yüklendi):']
                    for item in previous:
                        out.append('  '+item['severity'].upper()+' / '+item['page_type']+': '+item['issue'])
                except (ValueError,OSError,TypeError):
                    out+=['','Yerel YZ görüşü okunamadı; hiçbir kod değişikliği yapılmadı.']
            self.detail.configure(state='normal');self.detail.delete('1.0','end')
            self.detail.insert('1.0','\n'.join(out));self.detail.configure(state='disabled')
        except Exception as e:
            self.info.set(f'Tanı görüntüleme uyarısı: {type(e).__name__}')

    def _approve(self):
        p=self._selected_folder()
        if p is None:messagebox.showinfo('Web Arayüzü','Önce bir tanı oturumu seçin.');return
        yes=messagebox.askyesno('Sağlıklı yapı onayı',
           'Bu oturumdaki sayfalar, GenNext doğru çalışıyorken mi kaydedildi?\n\n'
           'Onay verirseniz sayfa yapıları gelecekte karşılaştırma için başlangıç kabul edilir.\n'
           'Hatalı sayfalar başlangıç olarak kaydedilmez.')
        if not yes:return
        try:
            approved=approve_baseline(self.root,p)
            self.info.set('Onaylanan sayfa türleri: '+(', '.join(approved) if approved else 'Yok (gerekli kontroller başarısız)'))
        except Exception as e:messagebox.showerror('Başlangıç kontrolü',str(e))

    def _package(self):
        folder=self._selected_folder()
        if folder is None:messagebox.showinfo('YZ tanı paketi','Önce bir kayıt seçin.');return
        try:
            self.last_zip=create_ai_package(self.root,folder,self.base/'DIAGNOSTICS'/'WEB_YZ_PAKETLERI')
            self.info.set('YZ paketi hazır: '+str(self.last_zip)+'. Otomatik gönderilmedi.')
            try:
                self.clipboard_clear();self.clipboard_append(str(self.last_zip))
            except Exception:pass
            messagebox.showinfo('YZ Tanı ZIP Hazır',
               'Tanı ZIP dosyası oluşturuldu. Dosya yolu panoya kopyalandı.\n\n'
               'Bu ZIP’i ChatGPT’ye yükleyebilirsiniz.\n\n'+str(self.last_zip))
        except Exception as e:messagebox.showerror('YZ tanı paketi',str(e))

    def _import_ai(self):
        source=filedialog.askopenfilename(parent=self,title='WEB arayüz YZ görüşü JSON seçin (genomik YZ dönüşü DEĞİL)',
               filetypes=[('Web YZ JSON','*.json')])
        if source:self.import_ai_file(source)

    def import_ai_file(self,source):
        """Import only ngs_web_ui_ai_review_v1 into its exact matching local session.

        Also used by the generic IGV importer to safely route a misplaced web
        review. This cannot modify application code or clinical review tables.
        """
        try:
            path=Path(source)
            if path.suffix.lower()!='.json' or path.stat().st_size>350000:
                raise ValueError('Yalnız 350 KB altındaki Web YZ JSON kabul edilir.')
            obj=json.loads(path.read_text(encoding='utf-8-sig'))
            findings=validate_ai_review(obj)
            sid=str(obj['session_id'])
            if not re.fullmatch(r'WEB_\d{8}_\d{6}_\d{6}',sid):
                raise ValueError('Tanı oturumu kimliği geçersiz.')
            folder=(self.root/'sessions'/sid).resolve()
            if folder.parent!=(self.root/'sessions').resolve() or not (folder/'SESSION.json').is_file():
                raise ValueError('JSON dosyasının tanı oturumu bu bilgisayarda bulunamadı: '+sid)
            overview=json.loads((folder/'SESSION.json').read_text(encoding='utf-8'))
            if overview.get('schema')!='ngs_web_interface_session_v1':
                raise ValueError('Yerel tanı oturumu şeması uyumsuz.')
            # Second check binds the imported findings to an existing session.
            findings=validate_ai_review(obj,session_id=folder.name)
            canonical={'schema':'ngs_web_ui_ai_review_v1','session_id':sid,
                       'findings':findings,
                       'instructions':'Display-only review. No automatic code updates.'}
            # Persist only validated, inert text, never the untrusted original JSON.
            dest=folder/'WEB_AI_REVIEW.json'
            staging=folder/'WEB_AI_REVIEW.json.tmp'
            try:
                staging.write_text(json.dumps(canonical,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
                staging.replace(dest)
            finally:
                if staging.exists():staging.unlink()
            self.refresh(highlight=folder)
            self.tree.selection_set(sid);self.tree.focus(sid);self.tree.see(sid)
            out=[f'WEB YZ GÖRÜŞÜ • {sid}',
                 f'İçe aktarılan öneri sayısı: {len(findings)}',
                 'SADECE METİNSEL İNCELEME • KOD VE GENOMİK YZ KAYITLARI DEĞİŞTİRİLMEDİ','']
            for i,item in enumerate(findings,1):
                out.extend([f"{i}. [{item['severity']}] {item['page_type']}",
                    'Bulgu: '+item['issue'],
                    'Öneri: '+item['suggested_change'],
                    'Test: '+item['test_to_run'],''])
            if not findings:out.append('Doldurulmuş YZ değerlendirmesi bulunmuyor.')
            self.detail.configure(state='normal');self.detail.delete('1.0','end')
            self.detail.insert('1.0','\n'.join(out));self.detail.configure(state='disabled')
            self.info.set(f'WEB YZ görüşü alındı: {len(findings)} öneri; eşleşen oturum {sid}. Kod değişmedi.')
            messagebox.showinfo('Web Arayüzü YZ Görüşü',
                f'{len(findings)} öneri {sid} oturumuna alındı.\nGenomik YZ kayıtları ve program kodu değişmedi.')
            return True
        except (ValueError,TypeError,KeyError,OSError) as e:
            messagebox.showerror('WEB YZ görüşü alınamadı',str(e))
            return False

    def _copy_zip(self):
        if not self.last_zip or not self.last_zip.is_file():
            self.info.set('Önce YZ tanı ZIP oluşturun.');return
        self.clipboard_clear();self.clipboard_append(str(self.last_zip))
        self.info.set('ZIP dosyasının yolu panoya kopyalandı.')

    def _open_root(self):
        try:os.startfile(str(self.root))
        except Exception:self.info.set(str(self.root))

    def on_browser_result(self,info):
        d=Path(info.get('session_dir') or '')
        self.refresh(highlight=d if d.is_dir() else None)
        self.info.set(f"Web inceleme tamamlandı · {info.get('pages',0)} sayfa görüntüsü · "
                      f"{info.get('issues',0)} uyarı · {info.get('events',0)} olay. "
                      'YZ ZIP oluşturup buraya yükleyebilirsiniz.')
