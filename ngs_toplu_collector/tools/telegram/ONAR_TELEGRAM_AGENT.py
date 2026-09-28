#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Repair a stale NGS Telegram agent lock without changing program or credentials."""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import time
from pathlib import Path


COLLECTOR = Path(r"C:\ngs\ngs_toplu_collector")
AGENT = COLLECTOR / "telegram_remote_agent.py"
LOG = COLLECTOR / "telegram_remote_agent_repair.log"


def python_processes() -> list[dict]:
    ps = (
        "$ErrorActionPreference='Stop';"
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.Name -match '^(python|pythonw|py)(\\.exe)?$' } | "
        "Select-Object ProcessId,Name,CommandLine | ConvertTo-Json -Compress"
    )
    cp = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps],
        capture_output=True, text=True, timeout=25,
    )
    if cp.returncode != 0:
        raise RuntimeError("Windows surec listesi okunamadi; kilide dokunulmadi: " + (cp.stderr or "bilinmeyen hata").strip())
    raw = cp.stdout.strip()
    if not raw:
        return []
    result = json.loads(raw)
    return result if isinstance(result, list) else [result]


def active_agents() -> list[dict]:
    needle = str(AGENT).casefold()
    return [p for p in python_processes() if needle in str(p.get("CommandLine") or "").casefold()]


def repair() -> int:
    if os.name != "nt":
        raise RuntimeError("Bu onarim yalniz Windows icindir.")
    if not AGENT.is_file():
        raise RuntimeError("Canli agent bulunamadi: " + str(AGENT))

    # Import the live module's location rule; do not guess where credentials/lock live.
    sys.path.insert(0, str(COLLECTOR))
    from remote_control import remote_root  # type: ignore

    root = Path(remote_root(COLLECTOR))
    lock = root / "telegram_agent.lock"
    stop = root / "telegram_agent.stop"
    status = root / "telegram_agent_status.json"
    print("Agent:", AGENT)
    print("Durum klasoru:", root)

    running = active_agents()
    if running:
        print("Calisan agent bulundu; guvenli durdurma isteniyor.")
        stop.write_text("stop\n", encoding="ascii")
        until = time.monotonic() + 14
        while time.monotonic() < until:
            time.sleep(0.5)
            running = active_agents()
            if not running:
                break
        if running:
            raise RuntimeError("Agent sureci durmadi (PID: " + ", ".join(str(p.get("ProcessId")) for p in running) + "). Zorla sonlandirilmadi; kilide dokunulmadi.")

    # Repeat the process check directly before moving the old lock.
    if active_agents():
        raise RuntimeError("Yeni agent sureci goruldu; kilide dokunulmadi.")
    if lock.is_file():
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = lock.with_name(lock.name + ".stale_" + stamp + "_" + str(os.getpid()) + ".bak")
        lock.rename(backup)
        print("Takili kilit yedeklendi:", backup)
    else:
        print("Eski kilit yok; dogrudan baslatiliyor.")

    # The live agent itself removes a stale stop marker when it acquires its lock.
    if active_agents():
        raise RuntimeError("Baska bir agent basladi; ikinci bir surec acilmadi.")
    start = time.time()
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    with LOG.open("a", encoding="utf-8", errors="replace") as log:
        log.write("\n[" + dt.datetime.now().isoformat(timespec="seconds") + "] ONARIM BASLAT " + str(AGENT) + "\n")
        log.flush()
        proc = subprocess.Popen(
            [sys.executable, str(AGENT)], cwd=str(COLLECTOR),
            stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            close_fds=True, creationflags=flags,
        )
    print("Yeni agent PID:", proc.pid)

    until = time.monotonic() + 42
    while time.monotonic() < until:
        time.sleep(1)
        if proc.poll() is not None:
            tail = "\n".join(LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-12:])
            raise RuntimeError("Agent erken kapandi (kod " + str(proc.returncode) + ").\n" + tail)
        try:
            data = json.loads(status.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        # The first status write precedes setMyCommands/getUpdates. Wait for a
        # later heartbeat from the polling loop before declaring success.
        if int(data.get("pid") or 0) != proc.pid or float(data.get("at") or 0) < start + 5:
            continue
        if data.get("network_error"):
            raise RuntimeError("Agent calisiyor ama mesaj dongusunde hata bildiriyor (network_error). PID=" + str(proc.pid))
        if data.get("running") is True:
            print("OK: Agent calisiyor ve durum kaydi yenileniyor. Telegram'da /durum deneyin.")
            print("Baslangic gunlugu:", LOG)
            return 0
    raise RuntimeError("Agent basladi ama 42 saniyede yeni durum kaydi dogrulanamadi. Gunluk: " + str(LOG))


if __name__ == "__main__":
    try:
        raise SystemExit(repair())
    except Exception as exc:
        print("HATA:", exc, file=sys.stderr)
        raise SystemExit(2)
