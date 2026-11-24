"""v0.4.73 Enlil Raporlama tabbed UI and record isolation tests (xvfb-run)."""
from __future__ import annotations
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch
from db import StateDB
from patient_workspace import PatientWorkspace

class ReportingTabsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.base=Path(self.temp.name)
        self.form_path=self.base/'forms.json'
        self.path_patch=patch('qc_enlil.patient_data_file_path',return_value=self.form_path)
        self.path_patch.start()
        self.db=StateDB(self.base/'data.sqlite3')
        self.old=self.db.upsert_sample(dict(run_uuid='run1',sample_uuid='s1',patient_id='MP8-26',
            case_family='MP8-26',pool_name='POOL 64',sample_type='DNA'))
        self.current=self.db.upsert_sample(dict(run_uuid='run2',sample_uuid='s2',patient_id='MP8-26',
            case_family='MP8-26',pool_name='POOL 65',sample_type='RNA'))
        self.other=self.db.upsert_sample(dict(run_uuid='run3',sample_uuid='s3',patient_id='MP9-26',
            case_family='MP9-26',pool_name='POOL 65',sample_type='DNA'))
        self.db.set_case_metadata('MP8-26',diagnosis='İlk adenokarsinom')
        self.db.set_sample_metadata(self.old,block_source='Dış',block_id='A1',tumor_percentage='70')
        self.root=tk.Tk();self.root.geometry('1500x890')
        self.ws=PatientWorkspace(self.root,self.base,self.db)
        self.ws.pack(fill='both',expand=True);self.root.update()
        self.qc=self.ws.qc

    def tearDown(self):
        try:self.root.destroy()
        except tk.TclError:pass
        self.db.cx.close();self.path_patch.stop();self.temp.cleanup()

    def test_six_tabs_and_persistent_context(self):
        labels=[self.qc.report_editor_tabs.tab(tab,'text') for tab in self.qc.report_editor_tabs.tabs()]
        self.assertEqual(len(labels),6)
        self.assertIn('Mutasyon',labels[0]);self.assertIn('HRD',labels[1]);self.assertIn('Geçmiş',labels[-1])
        self.ws.select_record(self.old)
        self.assertIn('İlk adenokarsinom',self.qc.report_summary_vars['diagnosis'].get())
        self.assertIn('A1',self.qc.report_summary_vars['block'].get())
        self.qc.show_report_section('biomarkers')
        self.assertEqual(self.qc.report_editor_tabs.select(),str(self.qc.report_page_hosts['biomarkers']))
        self.assertTrue(self.qc.standard_output.winfo_exists())

    def test_shared_quick_fields_save_to_exact_record(self):
        self.ws.select_record(self.old)
        self.ws.metadata.vars['diagnosis'].set('Yeni tanı')
        self.ws.metadata.vars['block_id'].set('A2')
        self.assertTrue(self.qc.save_metadata_from_report())
        self.assertEqual(self.db.get_case_metadata('MP8-26')['diagnosis'],'Yeni tanı')
        self.assertEqual(self.db.get_sample_metadata(self.old)['block_id'],'A2')
        self.assertEqual(self.db.get_sample_metadata(self.current)['block_id'],'')
        self.assertEqual(self.db.get_case_metadata('MP9-26')['diagnosis'],'')

    def test_general_macro_stain_overrides_are_exact_record_only(self):
        self.ws.select_record(self.old)
        widget=self.qc.section_editors['macro']
        widget.delete('1.0','end');widget.insert('1.0','Bu kayda özel blok metni')
        self.qc._section_edited('macro')
        self.qc.save_report_draft()
        self.assertIn('Bu kayda özel blok metni',self.qc.full_standard_text())
        self.assertEqual(self.qc.section_copy_text('macro'),'Bu kayda özel blok metni')
        self.assertTrue(self.ws.select_record(self.other))
        self.assertNotIn('Bu kayda özel blok metni',self.qc.full_standard_text())
        self.assertTrue(self.ws.select_record(self.old))
        self.assertEqual(self.qc.section_copy_text('macro'),'Bu kayda özel blok metni')
        self.qc.reset_report_section('macro')
        self.assertIn('A1',self.qc.section_copy_text('macro'))

    def test_prior_dna_rna_records_list_by_study(self):
        self.ws.select_record(self.current)
        self.qc.refresh_report_history()
        tree=self.qc.report_history_views['block']
        self.assertGreaterEqual(len(tree.get_children()),1)
        values=tree.item(tree.get_children()[0],'values')
        self.assertTrue(any('POOL 64' in str(v) for v in values))
        self.assertTrue(any('A1' in str(v) for v in values))

    def test_unsigned_report_copy_remains_blocked(self):
        self.ws.select_record(self.old)
        with patch('qc_enlil.messagebox.showwarning') as warning,patch.object(self.qc,'copy_text') as copy:
            self.qc.copy_standard_all()
        warning.assert_called();copy.assert_not_called()

    def test_saved_draft_survives_new_window(self):
        self.ws.select_record(self.old)
        self.qc.report_section_overrides['general']='Test teknik çalışma yöntem metni'
        self.assertTrue(self.qc.save_report_draft())
        self.assertIn('Test teknik çalışma yöntem metni',self.form_path.read_text('utf-8'))
        self.ws.select_record(self.other)
        self.ws.select_record(self.old)
        self.assertEqual(self.qc.section_copy_text('general'),'Test teknik çalışma yöntem metni')

if __name__=='__main__':unittest.main()
