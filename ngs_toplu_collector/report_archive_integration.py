"""Attach a focused report archive without replacing existing application code."""
from __future__ import annotations

import copy

from report_archive_panel import ReportArchivePanel


def _form_changes(app):
    """Read Tk values on the UI thread; overlay only this panel's unsaved edits."""
    qc = getattr(getattr(app, 'patient_workspace', None), 'qc', None)
    if qc is None:
        return {}
    current = copy.deepcopy(getattr(qc, 'patient_form_data', {}))
    previous = getattr(qc, '_persisted_form_snapshot', {})
    key = getattr(qc, 'active_form_key', '')
    if key.startswith('TECH::') and not getattr(qc, '_loading_form', False):
        state = current.setdefault(key, {})
        widget = getattr(qc, 'diagnosis_comment_text', None)
        if widget is not None:
            state['diagnosis_comment'] = widget.get('1.0', 'end').strip()
        owner = getattr(qc, 'signature_owner_var', None)
        if owner is not None:
            state['signature_owner'] = owner.get()
    return {key: value for key, value in current.items()
            if key.startswith('TECH::') and value != previous.get(key)}


def attach_report_archive(app, base, db, cfg=None):
    if hasattr(app, 'report_archive'):
        return app.report_archive
    notebook = app.workspaces

    def open_record(record_key, destination):
        workspace = app.patient_workspace
        tab = 'standard' if destination == 'report' else destination
        if not workspace.select_record(record_key):
            return False
        if workspace.show_tab(tab) is False:
            return False
        notebook.select(workspace)
        if destination == 'report':
            workspace.qc.show_report_section('report')
        return True

    panel = ReportArchivePanel(
        notebook, base, db, form_provider=lambda: _form_changes(app),
        open_report=lambda rk: open_record(rk, 'report'),
        open_metadata=lambda rk: open_record(rk, 'metadata'),
        open_dual=lambda rk: open_record(rk, 'dual'),
    )
    app.report_archive = panel
    notebook.insert(1, panel, text='  2. Rapor Arşivi  ')
    for attribute, label in (
        ('archive', '  3. Arşiv / Araştırma  '),
        ('patient_workspace', '  4. Hasta / Raporlama  '),
        ('web_panel', '  5. Web / Koklayıcı  '),
    ):
        widget = getattr(app, attribute, None)
        if widget is not None:
            notebook.tab(widget, text=label)

    # Existing callbacks may retain old bound methods. Refresh on tab entry
    # therefore does not depend on wrapping the app's data_changed callback.
    def workspace_changed(event=None):
        if event is not None and event.widget is not notebook:
            return
        if notebook.select() == str(panel):
            panel.activate()

    notebook.bind('<<NotebookTabChanged>>', workspace_changed, add='+')
    return panel
