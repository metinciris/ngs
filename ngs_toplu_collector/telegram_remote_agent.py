from __future__ import annotations

import argparse
import base64
import ctypes
from ctypes import wintypes
import getpass
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from remote_control import app_status_fresh, queue_remote_command, read_app_status, remote_root, _atomic_json, read_json

# NGS_PC_SHUTDOWN_V0_4_67
# Hidden Telegram commands. Intentionally NOT added to BotFather menu or /yardim.
_PC_SHUTDOWN_ALLOWED_IDS = {1196743952}
_PC_SHUTDOWN_PENDING_UNTIL = 0.0


def _pc_shutdown_int(value):
    try:
        if value is None or isinstance(value, bool):
            return None
        x = int(str(value).strip())
        return x if x > 0 else None
    except Exception:
        return None


def _pc_shutdown_command(text):
    try:
        token = str(text or "").strip().split()[0]
        token = token.split("@", 1)[0].lower()
        if token and not token.startswith("/"):
            token = "/" + token
        return token
    except Exception:
        return ""


def _pc_shutdown_extract(scope):
    """Extract command/user/chat from the already-authorized handler locals."""
    text = None
    user_id = None
    chat_id = None

    if not isinstance(scope, dict):
        return text, user_id, chat_id

    # The current agent normalizes commands before handle_authorized(), so
    # command/cmd may be 'pckapat' rather than '/pckapat'. Accept both.
    for key in ("command", "cmd", "text", "message_text"):
        value = scope.get(key)
        if isinstance(value, str) and value.strip():
            text = value.strip()
            break

    for key in ("user_id", "from_user_id", "telegram_user_id", "effective_user_id"):
        value = scope.get(key)
        try:
            if value is not None:
                user_id = int(value)
                break
        except Exception:
            pass

    for key in ("chat_id", "telegram_chat_id", "effective_chat_id"):
        value = scope.get(key)
        try:
            if value is not None:
                chat_id = int(value)
                break
        except Exception:
            pass

    return text, user_id, chat_id


def _pc_shutdown_find_token():
    import json as _json
    import os as _os
    import re as _re
    from pathlib import Path as _Path

    pat = _re.compile(r"^\\d{5,}:[A-Za-z0-9_-]{20,}$")
    for key in ("TELEGRAM_BOT_TOKEN", "NGS_TELEGRAM_BOT_TOKEN", "BOT_TOKEN", "TELEGRAM_TOKEN"):
        value = str(_os.environ.get(key, "")).strip()
        if pat.match(value):
            return value

    # Existing module globals.
    for key, value in list(globals().items()):
        if "token" in str(key).lower() and isinstance(value, str) and pat.match(value.strip()):
            return value.strip()

    def search_obj(obj, depth=0):
        if depth > 3:
            return None
        if isinstance(obj, dict):
            for k, v in obj.items():
                if "token" in str(k).lower() and isinstance(v, str) and pat.match(v.strip()):
                    return v.strip()
            for v in obj.values():
                if isinstance(v, (dict, list, tuple)):
                    got = search_obj(v, depth + 1)
                    if got:
                        return got
        elif isinstance(obj, (list, tuple)):
            for v in obj:
                got = search_obj(v, depth + 1)
                if got:
                    return got
        return None

    for value in list(globals().values()):
        if isinstance(value, dict):
            got = search_obj(value)
            if got:
                return got

    # Adjacent local config files.
    try:
        base = _Path(__file__).resolve().parent
        for filename in ("config.local.json", "config.json", "telegram_config.json"):
            p = base / filename
            if not p.exists():
                continue
            try:
                obj = _json.loads(p.read_text(encoding="utf-8-sig"))
                got = search_obj(obj)
                if got:
                    return got
            except Exception:
                pass
        envp = base / ".env"
        if envp.exists():
            for line in envp.read_text(encoding="utf-8-sig", errors="ignore").splitlines():
                if "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if "token" in k.lower() and pat.match(v.strip().strip('"\'')):
                    return v.strip().strip('"\'')
    except Exception:
        pass
    return None


