from pathlib import Path
import tempfile
import unittest
from db import StateDB

class LatestPoolSelectionTests(unittest.TestCase):
    def test_numeric_fallback_beats_older_dated_pool_when_new_date_missing(self):
        with tempfile.TemporaryDirectory() as td:
            db=StateDB(Path(td)/'x.sqlite3')
            db.upsert_pool('POOL 59 (DNA)','run59d','DNA','2026-09-16T08:00:00',1,'COMPLETED')
            db.upsert_pool('POOL 59 (RNA)','run59r','RNA','2026-09-16T08:10:00',1,'COMPLETED')
            db.upsert_pool('POOL 60 (DNA)','run60d','DNA','',1,'COMPLETED')
            self.assertEqual(db.pool_group_name(db.latest_pool()['pool_name']),'POOL 60')
            db.cx.close()

    def test_unspaced_live_pool_labels_group_and_select_correctly(self):
        # GenNext 18.09.2026 main Runs page used `POOL60 (DNA)` (no space).
        with tempfile.TemporaryDirectory() as td:
            db=StateDB(Path(td)/'x.sqlite3')
            db.upsert_pool('POOL 59 (DNA)','run59d','DNA','2026-09-10T12:00:00',1,'COMPLETED')
            db.upsert_pool('POOL 59 (RNA)','run59r','RNA','2026-09-10T13:43:00',1,'COMPLETED')
            db.upsert_pool('POOL60 (DNA)','run60d','DNA','2026-09-17T12:49:00',1,'COMPLETED')
            db.set_meta('latest_pool_name','POOL 60')
            self.assertTrue(db.is_real_pool_name('POOL60 (DNA)'))
            self.assertTrue(db.is_real_pool_name('POOL 60 (RNA)'))
            self.assertFalse(db.is_real_pool_name('POOL_UNKNOWN'))
            self.assertEqual(db.pool_group_name('POOL60 (DNA)'),'POOL 60')
            grp=db.latest_pool_group()
            self.assertEqual([x['run_uuid'] for x in grp],['run60d'])
            self.assertEqual(db.pool_group_name(db.latest_pool()['pool_name']),'POOL 60')
            db.cx.close()

    def test_unspaced_dna_rna_labels_share_one_group(self):
        with tempfile.TemporaryDirectory() as td:
            db=StateDB(Path(td)/'x.sqlite3')
            db.upsert_pool('POOL60 (DNA)','run60d','DNA','2026-09-17T12:49:00',1,'COMPLETED')
            db.upsert_pool('POOL60 (RNA)','run60r','RNA','2026-09-17T12:50:00',1,'COMPLETED')
            db.set_meta('latest_pool_name','POOL 60')
            grp=db.latest_pool_group()
            self.assertEqual({x['run_uuid'] for x in grp},{'run60d','run60r'})
            self.assertEqual({db.pool_group_name(x['pool_name']) for x in grp},{'POOL 60'})
            db.cx.close()

    def test_recent_runs_scan_order_is_authoritative(self):
        with tempfile.TemporaryDirectory() as td:
            db=StateDB(Path(td)/'x.sqlite3')
            db.upsert_pool('POOL 59 (DNA)','run59d','DNA','2026-09-18T10:00:00',1,'COMPLETED')
            db.upsert_pool('POOL 60 (DNA)','run60d','DNA','',1,'COMPLETED')
            db.set_meta('latest_pool_name','POOL 60')
            grp=db.latest_pool_group()
            self.assertEqual({db.pool_group_name(x['pool_name']) for x in grp},{'POOL 60'})
            self.assertEqual(db.pool_group_name(db.latest_pool()['pool_name']),'POOL 60')
            db.cx.close()

    def test_exact_latest_run_uuid_beats_manual_duplicate_labels(self):
        with tempfile.TemporaryDirectory() as td:
            db=StateDB(Path(td)/'x.sqlite3')
            db.upsert_pool('My Study','old-run','DNA','2026-09-10T10:00:00',2,'COMPLETED')
            db.upsert_pool('My Study','new-run','DNA','',1,'COMPLETED')
            db.set_meta('latest_run_uuid','new-run')
            db.set_meta('latest_run_uuids','["new-run"]')
            grp=db.latest_pool_group()
            self.assertEqual([x['run_uuid'] for x in grp],['new-run'])
            self.assertEqual(db.latest_pool()['run_uuid'],'new-run')
            db.cx.close()

    def test_processing_status_does_not_preemptively_mark_tasks_waiting(self):
        with tempfile.TemporaryDirectory() as td:
            db=StateDB(Path(td)/'x.sqlite3')
            rk='run::sample::DNA'
            db.upsert_pool('Manual Label','run','DNA','',1,'COMPLETED')
            db.upsert_sample({'record_key':rk,'sample_uuid':'sample','patient_id':'DEMO-1','case_family':'DEMO-1','is_rerun':0,'pool_name':'Manual Label','run_uuid':'run','sample_type':'DNA','depth':'','tmb':'','msi':'','hrd':'','run_status':'COMPLETED','sample_status':'PROCESSING','links':{}})
            db.ensure_tasks(rk,['run','snv','cnv','qc'])
            db.sync_task_readiness(rk,['run','snv','cnv','qc'],'COMPLETED','PROCESSING')
            self.assertTrue(all(db.task_status(rk,tp)=='pending' for tp in ('run','snv','cnv','qc')))
            db.set_task_waiting(rk,'qc','QC page still loading')
            db.sync_task_readiness(rk,['run','snv','cnv','qc'],'COMPLETED','PROCESSING')
            self.assertEqual(db.task_status(rk,'qc'),'waiting')
            db.cx.close()

    def test_collector_processing_badge_is_soft_but_failed_is_blocked(self):
        from collector import BulkCollector
        with tempfile.TemporaryDirectory() as td:
            db=StateDB(Path(td)/'x.sqlite3')
            c=BulkCollector(Path(td),{},db)
            ok,status,reason=c._upstream_ready({'run_status':'COMPLETED','sample_status':'PROCESSING'})
            self.assertTrue(ok);self.assertEqual(status,'')
            self.assertEqual(c._upstream_hint({'run_status':'COMPLETED','sample_status':'PROCESSING'}),'sample PROCESSING')
            ok,status,reason=c._upstream_ready({'run_status':'FAILED','sample_status':'PROCESSING'})
            self.assertFalse(ok);self.assertEqual(status,'blocked');self.assertIn('FAILED',reason)
            db.cx.close()

if __name__=='__main__':
    unittest.main(verbosity=2)
