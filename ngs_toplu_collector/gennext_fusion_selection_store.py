"""Independent RNA Fusion Selection history (v0.4.80).

Never overwrites the separate Clinical SNVs selections or manual Enlil text.
Raw RNA calls and unchecked candidates never count as reported choices.
"""
from __future__ import annotations

import json
from gennext_report_selection import snapshot_hash
from gennext_fusion_selection import FUSION_SCOPE
from gennext_selection_store import _now, run_phase, verified_single_mp_label


def ensure_fusion_schema(db):
    db.cx.executescript('''
    CREATE TABLE IF NOT EXISTS gennext_fusion_status(
       record_key TEXT PRIMARY KEY,analyzed INTEGER,last_run_check TEXT,
       run_phase TEXT NOT NULL DEFAULT '',last_attempt_at TEXT,
       last_error TEXT NOT NULL DEFAULT '',attempts INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS gennext_fusion_current(
       record_key TEXT PRIMARY KEY,analyzed INTEGER NOT NULL,
       included_count INTEGER NOT NULL,candidate_count INTEGER NOT NULL,
       selected_json TEXT NOT NULL,candidates_json TEXT NOT NULL,
       selection_hash TEXT NOT NULL,checked_at TEXT NOT NULL,source_url TEXT NOT NULL,
       clinical_scope TEXT NOT NULL DEFAULT 'RNA_FUSION_ONLY'
    );
    CREATE TABLE IF NOT EXISTS gennext_fusion_history(
       id INTEGER PRIMARY KEY AUTOINCREMENT,record_key TEXT NOT NULL,
       analyzed INTEGER NOT NULL,included_count INTEGER NOT NULL,candidate_count INTEGER NOT NULL,
       selected_json TEXT NOT NULL,candidates_json TEXT NOT NULL,
       selection_hash TEXT NOT NULL,changed_at TEXT NOT NULL,source_url TEXT NOT NULL,
       clinical_scope TEXT NOT NULL DEFAULT 'RNA_FUSION_ONLY'
    );
    CREATE INDEX IF NOT EXISTS idx_gennext_fusion_hist_key
      ON gennext_fusion_history(record_key,id DESC);
    ''')
    db.cx.commit()


def observe_fusion_phase(db, rk, phase):
    if phase not in ('ready','run_incomplete','run_unknown','run_failed'):
        raise ValueError('Gecersiz RUN durumu')
    with db.cx:
        db.cx.execute('''INSERT INTO gennext_fusion_status
         (record_key,run_phase,last_attempt_at,last_error,attempts)
         VALUES(?,?,?,'',0) ON CONFLICT(record_key) DO UPDATE SET
         run_phase=excluded.run_phase,last_attempt_at=excluded.last_attempt_at,last_error='' ''',
         (rk,phase,_now()))


def observe_fusion_analyzed(db,rk,analyzed):
    if not isinstance(analyzed,bool):raise ValueError('Analyzed bool gerekli')
    stamp=_now()
    with db.cx:
        db.cx.execute('''INSERT INTO gennext_fusion_status
          (record_key,analyzed,last_run_check,last_attempt_at,attempts,last_error)
          VALUES(?,?,?,?,1,'') ON CONFLICT(record_key) DO UPDATE SET
          analyzed=excluded.analyzed,last_run_check=excluded.last_run_check,
          last_attempt_at=excluded.last_attempt_at,attempts=attempts+1,last_error='' ''',
          (rk,int(analyzed),stamp,stamp))


def record_fusion_failure(db,rk,error):
    stamp=_now()
    with db.cx:
        db.cx.execute('''INSERT INTO gennext_fusion_status(record_key,last_attempt_at,last_error,attempts)
          VALUES(?,?,?,1) ON CONFLICT(record_key) DO UPDATE SET
          last_attempt_at=excluded.last_attempt_at,last_error=excluded.last_error,
          attempts=attempts+1''',(rk,stamp,str(error or '')[:700]))


def save_fusion_selection(db,rk,analyzed,data,url):
    if not isinstance(analyzed,bool) or data.get('complete') is not True:
        raise ValueError('RNA Analyzed ve tam Selection dogrulamasina ihtiyac var')
    if data.get('clinical_scope') != FUSION_SCOPE:
        raise ValueError('DNA/SNV Selection RNA tablosuna yazilamaz')
    if (data.get('included_count')!=len(data.get('selected') or []) or
        data.get('candidate_count')!=len(data.get('candidates') or [])):
        raise ValueError('RNA Fusion sayaclari tutarsiz')
    sample=db.get_sample(rk)
    if not sample or str(sample.get('sample_type') or '').upper() not in ('RNA','DNA+RNA'):
        raise ValueError('RNA teknik calisma bulunamadi')
    if run_phase(sample)!='ready':
        raise ValueError('RUN tamamlanmadi: Fusion sonucu kesinlestirilemez')
    source={**data,'analyzed':analyzed}
    digest=snapshot_hash(source)
    stamp=_now()
    values=(rk,int(analyzed),data['included_count'],data['candidate_count'],
            json.dumps(data['selected'],ensure_ascii=False,sort_keys=True),
            json.dumps(data['candidates'],ensure_ascii=False,sort_keys=True),
            digest,stamp,str(url),FUSION_SCOPE)
    with db.cx:
        old=db.cx.execute('SELECT selection_hash FROM gennext_fusion_current WHERE record_key=?',(rk,)).fetchone()
        db.cx.execute('''INSERT INTO gennext_fusion_current
        (record_key,analyzed,included_count,candidate_count,selected_json,candidates_json,
        selection_hash,checked_at,source_url,clinical_scope)
        VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(record_key) DO UPDATE SET
        analyzed=excluded.analyzed,included_count=excluded.included_count,
        candidate_count=excluded.candidate_count,selected_json=excluded.selected_json,
        candidates_json=excluded.candidates_json,selection_hash=excluded.selection_hash,
        checked_at=excluded.checked_at,source_url=excluded.source_url,
        clinical_scope=excluded.clinical_scope''',values)
        if old is None or old['selection_hash']!=digest:
            db.cx.execute('''INSERT INTO gennext_fusion_history
            (record_key,analyzed,included_count,candidate_count,selected_json,candidates_json,
            selection_hash,changed_at,source_url,clinical_scope)
            VALUES(?,?,?,?,?,?,?,?,?,?)''',values)
        db.cx.execute('''INSERT INTO gennext_fusion_status
            (record_key,analyzed,last_run_check,run_phase,last_attempt_at,last_error,attempts)
            VALUES(?,?,?,'ready',?,'',1) ON CONFLICT(record_key) DO UPDATE SET
            analyzed=excluded.analyzed,run_phase='ready',last_run_check=excluded.last_run_check,
            last_attempt_at=excluded.last_attempt_at,last_error='' ''',
            (rk,int(analyzed),stamp,stamp))
    return old is None or old['selection_hash']!=digest


