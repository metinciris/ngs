"""Local-only GenNext RUN selector for an editable POOL display name.

No GenNext mutation or patient records are changed. The chosen RUN is resolved
by its exact technical UUID; the caller performs the DB write after validation.
"""
from __future__ import annotations

import re
import tkinter as tk
from tkinter import ttk, messagebox

from patient_workspace_model import ordered_runs


def run_choices(db):
    """Show RUNs even when there is no selected sample in a hidden table."""
    cards, hint = ordered_runs(db)
    seen = set()
    choices = []
    for card in cards:
        run_uuid = str(card.get('run_uuid') or '').strip()
        if not run_uuid or run_uuid in seen:
            continue
        # Verify that this run exists locally. Never invent a RUN from a label.
        if not db.cx.execute('SELECT 1 FROM pools WHERE run_uuid=? LIMIT 1', (run_uuid,)).fetchone():
            continue
        seen.add(run_uuid)
        source_name = str(card.get('pool_name') or '').strip() or '(isim yok)'
        alias = db.run_alias(run_uuid)
        choices.append({
            'run_uuid': run_uuid,
            'source_name': source_name,
            'display_name': alias or db.pool_group_name(source_name) or source_name,
            'alias': alias,
            'sample_count': len(card.get('rows') or []),
            'run_type': str(card.get('run_type') or ''),
            'run_date': str(card.get('run_date') or ''),
            'from_last_scan': bool(card.get('source_order')),
            'unnamed': not db.is_real_pool_name(source_name),
        })
    return choices, hint


def propose_pool_name(db):
    """Suggest the next free-looking local number, without asserting run identity."""
    nums = []
    for row in db.cx.execute('SELECT run_uuid,pool_name FROM pools').fetchall():
        for label in (str(row['pool_name'] or ''), db.run_alias(row['run_uuid'])):
            m = re.fullmatch(r'POOL\s*([0-9]+)', db.pool_group_name(label), re.I)
            if m:
                nums.append(int(m.group(1)))
    return 'POOL ' + str(max(nums, default=0) + 1)


