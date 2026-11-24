"""Two local GenNext identities. Secrets are encrypted with Windows DPAPI.
No secrets are sent to logging, diagnostics or AI exports.
"""
from __future__ import annotations
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path

class DATA_BLOB(ctypes.Structure):
    _fields_ = [('cbData', wintypes.DWORD), ('pbData', ctypes.POINTER(ctypes.c_byte))]


def _protect(data: bytes, decrypt=False):
    if os.name != 'nt':
        raise RuntimeError('Şifre kaydı yalnız Windows kullanıcı hesabında desteklenir.')
    kernel = ctypes.windll.kernel32
    crypto = ctypes.windll.crypt32
    buffer = ctypes.create_string_buffer(data)
    source = DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    result = DATA_BLOB()
    func = crypto.CryptUnprotectData if decrypt else crypto.CryptProtectData
    if decrypt:
        okay = func(ctypes.byref(source), None, None, None, None, 0, ctypes.byref(result))
    else:
        okay = func(ctypes.byref(source), 'NGS GenNext', None, None, None, 0, ctypes.byref(result))
    if not okay:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        kernel.LocalFree(result.pbData)


class AccountStore:
    def __init__(self, base):
        self.base = Path(base)
        self.path = self.base / 'DATA' / 'LOCAL_SECRETS' / 'gennext_accounts.dpapi'

    def read(self):
        if not self.path.exists():
            return {'default': '1', 'accounts': {'1': {'email': '', 'password': ''}, '2': {'email': '', 'password': ''}}}
        payload = json.loads(_protect(self.path.read_bytes(), decrypt=True).decode('utf-8'))
        if payload.get('default') not in ('1', '2'):
            raise ValueError('Varsayılan hesap kaydı geçersiz.')
        return payload

    def save(self, data):
        if data.get('default') not in ('1', '2'):
            raise ValueError('Varsayılan hesap 1 veya 2 olmalıdır.')
        accounts = data.get('accounts', {})
        for key in ('1', '2'):
            record = accounts.get(key, {})
            if not isinstance(record, dict):
                raise ValueError('Hesap bilgisi geçersiz.')
            if record.get('email') and '@' not in record['email']:
                raise ValueError('Geçersiz e-posta adresi (Hesap ' + key + ')')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        encrypted = _protect(json.dumps(data, ensure_ascii=False).encode('utf-8'))
        import tempfile
        fd, temp = tempfile.mkstemp(dir=self.path.parent, prefix='.account_')
        try:
            with os.fdopen(fd, 'wb') as f:
                f.write(encrypted)
            os.replace(temp, self.path)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)

    def profile(self, account_id):
        if account_id not in ('1', '2'):
            raise ValueError('Tanımsız hesap.')
        return self.base / 'LOCAL_BROWSER_PROFILES' / ('gennext_account_' + account_id)
