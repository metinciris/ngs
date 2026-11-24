"""Synthetic-only tests for the visibly accessible GenNext RUN alias selector."""
from __future__ import annotations

import json
import tempfile
import tkinter as tk
import unittest
from pathlib import Path

from db import StateDB, make_record_key
from pool_alias_ui import PoolAliasDialog, propose_pool_name, run_choices
from patient_workspace_model import ORDER_KEY

RUN_NEW='8cece74b-3333-4444-5555-123456789abc'
RUN_OLD='ebd93d75-1111-4444-5555-123456789abc'
SMPL='f2345678-abcd-1234-1234-123456789abc'


class SyntheticPoolAliasTests(unittest.TestCase):
    def setUp(self):
        self.td=tempfile.TemporaryDirectory()
        self.db=StateDB(Path(self.td.name) / 'synthetic.sqlite3')
        self.db.upsert_pool('POOL 68 (DNA)',RUN_OLD,'DNA','2026-11-11',2,'Completed')
        self.db.upsert_pool('8cece74b',RUN_NEW,'DNA','2026-11-22',1,'Completed')
        self.key=make_record_key(RUN_NEW,SMPL,'DNA')
        self.db.upsert_sample({'record_key':self.key,'sample_uuid':SMPL,'patient_id':'SYNTHETIC',
            'case_family':'SYNTHETIC','pool_name':'8cece74b','run_uuid':RUN_NEW,
            'sample_type':'DNA','links':{},'run_status':'Completed','sample_status':'Analyzed'})
        self.db.set_meta(ORDER_KEY,json.dumps({'complete':False, 'runs':[
            {'run_uuid':RUN_NEW,'page':1,'position':0,'record_keys':[self.key]},
            {'run_uuid':RUN_OLD,'page':1,'position':1,'record_keys':[]}]}))

    def tearDown(self):
        self.db.cx.close()
        self.td.cleanup()

    def test_raw_unnamed_run_visible_without_sample_selection(self):
        runs,hint=run_choices(self.db)
        self.assertEqual([x['run_uuid'] for x in runs],[RUN_NEW,RUN_OLD])
        self.assertEqual(runs[0]['source_name'],'8cece74b')
        self.assertTrue(runs[0]['unnamed'])
        self.assertEqual(runs[0]['sample_count'],1)
        self.assertIn('kısmi',hint)

    def test_source_pool68_stays_distinct_and_unchanged(self):
        self.db.set_run_alias_and_latest(RUN_NEW,'POOL 69')
        cards,_=run_choices(self.db)
        self.assertEqual(cards[0]['display_name'],'POOL 69')
        self.assertEqual(cards[0]['source_name'],'8cece74b')
        self.assertEqual(cards[1]['display_name'],'POOL 68')
        self.assertEqual(self.db.get_sample(self.key)['pool_name'],'8cece74b')
        self.assertEqual(self.db.latest_record_keys(),[self.key])

    def test_next_label_considers_existing_aliases(self):
        self.assertEqual(propose_pool_name(self.db),'POOL 69')
        self.db.set_run_alias_and_latest(RUN_NEW,'POOL 69')
        self.assertEqual(propose_pool_name(self.db),'POOL 70')

    def test_existing_and_identical_pool_labels_never_merge_runs(self):
        self.db.set_run_alias_and_latest(RUN_NEW,'POOL 68')
        result=self.db.latest_scan_group()
        self.assertEqual(len(result),1)
        self.assertEqual(result[0]['run_uuid'],RUN_NEW)


class VisiblePoolAliasDialogTests(SyntheticPoolAliasTests):
    """Needs xvfb on Linux; Windows has its own normal display."""
    def setUp(self):
        super().setUp()
        self.root=tk.Tk()
        self.root.withdraw()

    def tearDown(self):
        self.root.destroy()
        super().tearDown()

    def test_dialog_lists_runs_and_prefills_only_unnamed_run(self):
        dlg=PoolAliasDialog(self.root,self.db)
        self.root.update()
        self.assertEqual(dlg.tree.get_children(),(RUN_NEW,RUN_OLD))
        self.assertEqual(dlg.tree.selection(),(RUN_NEW,))
        self.assertEqual(dlg._label_var.get(),'POOL 69')
        self.assertIn('8cece74b',dlg._selected_info.get())
        dlg._save()
        self.assertEqual(dlg.result,(RUN_NEW,'POOL 69'))
        self.assertEqual(self.db.run_alias(RUN_NEW),'')  # caller performs the actual write

    def test_dialog_preferred_run_from_pool_header_is_honoured(self):
        dlg=PoolAliasDialog(self.root,self.db,preferred_run=RUN_OLD)
        self.root.update()
        self.assertEqual(dlg.tree.selection(),(RUN_OLD,))
        self.assertEqual(dlg._label_var.get(),'POOL 68')
        dlg._cancel()
        self.assertIsNone(dlg.result)


if __name__=='__main__':
    unittest.main(verbosity=2)