def _pc_shutdown_reply(chat_id, text):
    try:
        import json as _json
        import urllib.request as _urlreq
        token = _pc_shutdown_find_token()
        if not token or not chat_id:
            return False
        payload = _json.dumps({"chat_id": int(chat_id), "text": str(text)}).encode("utf-8")
        req = _urlreq.Request(
            "https://api.telegram.org/bot" + token + "/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with _urlreq.urlopen(req, timeout=6) as resp:
            resp.read(1)
        return True
    except Exception:
        return False


def _pc_shutdown_start():
    import subprocess as _sp
    flags = getattr(_sp, "CREATE_NO_WINDOW", 0)
    _sp.Popen(["shutdown", "/s", "/t", "30"], creationflags=flags)


def _pc_shutdown_abort():
    import subprocess as _sp
    flags = getattr(_sp, "CREATE_NO_WINDOW", 0)
    _sp.Popen(["shutdown", "/a"], creationflags=flags)


def _pc_handle_hidden_command(scope):
    global _PC_SHUTDOWN_PENDING_UNTIL
    import time as _time

    text, user_id, chat_id = _pc_shutdown_extract(scope)
    cmd = _pc_shutdown_command(text)
    hidden = {"/pckapat", "/pckapat_onay", "/pciptal"}
    if cmd not in hidden:
        return None

    # This hook is deliberately installed inside handle_authorized().
    # If that handler exposes a user id, keep the explicit allow-list check too.
    if user_id is not None and user_id not in _PC_SHUTDOWN_ALLOWED_IDS:
        return "Yetkisiz komut."

    if cmd == "/pckapat":
        _PC_SHUTDOWN_PENDING_UNTIL = _time.time() + 60.0
        return (
            "PC kapatma istegi alindi. 60 saniye icinde /pckapat_onay yazin. "
            "Vazgecmek icin /pciptal."
        )

    if cmd == "/pckapat_onay":
        if _time.time() > float(_PC_SHUTDOWN_PENDING_UNTIL or 0):
            _PC_SHUTDOWN_PENDING_UNTIL = 0.0
            return "Kapatma onay suresi doldu. Once /pckapat yazin."
        _PC_SHUTDOWN_PENDING_UNTIL = 0.0
        _pc_shutdown_start()
        return "PC 30 saniye icinde kapatilacak. Iptal icin /pciptal."

    if cmd == "/pciptal":
        _PC_SHUTDOWN_PENDING_UNTIL = 0.0
        try:
            _pc_shutdown_abort()
            return "Bekleyen PC kapatma islemi iptal edildi."
        except Exception:
            return "Bekleyen bir Windows kapatma islemi bulunamadi."

    return None

BASE = Path(__file__).resolve().parent
ROOT = remote_root(BASE)
CONFIG_PATH = ROOT / 'telegram_config.json'
TOKEN_PATH = ROOT / 'telegram_token.dpapi'
STATE_PATH = ROOT / 'telegram_state.json'
LOCK_PATH = ROOT / 'telegram_agent.lock'
STOP_PATH = ROOT / 'telegram_agent.stop'
AGENT_STATUS_PATH = ROOT / 'telegram_agent_status.json'
STARTUP_NAME = 'NGS_Telegram_Remote.cmd'

MENU = {
    'keyboard': [
        ['▶ RUNS Tara', '✅ Son POOL Tamamla'],
        ['🧩 Eksikleri Tamamla', '🔄 Durum'],
        ['🖥 NGS Aç', '⛔ Tarama Durdur'],
        ['♻ NGS Restart'],
    ],
    'resize_keyboard': True,
    'is_persistent': True,
}

COMMANDS = [
    {'command': 'durum', 'description': 'NGS ve RUNS tarama durumunu goster'},
    {'command': 'ngs_ac', 'description': 'NGS programini ac'},
    {'command': 'runs_tara', 'description': 'GenNext RUNS taramasini baslat'},
    {'command': 'son_pool', 'description': 'En yeni POOLu bul ve eksiklerini tamamla'},
    {'command': 'eksikleri_tamamla', 'description': 'Son bilinen calismanin yalniz eksiklerini tamamla'},
    {'command': 'tarama_durdur', 'description': 'Aktif RUNS/POOL islemini guvenli noktada durdur'},
    {'command': 'restart', 'description': 'Takilan veya bos NGS programini yeniden baslat'},
    {'command': 'yardim', 'description': 'Tum komutlari ve kullanimini goster'},
    {'command': 'menu', 'description': 'Uzaktan kontrol tuslarini goster'},
]

TEXT_COMMANDS = {
    '▶ RUNS Tara': '/runs_tara',
    '✅ Son POOL Tamamla': '/son_pool',
    '🧩 Eksikleri Tamamla': '/eksikleri_tamamla',
    '🔄 Durum': '/durum',
    '🖥 NGS Aç': '/ngs_ac',
    '⛔ Tarama Durdur': '/tarama_durdur',
    '♻ NGS Restart': '/restart',
}


def _windows_only():
    if os.name != 'nt':
        raise RuntimeError('Telegram token sifreleme yalniz Windows kullanici oturumunda desteklenir.')


class DATA_BLOB(ctypes.Structure):
    _fields_ = [('cbData', wintypes.DWORD), ('pbData', ctypes.POINTER(ctypes.c_byte))]


def _blob(data: bytes):
    buf = ctypes.create_string_buffer(data)
    return DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte))), buf


