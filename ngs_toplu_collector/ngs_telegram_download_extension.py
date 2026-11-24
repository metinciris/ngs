#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Telegram /indir flow for the existing NGS remote agent.

v0.4.63
- Uses the raw Telegram message text, preserving case-sensitive Nextcloud tokens.
- Extracts public-share URLs even when Telegram/web clients wrap them as links.
- Accepts standard Nextcloud share URLs such as:
  Paylaşım bağlantısını doğrudan gönderin

No bot token is stored here. The existing agent keeps its own authentication
and passes its send-message helper into handle_message().
"""
from __future__ import annotations

import html
import json
import os
import re
import subprocess
import sys
import unicodedata
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

VERSION = "0.4.63"
DOWNLOADER_DIR = Path(os.environ.get("NGS_SAFE_DOWNLOADER_DIR", r"C:\NGS_Safe_Downloader"))
BRIDGE = DOWNLOADER_DIR / "NGS_Remote_Bridge.py"
_STATE: dict[str, dict[str, Any]] = {}

# Intentionally excludes Markdown wrapper characters []()<> and quotes.
# Nextcloud public-share links do not need those characters in the path.
_URL_RE = re.compile(r"https?://[A-Za-z0-9._~:/?#@!$&*+,;=%-]+", re.IGNORECASE)
_SHARE_PATH_RE = re.compile(r"(?:/index\.php)?/s/[^/?#]+", re.IGNORECASE)


def _key(chat_id: Any) -> str:
    return str(chat_id)


def _send(send_message: Callable[..., Any], chat_id: Any, text: str) -> None:
    send_message(chat_id, text)


def _python_console() -> str:
    exe = Path(sys.executable)
    if os.name == "nt" and exe.name.lower() == "pythonw.exe":
        candidate = exe.with_name("python.exe")
        if candidate.exists():
            return str(candidate)
    return str(exe)


def _bridge_json(*args: str, timeout: int = 30) -> dict[str, Any]:
    if not BRIDGE.is_file():
        return {
            "ok": False,
            "error": "NGS Remote Bridge bulunamadi: " + str(BRIDGE),
        }
    try:
        cp = subprocess.run(
            [_python_console(), str(BRIDGE), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        )
    except Exception as e:
        return {"ok": False, "error": f"Bridge calistirilamadi: {type(e).__name__}: {e}"}
    raw = (cp.stdout or "").strip()
    if raw:
        try:
            return json.loads(raw.splitlines()[-1])
        except Exception:
            pass
    err = (cp.stderr or raw or f"Bridge cikis kodu {cp.returncode}").strip()
    return {"ok": False, "error": err[:800]}


def _norm_token(text: str) -> str:
    s = unicodedata.normalize("NFKD", (text or "").strip().casefold())
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return s.replace("\u0131", "i")


def _looks_like_share_url(url: str) -> bool:
    try:
        p = urlsplit((url or "").strip())
    except Exception:
        return False
    if p.scheme.lower() not in {"http", "https"} or not p.netloc:
        return False
    return bool(_SHARE_PATH_RE.search(p.path or ""))


def _extract_share_url(text: Any) -> str:
    """Extract the first valid Nextcloud public-share URL, preserving case.

    Handles plain URLs, Telegram/Web link rendering, Markdown links, angle
    brackets and surrounding prose. The share token is case-sensitive, so the
    returned URL is never lower-cased.
    """
    raw = html.unescape(str(text or ""))
    # Remove common invisible characters that can arrive via copy/paste.
    raw = raw.replace("\u200b", "").replace("\u200c", "").replace("\u200d", "").replace("\ufeff", "")
    for match in _URL_RE.finditer(raw):
        candidate = match.group(0).rstrip(".,;:!?}")
        if _looks_like_share_url(candidate):
            return candidate
    return ""


def _format_duration(seconds: Any) -> str:
    try:
        sec = max(0, int(float(seconds)))
    except Exception:
        return "?"
    if sec < 60:
        return f"{sec} sn"
    minutes, sec = divmod(sec, 60)
    if minutes < 60:
        return f"{minutes} dk" + (f" {sec} sn" if sec else "")
    hours, minutes = divmod(minutes, 60)
    return f"{hours} sa {minutes} dk"


def _human_bytes(value: Any) -> str:
    try:
        n = max(0, float(value or 0))
    except Exception:
        return "?"
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    for unit in units:
        if n < 1024 or unit == units[-1]:
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return "?"


def _format_download_status(info: dict[str, Any]) -> str:
    if not info.get("ok"):
        return "⚠️ Indirme durumu okunamadi:\n" + str(info.get("error") or "Bilinmeyen hata")
    if not info.get("found"):
        return "ℹ️ Kayitli bir NGS indirme isi bulunamadi."

    phase = str(info.get("phase") or "unknown")
    status = str(info.get("status") or "unknown")
    phase_names = {
        "starting": "Hazirlaniyor", "scanned": "Dosyalar tarandi",
        "downloading": "Indiriliyor", "download": "Indiriliyor",
        "download_complete": "Indirme tamamlandi", "verifying": "Dogrulaniyor",
        "verification_complete": "Dogrulama tamamlandi", "repairing": "Hatalilar yeniden indiriliyor",
        "complete": "Tamamlandi", "incomplete": "Tamamlanamadi", "error": "Hata",
    }
    if status == "paused":
        state = "Duraklatildi"
    elif status == "verified":
        state = "Tamamlandi ve dogrulandi"
    elif status == "verification_failed":
        state = "Dogrulama hatasi"
    elif status == "verification_warning":
        state = "Dogrulama uyarisi"
    elif status == "stopped":
        state = "Durduruldu"
    elif status == "repairing":
        state = "Hatalilar yeniden indiriliyor"
    elif status == "verifying":
        state = "Dogrulaniyor"
    else:
        state = phase_names.get(phase, phase_names.get(str(info.get("job_stage") or ""), status or "Bilinmiyor"))

    lines = ["📥 NGS Indirme Durumu", "", f"Durum: {state}"]
    target = str(info.get("target") or "")
    if target:
        lines.append(f"📂 Hedef: {target}")

    total = int(info.get("total_bytes") or 0)
    done = int(info.get("done_bytes") or 0)
    pct = float(info.get("percent") or 0)
    ft = int(info.get("files_total") or 0)
    fc = int(info.get("files_completed") or 0)
    ff = int(info.get("files_failed") or 0)
    speed = float(info.get("speed_bytes_sec") or 0)
    eta = info.get("eta_seconds")

    if total > 0:
        lines.append(f"Ilerleme: %{pct:.1f} • {_human_bytes(done)} / {_human_bytes(total)}")
    if ft > 0:
        lines.append(f"Dosya: {fc} / {ft}" + (f" • hata: {ff}" if ff else ""))
    if phase in {"downloading", "download", "repairing"} or status in {"downloading", "repairing", "paused"}:
        if speed > 0:
            lines.append(f"Hiz: {_human_bytes(speed)}/sn")
        if eta is not None and float(eta or 0) > 0:
            lines.append(f"Tahmini kalan: {_format_duration(eta)}")

    if phase == "verifying" or status == "verifying":
        lines.append("🔍 Dogrulama: devam ediyor")
    elif phase == "repairing" or status == "repairing":
        lines.append("🛠 Dogrulama sonrasi onarim: devam ediyor")

    if status in {"verified", "verification_failed", "verification_warning"} or info.get("full_verified"):
        fastq = int(info.get("fastq_total") or 0)
        md = int(info.get("source_checksum_ok") or 0)
        crc = int(info.get("gzip_crc_ok") or 0)
        problems = int(info.get("verification_problems") or 0)
        if fastq:
            lines.append(f"🔍 FASTQ: {fastq} • kaynak checksum OK: {md} • GZIP/CRC OK: {crc}")
        if problems:
            lines.append(f"⚠️ Dogrulama sorunu: {problems}")

    free = str(info.get("disk_free") or "")
    drive = str(info.get("drive") or "")
    if free and free != "?":
        lines.append(f"💾 {drive + ' ' if drive else ''}bos alan: {free}")
    updated = str(info.get("updated") or "")
    if updated:
        lines.append(f"Son guncelleme: {updated}")

    if info.get("full_verified") and status == "verified":
        lines += ["", "✅ Indirme ve final dogrulama tamamlandi.", "💾 Disk guvenle cikarilabilir."]
    elif status in {"verification_failed", "verification_warning"}:
        lines += ["", "⚠️ Disk henuz guvenle cikarilmamali; dogrulama sonucunu kontrol edin."]
    return "\n".join(lines)


def _candidate_line(d: dict[str, Any], idx: int | None = None) -> str:
    prefix = f"{idx}) " if idx is not None else ""
    label = (d.get("label") or "").strip()
    drive = d.get("drive") or "?"
    free = d.get("free") or "?"
    usb = " USB" if d.get("usb") else ""
    if d.get("candidate_reason") == "non-system-local":
        usb += " (Windows USB olarak tanimadi)"
    shown = f"{drive}"
    if label:
        shown += f" - {label}"
    return f"{prefix}{shown} - {free} bos{usb}"


def _prepare_for_drive(url: str, drive: str, chat_id: Any, send_message: Callable[..., Any]) -> bool:
    info = _bridge_json("prepare", "--url", url, "--drive", drive)
    if not info.get("ok"):
        _STATE.pop(_key(chat_id), None)
        _send(send_message, chat_id, "\u26a0\ufe0f Hedef hazirlanamadi:\n" + str(info.get("error") or "Bilinmeyen hata"))
        return True
    _STATE[_key(chat_id)] = {
        "stage": "confirm",
        "url": url,
        "drive": info.get("drive") or drive,
        "preview": info,
    }
    label = (info.get("label") or "").strip()
    disk = str(info.get("drive") or drive)
    if label:
        disk += " - " + label
    warning = (
        "\u26a0\ufe0f Windows bu diski USB/removable olarak tanimadi; sistem diski olmadigi icin secilebilir.\n"
        if not info.get("usb") else ""
    )
    msg = (
        "\U0001F4E5 NGS indirme hazir\n\n"
        f"\U0001F517 Link: {info.get('url_display') or 'maskeli'}\n"
        f"\U0001F4BE HDD: {disk}\n"
        + warning
        + f"Bos alan: {info.get('free') or '?'}\n"
        + f"\U0001F4C2 Hedef: {info.get('target') or '?'}\n\n"
        + "Baslatmak icin INDIR, vazgecmek icin IPTAL yazin."
    )
    _send(send_message, chat_id, msg)
    return True


def _accept_url(url: str, chat_id: Any, send_message: Callable[..., Any]) -> bool:
    # Be defensive if a wrapped/raw message reaches here.
    extracted = _extract_share_url(url)
    if not extracted:
        _send(send_message, chat_id, "\U0001F517 Gecerli Nextcloud NGS baglantisi bulunamadi.\nOrnek: Paylaşım bağlantısını doğrudan gönderin\nIPTAL ile cikabilirsiniz.")
        return True
    url = extracted

    drives = _bridge_json("drives")
    if not drives.get("ok"):
        _STATE.pop(_key(chat_id), None)
        _send(send_message, chat_id, "\u26a0\ufe0f HDD taranamadi:\n" + str(drives.get("error") or "Bilinmeyen hata"))
        return True
    candidates = list(drives.get("candidates") or [])
    if not candidates:
        _STATE.pop(_key(chat_id), None)
        _send(send_message, chat_id, "\u26a0\ufe0f Sistem diski disinda yazilabilir hedef disk bulunamadi. HDD/SSD baglantisini kontrol edin.")
        return True
    if len(candidates) == 1:
        return _prepare_for_drive(url, str(candidates[0].get("drive") or ""), chat_id, send_message)
    _STATE[_key(chat_id)] = {
        "stage": "await_drive",
        "url": url,
        "candidates": candidates,
    }
    lines = ["\U0001F4BE Birden fazla hedef disk bulundu:"]
    for i, d in enumerate(candidates, 1):
        lines.append(_candidate_line(d, i))
    lines.append("")
    lines.append("Hedef surucu harfini gonderin (ornegin F:). IPTAL ile cikabilirsiniz.")
    _send(send_message, chat_id, "\n".join(lines))
    return True


def handle_message(text: Any, chat_id: Any, send_message: Callable[..., Any]) -> bool:
    """Return True when this message belongs to the /indir flow."""
    text = str(text or "").strip()
    key = _key(chat_id)

    first, sep, rest = text.partition(" ")
    command = first.split("@", 1)[0].lower()
    if command in {"/indir_durum", "/indirdurum"}:
        info = _bridge_json("status", timeout=20)
        _send(send_message, chat_id, _format_download_status(info))
        return True
    if command == "/indir":
        arg = rest.strip() if sep else ""
        if not arg:
            _STATE[key] = {"stage": "await_url"}
            _send(send_message, chat_id, "\U0001F517 NGS indirme baglantisini gonderin.\nOrnek: Paylaşım bağlantısını doğrudan gönderin\nIPTAL yazarak cikabilirsiniz.")
            return True
        url = _extract_share_url(arg)
        if not url:
            _STATE[key] = {"stage": "await_url"}
            _send(send_message, chat_id, "\U0001F517 Gecerli Nextcloud NGS baglantisi bulunamadi.\nIPTAL yazarak cikabilirsiniz.")
            return True
        _STATE[key] = {"stage": "await_url"}
        return _accept_url(url, chat_id, send_message)

    state = _STATE.get(key)
    if not state:
        return False

    # Other slash commands remain available while a download prompt is pending.
    if text.startswith("/"):
        return False

    norm = _norm_token(text)
    if norm in {"iptal", "vazgec", "vazgecme", "hayir", "cancel"}:
        _STATE.pop(key, None)
        _send(send_message, chat_id, "\u274c NGS indirme istegi iptal edildi.")
        return True

    stage = state.get("stage")
    if stage == "await_url":
        url = _extract_share_url(text)
        if not url:
            _send(send_message, chat_id, "\U0001F517 Lutfen Nextcloud NGS indirme baglantisini gonderin veya IPTAL yazin.\nOrnek: Paylaşım bağlantısını doğrudan gönderin")
            return True
        return _accept_url(url, chat_id, send_message)

    if stage == "await_drive":
        candidates = list(state.get("candidates") or [])
        raw = text.strip().upper().rstrip("\\/")
        if len(raw) == 1 and raw.isalpha():
            raw += ":"
        selected = None
        for d in candidates:
            if str(d.get("drive") or "").upper() == raw:
                selected = d
                break
            if (d.get("label") or "").strip().upper() == raw:
                selected = d
                break
        if not selected:
            _send(send_message, chat_id, "Hedef disk anlasilamadi. Surucu harfini gonderin (ornegin F:) veya IPTAL yazin.")
            return True
        return _prepare_for_drive(str(state.get("url") or ""), str(selected.get("drive") or ""), chat_id, send_message)

    if stage == "confirm":
        if norm not in {"indir", "evet", "onay", "baslat", "start"}:
            _send(send_message, chat_id, "Baslatmak icin INDIR, vazgecmek icin IPTAL yazin.")
            return True
        url = str(state.get("url") or "")
        drive = str(state.get("drive") or "")
        _send(send_message, chat_id, "\u25b6\ufe0f NGS Safe Downloader baslatiliyor...")
        result = _bridge_json("launch", "--url", url, "--drive", drive, timeout=40)
        _STATE.pop(key, None)
        if result.get("ok") and result.get("launched"):
            target = result.get("target") or "?"
            _send(
                send_message,
                chat_id,
                "\u2705 Downloader baslatildi.\n"
                f"\U0001F4C2 Hedef: {target}\n"
                "Indirme, dogrulama ve gerekirse onarma mevcut NGS Safe Downloader akisi ile devam edecek.",
            )
        else:
            _send(send_message, chat_id, "\u26a0\ufe0f Downloader baslatilamadi:\n" + str(result.get("error") or "Bilinmeyen hata"))
        return True

    _STATE.pop(key, None)
    return False
