from __future__ import annotations
import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from db import StateDB
from reporting_context import (build_record_context, save_manual_fields,
    ingest_clinical_observation, normalize_field, ContextConflict,
    save_metadata_form, approve_report, report_is_approved)


class ReportingContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'state.sqlite3'
        self.db=StateDB(self.path)
        self.rk=self.db.upsert_sample(dict(run_uuid='run',sample_uuid='one',sample_type='DNA',patient_id='MP1-26',case_family='MP1-26',pool_name='POOL 1',run_status='COMPLETED'))
        self.other=self.db.upsert_sample(dict(run_uuid='run',sample_uuid='two',sample_type='DNA',patient_id='MP2-26',case_family='MP2-26',pool_name='POOL 1',run_status='COMPLETED'))

    def tearDown(self):
        self.db.cx.close();self.tmp.cleanup()

    def observe(self, values, date='2026-10-18T10:25:32+03:00', rk=None):
        key=rk or self.rk;sample=self.db.get_sample(key)
        return {'record':{'record_key':key,'run_uuid':sample['run_uuid'],'sample_uuid':sample['sample_uuid'],'sample_type':sample['sample_type']},
                'source_observation':{'identity_verified':True,'kind':'live_dom','observed_at':date},
                'fields':{k:{'value':v,'raw':str(v),'availability':'observed' if v is not None else 'missing'} for k,v in values.items()}}

    def test_percentage_normalization_and_limits(self):
        for v in ('%50','50%','50','50,0',50):self.assertEqual(normalize_field('tumor_percentage',v),50)
        self.assertEqual(normalize_field('tumor_percentage','0'),0)
        self.assertEqual(normalize_field('tumor_percentage','100'),100)
        self.assertIsNone(normalize_field('tumor_percentage',''))
        for v in ('-1','101','NaN','Infinity',True):
            with self.assertRaises(ValueError):normalize_field('tumor_percentage',v)

    def test_record_identity_rejects_cross_sample(self):
        observation=self.observe({'hrd_score':24})
        with self.assertRaises(ValueError):ingest_clinical_observation(self.db,self.other,observation)
        self.assertIsNone(build_record_context(self.db,self.other)['biomarkers']['hrd_score'])

    def test_valid_source_only_updates_one_record(self):
        ingest_clinical_observation(self.db,self.rk,self.observe({'hrd_score':24,'hrd_loh':4,'hrd_tai':9,'hrd_lst':11}))
        self.assertEqual(build_record_context(self.db,self.rk)['biomarkers']['hrd_score'],24)
        self.assertIsNone(build_record_context(self.db,self.other)['biomarkers']['hrd_score'])

    def test_manual_override_survives_source_change_and_restart(self):
        save_manual_fields(self.db,self.rk,{'hrd_score':25,'tumor_percentage':80})
        ingest_clinical_observation(self.db,self.rk,self.observe({'hrd_score':24,'tumor_percentage':50}))
        ctx=build_record_context(self.db,self.rk)
        self.assertEqual(ctx['biomarkers']['hrd_score'],25)
        self.assertTrue(ctx['field_sources']['hrd_score']['source_conflict'])
        self.db.cx.close();self.db=StateDB(self.path)
        self.assertEqual(build_record_context(self.db,self.rk)['clinical']['tumor_percent'],80)
        self.db.upsert_sample(dict(run_uuid='run',sample_uuid='one',sample_type='DNA',patient_id='MP1-26',pool_name='POOL 1',hrd='3'))
        self.assertEqual(self.db.get_sample(self.rk)['hrd'],'25')

    def test_missing_na_zero_and_stale_observation(self):
        obs=self.observe({'hrd_score':None,'hrd_loh':8,'hrd_lst':3})
        obs['fields']['hrd_score']['availability']='not_available'
        ingest_clinical_observation(self.db,self.rk,obs)
        self.assertIsNone(build_record_context(self.db,self.rk)['biomarkers']['hrd_score'])
        ingest_clinical_observation(self.db,self.rk,self.observe({'hrd_score':0},'2026-10-19T00:00:00+03:00'))
        self.assertEqual(build_record_context(self.db,self.rk)['biomarkers']['hrd_score'],0)
        ingest_clinical_observation(self.db,self.rk,self.observe({'hrd_score':8},'2026-10-17T00:00:00+03:00'))
        self.assertEqual(build_record_context(self.db,self.rk)['biomarkers']['hrd_score'],0)
        ingest_clinical_observation(self.db,self.rk,self.observe({'hrd_score':None},'2026-10-20T00:00:00+03:00'))
        self.assertEqual(build_record_context(self.db,self.rk)['biomarkers']['hrd_score'],0)

    def test_deliberate_blank_and_return_to_web(self):
        ingest_clinical_observation(self.db,self.rk,self.observe({'source_tumor_type':'Specific diagnosis'}))
        save_manual_fields(self.db,self.rk,{'reporting_diagnosis':None})
        self.assertIsNone(build_record_context(self.db,self.rk)['clinical']['tumor_diagnosis'])
        save_manual_fields(self.db,self.rk,{},use_source=('reporting_diagnosis',))
        self.assertEqual(build_record_context(self.db,self.rk)['clinical']['tumor_diagnosis'],'Specific diagnosis')

    def test_source_diagnosis_is_a_proposal_until_manual_recording(self):
        ingest_clinical_observation(self.db,self.rk,self.observe({'source_tumor_type':'Specific type','source_clinical_diagnosis':'Broad diagnosis'}))
        context=build_record_context(self.db,self.rk)
        self.assertEqual(context['reporting_diagnosis_status'],'source_proposal')
        self.assertFalse(context['field_sources']['reporting_diagnosis']['pathologist_verified'])
        self.assertFalse(context['field_sources']['reporting_diagnosis']['manual_override']['present'])
        save_manual_fields(self.db,self.rk,{'reporting_diagnosis':'User diagnosis'})
        context=build_record_context(self.db,self.rk)
        self.assertEqual(context['clinical']['tumor_diagnosis'],'User diagnosis')
        self.assertEqual(context['clinical']['clinical_diagnosis'],'Broad diagnosis')

    def test_null_observed_and_invalid_timestamp_do_not_erase_source(self):
        ingest_clinical_observation(self.db,self.rk,self.observe({'hrd_score':24}))
        for value,stamp in ((None,'2026-10-19T00:00:00Z'),(5,'not-a-date'),(5,'2026-10-20T00:00:00')):
            obs=self.observe({'hrd_score':value},stamp);obs['fields']['hrd_score']['availability']='observed'
            ingest_clinical_observation(self.db,self.rk,obs)
            self.assertEqual(build_record_context(self.db,self.rk)['biomarkers']['hrd_score'],24)

    def test_conflicting_simultaneous_source_stays_visible_without_overwrite(self):
        ingest_clinical_observation(self.db,self.rk,self.observe({'hrd_score':24}))
        ingest_clinical_observation(self.db,self.rk,self.observe({'hrd_score':25}))
        field=build_record_context(self.db,self.rk)['field_sources']['hrd_score']
        self.assertEqual(field['effective_value'],24)
        self.assertTrue(field['source_conflict'])
        self.assertEqual(field['last_observation']['value'],25)

    def test_new_run_does_not_inherit_saved_case_diagnosis(self):
        build_record_context(self.db,self.rk)
        save_metadata_form(self.db,self.rk,{'diagnosis':'Prior block diagnosis'},{'block_id':'A1','tumor_percentage':80})
        later=self.db.upsert_sample(dict(run_uuid='later',sample_uuid='newblock',sample_type='DNA',patient_id='MP1-26',case_family='MP1-26',pool_name='POOL 2'))
        context=build_record_context(self.db,later)
        self.assertIsNone(context['clinical']['tumor_diagnosis'])
        self.assertIsNone(context['clinical']['block_no'])
        self.assertIsNone(context['clinical']['tumor_percent'])

    def test_profile_failure_rolls_back_clinical_editor(self):
        context=build_record_context(self.db,self.rk)
        with patch('reporting_context._save_patient_profile',side_effect=sqlite3.OperationalError('profile disk write failed')):
            with self.assertRaises(sqlite3.OperationalError):
                save_metadata_form(self.db,self.rk,{'diagnosis':'Not committed'},{'block_id':'A1'},expected_revision=context['revision'])
        self.assertEqual(self.db.get_case_metadata('MP1-26')['diagnosis'],'')
        self.assertIsNone(build_record_context(self.db,self.rk)['clinical']['block_no'])

    def test_old_window_cannot_erase_later_edit(self):
        revision=build_record_context(self.db,self.rk)['revision']
        save_manual_fields(self.db,self.rk,{'tumor_percentage':80},expected_revision=revision)
        with self.assertRaises(ContextConflict):save_manual_fields(self.db,self.rk,{'tumor_percentage':20},expected_revision=revision)
        self.assertEqual(build_record_context(self.db,self.rk)['clinical']['tumor_percent'],80)

    def test_atomic_metadata_form_rejects_invalid_percentage(self):
        before=self.db.get_case_metadata('MP1-26')
        with self.assertRaises(ValueError):save_metadata_form(self.db,self.rk,{'diagnosis':'Must not save'}, {'block_id':'B1','tumor_percentage':'101'})
        self.assertEqual(self.db.get_case_metadata('MP1-26'),before)
        self.assertEqual(self.db.get_sample_metadata(self.rk)['block_id'],'')

    def test_database_failure_rolls_back_fields_and_case(self):
        ctx=build_record_context(self.db,self.rk)
        with patch('reporting_context._mirror',side_effect=sqlite3.OperationalError('disk full')):
            with self.assertRaises(sqlite3.OperationalError):
                save_metadata_form(self.db,self.rk,{'diagnosis':'Not committed'}, {'block_id':'B1','tumor_percentage':50},expected_revision=ctx['revision'])
        self.assertEqual(self.db.get_case_metadata('MP1-26')['diagnosis'],'')
        self.assertIsNone(build_record_context(self.db,self.rk)['clinical']['tumor_diagnosis'])

    def test_different_block_does_not_inherit_tumor_fraction(self):
        rna=self.db.upsert_sample(dict(run_uuid='run',sample_uuid='rna',sample_type='RNA',patient_id='MP1-26',case_family='MP1-26',pool_name='POOL 1'))
        save_manual_fields(self.db,self.rk,{'block_id':'A1','tumor_percentage':80})
        self.assertEqual(self.db.technical_metadata_for_record(rna)['tumor_percentage'],'')
        self.assertIsNone(build_record_context(self.db,rna)['clinical']['tumor_percent'])

    def test_approval_bound_to_text_signer_and_values(self):
        save_manual_fields(self.db,self.rk,{'reporting_diagnosis':'Diagnosis','age_at_reference':65,'sex':'Bilinmiyor','block_id':'A1','tumor_percentage':100})
        with self.assertRaises(ValueError):approve_report(self.db,self.rk,'report','')
        approve_report(self.db,self.rk,'report','Dr. A')
        self.assertTrue(report_is_approved(self.db,self.rk,'report','Dr. A'))
        self.assertFalse(report_is_approved(self.db,self.rk,'changed report','Dr. A'))
        approve_report(self.db,self.rk,'report','Dr. A')
        save_manual_fields(self.db,self.rk,{'hrd_score':24})
        self.assertFalse(report_is_approved(self.db,self.rk,'report','Dr. A'))

    def test_full_feedback_clinical_import_and_repeat(self):
        fixture=Path(__file__).resolve().parents[1]/'feedback'/'AI_REVIEW_RETURN.json'
        if not fixture.exists():self.skipTest('Private POOL66 acceptance fixture is not part of clean source.')
        data=json.loads(fixture.read_text(encoding='utf-8-sig'))
        for pr in data['patient_reviews']:
            run,sample,kind=pr['record_key'].split('::')
            self.db.upsert_sample(dict(run_uuid=run,sample_uuid=sample,sample_type=kind,patient_id=pr['patient_id'],case_family=pr['patient_id'],pool_name='POOL 66'))
        first=self.db.import_ai_review(data);second=self.db.import_ai_review(data)
        self.assertEqual(first['clinical_records'],8);self.assertTrue(second['duplicate_import'])
        for pr,pct in zip(data['patient_reviews'],(50,80,60,30,30,90,90,100)):
            c=build_record_context(self.db,pr['record_key'])
            self.assertEqual(c['clinical']['tumor_percent'],pct);self.assertEqual(c['missing_fields'],['block_id'])


if __name__=='__main__':unittest.main()