def protect_token(token: str) -> bytes:
    _windows_only()
    crypt32 = ctypes.WinDLL('Crypt32.dll')
    kernel32 = ctypes.WinDLL('Kernel32.dll')
    crypt32.CryptProtectData.argtypes = [ctypes.POINTER(DATA_BLOB), wintypes.LPCWSTR, ctypes.POINTER(DATA_BLOB), ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DATA_BLOB)]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL
    source, keep = _blob(token.encode('utf-8'))
    target = DATA_BLOB()
    if not crypt32.CryptProtectData(ctypes.byref(source), 'NGS Telegram Remote', None, None, None, 0x1, ctypes.byref(target)):
        raise ctypes.WinError()
    try:
        data = ctypes.string_at(target.pbData, target.cbData)
        return base64.b64encode(data)
    finally:
        kernel32.LocalFree(ctypes.cast(target.pbData, ctypes.c_void_p))


def unprotect_token(encoded: bytes) -> str:
    _windows_only()
    crypt32 = ctypes.WinDLL('Crypt32.dll')
    kernel32 = ctypes.WinDLL('Kernel32.dll')
    crypt32.CryptUnprotectData.argtypes = [ctypes.POINTER(DATA_BLOB), ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(DATA_BLOB), ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DATA_BLOB)]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL
    raw = base64.b64decode(encoded)
    source, keep = _blob(raw)
    target = DATA_BLOB()
    if not crypt32.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 0x1, ctypes.byref(target)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(target.pbData, target.cbData).decode('utf-8')
    finally:
        kernel32.LocalFree(ctypes.cast(target.pbData, ctypes.c_void_p))


def telegram_call(token: str, method: str, payload=None, timeout=15):
    body = json.dumps(payload or {}, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(
        f'https://api.telegram.org/bot{token}/{method}',
        data=body,
        headers={'Content-Type': 'application/json', 'User-Agent': 'NGS-Telegram-Remote/1.0'},
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            value = json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f'Telegram API HTTP {exc.code}') from None
    except urllib.error.URLError:
        raise RuntimeError('Telegram API baglantisi kurulamadi.') from None
    if not value.get('ok'):
        raise RuntimeError('Telegram API istegi basarisiz.')
    return value.get('result')


def send_message(token, chat_id, text, keyboard=True):
    payload = {'chat_id': int(chat_id), 'text': str(text), 'disable_web_page_preview': True}
    if keyboard:
        payload['reply_markup'] = MENU
    return telegram_call(token, 'sendMessage', payload)


def load_config():
    cfg = read_json(CONFIG_PATH, {}) or {}
    if not cfg.get('allowed_user_id') or not cfg.get('allowed_chat_id'):
        raise RuntimeError('Telegram remote ayari eksik. TELEGRAM_REMOTE_KUR.ps1 calistirin.')
    return cfg


def startup_path():
    appdata = os.environ.get('APPDATA')
    if not appdata:
        raise RuntimeError('Windows APPDATA bulunamadi.')
    return Path(appdata) / 'Microsoft' / 'Windows' / 'Start Menu' / 'Programs' / 'Startup' / STARTUP_NAME


def stop_existing_agent(wait_sec=9.0):
    if not LOCK_PATH.exists():
        return
    ROOT.mkdir(parents=True, exist_ok=True)
    STOP_PATH.write_text('stop\n', encoding='ascii')
    deadline = time.time() + float(wait_sec)
    while time.time() < deadline and LOCK_PATH.exists():
        time.sleep(0.25)
    if LOCK_PATH.exists():
        raise RuntimeError('Eski Telegram agent durmadi. Gorev Yoneticisinden pythonw islemini kontrol edin.')
    try: STOP_PATH.unlink()
    except OSError: pass


def install_startup():
    path = startup_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    pythonw = _pythonw_executable()
    content = '@echo off\r\ncd /d "' + str(BASE) + '"\r\nstart "" /min "' + str(pythonw) + '" "' + str(Path(__file__).resolve()) + '"\r\n'
    path.write_text(content, encoding='utf-8')
    return path


def launch_agent_process():
    exe = _pythonw_executable()
    flags = 0
    if os.name == 'nt':
        flags = getattr(subprocess, 'DETACHED_PROCESS', 0) | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)
    subprocess.Popen([str(exe), str(Path(__file__).resolve())], cwd=str(BASE), close_fds=True, creationflags=flags)


