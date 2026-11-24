from __future__ import annotations
import sqlite3, json, time, re
from pathlib import Path
from source_validation import validate_qc_source, validate_fusion_source
from hrd_utils import normalize_hrd

SCHEMA='''
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS pools(
 id INTEGER PRIMARY KEY,
 pool_name TEXT NOT NULL,
 run_uuid TEXT,
 run_type TEXT,
 run_date TEXT,
 source_page INTEGER,
 run_status TEXT,
 UNIQUE(pool_name, run_uuid)
);
CREATE TABLE IF NOT EXISTS samples(
 id INTEGER PRIMARY KEY,
 record_key TEXT NOT NULL UNIQUE,
 sample_uuid TEXT NOT NULL,
 patient_id TEXT,
 case_family TEXT,
 is_rerun INTEGER NOT NULL DEFAULT 0,
 pool_name TEXT,
 run_uuid TEXT,
 sample_type TEXT,
 depth TEXT,
 tmb TEXT,
 msi TEXT,
 hrd TEXT,
 run_status TEXT,
 sample_status TEXT,
 links_json TEXT,
 discovered_at TEXT,
 updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_samples_sample_uuid ON samples(sample_uuid);
CREATE INDEX IF NOT EXISTS idx_samples_run_uuid ON samples(run_uuid);
CREATE INDEX IF NOT EXISTS idx_samples_pool_name ON samples(pool_name);
CREATE TABLE IF NOT EXISTS tasks(
 id INTEGER PRIMARY KEY,
 record_key TEXT NOT NULL,
 task_type TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending',
 attempts INTEGER NOT NULL DEFAULT 0,
 last_error TEXT,
 updated_at TEXT,
 UNIQUE(record_key, task_type)
);
CREATE TABLE IF NOT EXISTS files(
 id INTEGER PRIMARY KEY,
 record_key TEXT,
 sample_uuid TEXT,
 pool_name TEXT,
 patient_id TEXT,
 file_type TEXT,
 path TEXT,
 sha256 TEXT,
 size INTEGER,
 created_at TEXT,
 UNIQUE(sha256)
);
CREATE TABLE IF NOT EXISTS metrics(
 id INTEGER PRIMARY KEY,
 record_key TEXT,
 sample_uuid TEXT,
 step TEXT,
 duration_sec REAL,
 status TEXT,
 created_at TEXT
);
-- An explicitly saved laboratory value must survive an automated GenNext re-scan.
CREATE TABLE IF NOT EXISTS manual_biomarker_overrides(
 record_key TEXT PRIMARY KEY,
 tmb TEXT NOT NULL DEFAULT '',
 msi TEXT NOT NULL DEFAULT '',
 hrd TEXT NOT NULL DEFAULT '',
 updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS variant_decisions(
 id INTEGER PRIMARY KEY,
 record_key TEXT NOT NULL,
 variant_key TEXT NOT NULL,
 status TEXT,
 note TEXT,
 updated_at TEXT,
 UNIQUE(record_key, variant_key)
);
CREATE TABLE IF NOT EXISTS meta(
 key TEXT PRIMARY KEY,
 value TEXT
);
'''

def now(): return time.strftime('%Y-%m-%dT%H:%M:%S')

def make_record_key(run_uuid:str, sample_uuid:str, sample_type:str='DNA') -> str:
    return f"{run_uuid or 'NO_RUN'}::{sample_uuid or 'NO_SAMPLE'}::{(sample_type or 'DNA').upper()}"

