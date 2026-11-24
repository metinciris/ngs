"""NGS web-source completion supervisor (local, opt-in human rescue).

No report/Selection decisions are authored here.  Never mark a task complete just
because a page was opened or a download event was seen.  Uses independent SQLite
state so retry metadata survives application restarts and migrations are additive.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import shutil
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

VALID_KINDS = ('run','clinical','snv','cnv','qc','fusion','selection_dna','selection_rna')
DOWNLOAD_KINDS = {'run','clinical','snv','cnv','qc','fusion'}
RETRY_STATUSES = {'retry','waiting'}
MAX_AUTO_ATTEMPTS = 3
SCHEMA_VERSION = 1


def _stamp():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _sha256(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):
            h.update(chunk)
    return h.hexdigest()


def _clean(text):
    # Do not ever write raw exception payloads containing URL query strings,
    # authentication tokens, personal identifiers or full HTTP responses.
    msg=str(text or '')
    msg=re.sub(r'(?i)(token|password|cookie|authorization|secret|api[_-]?key)\s*[:=]\s*\S+',r'\1=[REDACTED]',msg)
    msg=re.sub(r'https?://\S+','[web adresi]',msg)
    return re.sub(r'\s+',' ',msg).strip()[:350]


def classify_failure(reason):
    s=str(reason or '').lower()
    if re.search(r'\b(401|403)\b|login|session expired|unauthorized|forbidden|oturum|giris yap',s):return 'login'
    if re.search(r'\b429\b|rate.?limit|too many requests|throttl',s):return 'rate_limit'
    if re.search(r'\b(500|502|503|504|522|524)\b|server error|bad gateway|service unavailable',s):return 'server'
    if re.search(r'timeout|timed out|zaman as|bekleme suresi|not responding|connection reset|network',s):return 'timeout'
    if re.search(r'404|not found|link yok|baglanti|bulunamad',s):return 'link_missing'
    if re.search(r'loading|processing|hazir degil|bekleniyor|not ready|analiz bitmemis',s):return 'source_not_ready'
    if re.search(r'kimlik|identity|uuid|sample mismatch',s):return 'identity'
    return 'other'


def backoff_seconds(category, attempts):
    # Conservative: no request storms; max three automatic retries per issue.
    if category=='rate_limit':return min(3600,300*max(1,attempts))
    if category=='login':return 3600
    if category=='source_not_ready':return min(3600,300*max(1,attempts))
    return min(1800,30 * 4**min(3,max(0,attempts-1)))


def expected_kinds(sample):
    st=str(sample.get('sample_type') or 'DNA').upper()
    kinds=['run']
    if st in ('DNA','DNA+RNA'):
        kinds.extend(['clinical','snv','cnv','qc','selection_dna'])
    if st in ('RNA','DNA+RNA'):
        if 'clinical' not in kinds and (sample.get('links') or {}).get('clinical'):
            kinds.append('clinical')
        kinds.extend(['fusion','qc','selection_rna'])
    return list(dict.fromkeys(kinds))


@lru_cache(maxsize=1024)
def _file_validated(path,kind,expected_mp,mtime_ns,size,expected_sample_uuid=''):
    try:
        if kind=='qc':
            from source_validation import validate_qc_source
            ok,reason,_=validate_qc_source(path,expected_mp)
            return ok,reason
        if kind=='fusion':
            from source_validation import validate_fusion_source
            ok,reason,_=validate_fusion_source(path)
            return ok,reason
        if kind in ('snv','cnv') and Path(path).suffix.lower() in ('.tsv','.csv'):
            ok,reason,_=validate_manual(path,kind,expected_mp)
            return ok,reason
        if kind in ('run','clinical') and Path(path).suffix.lower()=='.json':
            ok,reason,_=validate_manual(path,kind,expected_mp)
            if not ok:return ok,reason
            # The DB record key alone does not prove that the JSON inside it
            # belongs to this sample; a mismatched run/clinical file is always
            # a missing source, never a complete technical observation.
            data=json.loads(Path(path).read_text(encoding='utf-8-sig'))
            actual=str(data.get('sample_uuid') or '').strip()
            if expected_sample_uuid and actual.lower()!=str(expected_sample_uuid).lower():
                return False,'JSON sample_uuid başka teknik örneğe ait.'
            return True,reason
        if kind in ('snv','cnv') and Path(path).suffix.lower()=='.json':
            data=json.loads(Path(path).read_text(encoding='utf-8-sig'))
            return bool(isinstance(data,dict) and (data.get('status')=='no_data' or data.get('rows') is not None)), 'Kaynak JSON doğrulandı.'
        return False,'Beklenmeyen kaynak biçimi.'
    except Exception as exc:
        return False,_clean(f'Kaynak bütünlük kontrolü: {type(exc).__name__}')


def _source_exists(db,base,record_key,kind,expected_mp='',expected_sample_uuid=''):
    try:
        files=db.files_for_record(record_key,kind)
        if not files:return False
        # The latest registered source must pass; an older good copy does
        # not silently hide a newer corrupt/incomplete download.
        f=files[0]
        p=Path(f['path']);p=p if p.is_absolute() else Path(base)/p
        if p.is_file() and p.stat().st_size>0:
            stat=p.stat()
            ok,_=_file_validated(str(p),kind,str(expected_mp),stat.st_mtime_ns,stat.st_size,str(expected_sample_uuid))
            return bool(ok)
    except (OSError,KeyError,TypeError):pass
    return False


def _status_from_source(db,base,sample,kind):
    rk=sample['record_key']
    if kind.startswith('selection_'):
        try:
            if kind=='selection_dna':
                from gennext_selection_store import current_status
                status=current_status(db,rk)
            else:
                from gennext_fusion_selection_store import current_fusion_status
                status=current_fusion_status(db,rk)
        except Exception as exc:
            return 'retry',_clean(f'Selection kaynak durumunu okuma: {exc}')
        if status and status.get('currently_verified') and status.get('analyzed'):
            # Composite technical labels remain identity-review as elsewhere.
            if 'identity_review' in str(status.get('result_status') or ''):
                return 'manual','Selection mevcut ancak birleşik MP kimliği kontrol edilmeli.'
            return 'verified','Kaynak üzerinden güncel Selection doğrulandı.'
        state=str((status or {}).get('result_status') or 'awaiting_selection')
        err=(status or {}).get('last_error') or ''
        if 'incomplete' in state or state in ('run_incomplete','run_unknown'):
            return 'waiting','Selection için analiz henüz doğrulanmış biçimde tamamlanmamış.'
        if state=='run_failed':return 'blocked','Platform RUN/SAMPLE başarısız.'
        if 'identity_review' in state:return 'manual','Selection teknik hasta kimliği kontrol gerektiriyor.'
        return 'retry',_clean(err or 'Selection okunamadı / doğrulanmadı; yeniden deneme gerekiyor.')
    row=db.cx.execute('SELECT status,last_error FROM tasks WHERE record_key=? AND task_type=?',(rk,kind)).fetchone()
    if row is not None:
        task_status=str(row['status'] or '')
        if task_status=='done':
            if _source_exists(db,base,rk,kind,sample.get('patient_id') or '',sample.get('sample_uuid') or ''):
                return 'verified','Kaynak dosyası biçim/bütünlük bakımından doğrulandı; görev DONE.'
            return 'retry','DONE işaretli görev için doğrulanabilir tam kaynak dosyası bulunamadı.'
        if task_status=='blocked':return 'blocked',_clean(row['last_error'] or 'Platform analizi başarısız.')
        if task_status=='review' and '404' in str(row['last_error'] or ''):
            return 'retry','GenNext HTTP 404; source link requires manual/re-discovery check.'
        if task_status=='review':return 'retry',_clean(row['last_error'] or 'Kısmi veri; tam kaynak yeniden gerekli.')
        if task_status in ('waiting',):return 'waiting',_clean(row['last_error'] or 'Kaynak hazırlanıyor.')
        if task_status in ('error','not_available'):
            return 'retry',_clean(row['last_error'] or 'Kaynak alınamadı; bağlantı kontrol edilmeli.')
    if not (sample.get('links') or {}).get(kind):
        return 'waiting','İlgili kaynak bağlantısı henüz görülmedi; RUNS yeniden taranmalı.'
    return 'retry','Kaynak henüz indirilmedi.'


class RecoveryStore:
    def __init__(self,base):
        self.base=Path(base)
        self.path=self.base/'DATA'/'web_recovery.sqlite3'
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.cx=sqlite3.connect(self.path,timeout=12,check_same_thread=False)
        self.cx.row_factory=sqlite3.Row
        self.cx.execute('PRAGMA journal_mode=WAL')
        self.cx.execute('''CREATE TABLE IF NOT EXISTS recovery (
           record_key TEXT NOT NULL,kind TEXT NOT NULL,
           pool TEXT NOT NULL DEFAULT '',mp TEXT NOT NULL DEFAULT '',
           state TEXT NOT NULL DEFAULT 'retry',
           attempts INTEGER NOT NULL DEFAULT 0,next_attempt_epoch REAL NOT NULL DEFAULT 0,
           failure_class TEXT NOT NULL DEFAULT '',last_reason TEXT NOT NULL DEFAULT '',
           source_state TEXT NOT NULL DEFAULT '',source_reason TEXT NOT NULL DEFAULT '',
           updated_at TEXT NOT NULL,manual_source TEXT NOT NULL DEFAULT '',
           PRIMARY KEY(record_key,kind))''')
        self.cx.execute('CREATE INDEX IF NOT EXISTS idx_recovery_due ON recovery(state,next_attempt_epoch)')
        self.cx.commit()

    def close(self):self.cx.close()

    def synchronize(self,db,record_keys=None):
        allowed=set(record_keys) if record_keys is not None else None
        changed=0
        with self.cx:
            for row in db.samples():
                sample=dict(row)
                rk=sample['record_key']
                if allowed is not None and rk not in allowed:continue
                sample=db.get_sample(rk)
                for kind in expected_kinds(sample):
                    state,reason=_status_from_source(db,self.base,sample,kind)
                    prior=self.cx.execute('SELECT * FROM recovery WHERE record_key=? AND kind=?',(rk,kind)).fetchone()
                    prior=dict(prior) if prior else None
                    if state=='verified':
                        new_state='verified';next_epoch=0;attempts=0;failure=''
                    elif state=='blocked':
                        new_state='blocked';next_epoch=0;attempts=(prior or {}).get('attempts',0);failure='platform'
                    elif state=='waiting':
                        # Keep an already-escalated MANUAL outcome, even if the
                        # next audit still sees an unresolved source WAITING.
                        attempts=(prior or {}).get('attempts',0);failure=classify_failure(reason)
                        if prior and prior['state']=='manual' and prior['source_state']==state and prior['source_reason']==reason:
                            new_state='manual';next_epoch=0
                        elif attempts>=MAX_AUTO_ATTEMPTS or failure=='login':
                            new_state='manual';next_epoch=0
                        else:
                            new_state='waiting';next_epoch=(prior or {}).get('next_attempt_epoch',0) or time.time()+300
                    else:
                        same_issue=(prior and prior['source_state']==state and prior['source_reason']==reason)
                        if same_issue and prior['state'] in ('manual','retry','waiting'):
                            new_state=prior['state'];next_epoch=prior['next_attempt_epoch']
                            attempts=prior['attempts'];failure=prior['failure_class']
                        else:
                            new_state='retry';next_epoch=0;attempts=(prior or {}).get('attempts',0)
                            failure=classify_failure(reason)
                        if failure in ('login','identity') or '404' in str(reason):
                            new_state='manual';next_epoch=0
                        elif attempts>=MAX_AUTO_ATTEMPTS:
                            new_state='manual';next_epoch=0
                    old_tuple=tuple((prior or {}).get(x) for x in ('state','source_state','source_reason'))
                    new_tuple=(new_state,state,reason)
                    if old_tuple!=new_tuple:changed+=1
                    self.cx.execute('''INSERT INTO recovery(record_key,kind,pool,mp,state,attempts,
                       next_attempt_epoch,failure_class,last_reason,source_state,source_reason,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(record_key,kind) DO UPDATE SET
                       pool=excluded.pool,mp=excluded.mp,state=excluded.state,
                       attempts=excluded.attempts,next_attempt_epoch=excluded.next_attempt_epoch,
                       failure_class=excluded.failure_class,
                       source_state=excluded.source_state,source_reason=excluded.source_reason,
                       updated_at=excluded.updated_at''',(
                       rk,kind,str(sample.get('pool_name') or ''),str(sample.get('patient_id') or ''),
                       new_state,attempts,next_epoch,failure,reason,state,reason,_stamp()))
        return changed

    def mark_outcome(self,record_key,kind,outcome,reason=''):
        prior=self.cx.execute('SELECT * FROM recovery WHERE record_key=? AND kind=?',(record_key,kind)).fetchone()
        if prior is None:return
        prior=dict(prior)
        if outcome=='done':
            # Real source verification still occurs via synchronize().
            return
        if outcome=='stopped':return
        category=classify_failure(reason or prior['source_reason'])
        attempts=prior['attempts']+1
        state='manual' if attempts>=MAX_AUTO_ATTEMPTS or (category in ('login','identity') or '404' in str(reason)) else 'waiting' if outcome in ('waiting','blocked') else 'retry'
        next_epoch=0 if state=='manual' else time.time()+backoff_seconds(category,attempts)
        with self.cx:
            self.cx.execute('''UPDATE recovery SET state=?,attempts=?,next_attempt_epoch=?,failure_class=?,
                 last_reason=?,updated_at=? WHERE record_key=? AND kind=?''',(
                 state,attempts,next_epoch,category,_clean(reason or prior['source_reason']),_stamp(),record_key,kind))

    def rows(self,record_keys=None,active_only=False):
        sql='SELECT * FROM recovery';conds=[];args=[]
        if record_keys is not None:
            if not record_keys:return []
            conds.append('record_key IN ('+','.join('?'*len(record_keys))+')');args.extend(record_keys)
        if active_only:conds.append("state<>'verified'")
        if conds:sql+=' WHERE '+' AND '.join(conds)
        sql+=' ORDER BY CASE state WHEN \'manual\' THEN 0 WHEN \'retry\' THEN 1 WHEN \'waiting\' THEN 2 WHEN \'blocked\' THEN 3 ELSE 4 END,pool,mp,kind'
        return [dict(x) for x in self.cx.execute(sql,args).fetchall()]

    def due(self,record_keys=None,limit=4,force=False):
        rows=self.rows(record_keys,active_only=True)
        now=time.time()
        allowed={'retry','waiting','manual'} if force else {'retry','waiting'}
        candidates=[r for r in rows if r['state'] in allowed and (force or r['next_attempt_epoch']<=now)]
        # Fair scheduling across samples; a huge DNA source must not starve
        # every other MP's outstanding selections or RNA sources.
        grouped={};keys=[]
        for row in candidates:
            k=row['record_key']
            if k not in grouped:grouped[k]=[];keys.append(k)
            grouped[k].append(row)
        ordered=[]
        while keys:
            following=[]
            for k in keys:
                ordered.append(grouped[k].pop(0))
                if grouped[k]:following.append(k)
            keys=following
        return ordered[:max(0,min(30,int(limit)))]

    def set_manual(self,record_key,kind,reason):
        with self.cx:
            self.cx.execute("UPDATE recovery SET state='manual',next_attempt_epoch=0,last_reason=?,updated_at=? WHERE record_key=? AND kind=?",
                            (_clean(reason),_stamp(),record_key,kind))

    def report(self,db,record_keys=None):
        self.synchronize(db,record_keys)
        rows=self.rows(record_keys)
        result={'schema':'ngs_web_recovery_v1','generated_at':_stamp(),
            'total':len(rows),'verified':sum(r['state']=='verified' for r in rows),
            'missing':sum(r['state']!='verified' for r in rows),
            'manual':sum(r['state']=='manual' for r in rows),
            'waiting':sum(r['state']=='waiting' for r in rows),
            'retry':sum(r['state']=='retry' for r in rows),
            'blocked':sum(r['state']=='blocked' for r in rows),
            'entries':[{k:r[k] for k in ('record_key','kind','pool','mp','state','attempts','failure_class','last_reason','source_reason','updated_at')} for r in rows]}
        directory=self.base/'DATA'/'WEB_RECOVERY';directory.mkdir(parents=True,exist_ok=True)
        file=directory/'WEB_EKSIK_KAYNAK_RAPORU.json';tmp=file.with_suffix('.json.tmp')
        tmp.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        os.replace(tmp,file)
        result['local_report_path']=str(file)
        return result


def manual_guide(kind):
    return {
      'run':'RUNS > ilgili teknik çalışma > Run Detail. Sayfa açılmazsa önce RUNS taramasını yenileyin.',
      'clinical':'Run Detail > Clinical. Tüm sayfaların yüklenmesini bekleyin; mümkünse kaynağı JSON olarak dışa aktarın.',
      'snv':'Somatic Detail > SNV. LoD filtresini OFF yapın, sonuçlar yüklendikten sonra Download as TSV seçin.',
      'cnv':'Somatic Detail > CNV. Filter by Kit Genes OFF; tablo kararlı ve tam göründüğünde Download as TSV.',
      'qc':'Run Detail > QC Report > MultiQC > Data > Download Plot Data. Tam general_stats tablo ZIP indirin.',
      'fusion':'Somatic Detail > Fusion. Tablo tamamlandığında Download as TSV. Sadece ekranda görünen fallback yeterli değildir.',
      'selection_dna':'Run Detail > Clinical > Selection. Analyzed durumu ve seçilmiş kutuları görün; otomatik Selection yeniden taramasını çalıştırın.',
      'selection_rna':'Run Detail > Fusion Genes > Selection. Seçili füzyonları doğrulayın; RNA negatifliğini boş Selection ile çıkarmayın.'
    }.get(kind,'İlgili kaynak sayfasını açıp doğru teknik çalışma ve kaynak dosyasını kontrol edin.')


def validate_manual(path,kind,expected_mp=''):
    """Structure validation; identity is independently required for SNV/CNV/etc."""
    p=Path(path)
    if not p.is_file() or p.stat().st_size<=0:return False,'Kaynak boş veya bulunamadı.',False
    if p.stat().st_size>2*1024*1024*1024:return False,'Kaynak 2 GB üstünde; manuel inceleyin.',False
    if kind=='qc':
        from source_validation import validate_qc_source
        ok,reason,info=validate_qc_source(p,expected_mp)
        return ok,reason,bool(info.get('sample_match'))
    if kind=='fusion':
        from source_validation import validate_fusion_source
        ok,reason,_=validate_fusion_source(p)
        return ok,reason,False
    if kind in ('snv','cnv'):
        if p.suffix.lower() not in ('.tsv','.csv'):return False,'SNV/CNV kaynağı TSV veya CSV olmalı.',False
        with p.open('r',encoding='utf-8-sig',errors='replace',newline='') as f:
            head=f.readline(1024*1024)
            # A header-only export commonly means the website did not finish
            # rendering. Empty-by-assay is handled by the collector's explicit
            # and source-identified JSON no_data state, not an empty TSV.
            first_data=f.readline(1024*1024)
        if not first_data.strip():
            return False,'TSV/CSV yalnızca başlık içeriyor; yüklemenin tamamlanması doğrulanamadı.',False
        dialect='\t' if p.suffix.lower()=='.tsv' else ','
        header={re.sub(r'[^a-z0-9]+','',h.lower()) for h in next(csv.reader([head],delimiter=dialect),[])}
        if kind=='snv':
            fields={'chrom','start','ref','alt'}
            valid=fields.issubset(header) and bool({'generefgene','gene','genename'}&header)
        else:
            valid={'chrom','start','end'}.issubset(header) and bool({'genename','gene'}&header)
        if not valid:return False,'GenNext '+kind.upper()+' sütunları doğrulanamadı.',False
        return True,'TSV/CSV genomik sütunları doğrulandı. Örnek kimliği içerikten kesin kanıtlanmadı.',False
    if kind in ('run','clinical'):
        if p.suffix.lower()!='.json':return False,'Run/Clinical için JSON kaynağı gerekli.',False
        data=json.loads(p.read_text(encoding='utf-8-sig'))
        if not isinstance(data,dict):return False,'JSON nesne değil.',False
        if not str(data.get('sample_uuid') or '').strip():
            return False,'JSON içinde sample_uuid bulunmadı; teknik kimlik doğrulanamaz.',False
        if kind=='run':
            if not isinstance(data.get('clinical_observation'),dict):
                return False,'Run Detail klinik alanları bulunmadı; sayfa tam yüklenmemiş olabilir.',False
        else:
            genes=data.get('genes')
            if not isinstance(genes,list) or not genes:
                return False,'Clinical sayfasının gen listesi eksik veya boş.',False
            try:
                total=int(data.get('total_genes',0));captured=int(data.get('captured_gene_count',0))
            except (TypeError,ValueError):total=captured=0
            if not total or captured<total or len(genes)<total:
                return False,'Clinical gen sayfalama eksik; tüm genler toplanmamış.',False
        return True,'GenNext JSON alanları doğrulandı; teknik kayıt eşleşmesi ayrıca kontrol edilmeli.',False
    return False,'Bu kaynak türünde dosya içe alma desteklenmiyor.',False


def import_manual_source(base,db,record_key,kind,file_path,identity_confirmed=False):
    """Copy source transactionally, never overwrite originals. Requires sample match or explicit human attestation."""
    if kind not in DOWNLOAD_KINDS:raise ValueError('Selection yalnız platformun doğrulanmış Selection okuyucusuyla alınabilir.')
    sample=db.get_sample(record_key)
    if not sample:raise ValueError('Teknik örnek bulunamadı.')
    p=Path(file_path).resolve()
    ok,reason,matched=validate_manual(p,kind,sample.get('patient_id') or '')
    if not ok:raise ValueError(reason)
    if kind in ('run','clinical'):
        info=json.loads(p.read_text(encoding='utf-8-sig'))
        # When the source explicitly names a technical UUID, a mismatch is a
        # hard refusal (user attestation may never override contradictory IDs).
        for field,expected in (('sample_uuid',sample.get('sample_uuid')),('run_uuid',sample.get('run_uuid'))):
            observed=str(info.get(field) or '').strip()
            if observed and expected and observed.lower()!=str(expected).lower():
                raise ValueError(f'{field} teknik kimliği dosya ile uyuşmuyor; başka hastaya ekleme reddedildi.')
    if not (matched or identity_confirmed):
        ledger=RecoveryStore(base)
        try:ledger.set_manual(record_key,kind,'Dosya yapısı uygun fakat RUN/SAMPLE eşleşmesi için kullanıcı onayı gerekli.')
        finally:ledger.close()
        return {'state':'review','message':reason+' Hasta/MP/Run eşleşmesi için manuel onay gerekli.'}
    dest=Path(base)/'OUTPUT'/'WEB_RECOVERY_MANUAL'/uuid.uuid4().hex
    dest.mkdir(parents=True,exist_ok=False)
    final=dest/('manual_'+kind+p.suffix.lower())
    temp=final.with_suffix(final.suffix+'.part')
    try:
        shutil.copyfile(p,temp)
        if _sha256(temp)!=_sha256(p):raise ValueError('Manuel dosya SHA-256 doğrulaması başarısız.')
        os.replace(temp,final)
        digest=_sha256(final)
        db.add_file({'record_key':record_key,'sample_uuid':sample['sample_uuid'],
                     'pool_name':sample.get('pool_name'),'patient_id':sample.get('patient_id'),
                     'file_type':kind,'path':str(final.relative_to(Path(base))),
                     'sha256':digest,'size':final.stat().st_size})
        # Legacy files table has a global UNIQUE(sha256). A duplicate owned by
        # another sample is NOT a successful insert for this technical identity.
        if not any(f['sha256']==digest for f in db.files_for_record(record_key,kind)):
            raise ValueError('Aynı SHA-256 başka teknik kayda bağlı; yanlış örneğe ekleme engellendi.')
        db.set_task(record_key,kind,'done','Manuel kaynak; kimlik kullanıcı onayı veya QC sample eşleşmesiyle doğrulandı.')
        ledger=RecoveryStore(base)
        try:
            ledger.synchronize(db,[record_key])
        finally:ledger.close()
        return {'state':'verified','message':f'Manuel {kind.upper()} dosyası doğrulandı ve kaydedildi ({final.stat().st_size} byte).'}
    except Exception:
        temp.unlink(missing_ok=True)
        # Preserve the source and record directory for investigation if an I/O error occurs.
        raise