def reload_agent():
    _windows_only()
    stop_existing_agent()
    if not CONFIG_PATH.is_file() or not TOKEN_PATH.is_file():
        raise RuntimeError('Telegram remote ayari yok; once TELEGRAM_REMOTE_KUR.ps1 calistirin.')
    launch_agent_process()
    print('OK: Telegram agent yeni kaynakla yeniden baslatildi.')


def disable_agent():
    _windows_only()
    stop_existing_agent()
    try: startup_path().unlink()
    except FileNotFoundError: pass
    print('OK: Telegram agent durduruldu ve Windows baslangicindan kaldirildi. Sifreli token yerelde saklandi.')


def setup(args):
    _windows_only()
    ROOT.mkdir(parents=True, exist_ok=True)
    stop_existing_agent()
    token = getpass.getpass('Yeni Telegram bot tokenini yapistirin (ekranda gorunmez): ').strip()
    if not token:
        raise RuntimeError('Token bos birakilamaz.')
    info = telegram_call(token, 'getMe', {}, timeout=15)
    username = str(info.get('username') or '')
    if args.bot_username and username.casefold() != args.bot_username.lstrip('@').casefold():
        raise RuntimeError(f'Bu token @{username or "?"} botuna ait; beklenen @{args.bot_username.lstrip("@")}')
    cfg = {
        'schema': 1,
        'allowed_user_id': int(args.allowed_user),
        'allowed_chat_id': int(args.allowed_chat or args.allowed_user),
        'bot_username': username,
        'configured_at': time.time(),
        'privacy': 'technical_status_only_no_patient_data',
    }
    TOKEN_PATH.write_bytes(protect_token(token))
    _atomic_json(CONFIG_PATH, cfg)
    try:
        telegram_call(token, 'setMyCommands', {'commands': COMMANDS})
    except Exception:
        pass
    send_message(token, cfg['allowed_chat_id'], '✅ NGS uzaktan RUNS kontrolu hazir. Hasta/MP/tani/sonuc Telegram\'a gonderilmez.')
    launcher = install_startup()
    launch_agent_process()
    print(f'OK: @{username} ayarlandi. Yetkili UserID={cfg["allowed_user_id"]}.')
    print('Windows baslangici: ' + str(launcher))


def _pythonw_executable():
    exe = Path(sys.executable)
    if os.name == 'nt':
        name = exe.name.casefold()
        if name == 'python.exe':
            candidate = exe.with_name('pythonw.exe')
            if candidate.exists():
                return candidate
    return exe


def launch_ngs():
    if app_status_fresh(BASE):
        return False
    exe = _pythonw_executable()
    flags = 0
    if os.name == 'nt':
        flags = getattr(subprocess, 'DETACHED_PROCESS', 0) | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)
    subprocess.Popen([str(exe), str(BASE / 'app.py')], cwd=str(BASE), close_fds=True, creationflags=flags)
    return True


def status_text():
    status = read_app_status(BASE)
    if not app_status_fresh(BASE):
        return '⚪ NGS kapali veya yanit vermiyor.\nGenNext RUNS taramasi calismiyor.'
    busy = bool(status.get('job_busy'))
    current = str(status.get('current_command') or '')
    browser = 'hazir' if status.get('browser_ready') else 'kapali / hazir degil'
    remote_action = str(status.get('remote_action') or '')
    last_kind = str((status.get('last_event') or {}).get('kind') or '')
    if last_kind == 'login_failed' and not busy and not status.get('remote_scan_pending'):
        scan = '❌ Son uzaktan istek login asamasinda durdu; RUNS taramasi BASLAMADI'
    elif last_kind == 'remote_error' and not busy and not status.get('remote_scan_pending'):
        scan = '❌ Son uzaktan istek tamamlanamadi; RUNS taramasi baslamamis olabilir'
    elif busy and current == 'scan_runs':
        scan = '▶ RUNS taramasi calisiyor'
    elif busy and current == 'complete_latest':
        scan = '✅ Son POOL tamamlaniyor'
    elif busy and current == 'run_queue' and remote_action == 'complete_missing_latest':
        scan = '🧩 Son calismanin eksikleri tamamlaniyor'
    elif status.get('remote_scan_pending'):
        scan = '⏳ GenNext tarayicisi / oturumu hazirlaniyor'
    elif status.get('runs_scanned'):
        scan = '✅ Son RUNS taramasi bu oturumda tamamlandi'
    elif busy:
        scan = '⏳ Baska bir tarayici isi calisiyor'
    else:
        scan = '⚪ RUNS taramasi calismiyor'
    return f'🟢 NGS acik · v{status.get("version") or "?"}\nTarayici: {browser}\n{scan}'


