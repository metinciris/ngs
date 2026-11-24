"""Local archive, report versions and reproducible study cohorts.

No remote services are used. Observed-call analyses cover the EXISTING
Oncogenic/Likely Oncogenic index, not every locus in every raw alignment.
A reported-history row and an observed call remain separate facts.
"""
from __future__ import annotations

import collections
import csv
import datetime as dt
import hashlib
import hmac
import io
import json
import re
import sqlite3
import unicodedata
import zipfile
from pathlib import Path
from typing import Any

from db import StateDB

ARCHIVE_SCHEMA_VERSION = '2'
TABLES = '''
CREATE TABLE IF NOT EXISTS archive_report_documents (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 record_key TEXT NOT NULL, version INTEGER NOT NULL,
 title TEXT NOT NULL, report_text TEXT NOT NULL,
 document_status TEXT NOT NULL CHECK(document_status IN ('draft','user_confirmed')),
 content_sha256 TEXT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(record_key,version)
);
CREATE INDEX IF NOT EXISTS idx_archive_reports_record ON archive_report_documents(record_key,version);
CREATE TABLE IF NOT EXISTS archive_studies (
 id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
 revision INTEGER NOT NULL, notes TEXT, criteria_json TEXT NOT NULL,
 dataset TEXT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(name,revision)
);
CREATE TABLE IF NOT EXISTS archive_study_members (
 study_id INTEGER NOT NULL, record_key TEXT NOT NULL,
 PRIMARY KEY(study_id,record_key)
);
CREATE TABLE IF NOT EXISTS archive_assay_results (
 record_key TEXT PRIMARY KEY, result_state TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL,
 CHECK(result_state IN ('unset','negative','positive','indeterminate'))
);
'''


def timestamp() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec='seconds')


def norm(value: Any) -> str:
    s = str(value or '').casefold().replace('\u0307', '')
    s = s.translate(str.maketrans({'\u0131': 'i', '\u015f': 's', '\u011f': 'g', '\u00fc': 'u', '\u00f6': 'o', '\u00e7': 'c'}))
    return unicodedata.normalize('NFKC', s).strip()


def canonical_key(key: str) -> str:
    """Remove only the UI's SNV prefix; no genome-build/allele inference."""
    text = str(key or '').strip()
    if text.upper().startswith('SNV|'):
        text = text[4:]
    parts = text.split('|')
    if len(parts) == 4 and parts[0]:
        chrom = parts[0]
        if chrom.lower().startswith('chr'):
            chrom = chrom[3:]
        parts[0] = 'chr' + chrom.upper()
        return '|'.join(parts)
    return text


def number(value: Any) -> float | None:
    if value is None or str(value).strip() in ('', '.', '-', '\u2014'):
        return None
    try:
        n = float(str(value).strip().replace('%', '').replace(',', '.'))
        return n if n == n and abs(n) != float('inf') else None
    except (TypeError, ValueError):
        return None


def date_only(value: Any) -> str:
    s = str(value or '').strip()
    for fmt in ('%Y-%m-%d', '%d.%m.%Y', '%d/%m/%Y'):
        try:
            return dt.datetime.strptime(s[:10], fmt).date().isoformat()
        except ValueError:
            pass
    return ''


def latest_reported(rows: list[dict], decisions: list[dict], observed: list[dict]) -> list[dict]:
    """One observation per technical sample/key, keeping report event count.

    Conflicting CURRENT decisions do not erase historical reporting facts.
    Legacy viewer-only `reported` decisions are included and visibly labelled.
    """
    obs = {(r['record_key'], canonical_key(r['variant_key'])): r for r in observed}
    dec = {(r['record_key'], canonical_key(r['variant_key'])): r for r in decisions}
    result: dict[tuple, dict] = {}
    for r in sorted(rows, key=lambda x: x.get('id', 0)):
        if not r.get('confirmed'):
            continue
        k = (r['record_key'], canonical_key(r['variant_key']))
        old = result.get(k, {})
        item = dict(obs.get(k, {}))
        item.update(r)
        item['variant_key'] = k[1]
        item['history_event_count'] = old.get('history_event_count', 0) + 1
        item['history_ids'] = old.get('history_ids', []) + [r['id']]
        item['first_reported_at'] = old.get('first_reported_at') or r.get('created_at', '')
        item['last_reported_at'] = r.get('created_at', '')
        item['recorded_as_reported'] = True
        item['source_label'] = 'Onayli rapor gecmisi'
        result[k] = item
    for k, d in dec.items():
        if d.get('status') == 'reported' and k not in result:
            o = dict(obs.get(k, {}))
            o.update(record_key=k[0], variant_key=k[1], recorded_as_reported=True,
                     variant_type='CNV' if k[1].startswith('CNV|') else ('Clinical' if k[1].startswith('Clinical|') else 'SNV'),
                     source='variant_decision', source_label='Manuel rapor karari (goruntuleyici)',
                     history_event_count=0, history_ids=[], first_reported_at=d.get('updated_at', ''),
                     last_reported_at=d.get('updated_at', ''), source_text=d.get('note', ''),
                     protein=o.get('change', ''), cdna=o.get('cdna', ''))
            if k[1].startswith('CNV|'):
                o['gene'] = k[1].split('|')[-1]
            result[k] = o
    for k, r in result.items():
        d = dec.get(k, {})
        r['current_decision'] = d.get('status', '')
        r['decision_conflict'] = d.get('status') in ('artifact', 'not_reported')
        r['change'] = r.get('protein') or r.get('cdna') or r.get('feature') or r.get('change') or ''
        r['variant_type'] = r.get('variant_type') or 'SNV'
    return list(result.values())


