"""Clinical completion and explicit report approval against real Tk widgets.

Run with xvfb-run when no display is available. All records and doctor names are
synthetic, storage is temporary, and clipboard output is intercepted.
"""
from __future__ import annotations

import sqlite3
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from tkinter import ttk
from unittest.mock import patch

from clinical_completion import ClinicalCompletionDialog
from db import StateDB
from patient_workspace import PatientWorkspace
from reporting_context import build_record_context, save_manual_fields


def add_record(db, number, sample_type='DNA', case='TEST-CASE'):
    return db.upsert_sample({
        'run_uuid':f'test-run-{number}', 'sample_uuid':f'test-sample-{number}',
        'patient_id':f'MP{9000 + number}-26', 'case_family':case,
        'pool_name':'POOL UI TEST', 'sample_type':sample_type,
    })


def complete_fields(block='TEST-A', tumor=70):
    return {'age_at_reference':58, 'sex':'Kadın',
            'reporting_diagnosis':'Sentetik test tanısı',
            'block_id':block, 'tumor_percentage':tumor}


def descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from descendants(child)


class ClinicalCompletionUITests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.base=Path(self.temp.name)
        self.db=StateDB(self.base/'test.sqlite3')
        self.first=add_record(self.db,1)
        self.second=add_record(self.db,2,'RNA')
        partial=complete_fields()
        partial.pop('block_id');partial.pop('tumor_percentage')
        save_manual_fields(self.db,self.first,partial)
        save_manual_fields(self.db,self.second,complete_fields('TEST-RNA',90))
        self.root=tk.Tk();self.root.withdraw()

    def tearDown(self):
        self.root.destroy();self.db.cx.close();self.temp.cleanup()

    def dialog(self, *keys, **kwargs):
        window=ClinicalCompletionDialog(self.root,self.db,
            [self.db.get_sample(key) for key in keys],**kwargs)
        self.root.update_idletasks()
        return window

    def widget_for(self, window, row, name):
        variable=str(row['vars'][name])
        return next(widget for widget in descendants(window)
                    if isinstance(widget,(ttk.Entry,ttk.Combobox))
                    and str(widget.cget('textvariable'))==variable)

    def test_missing_only_protects_known_values_and_skips_complete_records(self):
        window=self.dialog(self.first,self.second,missing_only=True)
        self.assertEqual(len(window.rows),1)
        row=window.rows[0]
        self.assertEqual(row['record']['record_key'],self.first)
        for name in ('age_at_reference','reporting_diagnosis'):
            widget=self.widget_for(window,row,name)
            self.assertEqual(str(widget.cget('state')),'readonly')
            before=row['vars'][name].get()
            widget.delete(0,'end');widget.insert(0,'unintended overwrite')
            self.assertEqual(row['vars'][name].get(),before)
        sex=self.widget_for(window,row,'sex')
        self.assertEqual(str(sex.cget('state')),'disabled')
        self.assertIn('Bilinmiyor',sex.cget('values'))
        for name in ('block_id','tumor_percentage'):
            self.assertEqual(str(self.widget_for(window,row,name).cget('state')),'normal')

    def test_two_open_dialogs_do_not_overwrite_a_newer_save(self):
        first=self.dialog(self.first)
        stale=self.dialog(self.first)
        first.rows[0]['vars']['tumor_percentage'].set('80')
        stale.rows[0]['vars']['tumor_percentage'].set('20')
        with patch('clinical_completion.messagebox.showerror') as error:
            self.assertTrue(first.save())
            self.assertFalse(stale.save())
        error.assert_called_once()
        self.assertEqual(build_record_context(self.db,self.first)['clinical']['tumor_percent'],80)
        self.assertEqual(stale.rows[0]['vars']['tumor_percentage'].get(),'20')
        self.assertIn('kaydedilmedi',stale.status.get().lower())
        self.assertFalse(stale.result)

    def test_invalid_later_row_does_not_commit_an_earlier_valid_row(self):
        window=self.dialog(self.first,self.second)
        before={key:build_record_context(self.db,key)['revision']
                for key in (self.first,self.second)}
        window.rows[0]['vars']['age_at_reference'].set('60')
        window.rows[1]['vars']['tumor_percentage'].set('101')
        with patch('clinical_completion.messagebox.showerror') as error:
            self.assertFalse(window.save())
        error.assert_called_once()
        for key in (self.first,self.second):
            self.assertEqual(build_record_context(self.db,key)['revision'],before[key])
        self.assertEqual(build_record_context(self.db,self.first)['clinical']['age'],58)

    def test_missing_fields_accept_zero_without_changing_the_other_record(self):
        other=build_record_context(self.db,self.second)
        window=self.dialog(self.first,self.second,missing_only=True,allow_technical=True)
        window.rows[0]['vars']['block_id'].set('TEST-NEW')
        window.rows[0]['vars']['tumor_percentage'].set('0')
        window.save_and_close()
        self.assertTrue(window.result)
        self.assertFalse(window.winfo_exists())
        context=build_record_context(self.db,self.first)
        self.assertEqual(context['clinical']['tumor_percent'],0)
        self.assertEqual(context['clinical']['block_no'],'TEST-NEW')
        self.assertEqual(context['missing_fields'],[])
        untouched=build_record_context(self.db,self.second)
        self.assertEqual(untouched['revision'],other['revision'])
        self.assertEqual(untouched['clinical'],other['clinical'])

    def test_incomplete_export_requires_explicit_technical_continue(self):
        window=self.dialog(self.first,missing_only=True,allow_technical=True)
        window.save_and_close()
        self.assertFalse(window.result)
        self.assertTrue(window.winfo_exists())
        self.assertIn('Eksik bilgiler',window.status.get())
        window.technical_continue()
        self.assertTrue(window.result)
        self.assertFalse(window.winfo_exists())
        self.assertIn('block_id',build_record_context(self.db,self.first)['missing_fields'])

    def test_storage_failure_keeps_edits_visible_and_reports_unsaved(self):
        window=self.dialog(self.first)
        window.rows[0]['vars']['block_id'].set('TEST-UNSAVED')
        with (patch('clinical_completion.save_manual_fields',
                   side_effect=sqlite3.OperationalError('synthetic storage failure')),
              patch('clinical_completion.messagebox.showerror') as error):
            self.assertFalse(window.save())
        error.assert_called_once()
        self.assertEqual(window.rows[0]['vars']['block_id'].get(),'TEST-UNSAVED')
        self.assertIn('kaydedilmedi',window.status.get().lower())
        self.assertIsNone(build_record_context(self.db,self.first)['clinical']['block_no'])