def _message_command(text):
    text = str(text or '').strip()
    text = TEXT_COMMANDS.get(text, text)
    if not text.startswith('/'):
        return ''
    return text.split()[0].split('@')[0].casefold()


def _wait_command_result(command_id, timeout=5.0):
    path = remote_root(BASE) / 'results' / (str(command_id) + '.json')
    deadline = time.time() + float(timeout)
    while time.time() < deadline:
        data = read_json(path, None)
        if isinstance(data, dict):
            return data
        time.sleep(0.2)
    return None


def _process_command_line(pid):
    if os.name != 'nt':
        return ''
    try:
        script = "$p=Get-CimInstance Win32_Process -Filter 'ProcessId = %d'; if($p){$p.CommandLine}" % int(pid)
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        run = subprocess.run(['powershell.exe', '-NoProfile', '-Command', script], capture_output=True, text=True, timeout=6, creationflags=flags)
        return str(run.stdout or '').strip()
    except Exception:
        return ''


def _force_stop_recorded_ngs(pid):
    try: pid = int(pid)
    except (TypeError, ValueError): return False
    if pid <= 0 or not _pid_alive(pid): return True
    command_line = _process_command_line(pid)
    if not command_line or 'app.py' not in command_line.casefold():
        return False
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0
    try:
        run = subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'], capture_output=True, timeout=8, creationflags=flags)
        return run.returncode == 0
    except Exception:
        return False


def restart_ngs():
    status = read_app_status(BASE)
    if app_status_fresh(BASE):
        if bool(status.get('job_busy')):
            return 'busy', 'NGS yanit veriyor ve bir is calisiyor. Once /tarama_durdur kullanin; restart yapilmadi.'
        cid = queue_remote_command(BASE, 'shutdown_app')
        result = _wait_command_result(cid, 5.0)
        if result and result.get('status') not in ('accepted',):
            return 'blocked', str(result.get('message') or 'NGS guvenli kapanmayi kabul etmedi.')
        deadline = time.time() + 10.0
        while time.time() < deadline and app_status_fresh(BASE, max_age=2.5):
            time.sleep(0.3)
        if app_status_fresh(BASE, max_age=2.5):
            return 'error', 'NGS guvenli sekilde kapanmadi; zorla kapatma uygulanmadi.'
    else:
        pid = status.get('app_pid')
        if pid and _pid_alive(pid):
            if not _force_stop_recorded_ngs(pid):
                return 'error', 'Takili NGS sureci guvenli olarak dogrulanamadi; zorla kapatilmadi.'
            time.sleep(0.8)
    started = launch_ngs()
    return 'accepted', '♻ NGS yeniden baslatiliyor.' if started else '🟢 NGS yeniden acildi / zaten aciliyor.'


def help_text():
    return ('📘 NGS UZAKTAN KONTROL\n\n'
            '✅ /son_pool — RUNS listesini yeniler, en yeni POOLu bulur ve uygun eksikleri tamamlar.\n'
            '🧩 /eksikleri_tamamla — RUNS taramadan son bilinen calismanin eksiklerini tekrar dener.\n'
            '▶ /runs_tara — yalniz GenNext RUNS listesini tarar.\n'
            '🔄 /durum — NGS, tarayici, login ve son uzaktan islem durumunu gosterir.\n'
            '⛔ /tarama_durdur — aktif uzaktan islemi guvenli noktada durdurur.\n'
            '🖥 /ngs_ac — NGS programini acar.\n'
            '♻ /restart — takilan/bos NGSyi kontrollu yeniden baslatir. Aktif iste once durdurun.\n'
            "📋 /menu — tuslu ana menuyu gosterir.\n📥 /indir — NGS indirme baglantisini alir ve Safe Downloader'i baslatir.\n📊 /indir_durum — aktif NGS indirme ve dogrulama durumunu gosterir.\n\n"
            'Login dogrulanmadan RUNS baslatilmaz. Ilk Sign In gecikirse otomatik tekrar denenir.\n'
            'Hasta/MP/tani/sonuc Telegrama gonderilmez; Enlil uzaktan kontrol disidir.')