class ArchiveStore:
    def __init__(self, base: Path, db: StateDB):
        self.base, self.db = Path(base), db
        self._raw_cache = {}
        self._qc_cache = {}
        self.backup_path: Path | None = None
        if not db._table_exists('archive_report_documents'):
            # SQLite backup includes committed WAL state; never copy a live DB file alone.
            backup_dir = db.path.parent / 'backups'
            backup_dir.mkdir(parents=True, exist_ok=True)
            name = 'before_archive_v040_' + dt.datetime.now().strftime('%Y%m%d_%H%M%S_%f') + '.sqlite3'
            self.backup_path = backup_dir / name
            with sqlite3.connect(self.backup_path) as target:
                db.cx.backup(target)
        db.cx.executescript(TABLES)
        db.cx.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('archive_schema_version',?)", (ARCHIVE_SCHEMA_VERSION,))
        db.cx.commit()
        # Same private key used by the existing AI-export pseudonyms.
        from ai_export import _identity_secret
        self.secret = _identity_secret(self.base)

    def token(self, text: str, prefix: str = 'PT-') -> str:
        return prefix + hmac.new(self.secret, text.encode('utf-8'), hashlib.sha256).hexdigest()[:20]

    def snapshot(self) -> dict:
        """Consistent read-only snapshot on a dedicated connection (worker-safe)."""
        cx = sqlite3.connect(self.db.path.resolve().as_uri() + '?mode=ro', uri=True)
        cx.row_factory = sqlite3.Row
        try:
            cx.execute('BEGIN')
            existing = {r[0] for r in cx.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            def rows(table: str) -> list[dict]:
                return [dict(r) for r in cx.execute('SELECT * FROM ' + table)] if table in existing else []
            samples, cases, smeta, pools = rows('samples'), rows('case_metadata'), rows('sample_metadata'), rows('pools')
            tasks, files = rows('tasks'), rows('files')
            history, decisions, observed = rows('reported_variant_history'), rows('variant_decisions'), rows('oncogenic_observations')
            queue, reviews = rows('igv_queue'), rows('ai_patient_reviews')
            docs = [dict(r) for r in cx.execute('SELECT id,record_key,version,title,document_status,content_sha256,created_at FROM archive_report_documents')]
            studies = rows('archive_studies')
            assay_results = rows('archive_assay_results')
            manual_closures = rows('manual_report_closure')
            indexed = rows('analysis_indexed_files')
            gennext_cur = rows('gennext_selection_current')
            gennext_states = rows('gennext_selection_status')
            gennext_changes = rows('gennext_selection_history')
            fusion_cur = rows('gennext_fusion_current')
            fusion_states = rows('gennext_fusion_status')
            fusion_changes = rows('gennext_fusion_history')
        finally:
            cx.close()
        cm = {r['case_family']: r for r in cases}
        sm = {r['record_key']: r for r in smeta}
        pm = {(r['pool_name'], r['run_uuid']): r for r in pools}
        tm: dict[str, list] = collections.defaultdict(list)
        fm: dict[str, list] = collections.defaultdict(list)
        dm: dict[str, list] = collections.defaultdict(list)
        qm: dict[str, list] = collections.defaultdict(list)
        rev: dict[str, list] = collections.defaultdict(list)
        arm = {r['record_key']:r for r in assay_results}
        manually_closed = {r['record_key']: r for r in manual_closures}
        from gennext_selection_store import normalize_snapshot,run_phase
        run_state_by_key={s['record_key']:run_phase(s) for s in samples}
        gen_states={r['record_key']:r for r in gennext_states}
        sample_by_key={s['record_key']:s for s in samples}
        gen_current={}
        for row in gennext_cur:
            st=gen_states.get(row['record_key'],{})
            enriched=dict(row)
            enriched.update(latest_analyzed=st.get('analyzed'),
                            last_run_check=st.get('last_run_check'),
                            last_error=st.get('last_error'),
                            last_attempt_at=st.get('last_attempt_at'),
                            run_phase=run_state_by_key.get(row['record_key'],'run_unknown'),
                            patient_id=sample_by_key.get(row['record_key'],{}).get('patient_id') or '')
            gen_current[row['record_key']]=normalize_snapshot(enriched)
        from gennext_fusion_selection_store import normalize_fusion_snapshot
        fus_states={r['record_key']:r for r in fusion_states}
        fusion_current={}
        for row in fusion_cur:
            rk=row['record_key'];state=fus_states.get(rk,{})
            enriched=dict(row)
            enriched.update(latest_analyzed=state.get('analyzed'),
                last_run_check=state.get('last_run_check'),
                last_error=state.get('last_error'),
                last_attempt_at=state.get('last_attempt_at'),
                run_phase=run_state_by_key.get(rk,'run_unknown'))
            fusion_current[rk]=normalize_fusion_snapshot(enriched,sample_by_key.get(rk))

        for r in tasks: tm[r['record_key']].append(r)
        for r in files: fm[r['record_key']].append(r)
        for r in docs: dm[r['record_key']].append(r)
        for r in queue: qm[r['record_key']].append(r)
        for r in reviews: rev[r['record_key']].append(r)
        # Same local checksum-valid TC can group records; conflicting DOB/sex prevents auto-grouping.
        tc_cases: dict[str, list] = collections.defaultdict(list)
        for c in cases:
            tc = ''.join(ch for ch in str(c.get('national_id') or '') if ch.isascii() and ch.isdigit())
            if StateDB.validate_national_id(tc): tc_cases[tc].append(c)
        conflicting = set()
        for tc, cs in tc_cases.items():
            births = {date_only(c.get('birth_date')) for c in cs} - {''}
            sexes = {StateDB.normalize_sex(c.get('sex')) for c in cs} - {''}
            if len(births) > 1 or len(sexes) > 1: conflicting.add(tc)
        records: dict[str, dict] = {}
        for s in samples:
            rk = s['record_key']; family = s.get('case_family') or s.get('patient_id') or rk
            c, t = cm.get(family, {}), sm.get(rk, {})
            tc = ''.join(ch for ch in str(c.get('national_id') or '') if ch.isascii() and ch.isdigit())
            valid = StateDB.validate_national_id(tc)
            conflict = tc in conflicting
            entity = self.token('tc:' + tc if valid and not conflict else 'archive_case:' + family)
            run_date = date_only(pm.get((s['pool_name'], s['run_uuid']), {}).get('run_date'))
            age = StateDB.age_from_birth_date(c.get('birth_date'), run_date)
            age_basis = 'calisma_tarihinde_hesaplanan' if age is not None else 'onceki_yas_kaydi'
            if age is None: age = number(c.get('age'))
            statuses = [t['status'] for t in tm[rk]]
            collection = 'Veri yok' if not statuses else 'Tamam' if all(x in ('done', 'not_available') for x in statuses) else 'Hata' if any(x in ('error','blocked') for x in statuses) else 'Bekliyor' if 'waiting' in statuses else 'Eksik'
            r = dict(s)
            r.update(entity_key=entity, patient_link_id=entity,
                     identity_basis='same_validated_local_tc' if valid and not conflict else 'case_family_only',
                     identity_conflict=conflict, full_name=c.get('full_name') or '',
                     diagnosis=c.get('diagnosis') or '', sex=StateDB.normalize_sex(c.get('sex')),
                     age=age, age_basis=age_basis, birth_date=c.get('birth_date') or '',
                     requesting_physician=c.get('requesting_physician') or '',
                     reporting_physician=c.get('reporting_physician') or '',
                     case_note=c.get('note') or '', block_source=t.get('block_source') or '', block_id=t.get('block_id') or '',
                     tumor_percentage=t.get('tumor_percentage') or '', sample_note=t.get('note') or '',
                     run_date=run_date, tasks=tm[rk], files=sorted(fm[rk], key=lambda x:x['id'], reverse=True),
                     documents=sorted(dm[rk], key=lambda x:x['version'], reverse=True),
                     igv=qm[rk], ai_reviews=sorted(rev[rk], key=lambda x:x['id'], reverse=True),
                     collection_status=collection, pool_group=StateDB.pool_group_name(s.get('pool_name')),
                     assay_result_state=arm.get(rk,{}).get('result_state','unset'),
                     assay_result_note=arm.get(rk,{}).get('note',''),
                     manual_report_close_status=manually_closed.get(rk,{}).get('status','none'),
                     manual_report_close_note=manually_closed.get(rk,{}).get('note',''),
                     manual_report_close_by=manually_closed.get(rk,{}).get('recorded_by',''),
                     manual_report_close_at=manually_closed.get(rk,{}).get('updated_at',''),
                     gennext_analyzed=gen_states.get(rk,{}).get('analyzed'),
                     gennext_selection=gen_current.get(rk),
                     gennext_selection_error=gen_states.get(rk,{}).get('last_error') or '',
                     gennext_fusion=fusion_current.get(rk),
                     gennext_fusion_state=(fusion_current.get(rk) or {}).get('result_status') or
                       (run_state_by_key.get(rk) if run_state_by_key.get(rk)!='ready' else
                        'awaiting_fusion_selection'),
                     gennext_fusion_analyzed=fus_states.get(rk,{}).get('analyzed'),
                     gennext_fusion_error=fus_states.get(rk,{}).get('last_error') or '',
                     gennext_selection_state=(gen_current.get(rk) or {}).get('result_status') or
                         (run_state_by_key.get(rk) if run_state_by_key.get(rk)!='ready' else
                          ('analysis_incomplete_unread' if gen_states.get(rk,{}).get('analyzed')==0 else 'awaiting_selection')))
            r['document_state'] = 'Onayli rapor' if any(d['document_status']=='user_confirmed' for d in r['documents']) else 'Taslak rapor' if r['documents'] else 'Rapor yok'
            records[rk] = r
        reported = latest_reported(history, decisions, observed)
        report_keys={(v['record_key'],canonical_key(v['variant_key'])) for v in reported}
        gennext_candidates=[]
        for rk, sn in gen_current.items():
            if rk not in records:continue
            verified=sn.get('currently_verified')
            def _row(v, is_selected):
                from datetime import datetime
                vaf=number(str(v.get('vaf') or '').replace('%',''))
                ad_dp=str(v.get('ad_dp') or '').split('/')
                return dict(record_key=rk,variant_key=canonical_key(v.get('variant_key')),
                            variant_type='SNV',gene=v.get('gene') or '',protein=v.get('protein') or '',
                            cdna=v.get('cdna') or '',transcript='',
                            change=v.get('protein') or v.get('cdna') or '',
                            af=(vaf/100 if vaf is not None else None),
                            ad=ad_dp[0] if len(ad_dp)==2 else '',
                            dp=ad_dp[1] if len(ad_dp)==2 else '',
                            filter=v.get('filter') or '',
                            tier=v.get('tier') or '',
                            source='gennext_selection',
                            source_label='GenNext Analyzed + Selection' if sn.get('result_status')=='selected_positive' else 'GenNext aday / kimlik veya analiz kontrolu',
                            source_text='GenNext Clinical SNVs > Selection; otomatik ve salt-okunur; imzali Enlil raporu degildir',
                            last_reported_at=sn.get('checked_at') or '',
                            first_reported_at=sn.get('checked_at') or '',
                            created_at=sn.get('checked_at') or '',
                            history_event_count=0,history_ids=[],
                            gennext_analyzed=bool(sn.get('analyzed')),currently_verified=bool(verified),
                            selection_checked=bool(is_selected))
            if sn.get('result_status')=='selected_positive':
                for v in sn['selected']:
                    key=(rk,canonical_key(v['variant_key']))
                    if key not in report_keys:
                        reported.append(_row(v,True));report_keys.add(key)
                    else:
                        for existing in reported:
                            if (existing['record_key'],canonical_key(existing['variant_key']))==key:
                                existing['gennext_selection_also_selected']=True;break
            for v in sn['candidates']:
                gennext_candidates.append(_row(v,False))
            if sn.get('result_status')!='selected_positive':
                # Checked boxes with unresolved MP identity or unfinished analysis
                # are technical candidates, never confirmed reported variants.
                for v in sn['selected']:
                    gennext_candidates.append(_row(v,True))
        obsm = {(r['record_key'], canonical_key(r['variant_key'])): r for r in observed}
        artifacts: dict[tuple, dict] = {}
        for d in decisions + [dict(q, status='artifact', note=q.get('manual_note')) for q in queue if q.get('manual_decision')=='artifact']:
            if d.get('status') != 'artifact': continue
            k = (d['record_key'], canonical_key(d['variant_key']))
            r = dict(obsm.get(k, {})); r.update(d); r['variant_key'] = k[1]
            r['variant_type'] = 'CNV' if k[1].startswith('CNV|') else 'SNV'
            artifacts[k] = r
        histkeys = {(r['record_key'],r['variant_key']) for r in reported}
        artkeys = set(artifacts)
        for dataset in (reported, observed, list(artifacts.values()),gennext_candidates):
            for v in dataset:
                s = records.get(v['record_key'], {})
                v['variant_key'] = canonical_key(v['variant_key'])
                v['entity_key'] = s.get('entity_key', '')
                v['recorded_as_reported'] = (v['record_key'], v['variant_key']) in histkeys
                v['manual_artifact'] = (v['record_key'], v['variant_key']) in artkeys
                v['variant_type'] = v.get('variant_type') or 'SNV'
                v['change'] = v.get('protein') or v.get('cdna') or v.get('change') or ''
        for v in reported:
            if v['manual_artifact']: v['decision_conflict'] = True
        histcounts = collections.Counter(v['record_key'] for v in reported)
        for rk,r in records.items(): r['reported_count'] = histcounts[rk]
        groups: dict[str, dict] = {}
        for rk, r in records.items():
            g = groups.setdefault(r['entity_key'], {'entity_key':r['entity_key'], 'records':[], 'names':set(), 'mps':set(), 'pools':set(), 'diagnoses':set(), 'types':set(), 'reported_count':0, 'identity_conflict':False})
            g['records'].append(rk);g['names'].add(r['full_name']);g['mps'].add(r['patient_id']);g['pools'].add(r['pool_group']);g['diagnoses'].add(r['diagnosis']);g['types'].add(r['sample_type']);g['reported_count']+=r['reported_count'];g['identity_conflict']|=r['identity_conflict']
        for g in groups.values():
            for field in ('names','mps','pools','diagnoses','types'):
                g[field] = sorted((x for x in g[field] if x), key=norm)
            g['display_name'] = ' / '.join(g['names']) or 'Isim girilmemis'
            g['records'].sort(key=lambda rk:(records[rk]['run_date'], records[rk]['patient_id']), reverse=True)
        return {'records':records, 'groups':groups, 'reported':reported, 'observed':observed,
                'gennext_candidates':gennext_candidates,
                'gennext_selections':gen_current,
                'gennext_selection_states':gen_states,
                'gennext_selection_history':gennext_changes,
                'gennext_fusion_selections':fusion_current,
                'gennext_fusion_states':fus_states,
                'gennext_fusion_history':fusion_changes,
                'artifacts':list(artifacts.values()), 'studies':studies, 'history':history,
                'indexed_records':{r['record_key'] for r in indexed if r.get('index_type')=='snv_oncogenic'},
                'captured_at':timestamp()}

    def set_assay_result(self, record_key: str, state: str, note: str = '') -> None:
        if state not in ('unset','negative','positive','indeterminate'):
            raise ValueError('Gecersiz rapor sonuc durumu.')
        if not self.db.get_sample(record_key):
            raise ValueError('Teknik ornek bulunamadi.')
        with self.db.cx:
            if state == 'unset':
                self.db.cx.execute('DELETE FROM archive_assay_results WHERE record_key=?',(record_key,))
            else:
                self.db.cx.execute("""INSERT INTO archive_assay_results(record_key,result_state,note,updated_at)
                  VALUES(?,?,?,?) ON CONFLICT(record_key) DO UPDATE SET
                  result_state=excluded.result_state,note=excluded.note,updated_at=excluded.updated_at""",
                  (record_key,state,note.strip(),timestamp()))

    def _latest_local_file(self, record_key: str, file_type: str) -> Path | None:
        rows=self.db.files_for_record(record_key,file_type)
        for r in rows:
            p=Path(r['path']);p=p if p.is_absolute() else self.base/p
            if p.is_file():return p
        return None

    @staticmethod
    def _pick(row: dict, *names):
        low={str(k).casefold():k for k in row}
        for name in names:
            k=low.get(str(name).casefold())
            if k is not None and str(row.get(k) or '').strip():return str(row.get(k) or '').strip()
        return ''

    def raw_variants(self, record_key: str) -> list[dict]:
        """Latest local raw SNV/CNV/Fusion calls for one technical sample, on demand.

        This is intentionally separate from the fast oncogenic archive index. It is
        used for human reconciliation/reporting, never to infer a negative result.
        """
        paths={t:self._latest_local_file(record_key,t) for t in ('snv','cnv','fusion')}
        sig=tuple((t,str(p or ''),p.stat().st_mtime_ns if p else 0,p.stat().st_size if p else 0) for t,p in paths.items())
        cached=self._raw_cache.get(record_key)
        if cached and cached[0]==sig:return [dict(x) for x in cached[1]]
        from report_parser import load_variant_rows
        snv,cnv=load_variant_rows(paths['snv'],paths['cnv'])
        out=[]
        for row in snv:
            out.append({'record_key':record_key,'variant_key':canonical_key(row.get('_variant_key')),'variant_type':'SNV',
              'gene':self._pick(row,'Gene_refGene','Gene','Gene.refGene'),'change':self._pick(row,'aaChange','HGVSp','Protein'),
              'cdna':self._pick(row,'txChange','HGVSc','cDNA'),'transcript':self._pick(row,'tx','Transcript','Tx'),
              'oncogenic':self._pick(row,'Oncogenic'),'filter':self._pick(row,'Filter'),'af':number(self._pick(row,'AF','VAF')),
              'ad':self._pick(row,'AD'),'dp':self._pick(row,'DP'),'feature':'','raw':row})
        for row in cnv:
            event=self._pick(row,'SVType');fold=self._pick(row,'FoldChange','Fold Change');probe=self._pick(row,'Probes','Probe Count')
            detail=' '.join(x for x in [event,('Fold Change '+fold) if fold else '',('Probe '+probe) if probe else ''] if x)
            out.append({'record_key':record_key,'variant_key':row.get('_variant_key') or '', 'variant_type':'CNV',
              'gene':self._pick(row,'GeneName','Gene'),'change':detail,'cdna':'','transcript':self._pick(row,'Tx','Transcript'),
              'oncogenic':self._pick(row,'Oncogenic'),'filter':'','af':None,'ad':self._pick(row,'AD'),'dp':self._pick(row,'DP'),
              'feature':detail,'raw':row})
        fp=paths['fusion']
        if fp:
            try:
                if fp.suffix.lower()=='.json':
                    obj=json.loads(fp.read_text(encoding='utf-8-sig',errors='replace'))
                    if isinstance(obj,dict): rows=obj.get('rows') or obj.get('data') or obj.get('results') or []
                    else: rows=obj if isinstance(obj,list) else []
                else:
                    with fp.open('r',encoding='utf-8-sig',errors='replace',newline='') as f:
                        sample=f.read(8192);f.seek(0)
                        delim='\t' if '\t' in sample else ','
                        rows=list(csv.DictReader(f,delimiter=delim))
                for i,row in enumerate(rows):
                    if not isinstance(row,dict):continue
                    g1=self._pick(row,'Gene1','LeftGene','Gene A','5p_gene');g2=self._pick(row,'Gene2','RightGene','Gene B','3p_gene')
                    fusion=self._pick(row,'Fusion','FusionName','Name','Event') or '-'.join(x for x in (g1,g2) if x)
                    if not fusion:continue
                    key='FUSION|'+re.sub(r'\s+','',fusion.upper())+'|'+str(i+1)
                    out.append({'record_key':record_key,'variant_key':key,'variant_type':'FUSION','gene':fusion,
                      'change':fusion,'cdna':'','transcript':'','oncogenic':self._pick(row,'Oncogenic'),'filter':self._pick(row,'Filter'),
                      'af':number(self._pick(row,'AF','VAF')),'ad':self._pick(row,'AD','Supporting Reads'),'dp':self._pick(row,'DP','Total Reads'),
                      'feature':fusion,'raw':dict(row)})
            except Exception:
                pass
        dedup={}
        for row in out:
            k=(row['record_key'],row['variant_key'])
            if k not in dedup:dedup[k]=row
        rows=list(dedup.values());self._raw_cache[record_key]=(sig,rows)
        return [dict(x) for x in rows]

    def technical_metrics(self, record_key: str) -> dict:
        s=self.db.get_sample(record_key) or {}
        qc=self._latest_local_file(record_key,'qc')
        sig=(str(qc or ''),qc.stat().st_mtime_ns if qc else 0,qc.stat().st_size if qc else 0,s.get('msi'),s.get('hrd'),s.get('tmb'))
        old=self._qc_cache.get(record_key)
        if old and old[0]==sig:return dict(old[1])
        result={'tmb':s.get('tmb') or '', 'msi':s.get('msi') or '', 'hrd':s.get('hrd') or '', 'mean_coverage_x':None}
        if qc:
            try:
                from ai_export import _read_qc_package
                parsed=_read_qc_package(qc,s.get('patient_id') or '',False)
                km=parsed.get('key_metrics') or {}
                result['mean_coverage_x']=km.get('mean_coverage_x')
            except Exception:
                pass
        self._qc_cache[record_key]=(sig,result);return dict(result)

    def add_raw_as_reported(self, record_key: str, raw: dict, source='archive_raw_picker', source_text='') -> int:
        typ=raw.get('variant_type') or 'SNV';key=raw.get('variant_key') or ''
        if not key:raise ValueError('Varyant anahtari bulunamadi.')
        feature=raw.get('feature') or (raw.get('change') if typ in ('CNV','FUSION') else '')
        row_id=self.db.add_reported_variant(record_key,key,typ,raw.get('gene') or '',raw.get('transcript') or '',raw.get('cdna') or '',
          raw.get('change') if typ=='SNV' else '',source=source,source_text=source_text,match_score=100,confirmed=True,feature=feature)
        self.db.set_variant_decision(record_key,key,'reported','Arsivde ham varyant listesinden manuel onay')
        return row_id

    def report_reconciliation(self, record_key: str, text: str) -> dict:
        from report_parser import extract_reported_items, match_reported_items
        parsed=extract_reported_items(text)
        raw=self.raw_variants(record_key)
        snv=[r['raw'] for r in raw if r['variant_type']=='SNV'];cnv=[r['raw'] for r in raw if r['variant_type']=='CNV']
        matches=match_reported_items(parsed.get('positives') or [],snv,cnv)
        reported={canonical_key(r['variant_key']) for r in self.db.reported_variant_history(record_key=record_key,limit=100000) if r['confirmed']}
        for m in matches:
            key=canonical_key(m.get('variant_key')) if m.get('variant_key') else ''
            m['canonical_variant_key']=key
            if key and key in reported:m['reconcile_state']='reported'
            elif key:m['reconcile_state']='matched_unconfirmed'
            else:m['reconcile_state']='not_found'
        return {'matches':matches,'negative_genes':parsed.get('negative_genes') or [],'report_hash':parsed.get('report_hash') or '',
                'reported_count':sum(m['reconcile_state']=='reported' for m in matches),
                'matched_unconfirmed_count':sum(m['reconcile_state']=='matched_unconfirmed' for m in matches),
                'not_found_count':sum(m['reconcile_state']=='not_found' for m in matches)}

    def save_document(self, record_key: str, text: str, title: str = '', status: str = 'draft') -> int:
        if not self.db.get_sample(record_key): raise ValueError('Teknik ornek bulunamadi.')
        if not text.strip(): raise ValueError('Bos rapor metni kaydedilemez.')
        if status not in ('draft','user_confirmed'): raise ValueError('Gecersiz rapor durumu.')
        digest = hashlib.sha256(text.encode('utf-8')).hexdigest()
        cx=self.db.cx
        with cx:
            old=cx.execute('SELECT id,content_sha256,document_status FROM archive_report_documents WHERE record_key=? ORDER BY version DESC LIMIT 1',(record_key,)).fetchone()
            if old and old['content_sha256']==digest and old['document_status']==status: return old['id']
            version=cx.execute('SELECT COALESCE(MAX(version),0)+1 FROM archive_report_documents WHERE record_key=?',(record_key,)).fetchone()[0]
            cur=cx.execute('INSERT INTO archive_report_documents(record_key,version,title,report_text,document_status,content_sha256,created_at) VALUES(?,?,?,?,?,?,?)',
                           (record_key,version,title.strip() or 'Rapor metni',text,status,digest,timestamp()))
        return cur.lastrowid

    def document(self, doc_id: int) -> dict:
        r=self.db.cx.execute('SELECT * FROM archive_report_documents WHERE id=?',(int(doc_id),)).fetchone()
        return dict(r) if r else {}

    def save_study(self, name: str, criteria: dict, dataset: str, record_keys: list[str], notes: str = '') -> int:
        if not name.strip(): raise ValueError('Calisma adi gereklidir.')
        if dataset not in ('reported','observed','artifacts','gennext_candidates'): raise ValueError('Gecersiz veri kumesi.')
        if not record_keys: raise ValueError('Bos kohort kaydedilemez.')
        keys=sorted(set(record_keys))
        if set(keys)-{r['record_key'] for r in self.db.samples()}: raise ValueError('Kohortta bilinmeyen teknik ornek var.')
        with self.db.cx:
            version=self.db.cx.execute('SELECT COALESCE(MAX(revision),0)+1 FROM archive_studies WHERE name=?',(name.strip(),)).fetchone()[0]
            cur=self.db.cx.execute('INSERT INTO archive_studies(name,revision,notes,criteria_json,dataset,created_at) VALUES(?,?,?,?,?,?)',(name.strip(),version,notes,json.dumps(criteria,ensure_ascii=False),dataset,timestamp()))
            sid=cur.lastrowid
            self.db.cx.executemany('INSERT INTO archive_study_members(study_id,record_key) VALUES(?,?)',[(sid,rk) for rk in keys])
        return sid

    def study(self, study_id: int) -> dict:
        r=self.db.cx.execute('SELECT * FROM archive_studies WHERE id=?',(int(study_id),)).fetchone()
        if not r: raise ValueError('Calisma bulunamadi.')
        d=dict(r);d['criteria']=json.loads(d['criteria_json']);d['record_keys']=[r[0] for r in self.db.cx.execute('SELECT record_key FROM archive_study_members WHERE study_id=? ORDER BY record_key',(int(study_id),))]
        return d


def filter_records(snap: dict, criteria: dict, frozen_keys: list[str] | None = None) -> list[dict]:
    result=[]
    frozen=set(frozen_keys) if frozen_keys is not None else None
    for rk,s in snap['records'].items():
        if frozen is not None:
            if rk in frozen: result.append(s)
            continue
        query=norm(criteria.get('query'))
        hay=norm(' '.join(str(s.get(k) or '') for k in ('full_name','patient_id','case_family','pool_name','diagnosis','block_id')))
        if query and not all(t in hay for t in query.split()): continue
        if criteria.get('diagnosis') and norm(criteria['diagnosis']) not in norm(s['diagnosis']): continue
        pool=criteria.get('pool')
        if pool and norm(pool)!='tumu' and pool!=s['pool_group']: continue
        typ=criteria.get('sample_type')
        if typ and norm(typ)!='tumu' and typ!=s['sample_type']: continue
        sex=criteria.get('sex')
        if sex and norm(sex)!='tumu' and norm(sex)!=norm(s['sex']): continue
        if criteria.get('exclude_reruns') and s.get('is_rerun'): continue
        assay=criteria.get('assay_result')
        if assay and norm(assay)!='tumu' and assay!=s.get('assay_result_state','unset'): continue
        doc=criteria.get('report_filter','all')
        if doc=='reported' and not s['reported_count']: continue
        if doc=='without_reported' and s['reported_count']: continue
        if doc=='with_text' and not (s['documents'] or s['case_note'].strip()): continue
        if doc=='confirmed_text' and not any(d['document_status']=='user_confirmed' for d in s['documents']): continue
        age=number(s.get('age'))
        amin,amax=number(criteria.get('age_min')),number(criteria.get('age_max'))
        if amin is not None and (age is None or age<amin): continue
        if amax is not None and (age is None or age>amax): continue
        dfrom,dto=date_only(criteria.get('date_from')),date_only(criteria.get('date_to'))
        if dfrom and (not s['run_date'] or s['run_date']<dfrom): continue
        if dto and (not s['run_date'] or s['run_date']>dto): continue
        result.append(s)
    return sorted(result,key=lambda s:(s['run_date'],s['patient_id']),reverse=True)


def select_variants(snap: dict, records: list[dict], dataset='reported', search='', class_filter='', pass_only=False) -> list[dict]:
    keys={s['record_key'] for s in records}
    words=norm(search).split()
    out=[]
    for v in snap.get(dataset, []):
        if v['record_key'] not in keys: continue
        hay=norm(' '.join(str(v.get(k) or '') for k in ('gene','protein','cdna','transcript','change','variant_key','source_text')))
        if words and not all(w in hay for w in words): continue
        if class_filter in ('Oncogenic','Likely Oncogenic') and v.get('oncogenic')!=class_filter: continue
        if pass_only and str(v.get('filter') or '').upper()!='PASS': continue
        out.append(v)
    return sorted(out,key=lambda v:(norm(v.get('gene')),v.get('variant_key',''),v.get('record_key','')))


def variant_catalogue(variants: list[dict], records: dict[str, dict]) -> list[dict]:
    grouped: dict[str,dict]={}
    for v in variants:
        key=v['variant_key'];s=records.get(v['record_key'],{})
        g=grouped.setdefault(key,{'variant_key':key,'gene':'','change':'','variant_type':v.get('variant_type','SNV'),
                                'patients':set(),'records':set(),'pools':set(),'reported_records':set(),'artifact_records':set(),'afs':[]})
        g['gene']=g['gene'] or v.get('gene','');g['change']=g['change'] or v.get('change','')
        g['records'].add(v['record_key']);g['patients'].add(v.get('entity_key'));g['pools'].add(s.get('pool_group',''))
        if v.get('recorded_as_reported'):g['reported_records'].add(v['record_key'])
        if v.get('manual_artifact'):g['artifact_records'].add(v['record_key'])
        af=number(v.get('af'))
        if af is not None:g['afs'].append(af)
    out=[]
    for g in grouped.values():
        g['patient_count']=len(g['patients']-{None,''});g['sample_count']=len(g['records']);g['pool_count']=len(g['pools']-{''})
        g['reported_count']=len(g['reported_records']);g['artifact_count']=len(g['artifact_records']);g['af_min']=min(g['afs']) if g['afs'] else None;g['af_max']=max(g['afs']) if g['afs'] else None
        out.append(g)
    return sorted(out,key=lambda v:(-v['patient_count'],norm(v['gene']),v['variant_key']))


def research_summary(snap: dict, records: list[dict], variants: list[dict], dataset: str) -> dict:
    """Count numerator and cohort separately. Missing calls are NOT negatives."""
    patients={s['entity_key'] for s in records}; keys={s['record_key'] for s in records}
    gene_patients: dict[str,set]=collections.defaultdict(set)
    gene_samples: dict[str,set]=collections.defaultdict(set)
    for v in variants:
        # Multi-gene annotations stay unexpanded; no invented independent events.
        gene=v.get('gene') or '(gen bilgisi yok)'
        gene_patients[gene].add(v['entity_key']);gene_samples[gene].add(v['record_key'])
    return {
        'dataset':dataset, 'cohort_patient_groups':len(patients), 'cohort_technical_records':len(keys),
        'patients_with_matching_records':len({v['entity_key'] for v in variants}),
        'technical_records_with_matching_variants':len({v['record_key'] for v in variants}),
        'variant_observations':len(variants), 'distinct_variant_keys':len({v['variant_key'] for v in variants}),
        'indexed_technical_records':len(keys & snap['indexed_records']),
        'no_matching_variant_record':len(keys-{v['record_key'] for v in variants}),
        'identity_warning_groups':len({s['entity_key'] for s in records if s['identity_conflict']}),
        'unknown_run_date':sum(not s['run_date'] for s in records),
        'gene_counts':[{'gene':g,'patient_groups':len(ps),'technical_records':len(gene_samples[g])} for g,ps in sorted(gene_patients.items(),key=lambda x:(-len(x[1]),norm(x[0])))],
        'limitations':[
            'Sayilar yerel arsivdeki kayitlari gosterir; populasyon prevalansi veya test duyarliligi degildir.',
            'Varyant kaydi olmamasi test-negatiflik, yeterli lokus kapsami veya raporlanmadi karari degildir.',
            'Hasta gruplama ayni yerel kontrol-basamagi gecerli TC veya vaka ailesine dayanir; isim benzerligiyle birlestirilmez.',
            'Gozlenen veri kumesi yalniz mevcut Oncogenic/Likely Oncogenic indeksidir; tum ham SNV/CNV/RNA cagrilari degildir.',
            'Genom derlemesi eski indekste yer almiyor. Anahtar esitligi farkli derlemelerde ayni biyolojik varyant oldugunu kanitlamaz.',
            'Ayni hastanin farkli tumor ve teknik calismalari ayri tutulur. Tekrar calismalar hasta sayisini arttirmaz.',
        ]
    }


def _csv_text(headers: list[str], rows: list[dict]) -> bytes:
    out=io.StringIO(newline='');writer=csv.DictWriter(out,fieldnames=headers,extrasaction='ignore',lineterminator='\r\n')
    writer.writeheader()
    for row in rows:
        safe={}
        for key in headers:
            v=row.get(key,'')
            if isinstance(v,str) and (v.lstrip().startswith(('=','+','-','@')) or v.startswith(('\t','\r','\n'))):v="'"+v
            safe[key]='' if v is None else v
        writer.writerow(safe)
    return out.getvalue().encode('utf-8-sig')


def export_study_zip(store: ArchiveStore, snap: dict, records: list[dict], variants: list[dict], criteria: dict, dataset: str, path: Path, study: dict | None=None) -> Path:
    """Masked structured research export; no raw report/note, TC, DOB or UUID."""
    from ai_export import _mask_person_name
    names=sorted({s.get(k) for s in snap['records'].values() for k in ('full_name','requesting_physician','reporting_physician') if s.get(k)},key=len,reverse=True)
    def scrub(text):
        s=str(text or '')
        for name in names:s=re.sub(re.escape(name),lambda m:_mask_person_name(m.group(0),keep_titles=True),s,flags=re.I)
        s=re.sub(r'(?<!\d)\d{11}(?!\d)','[KIMLIK CIKARILDI]',s)
        s=re.sub(r'\b\d{2}[./]\d{2}[./]\d{4}\b','[TARIH CIKARILDI]',s)
        return s
    rmap={s['record_key']:s for s in records}
    tech=[]
    for s in records:
        tech.append({'patient_link_id':s['entity_key'],'technical_id':store.token(s['record_key'],'TS-'),
                     'masked_name':_mask_person_name(s['full_name']),'mp':scrub(s['patient_id']), 'pool':scrub(s['pool_name']),
                     'sample_type':s['sample_type'],'run_date':s['run_date'],'diagnosis':scrub(s['diagnosis']),
                     'sex':s['sex'],'age_at_run_or_legacy':s['age'],'age_basis':s['age_basis'],
                     'block_source':s.get('block_source') or '','tumor_percentage':s['tumor_percentage'],'rerun':int(s.get('is_rerun') or 0),
                     'reported_observation_count':s['reported_count'],'report_text_state':s['document_state'],
                     'collection_status':s['collection_status'],'tmb_source':s.get('tmb'),'msi_source':s.get('msi'),'hrd_source':s.get('hrd')})
    vv=[]
    for v in variants:
        s=rmap[v['record_key']]
        vv.append({'patient_link_id':s['entity_key'],'technical_id':store.token(s['record_key'],'TS-'),
                   'mp':scrub(s['patient_id']),'pool':scrub(s['pool_name']),'variant_type':v.get('variant_type','SNV'),
                   'variant_key':scrub(v['variant_key']),'gene':scrub(v.get('gene')),'transcript':scrub(v.get('transcript')),
                   'cdna':scrub(v.get('cdna')),'protein_or_change':scrub(v.get('protein') or v.get('change')),
                   'af_fraction':v.get('af'),'ad_source':v.get('ad'),'dp_source':v.get('dp'),
                   'filter':scrub(v.get('filter')),'oncogenic_source':scrub(v.get('oncogenic')),
                   'reported_history':bool(v.get('recorded_as_reported')),'manual_artifact':bool(v.get('manual_artifact')),
                   'decision_conflict':bool(v.get('decision_conflict')),'last_recorded_at':v.get('last_reported_at','')})
    patients=[]
    for pt in sorted({s['entity_key'] for s in records}):
        ss=[s for s in records if s['entity_key']==pt]
        patients.append({'patient_link_id':pt,'masked_name':' / '.join(sorted({_mask_person_name(s['full_name']) for s in ss})),
                         'mp_codes':' ; '.join(sorted({scrub(s['patient_id']) for s in ss})), 'technical_record_count':len(ss),
                         'identity_basis':ss[0]['identity_basis'],'identity_conflict':any(s['identity_conflict'] for s in ss)})
    summary=research_summary(snap,records,variants,dataset)
    summary.update(exported_at=timestamp(),source_snapshot_at=snap['captured_at'],
                   criteria={k:scrub(v) if isinstance(v,str) else v for k,v in criteria.items() if k!='query'},
                   text_search_omitted_for_privacy=bool(criteria.get('query')),
                   saved_study_id=study.get('id') if study else None, cohort_membership_frozen=bool(study),
                   national_id_included=False,birth_date_included=False,raw_report_text_included=False,
                   result_values_frozen=False)
    payload={
        'PATIENTS.csv':_csv_text(['patient_link_id','masked_name','mp_codes','technical_record_count','identity_basis','identity_conflict'],patients),
        'TECHNICAL_SAMPLES.csv':_csv_text(list(tech[0]) if tech else ['patient_link_id','mp'],tech),
        'VARIANT_OBSERVATIONS.csv':_csv_text(list(vv[0]) if vv else ['patient_link_id','mp','variant_key'],vv),
        'SUMMARY.json':json.dumps(summary,ensure_ascii=False,indent=2).encode('utf-8'),
        'README.md':('# Yerel arastirma aktarimi\n\nHasta/doktor adlari maskelidir. TC, dogum tarihi, UUID, vaka notu ve rapor metni aktarilmaz.\n'
                     'Maskeleme anonimlestirme garantisi degildir; veriyi kurumun uygun ortaminda saklayin.\n\n'
                     'PATIENTS bir hasta grubunu, TECHNICAL_SAMPLES her teknik calismayi, VARIANT_OBSERVATIONS secili veri kumesindeki gozlemleri gosterir.\n'
                     'Kayit yok = negatif degildir. Rapor gecmisi ile mevcut artefakt karari celisebilir; iki bilgi korunur.\n'
                     'Oncogenic indeksi tum varyantlar degildir. Farkli genom derlemeleri otomatik eslestirilmez.\n'
                     'Kayitli kohort uyeligi sabittir; tekrar disari aktarim guncel sonuc/kararlari kullanir. Bu ZIP tarihli bir veri goruntusudur.\n').encode('utf-8')
    }
    manifest={'format':'ngs_archive_study_v1','files':[{'path':n,'sha256':hashlib.sha256(b).hexdigest(),'size_bytes':len(b)} for n,b in payload.items()]}
    payload['MANIFEST.json']=json.dumps(manifest,indent=2).encode('utf-8')
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(path.suffix+'.partial')
    try:
        with zipfile.ZipFile(tmp,'w',zipfile.ZIP_DEFLATED) as z:
            for n,b in payload.items():z.writestr(n,b)
        tmp.replace(path)
    finally:
        if tmp.exists():tmp.unlink()
    return path