def normalize_fusion_snapshot(row, sample=None):
    d=dict(row)
    for field,to in (('selected_json','selected'),('candidates_json','candidates')):
        try:d[to]=json.loads(d.get(field) or '[]')
        except (ValueError,TypeError):d[to]=[]
    phase=run_phase(sample) if sample else (d.get('run_phase') or 'run_unknown')
    latest=d.get('latest_analyzed')
    attempt=d.get('last_attempt_at') or ''
    checked=d.get('checked_at') or ''
    fresh=not (attempt and checked and str(attempt)>str(checked))
    verified=(phase=='ready' and fresh and latest is not None and
              int(latest)==int(d['analyzed']) and not bool(d.get('last_error'))
              and d.get('clinical_scope')==FUSION_SCOPE)
    d['currently_verified']=verified
    if phase!='ready':d['result_status']=phase
    elif not verified:d['result_status']='needs_fusion_refresh'
    elif not d['analyzed']:d['result_status']='fusion_analysis_incomplete'
    elif not verified_single_mp_label((sample or {}).get('patient_id')):
        d['result_status']='fusion_identity_review'
    elif d['included_count']>0:d['result_status']='fusion_selected_positive'
    else:d['result_status']='no_fusion_selection'
    return d


def current_fusion_status(db,rk):
    if db._table_exists('gennext_fusion_current'):
        r=db.cx.execute('''SELECT c.*, s.analyzed AS latest_analyzed,s.last_attempt_at,
               s.last_error,s.run_phase FROM gennext_fusion_current c LEFT JOIN gennext_fusion_status s
               ON s.record_key=c.record_key WHERE c.record_key=?''',(rk,)).fetchone()
        if r:return normalize_fusion_snapshot(dict(r),db.get_sample(rk))
    if not db._table_exists('gennext_fusion_status'):return None
    r=db.cx.execute('SELECT * FROM gennext_fusion_status WHERE record_key=?',(rk,)).fetchone()
    if not r:return None
    d=dict(r);phase=run_phase(db.get_sample(rk))
    d.update({'selected':[],'candidates':[],'included_count':None,'candidate_count':None,
              'currently_verified':False,'result_status':phase if phase!='ready' else
                ('fusion_analysis_incomplete_unread' if d.get('analyzed')==0 else
                 'fusion_identity_review' if not verified_single_mp_label((db.get_sample(rk) or {}).get('patient_id')) else
                 'awaiting_fusion_selection')})
    return d


def next_fusion_batch(db, limit=5, record_keys=None):
    ensure_fusion_schema(db)
    clauses=["UPPER(COALESCE(s.sample_type,'')) IN ('RNA','DNA+RNA')",
             "COALESCE(s.run_uuid,'')<>'' AND COALESCE(s.sample_uuid,'')<>''"]
    args=[]
    if record_keys is not None:
        if not record_keys:return []
        clauses.append('s.record_key IN ('+','.join('?'*len(record_keys))+')')
        args.extend(record_keys)
    sql='''SELECT s.record_key,s.patient_id,s.pool_name,s.sample_type,s.run_status,
       s.sample_status,st.last_attempt_at,st.analyzed
       FROM samples s LEFT JOIN gennext_fusion_status st ON st.record_key=s.record_key
       WHERE '''+' AND '.join(clauses)+''' ORDER BY
         CASE WHEN UPPER(COALESCE(s.run_status,'')) IN ('COMPLETED','COMPLETE','DONE','FINISHED','SUCCEEDED','SUCCESS')
           THEN 0 ELSE 1 END,
         CASE WHEN st.record_key IS NULL THEN 0 ELSE 1 END,
         CASE WHEN st.analyzed IS NULL OR st.analyzed=0 THEN 0 ELSE 1 END,
         COALESCE(st.last_attempt_at,'') ASC,s.id DESC LIMIT ?'''
    args.append(max(0,int(limit)))
    return [dict(x) for x in db.cx.execute(sql,args).fetchall()]