def handle_authorized(token, cfg, message):
    # NGS_PC_REPLY_FIX_V0_4_70
    # NGS_PC_SHUTDOWN_V0_4_69
    _pc_text = str(message.get("text") or "").strip()
    _pc_word = _pc_text.split(maxsplit=1)[0].split("@", 1)[0].lower() if _pc_text else ""
    _pc_command = _pc_word[1:] if _pc_word.startswith("/") else _pc_word
    if _pc_command in ("pckapat", "pckapat_onay", "pciptal"):
        import subprocess as _pc_subprocess
        import time as _pc_time
        _pc_chat = int(message.get("chat", {}).get("id") or 0)
        _pc_pending = globals().get("_NGS_PC_SHUTDOWN_PENDING")
        if _pc_command == "pckapat":
            globals()["_NGS_PC_SHUTDOWN_PENDING"] = {"chat": _pc_chat, "until": _pc_time.monotonic() + 60.0}
            send_message(token, _pc_chat, "PC kapatma istegi alindi. 60 saniye icinde /pckapat_onay yazin. Vazgecmek icin /pciptal.")
            return
        if _pc_command == "pckapat_onay":
            if (not isinstance(_pc_pending, dict) or _pc_pending.get("chat") != _pc_chat
                    or _pc_time.monotonic() > _pc_pending.get("until", 0)):
                send_message(token, _pc_chat, "Kapatma onayi gecersiz veya suresi doldu. Once /pckapat yazin.")
                return
            _pc_flags = getattr(_pc_subprocess, "CREATE_NO_WINDOW", 0)
            try:
                _pc_subprocess.Popen(["shutdown.exe", "/s", "/t", "30"], creationflags=_pc_flags)
            except Exception as _pc_error:
                send_message(token, _pc_chat, "PC kapatma baslatilamadi: " + str(_pc_error))
                return
            globals()["_NGS_PC_SHUTDOWN_PENDING"] = None
            send_message(token, _pc_chat, "PC 30 saniye icinde kapatilacak. Iptal icin /pciptal yazabilirsiniz.")
            return
        globals()["_NGS_PC_SHUTDOWN_PENDING"] = None
        _pc_flags = getattr(_pc_subprocess, "CREATE_NO_WINDOW", 0)
        try:
            _pc_result = _pc_subprocess.run(["shutdown.exe", "/a"], creationflags=_pc_flags, capture_output=True, timeout=5)
            _pc_reply = ("Planlanan PC kapatma iptal edildi." if _pc_result.returncode == 0
                         else "Planlanmis bir PC kapatma bulunamadi.")
        except Exception as _pc_error:
            _pc_reply = "PC kapatma iptali denetlenemedi: " + str(_pc_error)
        send_message(token, _pc_chat, _pc_reply)
        return
    _pc_hidden_reply = _pc_handle_hidden_command(locals())
    if _pc_hidden_reply is not None:
        _pc_chat_id = int(message.get('chat', {}).get('id') or 0)
        send_message(token, _pc_chat_id, str(_pc_hidden_reply))
        return
    chat_id = int(message.get('chat', {}).get('id') or 0)
    command = _message_command(message.get('text'))
    # NGS_INDIR_PATCH_v0.4.63
    try:
        from ngs_telegram_download_extension import handle_message as _ngs_indir_handle_message
        _ngs_indir_send = lambda _cid, _txt: send_message(token, _cid, _txt)
        if _ngs_indir_handle_message(message.get('text'), chat_id, _ngs_indir_send):
            return
    except Exception as _ngs_indir_exc:
        try:
            _ngs_indir_send(chat_id, '/indir modulu hatasi: ' + str(_ngs_indir_exc))
        except Exception:
            pass
    # NGS_PC_SHUTDOWN_V0_4_68
    _pc_cmd_raw = str((command) or "").strip()
    _pc_cmd = (_pc_cmd_raw.split()[0].split("@", 1)[0].lower() if _pc_cmd_raw else "")
    if _pc_cmd.startswith("/"):
        _pc_cmd = _pc_cmd[1:]
    if _pc_cmd == "pckapat":
        import time as _pc_time
        globals()["_NGS_PC_SHUTDOWN_PENDING_UNTIL"] = _pc_time.time() + 60.0
        return "PC kapatma istegi alindi. Onay icin 60 saniye icinde /pckapat_onay yazin. Vazgecmek icin /pciptal."
    if _pc_cmd == "pckapat_onay":
        import time as _pc_time
        if _pc_time.time() > float(globals().get("_NGS_PC_SHUTDOWN_PENDING_UNTIL", 0) or 0):
            return "Kapatma onay suresi doldu. Once /pckapat yazin."
        globals()["_NGS_PC_SHUTDOWN_PENDING_UNTIL"] = 0.0
        import subprocess as _pc_sp
        _pc_flags = getattr(_pc_sp, "CREATE_NO_WINDOW", 0)
        _pc_sp.Popen(["shutdown.exe", "/s", "/t", "30"], creationflags=_pc_flags)
        return "PC 30 saniye icinde kapatilacak. Iptal icin /pciptal yazabilirsiniz."
    if _pc_cmd == "pciptal":
        globals()["_NGS_PC_SHUTDOWN_PENDING_UNTIL"] = 0.0
        import subprocess as _pc_sp
        _pc_flags = getattr(_pc_sp, "CREATE_NO_WINDOW", 0)
        try:
            _pc_sp.run(["shutdown.exe", "/a"], creationflags=_pc_flags, capture_output=True, timeout=5)
        except Exception:
            pass
        return "Bekleyen PC kapatma islemi iptal edildi."
    if command in ('/start', '/menu'):
        send_message(token, chat_id, 'NGS uzaktan kontrol\n\nAna komut: /son_pool\nTum kullanim icin /yardim\nEnlil uzaktan kontrol disidir.')
        return
    if command in ('/yardim', '/help'):
        send_message(token, chat_id, help_text())
        return
    if command == '/durum':
        send_message(token, chat_id, status_text())
        return
    if command == '/ngs_ac':
        started = launch_ngs()
        send_message(token, chat_id, '🖥 NGS aciliyor.' if started else '🟢 NGS zaten acik.')
        return
    if command == '/runs_tara':
        started = launch_ngs()
        queue_remote_command(BASE, 'scan_runs', user_id=cfg['allowed_user_id'])
        send_message(token, chat_id, '▶ RUNS tarama istegi kuyruga alindi.' + (' NGS aciliyor.' if started else ''))
        return
    if command == '/son_pool':
        started = launch_ngs()
        queue_remote_command(BASE, 'complete_latest', user_id=cfg['allowed_user_id'])
        send_message(token, chat_id, '✅ Son POOL tamamlama istegi kuyruga alindi. RUNS yeniden taranacak; en yeni POOLun yalniz eksik adimlari denenecek.' + (' NGS aciliyor.' if started else ''))
        return
    if command == '/eksikleri_tamamla':
        started = launch_ngs()
        queue_remote_command(BASE, 'complete_missing_latest', user_id=cfg['allowed_user_id'])
        send_message(token, chat_id, '🧩 Son bilinen calismanin eksik adimlari kuyruga alindi.' + (' NGS aciliyor.' if started else ''))
        return
    if command == '/restart':
        status, text = restart_ngs()
        send_message(token, chat_id, text)
        return
    if command == '/tarama_durdur':
        if not app_status_fresh(BASE):
            send_message(token, chat_id, '⚪ NGS acik degil; durdurulacak RUNS taramasi yok.')
            return
        queue_remote_command(BASE, 'stop_scan', user_id=cfg['allowed_user_id'])
        send_message(token, chat_id, '⛔ Durdurma istegi gonderildi; guvenli noktada duracak.')
        return
    if command:
        send_message(token, chat_id, 'Desteklenen komutlar: /durum /ngs_ac /runs_tara /son_pool /eksikleri_tamamla /tarama_durdur /restart /yardim /menu /indir /indir_durum /pckapat /pckapat_onay /pciptal')