class PoolAliasDialog(tk.Toplevel):
    def __init__(self, parent, db, preferred_run=''):
        super().__init__(parent)
        self.db = db
        self.result = None
        self.title('POOL Adlandır / Son RUN Seç')
        self.geometry('1020x610')
        self.minsize(790, 480)
        self.transient(parent)
        self.protocol('WM_DELETE_WINDOW', self._cancel)
        self._choices, order_hint = run_choices(db)
        self._by_uuid = {row['run_uuid']: row for row in self._choices}
        self._initial_pool = propose_pool_name(db)
        self._label_var = tk.StringVar(value=self._initial_pool)
        self._selected_info = tk.StringVar(value='RUN listesinden bir satır seçin. Hasta/örnek seçimi gerekmiyor.')

        shell = ttk.Frame(self, padding=16)
        shell.pack(fill='both', expand=True)
        ttk.Label(shell, text='1. GenNext RUN seçin → 2. Yerel POOL adı verin',
                  font=('Segoe UI', 15, 'bold')).pack(anchor='w', pady=(0, 6))
        ttk.Label(shell,
                  text='Teknik örnekler tablosunu bulmanız gerekmez. Bu liste, RUNS taramasıyla '
                       'kaydedilmiş çalışmalardır. Kodla görünen isimsiz RUN’ı (ör. 8cece74b) seçin.',
                  foreground='#234664', wraplength=900, justify='left').pack(anchor='w', pady=(0, 8))
        ttk.Label(shell, text=order_hint, foreground='#64748b').pack(anchor='w', pady=(0, 8))

        box = ttk.Frame(shell)
        box.pack(fill='both', expand=True)
        box.rowconfigure(0, weight=1)
        box.columnconfigure(0, weight=1)
        columns = ('raw', 'local', 'type', 'count', 'date', 'id')
        self.tree = ttk.Treeview(box, columns=columns, show='headings', height=12,
                                 selectmode='browse')
        heads = [('raw', 'GenNext çalışma adı', 215),
                 ('local', 'Yerel POOL etiketi', 155),
                 ('type', 'Tür', 85),
                 ('count', 'Örnek', 65),
                 ('date', 'Tarih', 125),
                 ('id', 'RUN kimliği (kısa)', 130)]
        for key, label, width in heads:
            self.tree.heading(key, text=label, anchor='w')
            self.tree.column(key, width=width, minwidth=55, anchor='w', stretch=key in ('raw','local'))
        self.tree.tag_configure('unnamed', background='#fff0cf')
        self.tree.tag_configure('manual', background='#dbf2e5')
        for row in self._choices:
            tags = ('manual',) if row['alias'] else ('unnamed',) if row['unnamed'] else ()
            self.tree.insert('', 'end', iid=row['run_uuid'], tags=tags,
                             values=(row['source_name'], row['display_name'], row['run_type'],
                                     row['sample_count'], row['run_date'],
                                     row['run_uuid'][:8]+'…'+row['run_uuid'][-4:]))
        ys = ttk.Scrollbar(box, orient='vertical', command=self.tree.yview)
        xs = ttk.Scrollbar(box, orient='horizontal', command=self.tree.xview)
        self.tree.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        self.tree.grid(row=0, column=0, sticky='nsew')
        ys.grid(row=0, column=1, sticky='ns')
        xs.grid(row=1, column=0, sticky='ew')
        self.tree.bind('<<TreeviewSelect>>', self._selection_changed)
        self.tree.bind('<Double-1>', self._selection_changed)

        ttk.Label(shell, textvariable=self._selected_info, foreground='#16436b',
                  wraplength=930).pack(anchor='w', pady=(12, 8))
        frm = ttk.Frame(shell)
        frm.pack(fill='x')
        ttk.Label(frm, text='Yerel ad:').pack(side='left', padx=(0, 8))
        self.name_entry = ttk.Entry(frm, textvariable=self._label_var, width=24,
                                    font=('Segoe UI', 12, 'bold'))
        self.name_entry.pack(side='left')
        ttk.Label(frm, text='(GenNext üzerindeki adı değiştirmez)',
                  foreground='#64748b').pack(side='left', padx=12)
        actions = ttk.Frame(shell)
        actions.pack(fill='x', pady=(16, 0))
        ttk.Button(actions, text='İptal', command=self._cancel).pack(side='right', padx=(10, 0))
        ttk.Button(actions, text='Kaydet ve bu RUN’ı SON POOL seç',
                   command=self._save).pack(side='right')

        initial = preferred_run if preferred_run in self._by_uuid else ''
        if not initial:
            unnamed = [r['run_uuid'] for r in self._choices if r['unnamed'] and not r['alias']]
            if len(unnamed) == 1:
                initial = unnamed[0]
        if not initial:
            manual = db.manual_latest_run_uuid()
            if manual in self._by_uuid:
                initial = manual
        if initial:
            self.tree.selection_set(initial)
            self.tree.see(initial)
            self._selection_changed()
        self.after_idle(self._position)

    def _position(self):
        try:
            self.update_idletasks()
            x = self.master.winfo_rootx() + max(0, (self.master.winfo_width() - self.winfo_width()) // 2)
            y = self.master.winfo_rooty() + max(0, (self.master.winfo_height() - self.winfo_height()) // 2)
            self.geometry('+' + str(x) + '+' + str(y))
            self.grab_set()
            self.focus_set()
        except tk.TclError:
            pass

    def _selection_changed(self, event=None):
        pick = self.tree.selection()
        if not pick:
            self._selected_info.set('Önce RUN listesinden bir satır seçin.')
            return
        row = self._by_uuid[pick[0]]
        self._selected_info.set(
            'Seçilen RUN: ' + row['source_name'] + ' | RUN kimliği: ' + row['run_uuid'] +
            ' | ' + str(row['sample_count']) + ' teknik örnek'
        )
        self._label_var.set(row['alias'] or
                            (self.db.pool_group_name(row['source_name'])
                             if not row['unnamed'] else self._initial_pool))

    def _save(self):
        selection = self.tree.selection()
        if not selection:
            messagebox.showwarning('RUN seçilmedi',
                'POOL 69 adını vereceğiniz RUN satırını seçin.', parent=self)
            return
        run_uuid = selection[0]
        row = self._by_uuid[run_uuid]
        label = self.db.pool_group_name(self._label_var.get())
        if not re.fullmatch(r'POOL [1-9][0-9]{0,4}', label):
            messagebox.showerror('Geçersiz POOL adı',
                'Yalnız POOL 69 biçiminde, 1–99999 arası isim kullanın.', parent=self)
            return
        if self.db.is_real_pool_name(row['source_name']) and row['display_name'] != label:
            if not messagebox.askyesno('Var olan isim değişecek',
                row['source_name']+' olarak görünen RUN’a '+label+
                ' yerel etiketi verilecek. Doğru RUN olduğuna emin misiniz?', parent=self):
                return
        self.result = (run_uuid, label)
        self.destroy()

    def _cancel(self):
        self.result = None
        self.destroy()


def ask_run_alias(parent, db, preferred_run=''):
    """Return (exact run UUID, canonical local label), or None on cancellation."""
    options, _ = run_choices(db)
    if not options:
        messagebox.showwarning('RUN bulunamadı',
            'Yerel RUN listesi boş. 1. POOL / YZ > Ana / Toplama ekranında RUNS’u Tara çalıştırın.',
            parent=parent)
        return None
    dialog = PoolAliasDialog(parent, db, preferred_run)
    parent.wait_window(dialog)
    return dialog.result
