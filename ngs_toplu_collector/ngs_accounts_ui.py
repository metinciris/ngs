"""Account selector. All UI writes occur on the main Tk thread."""
import tkinter as tk
from tkinter import ttk, messagebox
from ngs_accounts import AccountStore


def attach_account_manager(app, base):
    store = AccountStore(base)
    app.ngs_account_store = store
    try:
        data = store.read()
    except Exception as exc:
        messagebox.showerror('Hesap kasası', 'Kaydedilmiş hesaplar çözülemedi: ' + str(exc))
        data = {'default': '1', 'accounts': {'1': {}, '2': {}}}
    app.ngs_account_choice = tk.StringVar(value=data.get('default', '1'))

    bar = ttk.Frame(app.workframe)
    bar.pack(fill='x', padx=12, before=app.nb)
    ttk.Label(bar, text='GenNext hesabı:').pack(side='left', padx=(0, 6))
    picker_display = tk.StringVar()
    picker = ttk.Combobox(bar, textvariable=picker_display, state='readonly', width=32)
    picker.pack(side='left', padx=(0, 6))

    def labels():
        return {k: ('Hesap ' + k + ' · ' + (data['accounts'].get(k, {}).get('email') or 'Tanımlanmamış')) for k in ('1','2')}
    lookup = labels()
    picker['values'] = list(lookup.values())
    picker.set(lookup[app.ngs_account_choice.get()])

    def selected(_event=None):
        name = picker.get()
        for key, label in lookup.items():
            if name == label:
                app.ngs_account_choice.set(key)
                return
    picker.bind('<<ComboboxSelected>>', selected)

    def edit():
        nonlocal data, lookup
        dialog = tk.Toplevel(app)
        dialog.title('GenNext · iki güvenli hesap')
        dialog.geometry('580x380')
        dialog.transient(app)
        dialog.grab_set()
        default = tk.StringVar(value=data['default'])
        admin = tk.StringVar(value=str(data.get('admin', '1') or '1'))
        variables = {}
        ttk.Label(dialog, text='Şifreler Windows DPAPI ile kullanıcı hesabına bağlı şifrelenir.', wraplength=480).pack(padx=15, pady=(14, 10))
        for key in ('1', '2'):
            frame = ttk.LabelFrame(dialog, text='Hesap ' + key, padding=8)
            frame.pack(fill='x', padx=15, pady=4)
            email = tk.StringVar(value=data['accounts'].get(key, {}).get('email', ''))
            password = tk.StringVar(value=data['accounts'].get(key, {}).get('password', ''))
            ttk.Entry(frame, textvariable=email, width=34).pack(side='left', padx=(0, 8))
            ttk.Entry(frame, textvariable=password, width=21, show='●').pack(side='left')
            ttk.Radiobutton(frame, text='Yönetici', variable=admin, value=key).pack(side='right', padx=5)
            ttk.Radiobutton(frame, text='Varsayılan', variable=default, value=key).pack(side='right')
            variables[key] = (email, password)
        def save():
            nonlocal data, lookup
            proposed = {'default': default.get(), 'admin': admin.get(), 'accounts': {
                key: {'email': variables[key][0].get().strip(), 'password': variables[key][1].get()}
                for key in ('1','2')}}
            try:
                store.save(proposed)
            except Exception as exc:
                messagebox.showerror('Kayıt başarısız', str(exc), parent=dialog)
                return
            data = proposed
            lookup = labels()
            picker['values'] = list(lookup.values())
            app.ngs_account_choice.set(default.get())
            picker.set(lookup[default.get()])
            dialog.destroy()
        ttk.Button(dialog, text='Güvenli kaydet', command=save).pack(side='right', padx=15, pady=12)
        ttk.Button(dialog, text='İptal', command=dialog.destroy).pack(side='right')
    ttk.Button(bar, text='Hesapları yönet', command=edit).pack(side='left')

    old_start = app.start_browser
    def start_with_account():
        # The worker must own all browser operations; never switch an active context.
        key = app.ngs_account_choice.get()
        if key not in ('1','2'):
            messagebox.showwarning('GenNext hesabı', 'Önce hesap seçin.')
            return
        if getattr(app.browser, 'busy', False):
            messagebox.showwarning('Tarayıcı meşgul', 'Önce çalışan işi tamamlayın.')
            return
        try:
            account = store.read()['accounts'][key]
            if not account.get('email'):
                messagebox.showwarning('Hesap tanımsız', 'Önce hesap e-postasını kaydedin.')
                return
        except Exception as exc:
            messagebox.showerror('Hesap kasası', str(exc))
            return
        from config_loader import config_ready
        if not config_ready(app.browser.cfg):
            return old_start()
        app._submit('open_browser', account_id=key)

    # Existing buttons may hold previously bound callbacks, so bind fresh buttons.
    ttk.Button(bar, text='Seçili hesapla aç', command=start_with_account).pack(side='left', padx=5)
    def full_archive():
        key = app.ngs_account_choice.get()
        try:
            current = store.read()
            if key != current.get('admin', '1'):
                messagebox.showwarning('GenNext yönetici', 'Tam arşiv Selection taraması için yönetici olarak işaretlenen hesabı seçin.')
                return
            if not current.get('accounts', {}).get(key, {}).get('email'):
                messagebox.showwarning('GenNext yönetici', 'Yönetici hesabı tanımlı değil.')
                return
            if app.browser.busy:
                messagebox.showwarning('GenNext', 'Önce çalışan tarama tamamlanmalı.')
                return
            if not messagebox.askyesno('Tam arşiv Selection',
                    'Yerelde kayıtlı tüm DNA teknik çalışmalarının Selection ekranları, yönetici hesabıyla tek tek okunacak.\n'
                    'İşlem uzun sürebilir ve sunucu gecikmeleri olabilir. Devam edilsin mi?'):
                return
            app._submit('scan_archive_selections_admin', account_id=key)
        except Exception as exc:
            messagebox.showerror('GenNext hesap', str(exc))
    ttk.Button(bar, text='Yönetici: Tüm arşiv Selection', command=full_archive).pack(side='left', padx=6)
    return bar