class StateDB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path=path
        self.cx=sqlite3.connect(path, check_same_thread=False)
        self.cx.row_factory=sqlite3.Row
        self._migrate_if_needed()
        self.cx.executescript(SCHEMA)
        self._ensure_v016_columns()
        self._ensure_v024_columns()
        self.ensure_metadata_schema()
        self.ensure_reporting_schema()
        from gennext_selection_store import ensure_schema as ensure_gennext_selection_schema
        ensure_gennext_selection_schema(self)
        self.ensure_ai_review_schema()
        self.cx.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('schema_version','12')")
        # v0.1.5 and earlier could mark Clinical done without actually traversing all gene pages.
        # Reset once so v0.1.6 re-collects Clinical with the verified Playwright scraper.
        cv=self.cx.execute("SELECT value FROM meta WHERE key='clinical_scraper_version'").fetchone()
        if not cv or cv[0] != '2':
            self.cx.execute("UPDATE tasks SET status='pending', last_error='Clinical scraper v2 ile yeniden toplanacak', updated_at=? WHERE task_type='clinical'",(now(),))
            self.cx.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('clinical_scraper_version','2')")
        self.cx.commit()

    def _ensure_v016_columns(self):
        cols=self._columns('samples')
        if 'case_family' not in cols:
            self.cx.execute('ALTER TABLE samples ADD COLUMN case_family TEXT')
        if 'is_rerun' not in cols:
            self.cx.execute('ALTER TABLE samples ADD COLUMN is_rerun INTEGER NOT NULL DEFAULT 0')
        # Backfill old technical labels conservatively. Double-hyphen before year means rerun.
        rows=self.cx.execute('SELECT record_key,patient_id FROM samples').fetchall()
        import re as _re
        for r in rows:
            label=(r['patient_id'] or '').upper()
            m=_re.fullmatch(r'MP(\d{1,5})--(\d{2,4})',label)
            fam=f'MP{m.group(1)}-{m.group(2)}' if m else label
            rerun=1 if m else 0
            self.cx.execute('UPDATE samples SET case_family=COALESCE(case_family,?), is_rerun=CASE WHEN is_rerun=1 THEN 1 ELSE ? END WHERE record_key=?',(fam,rerun,r['record_key']))
        self.cx.commit()

    def _ensure_v024_columns(self):
        # v0.2.4: persist NGS platformu run/sample readiness so incomplete runs are not scraped.
        pcols=self._columns('pools')
        if pcols and 'run_status' not in pcols:
            self.cx.execute('ALTER TABLE pools ADD COLUMN run_status TEXT')
        scols=self._columns('samples')
        if 'run_status' not in scols:
            self.cx.execute('ALTER TABLE samples ADD COLUMN run_status TEXT')
        if 'sample_status' not in scols:
            self.cx.execute('ALTER TABLE samples ADD COLUMN sample_status TEXT')
        self.cx.commit()

    @staticmethod
    def pool_group_name(pool_name):
        """Return a conservative display/group label.

        Run labels are user-entered and are not guaranteed to start with POOL or to
        be unique.  Only an explicit trailing assay suffix is removed.  Numeric
        POOL labels keep their historic canonical spacing for old archive views.
        Latest-run selection itself does *not* depend on this name; it uses run UUIDs.
        """
        text=' '.join(str(pool_name or '').strip().split())
        text=re.sub(r'\s*\((?:DNA|RNA|DNA\s*\+\s*RNA|RNA\s*\+\s*DNA)\)\s*$','',text,flags=re.I).strip()
        m=re.fullmatch(r'POOL\s*(\d+)',text,re.I)
        return f'POOL {m.group(1)}' if m else text

    @staticmethod
    def is_real_pool_name(pool_name):
        # Do not depend on a literal space after POOL; live Runs currently uses
        # labels such as POOL60 (DNA). Exclude helper/test names like POOL_UNKNOWN.
        return bool(re.match(r'^\s*POOL\s*\d+\b',str(pool_name or ''),re.I))

    def _table_exists(self, name:str)->bool:
        return self.cx.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(name,)).fetchone() is not None

    def _columns(self, name:str)->set[str]:
        if not self._table_exists(name): return set()
        return {r[1] for r in self.cx.execute(f'PRAGMA table_info({name})').fetchall()}

    def _migrate_if_needed(self):
        # v0.1.2 and earlier used sample_uuid UNIQUE. This silently collapsed reruns/RNA
        # that can share the same sample UUID. Rebuild state tables around record_key.
        if not self._table_exists('samples'):
            return
        cols=self._columns('samples')
        if 'record_key' in cols:
            return
        stamp=time.strftime('%Y%m%d_%H%M%S')
        self.cx.execute('PRAGMA foreign_keys=OFF')
        self.cx.execute('BEGIN')
        try:
            for name in ('samples','tasks','files','metrics'):
                if self._table_exists(name):
                    self.cx.execute(f'ALTER TABLE {name} RENAME TO {name}_legacy_{stamp}')
            self.cx.executescript(SCHEMA)
            legacy_samples=f'samples_legacy_{stamp}'
            legacy_tasks=f'tasks_legacy_{stamp}'
            legacy_files=f'files_legacy_{stamp}'
            legacy_metrics=f'metrics_legacy_{stamp}'
            sample_map={}
            if self._table_exists(legacy_samples):
                for r in self.cx.execute(f'SELECT * FROM {legacy_samples}').fetchall():
                    d=dict(r)
                    rk=make_record_key(d.get('run_uuid') or '',d.get('sample_uuid') or '',d.get('sample_type') or 'DNA')
                    sample_map[d.get('sample_uuid')]=rk
                    self.cx.execute('''INSERT OR REPLACE INTO samples(record_key,sample_uuid,patient_id,case_family,is_rerun,pool_name,run_uuid,sample_type,depth,tmb,msi,hrd,links_json,discovered_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',(rk,d.get('sample_uuid'),d.get('patient_id'),d.get('case_family') or d.get('patient_id'),d.get('is_rerun') or 0,d.get('pool_name'),d.get('run_uuid'),d.get('sample_type'),d.get('depth'),d.get('tmb'),d.get('msi'),d.get('hrd'),d.get('links_json') or '{}',d.get('discovered_at'),d.get('updated_at')))
            if self._table_exists(legacy_tasks):
                for r in self.cx.execute(f'SELECT * FROM {legacy_tasks}').fetchall():
                    d=dict(r); rk=sample_map.get(d.get('sample_uuid'))
                    if not rk: continue
                    self.cx.execute('''INSERT OR REPLACE INTO tasks(record_key,task_type,status,attempts,last_error,updated_at) VALUES(?,?,?,?,?,?)''',(rk,d.get('task_type'),d.get('status') or 'pending',d.get('attempts') or 0,d.get('last_error'),d.get('updated_at')))
            if self._table_exists(legacy_files):
                for r in self.cx.execute(f'SELECT * FROM {legacy_files}').fetchall():
                    d=dict(r); rk=sample_map.get(d.get('sample_uuid'))
                    self.cx.execute('''INSERT OR IGNORE INTO files(record_key,sample_uuid,pool_name,patient_id,file_type,path,sha256,size,created_at) VALUES(?,?,?,?,?,?,?,?,?)''',(rk,d.get('sample_uuid'),d.get('pool_name'),d.get('patient_id'),d.get('file_type'),d.get('path'),d.get('sha256'),d.get('size'),d.get('created_at')))
            if self._table_exists(legacy_metrics):
                for r in self.cx.execute(f'SELECT * FROM {legacy_metrics}').fetchall():
                    d=dict(r); rk=sample_map.get(d.get('sample_uuid'))
                    self.cx.execute('''INSERT INTO metrics(record_key,sample_uuid,step,duration_sec,status,created_at) VALUES(?,?,?,?,?,?)''',(rk,d.get('sample_uuid'),d.get('step'),d.get('duration_sec'),d.get('status'),d.get('created_at')))
            self.cx.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('migrated_from_v012',?)",(stamp,))
            self.cx.commit()
        except Exception:
            self.cx.rollback()
            raise
        finally:
            self.cx.execute('PRAGMA foreign_keys=ON')

    def upsert_pool(self,pool_name,run_uuid='',run_type='',run_date='',source_page=None,run_status=''):
        self.cx.execute('''INSERT INTO pools(pool_name,run_uuid,run_type,run_date,source_page,run_status) VALUES(?,?,?,?,?,?)
        ON CONFLICT(pool_name,run_uuid) DO UPDATE SET run_type=excluded.run_type,run_date=excluded.run_date,source_page=excluded.source_page,run_status=excluded.run_status''',(pool_name,run_uuid,run_type,run_date,source_page,run_status));self.cx.commit()

    def upsert_sample(self,s):
        t=now(); links=json.dumps(s.get('links',{}),ensure_ascii=False)
        s = dict(s)
        s['hrd'] = normalize_hrd(s.get('hrd'), allow_bare=True)
        rk=s.get('record_key') or make_record_key(s.get('run_uuid',''),s.get('sample_uuid',''),s.get('sample_type','DNA'))
        s['record_key']=rk
        self.cx.execute('''INSERT INTO samples(record_key,sample_uuid,patient_id,case_family,is_rerun,pool_name,run_uuid,sample_type,depth,tmb,msi,hrd,run_status,sample_status,links_json,discovered_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(record_key) DO UPDATE SET sample_uuid=excluded.sample_uuid,patient_id=excluded.patient_id,case_family=excluded.case_family,is_rerun=excluded.is_rerun,pool_name=excluded.pool_name,run_uuid=excluded.run_uuid,sample_type=excluded.sample_type,depth=excluded.depth,tmb=CASE WHEN EXISTS(SELECT 1 FROM manual_biomarker_overrides m WHERE m.record_key=samples.record_key) THEN samples.tmb ELSE COALESCE(NULLIF(excluded.tmb,''),samples.tmb) END,msi=CASE WHEN EXISTS(SELECT 1 FROM manual_biomarker_overrides m WHERE m.record_key=samples.record_key) THEN samples.msi ELSE COALESCE(NULLIF(excluded.msi,''),samples.msi) END,hrd=CASE WHEN EXISTS(SELECT 1 FROM manual_biomarker_overrides m WHERE m.record_key=samples.record_key) THEN samples.hrd ELSE COALESCE(NULLIF(excluded.hrd,''),samples.hrd) END,run_status=excluded.run_status,sample_status=excluded.sample_status,links_json=excluded.links_json,updated_at=excluded.updated_at''',
        (rk,s['sample_uuid'],s.get('patient_id'),s.get('case_family') or s.get('patient_id'),int(bool(s.get('is_rerun'))),s.get('pool_name'),s.get('run_uuid'),s.get('sample_type'),s.get('depth'),s.get('tmb'),s.get('msi'),s.get('hrd'),s.get('run_status',''),s.get('sample_status',''),links,t,t));self.cx.commit()
        return rk

    def manual_biomarkers_for_record(self, record_key):
        """Saved manual TMB/MSI/HRD values for one exact technical DNA record."""
        if not record_key:
            return {}
        row=self.cx.execute(
            'SELECT tmb,msi,hrd,updated_at FROM manual_biomarker_overrides WHERE record_key=?',
            (record_key,)
        ).fetchone()
        return dict(row) if row else {}

    def save_manual_dna_biomarkers(self, record_key, tmb, msi_pct, hrd):
        """Atomically save authoritative numbers and an audit/overwrite guard.

        The incoming fields are already validated by the user interface. The
        DB rejects unknown/non-DNA records so reruns cannot be mixed up.
        """
        row=self.cx.execute('SELECT sample_type FROM samples WHERE record_key=?',(record_key,)).fetchone()
        if row is None or (row['sample_type'] or '').upper()!='DNA':
            raise ValueError('Kayitli bir DNA teknik ornegi secilmelidir.')
        # TMB keeps its unit so the AI exporter can parse the stored value;
        # MSI keeps its % unit and the QC form strips it for its numeric editor.
        tmb_db=f'{tmb} mut/Mb' if tmb and tmb!='N/A' else (tmb or '')
        msi_db=f'{msi_pct}%' if msi_pct and msi_pct!='N/A' else (msi_pct or '')
        hrd_db=normalize_hrd(hrd,allow_bare=True) if hrd else ''
        if hrd and not hrd_db:
            raise ValueError('HRD Score gecersiz.')
        timestamp=now()
        with self.cx:
            updated=self.cx.execute("""UPDATE samples SET tmb=?,msi=?,hrd=?,updated_at=?
                WHERE record_key=? AND UPPER(sample_type)='DNA' """,
                (tmb_db,msi_db,hrd_db,timestamp,record_key))
            if updated.rowcount!=1:
                raise ValueError('DNA kaydi guncellenemedi.')
            self.cx.execute("""INSERT INTO manual_biomarker_overrides(record_key,tmb,msi,hrd,updated_at)
                VALUES(?,?,?,?,?) ON CONFLICT(record_key) DO UPDATE SET
                tmb=excluded.tmb,msi=excluded.msi,hrd=excluded.hrd,updated_at=excluded.updated_at""",
                (record_key,tmb_db,msi_db,hrd_db,timestamp))
        return True

    def set_sample_hrd(self,record_key,score):
        """Persist an exact HRD Score captured from the dedicated GenNext Clinical HRD card."""
        value=normalize_hrd(score, allow_bare=True)
        if not record_key or not value:
            return False
        row=self.cx.execute('SELECT 1 FROM manual_biomarker_overrides WHERE record_key=?',(record_key,)).fetchone()
        if row:  # An explicit manual save takes precedence over later scans.
            return False
        result=self.cx.execute('UPDATE samples SET hrd=?, updated_at=? WHERE record_key=?',(value,now(),record_key))
        self.cx.commit()
        return result.rowcount==1

    def ensure_tasks(self,record_key,types):
        for tp in types:
            self.cx.execute('INSERT OR IGNORE INTO tasks(record_key,task_type,status,updated_at) VALUES(?,?,?,?)',(record_key,tp,'pending',now()))
        self.cx.commit()

    def set_task(self,record_key,tp,status,error=None):
        self.cx.execute('''INSERT INTO tasks(record_key,task_type,status,attempts,last_error,updated_at) VALUES(?,?,?,?,?,?)
        ON CONFLICT(record_key,task_type) DO UPDATE SET status=excluded.status,attempts=tasks.attempts+1,last_error=excluded.last_error,updated_at=excluded.updated_at''',(record_key,tp,status,1,error,now()));self.cx.commit()

    def add_file(self,rec):
        self.cx.execute('INSERT OR IGNORE INTO files(record_key,sample_uuid,pool_name,patient_id,file_type,path,sha256,size,created_at) VALUES(?,?,?,?,?,?,?,?,?)',
                        (rec.get('record_key'),rec.get('sample_uuid'),rec.get('pool_name'),rec.get('patient_id'),rec.get('file_type'),rec.get('path'),rec.get('sha256'),rec.get('size'),now())); self.cx.commit()

    def add_metric(self,record_key,sample_uuid,step,duration,status):
        self.cx.execute('INSERT INTO metrics(record_key,sample_uuid,step,duration_sec,status,created_at) VALUES(?,?,?,?,?,?)',(record_key,sample_uuid,step,duration,status,now()));self.cx.commit()

    def samples(self):
        return self.cx.execute('''SELECT s.* FROM samples s
            LEFT JOIN pools p ON p.pool_name=s.pool_name AND p.run_uuid=s.run_uuid
            ORDER BY CASE WHEN p.run_date IS NULL OR p.run_date='' THEN 1 ELSE 0 END, p.run_date DESC, s.pool_name, s.case_family, s.patient_id, s.sample_type''').fetchall()

    def samples_for_run(self, run_uuid):
        return self.cx.execute('SELECT * FROM samples WHERE run_uuid=? ORDER BY case_family,patient_id,sample_type',(run_uuid,)).fetchall()

    def _pool_rows_for_group(self, group_name):
        group=self.pool_group_name(group_name)
        if not group:return []
        # Read candidates without SQL `LIKE 'POOL %'`: current GenNext can emit
        # `POOL60 (DNA)` without a space. Python validation below keeps only real
        # numeric POOL labels and then applies the canonical group normalizer.
        rows=[dict(r) for r in self.cx.execute("""SELECT * FROM pools
            ORDER BY COALESCE(source_page,999999) ASC,
                     CASE WHEN run_date IS NULL OR run_date='' THEN 1 ELSE 0 END,
                     run_date DESC,id DESC""").fetchall()]
        return [r for r in rows if self.is_real_pool_name(r.get('pool_name')) and self.pool_group_name(r.get('pool_name'))==group]

    def run_alias(self, run_uuid):
        """User-entered local display name, keyed by exact RUN UUID, not MP/POOL text."""
        try:
            data=json.loads(self.get_meta('run_aliases_v0493','{}') or '{}')
        except (ValueError,TypeError):
            return ''
        return str(data.get(str(run_uuid or ''), '') or '') if isinstance(data,dict) else ''

    def display_pool_name(self, run_uuid, raw_name):
        return self.run_alias(run_uuid) or self.pool_group_name(raw_name)

    def manual_latest_run_uuid(self):
        """Explicit run choice only; invalid/deleted values never select another run."""
        return self.get_meta('manual_latest_run_uuid_v0493','') or ''

    def set_run_alias_and_latest(self, run_uuid, new_name):
        """Atomic, reversible local label+latest selection. Never edits remote pool/MP.

        Aliases never merge technical runs or mutate sample/file/variant keys.
        An explicitly selected run remains one run even if two share a POOL alias.
        """
        run_uuid=str(run_uuid or '').strip()
        row=self.cx.execute('SELECT run_uuid FROM pools WHERE run_uuid=? LIMIT 1',(run_uuid,)).fetchone()
        if not row or not run_uuid:
            raise ValueError('Secilen RUN teknik kaydi bulunamadi. Once RUNS tarayin.')
        name=self.pool_group_name(new_name)
        if not re.fullmatch(r'POOL [1-9][0-9]{0,4}',name):
            raise ValueError('POOL etiketi POOL 69 gibi, yalniz sayisal olmalidir.')
        try:
            aliases=json.loads(self.get_meta('run_aliases_v0493','{}') or '{}')
        except (ValueError,TypeError):
            aliases={}
        if not isinstance(aliases,dict):aliases={}
        aliases[run_uuid]=name
        with self.cx:
            self.cx.execute('INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)',
                            ('run_aliases_v0493',json.dumps(aliases,ensure_ascii=False)))
            self.cx.execute('INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)',
                            ('manual_latest_run_uuid_v0493',run_uuid))
        return name

    def clear_manual_latest_run(self):
        with self.cx:
            self.cx.execute('DELETE FROM meta WHERE key=?',('manual_latest_run_uuid_v0493',))

    def latest_scan_group(self):
        """Runs selected by the most recent browser scan, identified by run UUID.

        The Runs label is manually editable and can be duplicated, so it is unsafe
        as a primary key.  scan_runs stores the contiguous top-run UUID list; this
        method resolves those exact rows.  Old databases fall back to latest_run_uuid.
        """
        manual=self.manual_latest_run_uuid()
        if manual:
            selected=self.cx.execute('SELECT * FROM pools WHERE run_uuid=? ORDER BY id DESC LIMIT 1',
                                     (manual,)).fetchone()
            if selected:return [dict(selected)]
        raw=self.get_meta('latest_run_uuids','') or ''
        try:
            run_uuids=[str(x) for x in json.loads(raw) if str(x)] if raw else []
        except Exception:
            run_uuids=[]
        if not run_uuids:
            one=self.get_meta('latest_run_uuid','')
            if one:run_uuids=[one]
        rows=[]
        for ru in run_uuids:
            r=self.cx.execute('SELECT * FROM pools WHERE run_uuid=? ORDER BY id DESC LIMIT 1',(ru,)).fetchone()
            if r:rows.append(dict(r))
        return rows

    def latest_record_keys(self):
        rows=self.latest_scan_group() or self.latest_pool_group()
        out=[]
        for r in rows:
            out.extend(x['record_key'] for x in self.samples_for_run(r.get('run_uuid') or ''))
        return list(dict.fromkeys(out))

    def latest_pool(self):
        # Exact run UUID from the most recent Runs scan is authoritative. Labels are
        # manually editable and therefore never select the active work item.
        exact=self.latest_scan_group()
        if exact:return exact[0]
        scanned=self.get_meta('latest_pool_name','')
        if scanned:
            rows=self._pool_rows_for_group(scanned)
            if rows:return rows[0]
        # Startup fallback before a Runs scan. POOL numbers are monotonic in the
        # laboratory workflow and are safer than a temporarily missing run_date.
        rows=[dict(r) for r in self.cx.execute("SELECT * FROM pools").fetchall()]
        rows=[r for r in rows if self.is_real_pool_name(r.get('pool_name'))]
        if not rows:return None
        def key(r):
            m=re.search(r'\bPOOL\s*(\d+)\b',str(r.get('pool_name') or ''),re.I)
            n=int(m.group(1)) if m else -1
            return (n,r.get('run_date') or '',r.get('id') or 0)
        return max(rows,key=key)

    def latest_pool_group(self):
        exact=self.latest_scan_group()
        if exact:return exact
        scanned=self.get_meta('latest_pool_name','')
        if scanned:
            rows=self._pool_rows_for_group(scanned)
            if rows:return rows
        latest=self.latest_pool()
        return self._pool_rows_for_group(latest.get('pool_name')) if latest else []

    def set_task_waiting(self,record_key,tp,reason,status='waiting'):
        # Upstream NGS platformu is not ready; this is not a collector failure and does not count as an attempt.
        self.cx.execute('''INSERT INTO tasks(record_key,task_type,status,attempts,last_error,updated_at) VALUES(?,?,?,?,?,?)
        ON CONFLICT(record_key,task_type) DO UPDATE SET status=excluded.status,last_error=excluded.last_error,updated_at=excluded.updated_at''',
        (record_key,tp,status,0,reason,now())); self.cx.commit()

    def sync_task_readiness(self,record_key,types,run_status='',sample_status=''):
        """Sync fatal upstream state without treating PROCESSING as a hard gate.

        Live GenNext can expose usable Clinical/SNV/CNV/QC pages while the sample
        badge still says PROCESSING.  Only explicit FAILED/ERROR/CANCELED states
        block collection.  Old waiting rows created solely from the badge are
        released to pending; page-level DataNotReady remains waiting until retried.
        """
        rs=(run_status or '').strip().upper(); ss=(sample_status or '').strip().upper()
        bad={'ERROR','CANCELED','CANCELLED','FAILED'}
        fatal=''
        if rs in bad:fatal=f'NGS platformu run durumu: {rs}'
        elif ss in bad:fatal=f'NGS platformu sample durumu: {ss}'
        if fatal:
            for tp in types:
                self.cx.execute("UPDATE tasks SET status='blocked',last_error=?,updated_at=? WHERE record_key=? AND task_type=? AND status IN ('pending','waiting','blocked')",(fatal,now(),record_key,tp))
        else:
            self.cx.execute("""UPDATE tasks SET status='pending',last_error=NULL,updated_at=?
              WHERE record_key=? AND status IN ('waiting','blocked')
                AND (last_error LIKE 'NGS platformu run durumu:%' OR last_error LIKE 'NGS platformu sample durumu:%')""",(now(),record_key))
        self.cx.commit()

    def files_for_record(self, record_key, file_type=None):
        if file_type:
            return self.cx.execute('SELECT * FROM files WHERE record_key=? AND file_type=? ORDER BY id DESC',(record_key,file_type)).fetchall()
        return self.cx.execute('SELECT * FROM files WHERE record_key=? ORDER BY id DESC',(record_key,)).fetchall()

    def unfinished_tasks(self, record_keys=None):
        sql="SELECT * FROM tasks WHERE status NOT IN ('done','not_available')"; args=[]
        if record_keys:
            qs=','.join('?' for _ in record_keys); sql += f' AND record_key IN ({qs})'; args.extend(record_keys)
        return self.cx.execute(sql,args).fetchall()

    def set_task_review(self,record_key,tp,reason):
        """Mark a collected-but-incomplete source for human review.

        REVIEW is deliberately different from ERROR: a fallback representation may
        exist locally, but the preferred source export was not captured. It stays in
        unfinished_tasks so one-click retry/manual completion can repair it.
        """
        self.cx.execute("""INSERT INTO tasks(record_key,task_type,status,attempts,last_error,updated_at) VALUES(?,?,?,?,?,?)
          ON CONFLICT(record_key,task_type) DO UPDATE SET status='review',last_error=excluded.last_error,updated_at=excluded.updated_at""",
          (record_key,tp,'review',0,reason,now()));self.cx.commit()

    def audit_fusion_fallbacks(self,base):
        """Downgrade old Fusion DONE flags when the newest local source is incomplete.

        v0.4.5 also catches structured files that contain Loading placeholders,
        React ``[object Object]`` cells, or candidate-only rows without read/breakpoint
        evidence. A later complete source/manual review remains DONE.
        """
        root=Path(base);changed=0
        rows=self.cx.execute("SELECT record_key FROM tasks WHERE task_type='fusion' AND status='done'").fetchall()
        for row in rows:
            files=self.files_for_record(row['record_key'],'fusion')
            path=None
            for f in files:
                p=Path(f['path']);p=p if p.is_absolute() else root/p
                if p.is_file():path=p;break
            if path is None:
                reason='Fusion görevi TAMAM görünüyordu ancak yerel Fusion kaynak dosyası bulunamadı.'
            else:
                ok,why,info=validate_fusion_source(path)
                if ok:continue
                reason='Fusion görevi TAMAM görünüyordu ancak kaynak eksik/inceleme gerektiriyor: '+why
            self.cx.execute("UPDATE tasks SET status='review',last_error=?,updated_at=? WHERE record_key=? AND task_type='fusion'",(reason,now(),row['record_key']))
            changed+=1
        if changed:self.cx.commit()
        return changed

    def audit_qc_sources(self,base):
        """Downgrade stale QC=done flags when no verifiable local QC source exists.

        Older releases could preserve a DONE task even after an interrupted/missing
        file. The audit changes only the task visibility; it never fabricates QC.
        """
        root=Path(base);changed=0
        rows=self.cx.execute("""SELECT t.record_key,s.patient_id FROM tasks t LEFT JOIN samples s ON s.record_key=t.record_key
          WHERE t.task_type='qc' AND t.status='done'""").fetchall()
        for row in rows:
            path=None
            for f in self.files_for_record(row['record_key'],'qc'):
                p=Path(f['path']);p=p if p.is_absolute() else root/p
                if p.is_file():path=p;break
            if path is None:
                reason='QC görevi TAMAM görünüyordu ancak yerel QC kaynak dosyası bulunamadı; yeniden indirme veya manuel kaynak ekleme gerekli.'
            else:
                ok,why,_info=validate_qc_source(path,row['patient_id'] or '')
                if ok:continue
                reason='QC görevi TAMAM görünüyordu ancak yerel kaynak doğrulanamadı: '+why
            self.cx.execute("UPDATE tasks SET status='review',last_error=?,updated_at=? WHERE record_key=? AND task_type='qc'",(reason,now(),row['record_key']))
            changed+=1
        if changed:self.cx.commit()
        return changed

    def overall_status(self, record_key):
        rows=self.cx.execute('SELECT status FROM tasks WHERE record_key=?',(record_key,)).fetchall()
        if not rows:return '—'
        sts=[r['status'] for r in rows]
        if all(x in ('done','not_available') for x in sts): return '✓'
        if any(x in ('error','blocked') for x in sts): return 'HATA'
        if any(x=='review' for x in sts): return 'KONTROL'
        if any(x=='waiting' for x in sts): return 'BEKLİYOR'
        return '○'

    def set_variant_decision(self,record_key,variant_key,status=None,note=None):
        if not status:
            self.cx.execute('DELETE FROM variant_decisions WHERE record_key=? AND variant_key=?',(record_key,variant_key))
        else:
            self.cx.execute('''INSERT INTO variant_decisions(record_key,variant_key,status,note,updated_at) VALUES(?,?,?,?,?)
                ON CONFLICT(record_key,variant_key) DO UPDATE SET status=excluded.status,note=excluded.note,updated_at=excluded.updated_at''',
                (record_key,variant_key,status,note,now()))
        self.cx.commit()

    def variant_decisions(self,record_key):
        return {r['variant_key']:r['status'] for r in self.cx.execute('SELECT variant_key,status FROM variant_decisions WHERE record_key=?',(record_key,)).fetchall()}

    def tasks(self):
        return self.cx.execute('SELECT * FROM tasks').fetchall()

    def get_sample(self,record_key):
        r=self.cx.execute('SELECT * FROM samples WHERE record_key=?',(record_key,)).fetchone()
        if not r:return None
        d=dict(r);d['links']=json.loads(d.pop('links_json') or '{}');return d

    def task_status(self,record_key,tp):
        r=self.cx.execute('SELECT status FROM tasks WHERE record_key=? AND task_type=?',(record_key,tp)).fetchone();return r['status'] if r else None

    def ensure_ai_review_schema(self):
        self.cx.executescript("""
        CREATE TABLE IF NOT EXISTS ai_review_sessions(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          imported_at TEXT NOT NULL, schema_name TEXT, pool_group TEXT, sample_scope TEXT,
          source_path TEXT, source_sha256 TEXT, pool_risk TEXT, overall_summary TEXT, raw_json TEXT
        );
        CREATE TABLE IF NOT EXISTS ai_patient_reviews(
          id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER NOT NULL,
          record_key TEXT, patient_link_id TEXT, patient_id TEXT, sample_type TEXT,
          release_recommendation TEXT, summary TEXT, notes_json TEXT, imported_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_ai_patient_session ON ai_patient_reviews(session_id);
        CREATE TABLE IF NOT EXISTS ai_variant_reviews(
          id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER NOT NULL,
          record_key TEXT, patient_link_id TEXT, patient_id TEXT, variant_key TEXT, gene TEXT, change TEXT,
          review_status TEXT, confidence TEXT, report_recommendation TEXT, rationale TEXT,
          igv_required INTEGER DEFAULT 0, igv_reason TEXT, igv_priority TEXT, raw_json TEXT, imported_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_ai_variant_session ON ai_variant_reviews(session_id);
        CREATE INDEX IF NOT EXISTS idx_ai_variant_key ON ai_variant_reviews(record_key,variant_key);
        CREATE TABLE IF NOT EXISTS ai_artifact_reviews(
          id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER NOT NULL,
          variant_key TEXT, gene TEXT, change TEXT, status TEXT, confidence TEXT, reason TEXT,
          affected_patient_ids_json TEXT, raw_json TEXT, imported_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_ai_artifact_session ON ai_artifact_reviews(session_id);
        CREATE INDEX IF NOT EXISTS idx_ai_artifact_key ON ai_artifact_reviews(variant_key);
        CREATE TABLE IF NOT EXISTS igv_queue(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          record_key TEXT NOT NULL, patient_link_id TEXT, patient_id TEXT, variant_key TEXT NOT NULL,
          gene TEXT, change TEXT, region TEXT, priority TEXT, reason TEXT, source_session_id INTEGER,
          status TEXT NOT NULL DEFAULT 'pending', screenshot_path TEXT, manual_decision TEXT, manual_note TEXT,
          created_at TEXT, updated_at TEXT, UNIQUE(record_key,variant_key)
        );
        CREATE INDEX IF NOT EXISTS idx_igv_queue_status ON igv_queue(status,priority);
        """)
        cols=self._columns('igv_queue')
        for col, ddl in [
            ('capture_meta_path', "TEXT DEFAULT ''"),
            ('auto_settings_json', "TEXT DEFAULT ''"),
            ('ai_ready', "INTEGER DEFAULT 0"),
            ('capture_error', "TEXT DEFAULT ''"),
            ('captured_at', "TEXT DEFAULT ''"),
        ]:
            if col not in cols:
                self.cx.execute(f'ALTER TABLE igv_queue ADD COLUMN {col} {ddl}')
        self.cx.commit()

    def import_ai_review(self, data, source_path='', source_sha256=''):
        """Persist a validated AI review return without overwriting manual report decisions."""
        self.ensure_ai_review_schema()
        src=data.get('source_bundle') or {}
        pool=data.get('pool_review') or {}
        raw=json.dumps(data,ensure_ascii=False)
        cur=self.cx.execute("""INSERT INTO ai_review_sessions(imported_at,schema_name,pool_group,sample_scope,source_path,source_sha256,pool_risk,overall_summary,raw_json)
          VALUES(?,?,?,?,?,?,?,?,?)""",(now(),data.get('schema') or '',src.get('pool_group') or '',src.get('sample_scope') or '',source_path or '',source_sha256 or '',pool.get('risk_level') or '',pool.get('summary') or '',raw))
        session_id=int(cur.lastrowid)
        warnings=[]
        patient_count=variant_count=queue_count=0
        for pr in data.get('patient_reviews') or []:
            rk=str(pr.get('record_key') or '').strip()
            if rk and not self.get_sample(rk): warnings.append(f"Bilinmeyen record_key (hasta): {rk}")
            self.cx.execute("""INSERT INTO ai_patient_reviews(session_id,record_key,patient_link_id,patient_id,sample_type,release_recommendation,summary,notes_json,imported_at)
              VALUES(?,?,?,?,?,?,?,?,?)""",(session_id,rk,pr.get('patient_link_id') or '',pr.get('patient_id') or '',pr.get('sample_type') or '',pr.get('release_recommendation') or '',pr.get('summary') or '',json.dumps(pr.get('notes') or [],ensure_ascii=False),now()))
            patient_count+=1
        review_by_key={}
        for vr in data.get('variant_reviews') or []:
            rk=str(vr.get('record_key') or '').strip(); vk=str(vr.get('variant_key') or '').strip()
            if not rk or not vk:
                warnings.append('record_key/variant_key eksik varyant inceleme satırı atlandı.')
                continue
            if not self.get_sample(rk): warnings.append(f"Bilinmeyen record_key (varyant): {rk}")
            self.cx.execute("""INSERT INTO ai_variant_reviews(session_id,record_key,patient_link_id,patient_id,variant_key,gene,change,review_status,confidence,report_recommendation,rationale,igv_required,igv_reason,igv_priority,raw_json,imported_at)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(session_id,rk,vr.get('patient_link_id') or '',vr.get('patient_id') or '',vk,vr.get('gene') or '',vr.get('change') or '',vr.get('review_status') or '',vr.get('confidence') or '',vr.get('report_recommendation') or '',vr.get('rationale') or '',1 if vr.get('igv_required') else 0,vr.get('igv_reason') or '',vr.get('igv_priority') or '',json.dumps(vr,ensure_ascii=False),now()))
            variant_count+=1; review_by_key[(rk,vk)]=vr
        artifact_count=0
        for ar in data.get('artifact_reviews') or []:
            vk=str(ar.get('variant_key') or '').strip()
            if not vk: continue
            self.cx.execute("""INSERT INTO ai_artifact_reviews(session_id,variant_key,gene,change,status,confidence,reason,affected_patient_ids_json,raw_json,imported_at)
              VALUES(?,?,?,?,?,?,?,?,?,?)""",(session_id,vk,ar.get('gene') or '',ar.get('change') or '',ar.get('status') or '',ar.get('confidence') or '',ar.get('reason') or '',json.dumps(ar.get('affected_patient_ids') or [],ensure_ascii=False),json.dumps(ar,ensure_ascii=False),now()))
            artifact_count+=1
        requests=[]; seen=set()
        for req in data.get('igv_requests') or []:
            rk=str(req.get('record_key') or '').strip();vk=str(req.get('variant_key') or '').strip()
            if rk and vk and (rk,vk) not in seen:
                requests.append(dict(req));seen.add((rk,vk))
        for (rk,vk),vr in review_by_key.items():
            if vr.get('igv_required') and (rk,vk) not in seen:
                requests.append({
                    'record_key':rk,'patient_link_id':vr.get('patient_link_id'),'patient_id':vr.get('patient_id'),
                    'variant_key':vk,'gene':vr.get('gene'),'change':vr.get('change'),'region':vr.get('igv_region') or '',
                    'priority':vr.get('igv_priority') or 'normal','reason':vr.get('igv_reason') or vr.get('rationale') or 'YZ IGV incelemesi önerdi.'
                });seen.add((rk,vk))
        for req in requests:
            rk=str(req.get('record_key') or '').strip();vk=str(req.get('variant_key') or '').strip()
            if not rk or not vk: continue
            if not self.get_sample(rk):
                warnings.append(f"IGV isteği eşleşmedi: {rk}"); continue
            self.cx.execute("""INSERT INTO igv_queue(record_key,patient_link_id,patient_id,variant_key,gene,change,region,priority,reason,source_session_id,status,created_at,updated_at)
              VALUES(?,?,?,?,?,?,?,?,?,?,'pending',?,?)
              ON CONFLICT(record_key,variant_key) DO UPDATE SET
                patient_link_id=excluded.patient_link_id,patient_id=excluded.patient_id,gene=excluded.gene,change=excluded.change,
                region=CASE WHEN TRIM(COALESCE(excluded.region,''))<>'' THEN excluded.region ELSE igv_queue.region END,
                priority=excluded.priority,reason=excluded.reason,source_session_id=excluded.source_session_id,updated_at=excluded.updated_at""",
              (rk,req.get('patient_link_id') or '',req.get('patient_id') or '',vk,req.get('gene') or '',req.get('change') or '',req.get('region') or '',req.get('priority') or 'normal',req.get('reason') or '',session_id,now(),now()))
            queue_count+=1
        self.cx.commit()
        return {'session_id':session_id,'patient_reviews':patient_count,'variant_reviews':variant_count,'artifact_reviews':artifact_count,'igv_requests':queue_count,'warnings':warnings}

    def ai_review_sessions(self, limit=50):
        self.ensure_ai_review_schema()
        return self.cx.execute('SELECT * FROM ai_review_sessions ORDER BY id DESC LIMIT ?',(int(limit),)).fetchall()

    def igv_queue_rows(self, status_filter=None):
        self.ensure_ai_review_schema()
        sql="""SELECT q.*,s.pool_name,s.sample_type,s.sample_uuid,s.run_uuid,cm.full_name
          FROM igv_queue q LEFT JOIN samples s ON s.record_key=q.record_key
          LEFT JOIN case_metadata cm ON cm.case_family=COALESCE(NULLIF(s.case_family,''),s.patient_id) WHERE 1=1"""
        args=[]
        if status_filter and status_filter!='Tümü': sql+=' AND q.status=?';args.append(status_filter)
        sql+=" ORDER BY CASE q.status WHEN 'pending' THEN 0 WHEN 'opened' THEN 1 WHEN 'captured' THEN 2 ELSE 3 END, CASE LOWER(COALESCE(q.priority,'')) WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END, q.id DESC"
        return self.cx.execute(sql,args).fetchall()

    def get_igv_queue_item(self, item_id):
        self.ensure_ai_review_schema()
        r=self.cx.execute('SELECT * FROM igv_queue WHERE id=?',(int(item_id),)).fetchone();return dict(r) if r else None

    def update_igv_queue_item(self, item_id, status=None, screenshot_path=None, manual_decision=None, manual_note=None,
                              capture_meta_path=None, auto_settings_json=None, ai_ready=None, capture_error=None, captured_at=None):
        self.ensure_ai_review_schema()
        fields=[];args=[]
        for col,val in [('status',status),('screenshot_path',screenshot_path),('manual_decision',manual_decision),('manual_note',manual_note),
                        ('capture_meta_path',capture_meta_path),('auto_settings_json',auto_settings_json),('ai_ready',ai_ready),
                        ('capture_error',capture_error),('captured_at',captured_at)]:
            if val is not None: fields.append(f'{col}=?');args.append(val)
        if not fields:return
        fields.append('updated_at=?');args.append(now());args.append(int(item_id))
        self.cx.execute('UPDATE igv_queue SET '+','.join(fields)+' WHERE id=?',args);self.cx.commit()

    def igv_queue_ai_context(self, item_id):
        """Return latest AI review context for one IGV row without changing manual decisions."""
        self.ensure_ai_review_schema()
        item=self.get_igv_queue_item(item_id)
        if not item:return {}
        row=self.cx.execute("""SELECT * FROM ai_variant_reviews
          WHERE record_key=? AND variant_key=? ORDER BY id DESC LIMIT 1""",(item['record_key'],item['variant_key'])).fetchone()
        return dict(row) if row else {}


    def ensure_reporting_schema(self):
        self.cx.executescript("""
        CREATE TABLE IF NOT EXISTS reported_variant_history(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          record_key TEXT, case_family TEXT, patient_id TEXT, pool_name TEXT, sample_type TEXT,
          variant_key TEXT, variant_type TEXT, gene TEXT, transcript TEXT, cdna TEXT, protein TEXT,
          source TEXT, source_text TEXT, report_hash TEXT, match_score INTEGER, confirmed INTEGER DEFAULT 1,
          created_at TEXT,
          UNIQUE(record_key,variant_key,report_hash,source)
        );
        CREATE INDEX IF NOT EXISTS idx_reported_history_record ON reported_variant_history(record_key,created_at);
        CREATE INDEX IF NOT EXISTS idx_reported_history_case ON reported_variant_history(case_family,created_at);
        CREATE INDEX IF NOT EXISTS idx_reported_history_variant ON reported_variant_history(variant_key);
        """)
        cols=self._columns('reported_variant_history')
        if 'feature' not in cols:
            self.cx.execute("ALTER TABLE reported_variant_history ADD COLUMN feature TEXT DEFAULT ''")
        self.cx.commit()

    def add_reported_variant(self, record_key, variant_key, variant_type='SNV', gene='', transcript='', cdna='', protein='',
                             source='manual', source_text='', report_hash='', match_score=0, confirmed=True, feature=''):
        self.ensure_reporting_schema()
        s=self.get_sample(record_key) or {}
        case_family=s.get('case_family') or s.get('patient_id') or ''
        patient_id=s.get('patient_id') or ''
        pool_name=s.get('pool_name') or ''
        sample_type=s.get('sample_type') or ''
        cur=self.cx.execute("""INSERT OR IGNORE INTO reported_variant_history(
          record_key,case_family,patient_id,pool_name,sample_type,variant_key,variant_type,gene,transcript,cdna,protein,feature,
          source,source_text,report_hash,match_score,confirmed,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
          (record_key,case_family,patient_id,pool_name,sample_type,variant_key or '',variant_type or '',gene or '',transcript or '',cdna or '',protein or '',feature or '',
           source or 'manual',source_text or '',report_hash or '',int(match_score or 0),1 if confirmed else 0,now()))
        self.cx.commit()
        return int(cur.lastrowid or 0)

    def reported_variant_history(self, record_key=None, case_family=None, limit=5000):
        self.ensure_reporting_schema()
        sql="""SELECT h.*,cm.full_name,cm.birth_date,cm.sex FROM reported_variant_history h
          LEFT JOIN case_metadata cm ON cm.case_family=h.case_family WHERE 1=1"""
        args=[]
        if record_key:
            sql+=' AND h.record_key=?';args.append(record_key)
        if case_family:
            sql+=' AND h.case_family=?';args.append(case_family)
        sql+=' ORDER BY h.id DESC LIMIT ?';args.append(int(limit))
        return self.cx.execute(sql,args).fetchall()

    def remove_reported_variant_history(self, row_id):
        self.ensure_reporting_schema()
        r=self.cx.execute('SELECT record_key,variant_key FROM reported_variant_history WHERE id=?',(int(row_id),)).fetchone()
        self.cx.execute('DELETE FROM reported_variant_history WHERE id=?',(int(row_id),))
        self.cx.commit()
        return dict(r) if r else None

    @staticmethod
    def normalize_decision_variant_key(variant_key):
        v=str(variant_key or '').strip()
        return v[4:] if v.upper().startswith('SNV|') else v

    def artifact_variant_summary(self):
        """Known local artifact decisions across the archive. No autonomous artifact calling."""
        rows=[]
        for r in self.cx.execute("""SELECT vd.record_key,vd.variant_key,vd.status,vd.note,s.pool_name,s.patient_id,s.case_family
          FROM variant_decisions vd LEFT JOIN samples s ON s.record_key=vd.record_key
          WHERE LOWER(COALESCE(vd.status,''))='artifact'""").fetchall():
            d=dict(r);d['gene']='';d['change']='';rows.append(d)
        if self._table_exists('igv_queue'):
            for r in self.cx.execute("""SELECT q.record_key,q.variant_key,'artifact' status,q.manual_note note,q.gene,q.change,s.pool_name,s.patient_id,s.case_family
              FROM igv_queue q LEFT JOIN samples s ON s.record_key=q.record_key
              WHERE LOWER(COALESCE(q.manual_decision,''))='artifact'""").fetchall():
                rows.append(dict(r))
        grouped={}
        for r in rows:
            key=self.normalize_decision_variant_key(r.get('variant_key'))
            if not key: continue
            g=grouped.setdefault(key,{'variant_key':key,'gene':'','change':'','sample_count':0,'pool_count':0,'patients':set(),'pools':set(),'record_keys':set(),'notes':[]})
            rk=r.get('record_key') or ''
            if rk and rk not in g['record_keys']:
                g['record_keys'].add(rk);g['sample_count']+=1
            if r.get('pool_name'):g['pools'].add(r.get('pool_name'))
            if r.get('patient_id'):g['patients'].add(r.get('patient_id'))
            if r.get('gene') and not g['gene']:g['gene']=r.get('gene')
            if r.get('change') and not g['change']:g['change']=r.get('change')
            if r.get('note') and r.get('note') not in g['notes']:g['notes'].append(r.get('note'))
        out=[]
        for g in grouped.values():
            g['pool_count']=len(g['pools']);g['pools']=sorted(g['pools']);g['patients']=sorted(g['patients']);g.pop('record_keys',None);out.append(g)
        out.sort(key=lambda x:(x['sample_count'],x['pool_count']),reverse=True)
        return out

    def ensure_analysis_schema(self):
        self.cx.executescript('''
        CREATE TABLE IF NOT EXISTS oncogenic_observations(
          id INTEGER PRIMARY KEY, record_key TEXT NOT NULL, pool_name TEXT, sample_uuid TEXT, patient_id TEXT, case_family TEXT, sample_type TEXT, is_rerun INTEGER DEFAULT 0,
          variant_key TEXT NOT NULL, chrom TEXT, start TEXT, ref TEXT, alt TEXT, gene TEXT, change TEXT, oncogenic TEXT, filter TEXT, af REAL, ad TEXT, dp TEXT, highest_level TEXT, source_sha TEXT, source_path TEXT, indexed_at TEXT,
          UNIQUE(record_key,variant_key)
        );
        CREATE INDEX IF NOT EXISTS idx_onc_pool ON oncogenic_observations(pool_name);
        CREATE INDEX IF NOT EXISTS idx_onc_variant ON oncogenic_observations(variant_key);
        CREATE TABLE IF NOT EXISTS analysis_indexed_files(
          record_key TEXT NOT NULL, index_type TEXT NOT NULL, source_sha TEXT, row_count INTEGER, indexed_at TEXT, PRIMARY KEY(record_key,index_type)
        );
        ''')
        self.cx.commit()

    def get_indexed_file(self,record_key,index_type):
        self.ensure_analysis_schema()
        return self.cx.execute('SELECT * FROM analysis_indexed_files WHERE record_key=? AND index_type=?',(record_key,index_type)).fetchone()

    def replace_oncogenic_observations(self,record_key,obs,source_sha,row_count):
        self.ensure_analysis_schema()
        self.cx.execute('BEGIN')
        try:
            self.cx.execute('DELETE FROM oncogenic_observations WHERE record_key=?',(record_key,))
            for o in obs:
                self.cx.execute('''INSERT OR REPLACE INTO oncogenic_observations(record_key,pool_name,sample_uuid,patient_id,case_family,sample_type,is_rerun,variant_key,chrom,start,ref,alt,gene,change,oncogenic,filter,af,ad,dp,highest_level,source_sha,source_path,indexed_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',(o.get('record_key'),o.get('pool_name'),o.get('sample_uuid'),o.get('patient_id'),o.get('case_family'),o.get('sample_type'),o.get('is_rerun') or 0,o.get('variant_key'),o.get('chrom'),o.get('start'),o.get('ref'),o.get('alt'),o.get('gene'),o.get('change'),o.get('oncogenic'),o.get('filter'),o.get('af'),o.get('ad'),o.get('dp'),o.get('highest_level'),o.get('source_sha'),o.get('source_path'),now()))
            self.cx.execute('''INSERT OR REPLACE INTO analysis_indexed_files(record_key,index_type,source_sha,row_count,indexed_at) VALUES(?,?,?,?,?)''',(record_key,'snv_oncogenic',source_sha,row_count,now()))
            self.cx.commit()
        except Exception:
            self.cx.rollback();raise

    def oncogenic_observations(self,pool_name=None,pass_only=False):
        self.ensure_analysis_schema()
        sql='SELECT * FROM oncogenic_observations WHERE 1=1';args=[]
        if pool_name:
            sql+=' AND pool_name=?';args.append(pool_name)
        if pass_only:
            sql+=" AND UPPER(COALESCE(filter,''))='PASS'"
        return self.cx.execute(sql,args).fetchall()

    def oncogenic_observations_for_run(self,run_uuid,pass_only=False):
        """POOL spread uses the immutable RUN UUID, not a mutable/duplicated label.

        The join also excludes orphaned historical index rows after a local RUN
        is removed. No changes are made to existing index or patient records.
        """
        self.ensure_analysis_schema()
        if not run_uuid:
            return []
        sql=('SELECT o.* FROM oncogenic_observations o '
             'INNER JOIN samples s ON s.record_key=o.record_key '
             'WHERE s.run_uuid=?')
        if pass_only:
            sql+=" AND UPPER(COALESCE(o.filter,''))='PASS'"
        return self.cx.execute(sql,(str(run_uuid),)).fetchall()

    def global_variant_counts(self,variant_keys):
        self.ensure_analysis_schema()
        if not variant_keys:return {}
        out={}
        for i in range(0,len(variant_keys),500):
            chunk=variant_keys[i:i+500];qs=','.join('?' for _ in chunk)
            sql='SELECT variant_key,COUNT(DISTINCT record_key) samples,COUNT(DISTINCT pool_name) pools FROM oncogenic_observations WHERE variant_key IN ('+qs+') GROUP BY variant_key'
            for r in self.cx.execute(sql,chunk).fetchall():
                out[r['variant_key']]={'samples':r['samples'],'pools':r['pools']}
        return out

    def sample_depth(self,record_key):
        r=self.cx.execute('SELECT depth FROM samples WHERE record_key=?',(record_key,)).fetchone()
        return r['depth'] if r else None

    def get_meta(self,key,default=None):
        r=self.cx.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()
        return r['value'] if r else default

    def set_meta(self,key,value):
        self.cx.execute('INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)',(key,str(value))); self.cx.commit()

    def reset_task_type(self,tp,reason='parser güncellemesi'):
        self.cx.execute("UPDATE tasks SET status='pending', last_error=?, updated_at=? WHERE task_type=?",(reason,now(),tp)); self.cx.commit()


    def ensure_metadata_schema(self):
        # Getters call this for compatibility. Once startup migration succeeds,
        # reading metadata must not rerun full-table updates, obtain a write
        # lock, or implicitly commit an unrelated caller transaction.
        if getattr(self, '_metadata_schema_ready_v0482', False):
            return
        self.cx.executescript("""
        CREATE TABLE IF NOT EXISTS case_metadata(
          case_family TEXT PRIMARY KEY,
          full_name TEXT,
          national_id TEXT,
          birth_date TEXT,
          age TEXT,
          diagnosis TEXT,
          sex TEXT,
          requesting_physician TEXT,
          reporting_physician TEXT,
          enlil_biopsy_no TEXT,
          enlil_request_no TEXT,
          enlil_file_no TEXT,
          enlil_requesting_physician TEXT,
          enlil_request_date TEXT,
          enlil_first_accept_date TEXT,
          enlil_last_lookup_at TEXT,
          note TEXT,
          updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS sample_metadata(
          record_key TEXT PRIMARY KEY,
          block_id TEXT,
          block_section TEXT,
          block_source TEXT,
          tumor_percentage TEXT,
          note TEXT,
          updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS patient_profiles(
          national_id TEXT PRIMARY KEY,
          full_name TEXT,
          birth_date TEXT,
          sex TEXT,
          updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS qc_protocol_links(
          protocol_key TEXT NOT NULL,
          sample_type TEXT NOT NULL DEFAULT '',
          record_key TEXT,
          case_family TEXT,
          source TEXT,
          updated_at TEXT,
          PRIMARY KEY(protocol_key,sample_type)
        );
        CREATE INDEX IF NOT EXISTS idx_qc_protocol_links_record ON qc_protocol_links(record_key);
        """)
        # v0.2.3: existing databases gain doctor fields without losing data.
        cols=self._columns('case_metadata')
        if 'requesting_physician' not in cols:
            self.cx.execute('ALTER TABLE case_metadata ADD COLUMN requesting_physician TEXT')
        if 'reporting_physician' not in cols:
            self.cx.execute('ALTER TABLE case_metadata ADD COLUMN reporting_physician TEXT')
        if 'birth_date' not in cols:
            self.cx.execute('ALTER TABLE case_metadata ADD COLUMN birth_date TEXT')
        for col in ('enlil_biopsy_no','enlil_request_no','enlil_file_no','enlil_requesting_physician','enlil_request_date','enlil_first_accept_date','enlil_last_lookup_at'):
            if col not in cols:
                self.cx.execute(f'ALTER TABLE case_metadata ADD COLUMN {col} TEXT')
        sm_cols=self._columns('sample_metadata')
        if 'block_source' not in sm_cols:
            self.cx.execute('ALTER TABLE sample_metadata ADD COLUMN block_source TEXT')
        # Legacy builds sometimes encoded institution/external source in block_section.
        # Seed the dedicated source once, but keep block_section untouched.
        self.cx.execute("UPDATE sample_metadata SET block_source='Dış' WHERE TRIM(COALESCE(block_source,''))='' AND (LOWER(COALESCE(block_section,'')) LIKE '%dış%' OR LOWER(COALESCE(block_section,'')) LIKE '%dis%' OR LOWER(COALESCE(block_section,'')) LIKE '%external%')")
        self.cx.execute("UPDATE sample_metadata SET block_source='SDÜ Tıp Fakültesi' WHERE TRIM(COALESCE(block_source,''))='' AND (LOWER(COALESCE(block_section,'')) LIKE '%sdu%' OR LOWER(COALESCE(block_section,'')) LIKE '%sdü%')")
        # v0.2.8: normalize legacy single-letter sex values entered manually.
        self.cx.execute("UPDATE case_metadata SET sex='Erkek' WHERE UPPER(TRIM(COALESCE(sex,''))) IN ('E','ERKEK','M','MALE')")
        self.cx.execute("UPDATE case_metadata SET sex='Kadın' WHERE UPPER(TRIM(COALESCE(sex,''))) IN ('K','KADIN','F','FEMALE')")
        # v0.4.1: patient demographics are patient-level, while diagnosis/notes remain
        # case/tumor-level. Seed one canonical local profile per checksum-valid TC.
        seeded=self.cx.execute("SELECT value FROM meta WHERE key='patient_profile_seed_v041'").fetchone()
        if not seeded:
            rows=self.cx.execute("SELECT * FROM case_metadata ORDER BY COALESCE(updated_at,'') DESC, case_family").fetchall()
            for r in rows:
                d=dict(r); nid=''.join(ch for ch in str(d.get('national_id') or '') if ch.isdigit())
                if not self.validate_national_id(nid): continue
                self.cx.execute("""INSERT OR IGNORE INTO patient_profiles(national_id,full_name,birth_date,sex,updated_at) VALUES(?,?,?,?,?)""",
                    (nid,d.get('full_name') or '',d.get('birth_date') or '',self.normalize_sex(d.get('sex')),d.get('updated_at') or now()))
            self.cx.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('patient_profile_seed_v041','1')")
        self.cx.commit()
        self._metadata_schema_ready_v0482 = True

    @staticmethod
    def validate_national_id(value):
        # Turkish Republic identity number checksum; used only for local consistency/linking.
        v=''.join(ch for ch in str(value or '') if ch.isdigit())
        if len(v)!=11 or v[0]=='0': return False
        d=[int(x) for x in v]
        tenth=((sum(d[0:9:2])*7)-sum(d[1:8:2]))%10
        eleventh=sum(d[:10])%10
        return d[9]==tenth and d[10]==eleventh

    @staticmethod
    def normalize_sex(value):
        v=str(value or '').strip().casefold()
        if v in ('e','erkek','m','male'): return 'Erkek'
        if v in ('k','kadın','kadin','f','female'): return 'Kadın'
        return str(value or '').strip()

    @staticmethod
    def age_from_birth_date(birth_date, reference_date):
        import datetime as _dt
        def parse_date(value):
            if isinstance(value,_dt.datetime):return value.date()
            if isinstance(value,_dt.date):return value
            text=str(value or '').strip()
            if not text:return None
            # ISO timestamps and common ENLIL date/date-time forms.
            try:return _dt.datetime.fromisoformat(text.replace('Z','+00:00')).date()
            except Exception:pass
            first=text.split()[0]
            for fmt in ('%d.%m.%Y','%d/%m/%Y','%Y-%m-%d','%d-%m-%Y'):
                try:return _dt.datetime.strptime(first,fmt).date()
                except Exception:pass
            return None
        b=parse_date(birth_date);r=parse_date(reference_date)
        if not b or not r or b>r:return None
        age=r.year-b.year-((r.month,r.day)<(b.month,b.day))
        return age if 0<=age<=120 else None

    def case_reference_date(self,case_family):
        r=self.cx.execute("""SELECT MIN(p.run_date) AS run_date
          FROM samples s LEFT JOIN pools p ON p.pool_name=s.pool_name AND p.run_uuid=s.run_uuid
          WHERE COALESCE(NULLIF(s.case_family,''),s.patient_id)=? AND TRIM(COALESCE(p.run_date,''))<>''
        """,(case_family or '',)).fetchone()
        return (r['run_date'] if r and r['run_date'] else '')

    def case_age_reference_date(self,case_family):
        """Best clinical reference date for age-at-diagnosis/reporting context.

        ENLIL pathology request date is preferred because it is closer to the specimen/
        diagnostic episode.  First NGS run date is the safe fallback for older records.
        """
        self.ensure_metadata_schema()
        r=self.cx.execute('SELECT enlil_request_date,enlil_first_accept_date FROM case_metadata WHERE case_family=?',(case_family or '',)).fetchone()
        if r:
            for key in ('enlil_request_date','enlil_first_accept_date'):
                value=str(r[key] or '').strip()
                if value:return value
        return self.case_reference_date(case_family)

    def get_case_metadata(self,case_family):
        self.ensure_metadata_schema()
        r=self.cx.execute('SELECT * FROM case_metadata WHERE case_family=?',(case_family or '',)).fetchone()
        if r:
            d=dict(r); d['sex']=self.normalize_sex(d.get('sex')); return d
        return {'case_family':case_family or '','full_name':'','national_id':'','birth_date':'','age':'','diagnosis':'','sex':'','requesting_physician':'','reporting_physician':'','enlil_biopsy_no':'','enlil_request_no':'','enlil_file_no':'','enlil_requesting_physician':'','enlil_request_date':'','enlil_first_accept_date':'','enlil_last_lookup_at':'','note':'','updated_at':''}

    def set_case_metadata(self,case_family,full_name='',national_id='',age='',diagnosis='',sex='',note='',requesting_physician='',reporting_physician='',birth_date=''):
        self.ensure_metadata_schema()
        sex=self.normalize_sex(sex)
        self.cx.execute("""INSERT INTO case_metadata(case_family,full_name,national_id,birth_date,age,diagnosis,sex,requesting_physician,reporting_physician,note,updated_at)
          VALUES(?,?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(case_family) DO UPDATE SET full_name=excluded.full_name,national_id=excluded.national_id,birth_date=excluded.birth_date,age=excluded.age,diagnosis=excluded.diagnosis,sex=excluded.sex,requesting_physician=excluded.requesting_physician,reporting_physician=excluded.reporting_physician,note=excluded.note,updated_at=excluded.updated_at""",
          (case_family or '',full_name,national_id,birth_date,age,diagnosis,sex,requesting_physician,reporting_physician,note,now()))
        self.cx.commit()

    def set_case_reporting_physician(self,case_family,reporting_physician=''):
        """Update only the case reporting physician.

        v0.4.38: reporting physician is owned by QC / Enlil > right-bottom
        signature owner.  This narrow update prevents that workflow from
        overwriting demographics, diagnosis, requesting physician or case note.
        """
        self.ensure_metadata_schema()
        case_family=str(case_family or '').strip()
        if not case_family:return False
        value=str(reporting_physician or '').strip()
        current=self.get_case_metadata(case_family)
        if str(current.get('reporting_physician') or '').strip()==value:return False
        cur=self.cx.execute('UPDATE case_metadata SET reporting_physician=?, updated_at=? WHERE case_family=?',(value,now(),case_family))
        if cur.rowcount==0:
            self.cx.execute('INSERT INTO case_metadata(case_family,reporting_physician,updated_at) VALUES(?,?,?)',(case_family,value,now()))
        self.cx.commit()
        return True

    def apply_enlil_lookup(self,case_family,query,primary):
        """Merge authoritative ENLIL demographics plus optional pathology request metadata.

        Name, validated TC, birth date and (when locally empty) explicit ENLIL sex
        populate the shared patient/case record.  A manually saved sex in
        Hasta / Blok / Ortak Bilgiler is authoritative and is never overwritten.
        Request/file/doctor/acceptance fields are stored separately and are not used in
        reporting unless the user explicitly copies them into a shared field.
        """
        self.ensure_metadata_schema();case_family=str(case_family or '').strip()
        if not case_family:raise ValueError('ENLIL sonucu için vaka ailesi bulunamadı.')
        primary=dict(primary or {});old=self.get_case_metadata(case_family)
        name=str(primary.get('ad_soyad') or '').strip() or old.get('full_name') or ''
        tc=''.join(ch for ch in str(primary.get('tc_no') or '') if ch.isdigit())
        if not self.validate_national_id(tc):tc=str(old.get('national_id') or '').strip()
        dob=str(primary.get('dogum_tarihi') or '').strip() or old.get('birth_date') or ''
        incoming_sex=self.normalize_sex(primary.get('cinsiyet') or primary.get('sex') or '')
        # Hasta / Blok / Ortak Bilgiler remains authoritative. ENLIL only fills sex
        # when the shared local field is empty.
        local_sex=self.normalize_sex(old.get('sex') or '')
        sex=local_sex or (incoming_sex if incoming_sex in ('Erkek','Kadın') else '')
        ref=(str(primary.get('istem_tarihi') or '').strip() or
             str(primary.get('ilk_istek_kabul_tarihi') or '').strip() or
             self.case_reference_date(case_family))
        derived=self.age_from_birth_date(dob,ref) if dob and ref else None
        if derived is None:
            try:
                raw_age=int(float(str(primary.get('yas') or '').strip().replace(',','.')))
                derived=raw_age if 0<=raw_age<=120 else None
            except Exception:derived=None
        age=str(derived) if derived is not None else (old.get('age') or '')
        stamp=now()
        self.cx.execute("""INSERT INTO case_metadata(
          case_family,full_name,national_id,birth_date,age,diagnosis,sex,requesting_physician,reporting_physician,
          enlil_biopsy_no,enlil_request_no,enlil_file_no,enlil_requesting_physician,enlil_request_date,enlil_first_accept_date,enlil_last_lookup_at,note,updated_at)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(case_family) DO UPDATE SET
            full_name=excluded.full_name,national_id=excluded.national_id,birth_date=excluded.birth_date,age=excluded.age,sex=excluded.sex,
            enlil_biopsy_no=excluded.enlil_biopsy_no,enlil_request_no=excluded.enlil_request_no,enlil_file_no=excluded.enlil_file_no,
            enlil_requesting_physician=excluded.enlil_requesting_physician,enlil_request_date=excluded.enlil_request_date,enlil_first_accept_date=excluded.enlil_first_accept_date,
            enlil_last_lookup_at=excluded.enlil_last_lookup_at,updated_at=excluded.updated_at""",(
              case_family,name,tc,dob,age,old.get('diagnosis') or '',sex,
              old.get('requesting_physician') or '',old.get('reporting_physician') or '',
              str(query or primary.get('biyopsi_no') or '').strip(),str(primary.get('istem_no') or '').strip(),
              str(primary.get('dosya_no') or '').strip(),str(primary.get('istem_doktor') or '').strip(),str(primary.get('istem_tarihi') or '').strip(),
              str(primary.get('ilk_istek_kabul_tarihi') or '').strip(),stamp,old.get('note') or '',stamp))
        self.cx.commit()
        if self.validate_national_id(tc):
            self.set_patient_profile(tc,full_name=name,birth_date=dob,sex=sex,sync_cases=True)
        return self.get_case_metadata(case_family)

    def get_patient_profile(self,national_id):
        """Return canonical local-only patient demographics for a valid TC.

        Diagnosis, physicians, case note, block and tumor percentage are deliberately
        excluded because one patient can have multiple independent tumors/cases.
        """
        self.ensure_metadata_schema()
        nid=''.join(ch for ch in str(national_id or '') if ch.isdigit())
        if not self.validate_national_id(nid): return {}
        r=self.cx.execute('SELECT * FROM patient_profiles WHERE national_id=?',(nid,)).fetchone()
        if r:
            d=dict(r); d['sex']=self.normalize_sex(d.get('sex')); return d
        # Conservative legacy fallback if a pre-v0.4.1 row was added after the seed.
        r=self.cx.execute("""SELECT full_name,birth_date,sex,updated_at FROM case_metadata
          WHERE REPLACE(REPLACE(REPLACE(TRIM(COALESCE(national_id,'')),' ',''),'-',''),'.','')=?
          ORDER BY COALESCE(updated_at,'') DESC LIMIT 1""",(nid,)).fetchone()
        if not r:return {}
        d=dict(r);d['national_id']=nid;d['sex']=self.normalize_sex(d.get('sex'));return d

    def set_patient_profile(self,national_id,full_name='',birth_date='',sex='',sync_cases=True):
        """Merge patient-level demographics and optionally synchronize linked cases.

        Empty values do not erase an existing profile. This prevents a newly-created
        tumor/case row from blanking demographics before TC auto-fill has occurred.
        """
        self.ensure_metadata_schema()
        nid=''.join(ch for ch in str(national_id or '') if ch.isdigit())
        if not self.validate_national_id(nid): raise ValueError('Geçerli TC Kimlik No gerekli.')
        old=self.get_patient_profile(nid)
        name=(str(full_name).strip() or old.get('full_name') or '')
        dob=(str(birth_date).strip() or old.get('birth_date') or '')
        sx=self.normalize_sex(str(sex).strip() or old.get('sex') or '')
        self.cx.execute("""INSERT INTO patient_profiles(national_id,full_name,birth_date,sex,updated_at) VALUES(?,?,?,?,?)
          ON CONFLICT(national_id) DO UPDATE SET full_name=excluded.full_name,birth_date=excluded.birth_date,sex=excluded.sex,updated_at=excluded.updated_at""",
          (nid,name,dob,sx,now()))
        if sync_cases:
            # Propagate a deliberate correction only to blank/canonical rows. A case
            # that already contains a different non-empty DOB/sex is a potential
            # identity conflict and must never be silently overwritten.
            old_name=str(old.get('full_name') or '').strip(); old_dob=str(old.get('birth_date') or '').strip(); old_sex=self.normalize_sex(old.get('sex'))
            linked=self.cx.execute("""SELECT case_family,full_name,birth_date,sex FROM case_metadata
              WHERE REPLACE(REPLACE(REPLACE(TRIM(COALESCE(national_id,'')),' ',''),'-',''),'.','')=?""",(nid,)).fetchall()
            for row in linked:
                d=dict(row);cur_name=str(d.get('full_name') or '').strip();cur_dob=str(d.get('birth_date') or '').strip();cur_sex=self.normalize_sex(d.get('sex'))
                name_ok=(not cur_name) or (not old_name) or cur_name==old_name or cur_name==name
                dob_ok=(not cur_dob) or (not old_dob) or cur_dob==old_dob or cur_dob==dob
                sex_ok=(not cur_sex) or (not old_sex) or cur_sex==old_sex or cur_sex==sx
                if name_ok and dob_ok and sex_ok:
                    self.cx.execute('UPDATE case_metadata SET full_name=?,birth_date=?,sex=?,updated_at=? WHERE case_family=?',(name,dob,sx,now(),d['case_family']))
        self.cx.commit()
        return self.get_patient_profile(nid)

    def patient_identity_summary(self,national_id,current_case_family=''):
        """Summarize tumor/case and technical records linked by validated local TC.

        A patient can have many case families (different tumors) and each case can have
        DNA/RNA and rerun technical records. No diagnosis is copied between cases.
        """
        self.ensure_metadata_schema()
        nid=''.join(ch for ch in str(national_id or '') if ch.isdigit())
        empty={'national_id':nid,'case_count':0,'technical_count':0,'dna_count':0,'rna_count':0,'rerun_count':0,'patient_ids':[],'diagnoses':[],'case_families':[]}
        if not self.validate_national_id(nid): return empty
        rows=self.cx.execute("""SELECT cm.case_family,cm.diagnosis,s.patient_id,s.sample_type,s.is_rerun,s.record_key
          FROM case_metadata cm LEFT JOIN samples s ON COALESCE(NULLIF(s.case_family,''),s.patient_id)=cm.case_family
          WHERE REPLACE(REPLACE(REPLACE(TRIM(COALESCE(cm.national_id,'')),' ',''),'-',''),'.','')=?
          ORDER BY cm.case_family,s.patient_id,s.sample_type""",(nid,)).fetchall()
        fams=[];ids=[];diags=[];tech=set();dna=rna=rerun=0
        for r in rows:
            d=dict(r);fam=d.get('case_family') or ''
            if fam and fam not in fams:fams.append(fam)
            pid=d.get('patient_id') or ''
            if pid and pid not in ids:ids.append(pid)
            dx=(d.get('diagnosis') or '').strip()
            if dx and dx not in diags:diags.append(dx)
            rk=d.get('record_key')
            if rk and rk not in tech:
                tech.add(rk);typ=(d.get('sample_type') or '').upper()
                if typ=='DNA':dna+=1
                elif typ=='RNA':rna+=1
                if d.get('is_rerun'):rerun+=1
        return {'national_id':nid,'case_count':len(fams),'technical_count':len(tech),'dna_count':dna,'rna_count':rna,'rerun_count':rerun,'patient_ids':ids,'diagnoses':diags,'case_families':fams}

    @staticmethod
    def normalize_protocol_key(value):
        """Canonical MP protocol key used only for local QC/collector linking."""
        text=str(value or '').strip().upper().replace(' ', '')
        m=re.search(r'MP0*(\d+)(?:-+|/+|_+)(\d{2,4})(?!\d)',text)
        if not m:return ''
        return f'MP{int(m.group(1))}-{m.group(2)[-2:]}'

    def set_qc_protocol_link(self,protocol_key,sample_type,record_key,case_family='',source='manual'):
        self.ensure_metadata_schema()
        key=self.normalize_protocol_key(protocol_key)
        typ=str(sample_type or '').strip().upper()
        if not key:raise ValueError('Geçerli MP hasta protokolü gerekli.')
        sample=self.get_sample(record_key) if record_key else None
        if not sample:raise ValueError('Eşleştirilecek NGS teknik kayıt bulunamadı.')
        fam=case_family or sample.get('case_family') or sample.get('patient_id') or ''
        self.cx.execute("""INSERT INTO qc_protocol_links(protocol_key,sample_type,record_key,case_family,source,updated_at)
          VALUES(?,?,?,?,?,?) ON CONFLICT(protocol_key,sample_type) DO UPDATE SET
          record_key=excluded.record_key,case_family=excluded.case_family,source=excluded.source,updated_at=excluded.updated_at""",
          (key,typ,record_key,fam,source,now()))
        self.cx.commit()
        return {'protocol_key':key,'sample_type':typ,'record_key':record_key,'case_family':fam,'source':source}

    def get_qc_protocol_link(self,protocol_key,sample_type=''):
        self.ensure_metadata_schema()
        key=self.normalize_protocol_key(protocol_key);typ=str(sample_type or '').strip().upper()
        if not key:return {}
        rows=self.cx.execute("""SELECT * FROM qc_protocol_links WHERE protocol_key=? AND sample_type IN (?, '')
          ORDER BY CASE WHEN sample_type=? THEN 0 ELSE 1 END,updated_at DESC""",(key,typ,typ)).fetchall()
        for row in rows:
            d=dict(row);sample=self.get_sample(d.get('record_key') or '')
            if sample:return d
        return {}

    def delete_qc_protocol_link(self,protocol_key,sample_type=''):
        self.ensure_metadata_schema();key=self.normalize_protocol_key(protocol_key);typ=str(sample_type or '').strip().upper()
        if not key:return 0
        cur=self.cx.execute('DELETE FROM qc_protocol_links WHERE protocol_key=? AND sample_type=?',(key,typ));self.cx.commit();return cur.rowcount

    def protocol_match_candidates(self,protocol_key,sample_type='',limit=100):
        """Return deterministic manual-match candidates. Fuzzy results are never auto-linked."""
        self.ensure_metadata_schema();key=self.normalize_protocol_key(protocol_key);typ=str(sample_type or '').strip().upper()
        m=re.match(r'MP(\d+)-(\d{2})$',key or '')
        target_num=int(m.group(1)) if m else None;target_year=m.group(2) if m else ''
        rows=self.cx.execute("""SELECT s.*,cm.full_name,cm.diagnosis,cm.national_id,cm.birth_date,cm.sex,
          cm.requesting_physician,cm.reporting_physician
          FROM samples s LEFT JOIN case_metadata cm ON cm.case_family=COALESCE(NULLIF(s.case_family,''),s.patient_id)
          ORDER BY COALESCE(s.updated_at,s.discovered_at) DESC""").fetchall()
        out=[]
        for row in rows:
            d=dict(row);pid=self.normalize_protocol_key(d.get('patient_id'));fam=self.normalize_protocol_key(d.get('case_family'))
            score=0;reasons=[]
            if key and pid==key:score+=120;reasons.append('hasta protokolü tam eşleşme')
            if key and fam==key:score+=115;reasons.append('vaka protokolü tam eşleşme')
            cmatch=re.match(r'MP(\d+)-(\d{2})$',pid or fam or '')
            if target_num is not None and cmatch:
                n=int(cmatch.group(1));y=cmatch.group(2)
                if n==target_num and y!=target_year:score+=70;reasons.append('protokol no aynı, yıl farklı')
                elif y==target_year:
                    diff=abs(n-target_num)
                    if diff==1:score+=35;reasons.append('aynı yıl, komşu protokol')
                    elif diff<=3:score+=20;reasons.append('aynı yıl, yakın protokol')
            if typ and (d.get('sample_type') or '').upper()==typ:score+=12;reasons.append('örnek türü aynı')
            if not d.get('is_rerun'):score+=3
            if d.get('full_name'):score+=2
            d['match_score']=score;d['match_reason']='; '.join(reasons) or 'manuel aday';d['protocol_display']=pid or self.normalize_protocol_key(d.get('case_family')) or str(d.get('patient_id') or '')
            out.append(d)
        out.sort(key=lambda x:(-int(x.get('match_score') or 0),str(x.get('patient_id') or ''),str(x.get('sample_type') or '')))
        return out[:max(1,int(limit or 100))]

    def diagnosis_suggestions(self,typed='',limit=100):
        """Previously saved tumor diagnoses for non-blocking type-ahead suggestions."""
        self.ensure_metadata_schema()
        q=str(typed or '').strip().casefold()
        rows=self.cx.execute("""SELECT diagnosis,COUNT(*) AS n,MAX(COALESCE(updated_at,'')) AS last_seen
          FROM case_metadata WHERE TRIM(COALESCE(diagnosis,''))<>''
          GROUP BY diagnosis ORDER BY n DESC,last_seen DESC,diagnosis""").fetchall()
        out=[];seen=set()
        for r in rows:
            val=str(r['diagnosis'] or '').strip()
            key=val.casefold()
            if not val or key in seen:continue
            if q and q not in key:continue
            seen.add(key);out.append(val)
            if len(out)>=max(1,int(limit or 100)):break
        return out

    def doctor_suggestions(self,role=None):
        # Keep requesting/reporting physician histories separate.
        self.ensure_metadata_schema()
        role=(role or '').strip().lower()
        if role in ('requesting','request','istem','requesting_physician'):
            cols=('requesting_physician',)
        elif role in ('reporting','report','rapor','reporting_physician'):
            cols=('reporting_physician',)
        else:
            cols=('requesting_physician','reporting_physician')
        vals=set()
        for col in cols:
            for r in self.cx.execute(f"SELECT DISTINCT {col} AS v FROM case_metadata WHERE TRIM(COALESCE({col},''))<>'' ORDER BY {col}").fetchall():
                if r['v']: vals.add(str(r['v']).strip())
        return sorted(vals,key=lambda x:x.casefold())

    def related_patient_ids_for_case(self,case_family):
        # Return other MP/patient labels sharing a validated local TC. TC itself is never returned.
        self.ensure_metadata_schema()
        cm=self.get_case_metadata(case_family)
        nid=''.join(ch for ch in str(cm.get('national_id') or '') if ch.isdigit())
        if not self.validate_national_id(nid): return []
        rows=self.cx.execute("""SELECT DISTINCT s.patient_id
          FROM case_metadata cm JOIN samples s ON COALESCE(NULLIF(s.case_family,''),s.patient_id)=cm.case_family
          WHERE REPLACE(REPLACE(REPLACE(TRIM(COALESCE(cm.national_id,'')),' ',''),'-',''),'.','')=?
            AND cm.case_family<>? AND TRIM(COALESCE(s.patient_id,''))<>''
          ORDER BY s.patient_id""",(nid,case_family or '')).fetchall()
        return [r['patient_id'] for r in rows if r['patient_id']]

    def previous_metadata_history(self, record_key, national_id='', limit=150):
        """Other work records tied to this case or a validated local patient identity.

        Values are exact saved case/technical metadata. Case diagnosis is a SHARED
        case field, not proof that every individual run had a signed diagnosis.
        """
        self.ensure_metadata_schema()
        current = self.get_sample(record_key)
        if not current:
            return []
        family = current.get('case_family') or current.get('patient_id') or ''
        from_case = self.get_case_metadata(family)
        nid = ''.join(c for c in str(national_id or from_case.get('national_id') or '') if c.isdigit())
        valid = self.validate_national_id(nid)
        query = """SELECT s.record_key,s.patient_id,s.case_family,s.pool_name,s.sample_type,
            s.is_rerun,s.run_uuid,s.updated_at AS work_updated_at,
            cm.diagnosis,cm.national_id,cm.updated_at AS case_updated_at,
            sm.block_id,sm.block_source,sm.tumor_percentage,sm.note,
            sm.updated_at AS sample_updated_at
            FROM samples s
            LEFT JOIN case_metadata cm ON cm.case_family=COALESCE(NULLIF(s.case_family,''),s.patient_id)
            LEFT JOIN sample_metadata sm ON sm.record_key=s.record_key
            WHERE s.record_key<>? AND (
              COALESCE(NULLIF(s.case_family,''),s.patient_id)=?
              OR (?=1 AND REPLACE(REPLACE(REPLACE(TRIM(COALESCE(cm.national_id,'')),' ',''),'-',''),'.','')=?)
            ) ORDER BY COALESCE(sm.updated_at,cm.updated_at,s.updated_at,'') DESC,
              s.pool_name DESC,s.record_key DESC LIMIT ?"""
        rows = self.cx.execute(query,(record_key,family,int(valid),nid if valid else '',max(1,min(int(limit),300)))).fetchall()
        return [{**dict(row), 'same_case': (row['case_family'] or row['patient_id'] or '')==family,
                 'diagnosis_provenance':'Vaka ortak tanısı (imzalı çalışma raporu doğrulanmadı)'} for row in rows]

    def get_sample_metadata(self,record_key):
        self.ensure_metadata_schema()
        r=self.cx.execute('SELECT * FROM sample_metadata WHERE record_key=?',(record_key,)).fetchone()
        if r:
            d=dict(r);d['block_source']=d.get('block_source') or ''
            return d
        return {'record_key':record_key,'block_id':'','block_section':'','block_source':'','tumor_percentage':'','note':'','updated_at':''}

    def set_sample_metadata(self,record_key,block_id='',block_section='',tumor_percentage='',note='',block_source=None):
        """Save metadata for one exact technical sample.

        block_source=None preserves the existing dedicated source for backwards-compatible
        callers. Passing '' explicitly clears it. block_id/source/tumor stay on the same
        record_key so QC/Enlil cannot mix siblings from one patient/case.
        """
        self.ensure_metadata_schema()
        self.cx.execute('''INSERT INTO sample_metadata(record_key,block_id,block_section,block_source,tumor_percentage,note,updated_at)
          VALUES(?,?,?,?,?,?,?)
          ON CONFLICT(record_key) DO UPDATE SET block_id=excluded.block_id,block_section=excluded.block_section,
            block_source=CASE WHEN excluded.block_source IS NULL THEN sample_metadata.block_source ELSE excluded.block_source END,
            tumor_percentage=excluded.tumor_percentage,note=excluded.note,updated_at=excluded.updated_at''',
          (record_key,block_id,block_section,block_source,tumor_percentage,note,now()))
        self.cx.commit()

    def technical_metadata_for_record(self,record_key,inherit_case=True):
        """Return only this technical record's block and histologic tumor fraction.

        The legacy argument is retained for callers. An empty block does not
        establish a link to any sibling. Explicit history selection is available.
        """
        out=dict(self.get_sample_metadata(record_key));out['inherited_from']=''
        return out

    def metadata_for_record(self,record_key):
        s=self.get_sample(record_key)
        if not s:return {}
        cm=self.get_case_metadata(s.get('case_family') or s.get('patient_id') or '')
        sm=self.technical_metadata_for_record(record_key)
        return {**s,**{f'case_{k}':v for k,v in cm.items() if k!='case_family'},**{f'sample_{k}':v for k,v in sm.items() if k!='record_key'}}

    def unfinished_task_details(self,record_keys=None):
        self.ensure_metadata_schema()
        sql='''SELECT t.record_key,t.task_type,t.status,t.attempts,t.last_error,t.updated_at,
          s.pool_name,s.patient_id,s.case_family,s.sample_type,s.run_status,s.sample_status,cm.full_name
          FROM tasks t JOIN samples s ON s.record_key=t.record_key
          LEFT JOIN case_metadata cm ON cm.case_family=COALESCE(NULLIF(s.case_family,''),s.patient_id)
          WHERE t.status NOT IN ('done','not_available')'''
        args=[]
        if record_keys:
            qs=','.join('?' for _ in record_keys);sql+=f' AND t.record_key IN ({qs})';args.extend(record_keys)
        sql+=' ORDER BY s.pool_name,s.patient_id,t.task_type'
        return self.cx.execute(sql,args).fetchall()

    def oncogenic_archive_variants(self,class_filter=None,search=''):
        self.ensure_analysis_schema(); self.ensure_metadata_schema()
        sql="""SELECT o.variant_key,MAX(o.gene) gene,MAX(o.change) change,MAX(o.oncogenic) oncogenic,
          COUNT(DISTINCT o.record_key) samples,COUNT(DISTINCT o.pool_name) pools,
          MAX(COALESCE(o.highest_level,'')) highest_level,MAX(o.af) max_af
          FROM oncogenic_observations o WHERE 1=1"""
        args=[]
        if class_filter and class_filter!='Tümü':
            sql+=' AND o.oncogenic=?';args.append(class_filter)
        q=(search or '').strip()
        if q:
            like='%'+q+'%'
            sql+=" AND (o.variant_key LIKE ? OR COALESCE(o.gene,'') LIKE ? OR COALESCE(o.change,'') LIKE ? OR COALESCE(o.patient_id,'') LIKE ?)"
            args += [like,like,like,like]
        sql+=" GROUP BY o.variant_key ORDER BY CASE MAX(o.oncogenic) WHEN 'Oncogenic' THEN 0 ELSE 1 END, COUNT(DISTINCT o.record_key) DESC, MAX(o.gene), MAX(o.change)"
        return self.cx.execute(sql,args).fetchall()

    def oncogenic_variant_observations(self,variant_key):
        self.ensure_analysis_schema(); self.ensure_metadata_schema()
        return self.cx.execute('''SELECT o.*,s.depth,s.tmb,s.msi,s.hrd,
          cm.full_name,cm.national_id,cm.birth_date,cm.age,cm.diagnosis,cm.sex,cm.requesting_physician,cm.reporting_physician,
          sm.block_id,sm.tumor_percentage
          FROM oncogenic_observations o
          LEFT JOIN samples s ON s.record_key=o.record_key
          LEFT JOIN case_metadata cm ON cm.case_family=COALESCE(NULLIF(s.case_family,''),s.patient_id)
          LEFT JOIN sample_metadata sm ON sm.record_key=o.record_key
          WHERE o.variant_key=?
          ORDER BY CASE o.oncogenic WHEN 'Oncogenic' THEN 0 ELSE 1 END, o.af DESC, o.pool_name, o.patient_id''',(variant_key,)).fetchall()

# === NGS_AI_JSON_DB_V0432 ===
# Preserve the existing import logic, then apply V2 request identity / force-new-task semantics.
_ngs_original_import_ai_review_v0432 = StateDB.import_ai_review


def _ngs_import_ai_review_v0432(self, data, source_path='', source_sha256=''):
    result = _ngs_original_import_ai_review_v0432(self, data, source_path, source_sha256)
    try:
        self.ensure_ai_review_schema()
        cols={r['name'] for r in self.cx.execute('PRAGMA table_info(igv_queue)').fetchall()}
        if 'request_id' not in cols: self.cx.execute("ALTER TABLE igv_queue ADD COLUMN request_id TEXT DEFAULT ''")
        if 'request_revision' not in cols: self.cx.execute("ALTER TABLE igv_queue ADD COLUMN request_revision INTEGER DEFAULT 0")
        self.cx.commit()
        for req in data.get('igv_requests') or []:
            if not isinstance(req,dict): continue
            rk=str(req.get('record_key') or '').strip();vk=str(req.get('variant_key') or '').strip()
            if not rk or not vk: continue
            rid=str(req.get('request_id') or '').strip();
            try: rev=int(req.get('request_revision') or 0)
            except Exception: rev=0
            execution=req.get('execution') or {};force=bool(execution.get('force_new_task'))
            reset_manual=bool(execution.get('reset_manual_decision'))
            if force:
                sql="""UPDATE igv_queue SET request_id=?,request_revision=?,status='pending',screenshot_path='',capture_meta_path='',auto_settings_json='',ai_ready=0,capture_error='',captured_at='',updated_at=CURRENT_TIMESTAMP"""
                if reset_manual: sql += ",manual_decision='',manual_note=''"
                sql += " WHERE record_key=? AND variant_key=?"
                self.cx.execute(sql,(rid,rev,rk,vk))
            else:
                self.cx.execute("UPDATE igv_queue SET request_id=?,request_revision=? WHERE record_key=? AND variant_key=?",(rid,rev,rk,vk))
        self.cx.commit()
    except Exception as exc:
        result.setdefault('warnings',[]).append('V2 request identity uygulanamadı: '+str(exc))
    return result

StateDB.import_ai_review = _ngs_import_ai_review_v0432

# === NGS_IGV_AUDIT_LINEAGE_V0433 ===
# Preserve superseded IGV evidence and request lineage before force-new-task resets.
_ngs_import_ai_review_v0433_prev = StateDB.import_ai_review


def _ngs_safe_path_v0433(value):
    import re as _re
    return _re.sub(r'[^A-Za-z0-9._-]+','_',str(value or 'NA'))[:100]


def _ngs_ensure_igv_audit_schema_v0433(self):
    self.ensure_ai_review_schema()
    cols={r['name'] for r in self.cx.execute('PRAGMA table_info(igv_queue)').fetchall()}
    for col,ddl in [
        ('parent_request_id',"TEXT DEFAULT ''"),('source_identity_json',"TEXT DEFAULT ''"),
        ('pileup_json',"TEXT DEFAULT ''"),('coverage_audit_json',"TEXT DEFAULT ''"),
    ]:
        if col not in cols:self.cx.execute(f'ALTER TABLE igv_queue ADD COLUMN {col} {ddl}')
    self.cx.executescript('''
    CREATE TABLE IF NOT EXISTS igv_snapshot_archive(
      id INTEGER PRIMARY KEY AUTOINCREMENT, queue_id INTEGER, record_key TEXT, variant_key TEXT,
      source_request_id TEXT, source_request_revision INTEGER DEFAULT 0,
      superseded_by_request_id TEXT, superseded_by_request_revision INTEGER DEFAULT 0,
      screenshot_path TEXT, capture_meta_path TEXT, archive_manifest_path TEXT, archived_at TEXT, archive_note TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_igv_archive_queue ON igv_snapshot_archive(queue_id,archived_at);
    CREATE INDEX IF NOT EXISTS idx_igv_archive_variant ON igv_snapshot_archive(record_key,variant_key);
    ''')
    acols={r['name'] for r in self.cx.execute('PRAGMA table_info(igv_snapshot_archive)').fetchall()}
    if 'archive_manifest_path' not in acols:self.cx.execute("ALTER TABLE igv_snapshot_archive ADD COLUMN archive_manifest_path TEXT DEFAULT ''")
    self.cx.commit()


def _ngs_archive_current_igv_v0433(self,row,new_request_id,new_revision):
    import shutil as _shutil, time as _time
    from pathlib import Path as _Path
    if not row:return None
    shot_raw=str(row.get('screenshot_path') or '').strip();meta_raw=str(row.get('capture_meta_path') or '').strip()
    shot=_Path(shot_raw) if shot_raw else None;meta=_Path(meta_raw) if meta_raw else None
    if not (shot and shot.exists()) and not (meta and meta.exists()):return None
    base=self.path.parent.parent;stamp=_time.strftime('%Y%m%d_%H%M%S')
    out=base/'IGV_SNAPSHOTS'/'ARCHIVE'/_ngs_safe_path_v0433(row.get('record_key'))/_ngs_safe_path_v0433(row.get('variant_key'))/f'{stamp}_{_ngs_safe_path_v0433(row.get("request_id") or "legacy")}'
    out.mkdir(parents=True,exist_ok=True)
    moved_shot=moved_meta=''
    try:
        if shot and shot.exists():
            dst=out/shot.name;_shutil.move(str(shot),str(dst));moved_shot=str(dst)
        if meta and meta.exists():
            dst=out/meta.name;_shutil.move(str(meta),str(dst));moved_meta=str(dst)
    except Exception:
        # Audit preservation must fail safe: if moving fails, leave source paths intact.
        return None
    manifest=out/'ARCHIVE_META.json'
    manifest.write_text(json.dumps({'schema':'ngs_igv_snapshot_archive_v1','archived_at':now(),'queue_id':row.get('id'),'record_key':row.get('record_key') or '',
      'variant_key':row.get('variant_key') or '','source_request_id':row.get('request_id') or '','source_request_revision':int(row.get('request_revision') or 0),
      'superseded_by':{'request_id':new_request_id or '','request_revision':int(new_revision or 0)},'screenshot_path':moved_shot,'capture_meta_path':moved_meta},ensure_ascii=False,indent=2),encoding='utf-8')
    self.cx.execute('''INSERT INTO igv_snapshot_archive(queue_id,record_key,variant_key,source_request_id,source_request_revision,
      superseded_by_request_id,superseded_by_request_revision,screenshot_path,capture_meta_path,archive_manifest_path,archived_at,archive_note)
      VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',(row.get('id'),row.get('record_key') or '',row.get('variant_key') or '',row.get('request_id') or '',int(row.get('request_revision') or 0),
      new_request_id or '',int(new_revision or 0),moved_shot,moved_meta,str(manifest),now(),'force_new_task: önceki IGV kanıtı arşivlendi'))
    self.cx.commit();return {'screenshot_path':moved_shot,'capture_meta_path':moved_meta,'archive_manifest_path':str(manifest),'folder':str(out)}


def _ngs_import_ai_review_v0433(self,data,source_path='',source_sha256=''):
    _ngs_ensure_igv_audit_schema_v0433(self)
    archive_warnings=[];pre={}
    for req in data.get('igv_requests') or []:
        if not isinstance(req,dict):continue
        rk=str(req.get('record_key') or '').strip();vk=str(req.get('variant_key') or '').strip()
        if not rk or not vk:continue
        row=self.cx.execute('SELECT * FROM igv_queue WHERE record_key=? AND variant_key=?',(rk,vk)).fetchone();row=dict(row) if row else None
        pre[(rk,vk)]=row
        execution=req.get('execution') or {}
        if row and execution.get('force_new_task') and (row.get('screenshot_path') or row.get('capture_meta_path')):
            arc=_ngs_archive_current_igv_v0433(self,row,str(req.get('request_id') or ''),int(req.get('request_revision') or 0))
            if not arc:archive_warnings.append(f'Önceki IGV arşivlenemedi: {rk} | {vk}')
    result=_ngs_import_ai_review_v0433_prev(self,data,source_path,source_sha256)
    _ngs_ensure_igv_audit_schema_v0433(self)
    for req in data.get('igv_requests') or []:
        if not isinstance(req,dict):continue
        rk=str(req.get('record_key') or '').strip();vk=str(req.get('variant_key') or '').strip()
        if not rk or not vk:continue
        old=pre.get((rk,vk)) or {};parent=str(req.get('parent_request_id') or '').strip()
        try:rev=int(req.get('request_revision') or 0)
        except Exception:rev=0
        if not parent and rev>1 and old.get('request_id'):parent=str(old.get('request_id') or '')
        source_identity=req.get('source_files') or req.get('source_identity') or {}
        pileup=req.get('pileup') or {};coverage=req.get('coverage_audit') or {}
        self.cx.execute('''UPDATE igv_queue SET parent_request_id=?,source_identity_json=?,pileup_json=?,coverage_audit_json=? WHERE record_key=? AND variant_key=?''',
          (parent,json.dumps(source_identity,ensure_ascii=False) if source_identity else '',json.dumps(pileup,ensure_ascii=False) if pileup else '',json.dumps(coverage,ensure_ascii=False) if coverage else '',rk,vk))
    self.cx.commit()
    if archive_warnings:result.setdefault('warnings',[]).extend(archive_warnings)
    return result


def _ngs_igv_snapshot_archive_v0433(self,queue_id=None,record_key=None,variant_key=None,limit=500):
    _ngs_ensure_igv_audit_schema_v0433(self);sql='SELECT * FROM igv_snapshot_archive WHERE 1=1';args=[]
    if queue_id is not None:sql+=' AND queue_id=?';args.append(int(queue_id))
    if record_key:sql+=' AND record_key=?';args.append(record_key)
    if variant_key:sql+=' AND variant_key=?';args.append(variant_key)
    sql+=' ORDER BY id DESC LIMIT ?';args.append(int(limit));return self.cx.execute(sql,args).fetchall()

StateDB.ensure_igv_audit_schema=_ngs_ensure_igv_audit_schema_v0433
StateDB.igv_snapshot_archive=_ngs_igv_snapshot_archive_v0433
StateDB.import_ai_review=_ngs_import_ai_review_v0433

# Latest integrity adapters replace the old unconditional IGV reset and retain
# one record-scoped source/manual history for clinical reporting.
from igv_workflow_v0481 import install_igv_workflow
install_igv_workflow(StateDB)
from reporting_context import install_reporting_context
install_reporting_context(StateDB)

# v0.4.83 additive local review history; never sets GenNext Selection.
from review_workspace_store import install_review_workspace
install_review_workspace(StateDB)