def _notify_new_event(token, cfg, state):
    status = read_app_status(BASE)
    event = status.get('last_event') or {}
    event_id = str(event.get('id') or '')
    if not event_id or event_id == state.get('last_event_id'):
        return state
    state['last_event_id'] = event_id
    kind = str(event.get('kind') or '')
    text = None
    if kind == 'scan_started':
        text = '▶ GenNext RUNS taramasi basladi.'
    elif kind == 'scan_completed':
        n = event.get('technical_records')
        text = '✅ GenNext RUNS taramasi tamamlandi.' + (f' {int(n)} teknik kayit bulundu.' if n is not None else '')
    elif kind == 'scan_stopped':
        text = '⛔ RUNS taramasi durduruldu.'
    elif kind == 'scan_error':
        text = '❌ RUNS taramasi tamamlanamadi. PC uzerinde NGS programini kontrol edin.'
    elif kind == 'browser_starting':
        text = '🌐 GenNext tarayicisi aciliyor; kayitli oturum ve RUNS erisimi dogrulaniyor.'
    elif kind == 'login_failed':
        text = '❌ GenNext login dogrulanamadi. RUNS taramasi BASLATILMADI. Bir kez /runs_tara ile tekrar deneyin; tekrarlarsa PCde Sign In durumunu kontrol edin.'
    elif kind == 'latest_started':
        text = '✅ Son POOL tamamlama basladi.'
    elif kind == 'latest_completed':
        text = '✅ Son POOL tamamlandi; uygun eksik adim kalmadi.'
    elif kind == 'latest_waiting':
        text = '⏳ Son POOLda henuz hazir olmayan sayfa/cikti var; hazir olanlar alindi. Sonra /eksikleri_tamamla kullanabilirsiniz.'
    elif kind == 'latest_attention':
        text = '⚠ Son POOL islemi bitti; kontrol/hata/eksik adim kaldi. /durum ve gerekirse /eksikleri_tamamla kullanin.'
    elif kind == 'missing_started':
        text = '🧩 Son calismanin eksikleri tamamlanmaya baslandi.'
    elif kind == 'missing_completed':
        text = '✅ Son calismanin uygun eksikleri tamamlandi.'
    elif kind == 'missing_attention':
        text = '⚠ Eksik tamamlama bitti; hala hazir olmayan veya kontrol isteyen adim var.'
    if text:
        send_message(token, cfg['allowed_chat_id'], text)
    return state