class ReportApprovalUITests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name)
        self.patches=[patch('qc_enlil.patient_data_file_path',return_value=self.base/'forms.json'),
                      patch('qc_enlil.settings_file_path',return_value=self.base/'settings.json')]
        for item in self.patches:item.start()
        self.db=StateDB(self.base/'test.sqlite3')
        self.record=add_record(self.db,10)
        save_manual_fields(self.db,self.record,complete_fields())
        self.root=tk.Tk();self.root.geometry('1500x890')
        self.workspace=PatientWorkspace(self.root,self.base,self.db)
        self.workspace.pack(fill='both',expand=True);self.root.update()
        self.assertTrue(self.workspace.select_record(self.record))
        self.qc=self.workspace.qc
        for index,var in enumerate(self.qc.doctor_name_vars):var.set(f'Test Dr {index+1}')
        self.qc.select_signature_owner(0)
        self.root.update_idletasks()
        self.assertFalse(self.workspace.metadata.is_dirty())

    def tearDown(self):
        self.root.destroy();self.db.cx.close()
        for item in reversed(self.patches):item.stop()
        self.temp.cleanup()

    def approval_state(self):
        return build_record_context(self.db,self.record)['approval'].get('state')

    def test_signed_report_copy_waits_for_explicit_review_approval(self):
        # Exercise the final class method: an earlier duplicate implementation
        # must not let the inherited copy handler bypass report approval.
        with (patch('patient_workspace.messagebox.showwarning') as warning,
             patch('patient_workspace.messagebox.askyesno',return_value=False) as confirm,
              patch.object(self.qc,'copy_text') as copy):
            self.qc.copy_standard_all()
            copy.assert_not_called()
            self.assertFalse(self.qc.approve_current_report())
            self.qc.copy_standard_all()
        confirm.assert_called_once();warning.assert_called();copy.assert_not_called()
        self.assertNotEqual(self.approval_state(),'approved')

    def test_approved_text_copies_but_an_edited_report_requires_new_approval(self):
        with patch('patient_workspace.messagebox.askyesno',return_value=True) as confirm:
            self.assertTrue(self.qc.approve_current_report())
        confirm.assert_called_once();self.assertEqual(self.approval_state(),'approved')
        reviewed=self.qc.full_standard_text()
        with patch.object(self.qc,'copy_text') as copy:
            self.qc.copy_standard_all()
        copy.assert_called_once_with(reviewed)
        self.qc.diagnosis_comment_text.insert('end','\nSentetik ek yorum: yeniden inceleme gerekir.')
        self.qc.refresh_standard_sections()
        with (patch('patient_workspace.messagebox.showwarning') as warning,
              patch.object(self.qc,'copy_text') as copy):
            self.qc.copy_standard_all()
        copy.assert_not_called();warning.assert_called_once()
        self.assertEqual(self.approval_state(),'needs_review')

    def test_changed_signer_cannot_reuse_another_signers_approval(self):
        with patch('patient_workspace.messagebox.askyesno',return_value=True):
            self.assertTrue(self.qc.approve_current_report())
        self.qc.select_signature_owner(1)
        with (patch('patient_workspace.messagebox.showwarning') as warning,
              patch.object(self.qc,'copy_text') as copy):
            self.qc.copy_standard_all()
        copy.assert_not_called();warning.assert_called_once()
        self.assertEqual(self.approval_state(),'needs_review')

    def test_unsaved_metadata_or_biomarkers_cannot_be_approved(self):
        self.workspace.metadata.vars['block_id'].set('TEST-UNSAVED')
        with (patch('patient_workspace.messagebox.showwarning') as warning,
              patch('patient_workspace.messagebox.askyesno') as confirm):
            self.assertFalse(self.qc.approve_current_report())
        confirm.assert_not_called();warning.assert_called_once()
        self.assertEqual(build_record_context(self.db,self.record)['clinical']['block_no'],'TEST-A')
        self.workspace.metadata.vars['block_id'].set('TEST-A')
        self.assertFalse(self.workspace.metadata.is_dirty())
        self.qc.hrd_score_var.set('24')
        with (patch('patient_workspace.messagebox.showwarning') as warning,
              patch('patient_workspace.messagebox.askyesno') as confirm):
            self.assertFalse(self.qc.approve_current_report())
        confirm.assert_not_called();warning.assert_called_once()
        self.assertIsNone(build_record_context(self.db,self.record)['biomarkers']['hrd_score'])
        self.assertNotEqual(self.approval_state(),'approved')

    def test_context_change_during_review_cannot_approve_the_previous_display(self):
        displayed=[]
        def concurrent_save(*args, **kwargs):
            # Another open editor saves after the confirmation opens. Its value
            # has not yet been loaded into this report's visible controls.
            displayed.append(self.qc.hrd_score_var.get())
            save_manual_fields(self.db,self.record,{'hrd_score':24})
            return True
        with (patch('patient_workspace.messagebox.askyesno',side_effect=concurrent_save),
              patch('patient_workspace.messagebox.showerror'),
              patch('patient_workspace.messagebox.showwarning')):
            self.assertFalse(self.qc.approve_current_report())
        self.assertEqual(displayed,[''])
        self.assertEqual(build_record_context(self.db,self.record)['biomarkers']['hrd_score'],24)
        self.assertNotEqual(self.approval_state(),'approved')

    def test_clean_but_outdated_metadata_must_reload_before_approval(self):
        save_manual_fields(self.db,self.record,
                           {'reporting_diagnosis':'Sentetik dış pencere düzeltmesi'})
        self.assertFalse(self.workspace.metadata.is_dirty())
        self.assertEqual(self.workspace.metadata.vars['diagnosis'].get(),'Sentetik test tanısı')
        with (patch('patient_workspace.messagebox.askyesno',return_value=True) as confirm,
              patch('patient_workspace.messagebox.showerror'),
              patch('patient_workspace.messagebox.showwarning') as warning):
            self.assertFalse(self.qc.approve_current_report())
        confirm.assert_not_called();warning.assert_called_once()
        self.assertEqual(build_record_context(self.db,self.record)['clinical']['tumor_diagnosis'],
                         'Sentetik dış pencere düzeltmesi')
        self.assertNotEqual(self.approval_state(),'approved')


if __name__=='__main__':
    unittest.main()
