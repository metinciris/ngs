from pathlib import Path
import tempfile
import unittest

from db import StateDB, make_record_key
from collector import parse_runs_footer_progress
from gennext_selection_navigation import verified_run_link
from gennext_report_selection import SelectionReadError
from gennext_selection_store import run_phase

RUN1 = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
RUN2 = '11111111-2222-3333-4444-555555555555'
SAMPLE = '99999999-aaaa-bbbb-cccc-111111111111'
BASE = 'https://app.gennext.bio/runs?page=1&perPage=7'


class PoolMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = StateDB(Path(self.tmp.name) / 'state.sqlite3')
        self.db.upsert_pool('POOL 68 (DNA)', RUN1, 'DNA', '2026-11-11', 1, 'COMPLETED')
        self.db.upsert_pool('8CECE74B', RUN2, 'DNA', '', 1, '')
        self.rk = make_record_key(RUN2, SAMPLE, 'DNA')
        self.db.upsert_sample({
            'record_key': self.rk, 'sample_uuid': SAMPLE, 'patient_id': 'TEST-ONLY',
            'case_family': 'TEST-ONLY', 'is_rerun': 0, 'pool_name': '8CECE74B',
            'run_uuid': RUN2, 'sample_type': 'DNA', 'depth': '',
            'tmb': '', 'msi': '', 'hrd': '', 'run_status': '', 'sample_status': '', 'links': {},
        })
        self.db.set_meta('latest_run_uuid', RUN1)
        self.db.set_meta('latest_run_uuids', '["'+RUN1+'"]')

    def tearDown(self):
        self.db.cx.close()
        self.tmp.cleanup()

    def test_explicit_pool_alias_pins_only_exact_new_run(self):
        self.assertEqual(self.db.latest_pool()['run_uuid'], RUN1)
        self.assertEqual(self.db.set_run_alias_and_latest(RUN2, 'pool69'), 'POOL 69')
        self.assertEqual(self.db.latest_pool()['run_uuid'], RUN2)
        self.assertEqual([r['run_uuid'] for r in self.db.latest_pool_group()], [RUN2])
        self.assertEqual(self.db.latest_record_keys(), [self.rk])
        self.assertEqual(self.db.display_pool_name(RUN2,'8CECE74B'), 'POOL 69')
        self.assertEqual(self.db.display_pool_name(RUN1,'POOL 68 (DNA)'), 'POOL 68')
        self.assertEqual(self.db.get_sample(self.rk)['pool_name'], '8CECE74B')
        self.assertEqual(self.db.get_sample(self.rk)['run_uuid'], RUN2)

    def test_repeat_scan_does_not_clear_pin_or_alias(self):
        self.db.set_run_alias_and_latest(RUN2, 'POOL 69')
        self.db.set_meta('latest_run_uuid', RUN1)
        self.db.set_meta('latest_run_uuids', '["'+RUN1+'"]')
        self.assertEqual(self.db.latest_pool()['run_uuid'], RUN2)
        self.db.clear_manual_latest_run()
        self.assertEqual(self.db.latest_pool()['run_uuid'], RUN1)
        self.assertEqual(self.db.run_alias(RUN2),'POOL 69')

    def test_bad_alias_and_nonexistent_run_rejected(self):
        for name in ('POOL UNKNOWN', '69', 'POOL 0', 'POOL -1', 'POOL 999999'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.db.set_run_alias_and_latest(RUN2, name)
        with self.assertRaises(ValueError):
            self.db.set_run_alias_and_latest('nonexistent', 'POOL 69')
        self.assertFalse(self.db.manual_latest_run_uuid())

    def test_identical_alias_does_not_merge_run_or_samples(self):
        self.db.set_run_alias_and_latest(RUN2, 'POOL 68')
        self.assertEqual(len(self.db.latest_pool_group()), 1)
        self.assertEqual(self.db.latest_pool_group()[0]['run_uuid'], RUN2)
        self.assertEqual(self.db.latest_record_keys(), [self.rk])

    def test_missing_run_status_not_negative(self):
        self.assertEqual(run_phase(self.db.get_sample(self.rk)), 'run_unknown')

    def test_existing_archived_values_unchanged_by_alias(self):
        self.db.ensure_tasks(self.rk, ['run'])
        self.db.set_task(self.rk, 'run','done')
        self.db.set_run_alias_and_latest(RUN2,'POOL 69')
        self.assertEqual(self.db.task_status(self.rk,'run'),'done')


class PaginationTests(unittest.TestCase):
    def test_detects_8_of_20_not_complete(self):
        self.assertEqual(parse_runs_footer_progress('1-8 of 20 RUNS'), (1,8,20))
        self.assertLess(parse_runs_footer_progress('1-8 of 20 RUNS')[1], 20)
    def test_end_of_table(self):
        self.assertEqual(parse_runs_footer_progress('17-20 of 20 RUNS'), (17,20,20))
    def test_rejects_malformed_footer(self):
        self.assertIsNone(parse_runs_footer_progress('Next Page'))
        self.assertIsNone(parse_runs_footer_progress('9-22 of 20 RUNS'))


class VendorLinkMigrationTests(unittest.TestCase):
    def sample(self):
        return {'run_uuid':RUN2,'sample_uuid':SAMPLE}

    def test_old_beta_run_link_rebased_to_app_after_uuid_checks(self):
        raw = 'https://beta.gennext.bio/run-detail-somatic/' + RUN2 + '?sampleUuid=' + SAMPLE
        actual=verified_run_link(raw,self.sample(),BASE)
        self.assertEqual(actual,'https://app.gennext.bio/run-detail-somatic/'+RUN2+'?sampleUuid='+SAMPLE)

    def test_collector_run_task_uses_verified_app_link(self):
        from collector import BulkCollector
        with tempfile.TemporaryDirectory() as td:
            db=StateDB(Path(td) / 'tmp.sqlite3')
            collector=BulkCollector(Path(td),{'runs_url':BASE},db)
            old='https://beta.gennext.bio/run-detail-somatic/'+RUN2+'?sampleUuid='+SAMPLE
            result=collector._validated_source_url(self.sample(),'run',old)
            self.assertTrue(result.startswith('https://app.gennext.bio/'))
            self.assertIn(RUN2,result)
            db.cx.close()

    def test_same_origin_and_relative_supported(self):
        path='/run-detail-somatic/'+RUN2+'?sampleUuid='+SAMPLE
        self.assertEqual(verified_run_link(path,self.sample(),BASE),'https://app.gennext.bio'+path)

    def test_wrong_uuid_never_rebased(self):
        with self.assertRaises(SelectionReadError):
            verified_run_link('https://beta.gennext.bio/run-detail-somatic/'+RUN1+'?sampleUuid='+SAMPLE,
                              self.sample(),BASE)
        with self.assertRaises(SelectionReadError):
            verified_run_link('https://beta.gennext.bio/run-detail-somatic/'+RUN2+'?sampleUuid='+RUN1,
                              self.sample(),BASE)

    def test_unknown_server_and_unsafe_variants_blocked(self):
        suffix='/run-detail-somatic/'+RUN2+'?sampleUuid='+SAMPLE
        for prefix in ('https://not-gennext.example','http://beta.gennext.bio',
                       'https://beta.gennext.bio:443', 'https://beta.gennext.bio@evil.example',
                       '//evil.example'):
            with self.subTest(prefix=prefix), self.assertRaises(SelectionReadError):
                verified_run_link(prefix+suffix,self.sample(),BASE)
        with self.assertRaises(SelectionReadError):
            verified_run_link('https://beta.gennext.bio'+suffix+'&token=anything',self.sample(),BASE)

if __name__=='__main__':
    unittest.main(verbosity=2)