def _pid_alive(pid):
    try:
        pid = int(pid)
        if pid <= 0:
            return False
    except (TypeError, ValueError):
        return False
    if os.name == 'nt':
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def acquire_lock():
    ROOT.mkdir(parents=True, exist_ok=True)
    if LOCK_PATH.exists():
        old = read_json(LOCK_PATH, {}) or {}
        if _pid_alive(old.get('pid')):
            return False
        try: LOCK_PATH.unlink()
        except OSError: return False
    try:
        fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        json.dump({'pid': os.getpid(), 'started_at': time.time()}, stream)
    return True


def run_agent():
    _windows_only()
    if not acquire_lock():
        return 0
    try:
        cfg = load_config()
        token = unprotect_token(TOKEN_PATH.read_bytes())
        state = read_json(STATE_PATH, {}) or {}
        offset = int(state.get('offset') or 0)
        STOP_PATH.unlink(missing_ok=True)
        _atomic_json(AGENT_STATUS_PATH, {'running': True, 'pid': os.getpid(), 'at': time.time(), 'bot': cfg.get('bot_username')})
        try:
            telegram_call(token, 'setMyCommands', {'commands': COMMANDS})
        except Exception:
            pass
        while not STOP_PATH.exists():
            try:
                updates = telegram_call(token, 'getUpdates', {
                    'offset': offset,
                    'timeout': 5,
                    'allowed_updates': ['message'],
                }, timeout=10) or []
                for update in updates:
                    offset = max(offset, int(update.get('update_id') or 0) + 1)
                    message = update.get('message') or {}
                    sender = int(message.get('from', {}).get('id') or 0)
                    chat = int(message.get('chat', {}).get('id') or 0)
                    # Unauthorized users are silently ignored; do not disclose an active control bot.
                    if sender != int(cfg['allowed_user_id']) or chat != int(cfg['allowed_chat_id']):
                        continue
                    handle_authorized(token, cfg, message)
                state['offset'] = offset
                state = _notify_new_event(token, cfg, state)
                _atomic_json(STATE_PATH, state)
                _atomic_json(AGENT_STATUS_PATH, {'running': True, 'pid': os.getpid(), 'at': time.time(), 'bot': cfg.get('bot_username')})
            except Exception:
                # Do not log token-bearing URLs or full Telegram errors. Keep retrying.
                _atomic_json(AGENT_STATUS_PATH, {'running': True, 'pid': os.getpid(), 'at': time.time(), 'bot': cfg.get('bot_username'), 'network_error': True})
                time.sleep(4)
        return 0
    finally:
        try: _atomic_json(AGENT_STATUS_PATH, {'running': False, 'pid': os.getpid(), 'at': time.time()})
        except Exception: pass
        try: STOP_PATH.unlink()
        except OSError: pass
        try: LOCK_PATH.unlink()
        except OSError: pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--setup', action='store_true')
    parser.add_argument('--disable', action='store_true')
    parser.add_argument('--reload-agent', action='store_true')
    parser.add_argument('--allowed-user', type=int, default=1196743952)
    parser.add_argument('--allowed-chat', type=int, default=1196743952)
    parser.add_argument('--bot-username', default='enlilko_bot')
    args = parser.parse_args()
    try:
        if args.setup:
            setup(args)
            return 0
        elif args.reload_agent:
            reload_agent()
            return 0
        if args.disable:
            disable_agent()
            return 0
        return run_agent()
    except Exception as exc:
        print('HATA: ' + str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
